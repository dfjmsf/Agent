import os
import json
import time
import logging
import threading
from typing import List, Dict, Any, Optional, Tuple
from core.llm_client import default_llm
from core.prompt import Prompts
from core.state_manager import global_state_manager
from core.ws_broadcaster import global_broadcaster
from agents.coder import CoderAgent
from agents.reviewer import ReviewerAgent
from core.database import (
    append_event, get_recent_events, rename_project_events,
    recall, memorize,
    create_project_meta, update_project_status, rename_project_meta,
)

logger = logging.getLogger("ManagerAgent")

MAX_RETRIES = int(os.getenv("MAX_RETRIES", 5))

class ManagerAgent:
    """
    主控 Agent (Manager)
    专职：需求拆解、任务派发、循环调度、熔断控制。
    """
    def __init__(self, project_id: str = "default_project"):
        self.model = os.getenv("MODEL_PLANNER", "qwen3-max")
        self.llm_client = default_llm
        self.project_id = project_id
        self.coder = CoderAgent(project_id)
        self.reviewer = ReviewerAgent(project_id)

    def plan_tasks(self, user_requirement: str) -> dict:
        """
        调用大模型，将用户的一句话需求拆解为 JSON 格式的 Task List
        """
        logger.info("🧠 Manager 正在思考架构并拆解任务...")
        
        # 1. 查询结构化短期记忆 (Sliding Window History)
        recent_events = get_recent_events(project_id=self.project_id, limit=10, caller="Manager")
        history_str = "\n".join([f"[{e.role}/{e.event_type}]: {e.content[:100]}..." for e in recent_events]) if recent_events else "无近期对话。"
        
        # 2. 查询长期经验记忆 (RAG)
        past_experience = recall(user_requirement, n_results=3, project_id=self.project_id, caller="Manager")
        experience_str = "\n".join(past_experience) if past_experience else "无相关历史经验。"

        # 3. 构造环境变量声明 (防止后续循环乱改项目名并提供结构化视野)
        is_new_project = "新建项目" in self.project_id or self.project_id == "default_project"
        if is_new_project:
            env_context = "【项目环境】\n这是基于主人的首个请求刚刚创建的全新宇宙草稿。请为它起一个酷炫、精简的纯英文字符串作为 JSON 中的 `project_name` 字段。"
        else:
            vfs = global_state_manager.get_vfs(self.project_id)
            existing_files = list(vfs.get_all_vfs().keys())
            file_tree = "\n".join([f"- {f}" for f in existing_files]) if existing_files else "目录暂空。"
            env_context = (
                f"【项目环境】\n当前项目宇宙已永久命名并固化为: `{self.project_id}`\n"
                f"你 MUST 且只能将 JSON 里的 `project_name` 固定为 `{self.project_id}`，绝对不允许修改项目名！！！\n"
                f"【已有文件架构】:\n{file_tree}\n"
                f"这是当前项目内存里的所有文件，你可以在此基础上规划对旧文件的修改（覆盖），或者指派增加全新的子模块文件。"
            )

        system_prompt = Prompts.MANAGER_SYSTEM + f"\n\n【近期对话上下文】\n{history_str}\n\n【RAG 检索到的过往血泪经验】\n{experience_str}\n\n{env_context}"
        user_prompt = f"主人的开发需求：\n{user_requirement}\n请严格按照 JSON Schema 输出。"
        
        try:
            # Planner 不使用任何工具，只输出纯文本 JSON
            raw_response = self.llm_client.chat_completion(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]
            )
            # 极简清洗，提取 JSON (防止大模型带了 Markdown 代码块)
            json_str = raw_response.content
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0].strip()
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0].strip()
                
            plan = json.loads(json_str)
            
            # 确保默认存在的健壮性
            if "project_name" not in plan:
                plan["project_name"] = "AutoGen_Project"
            if "tasks" not in plan:
                plan["tasks"] = []
                
            logger.info(f"✅ 任务拆解完成: {plan.get('project_name')}")
            global_broadcaster.emit_sync("Manager", "plan_ready", f"成功拆解任务: {plan.get('project_name')}", {"plan": plan})
            
            return plan
            
        except json.JSONDecodeError as e:
            logger.error(f"Manager 返回的并不是标准的 JSON 格式：\n{raw_response}")
            return {"project_name": "Error_Project", "architecture_summary": "解析失败", "tasks": []}
        except Exception as e:
            logger.error(f"Manager 拆解任务时发生异常: {e}")
            return {"project_name": "Error_Project", "architecture_summary": "API异常", "tasks": []}

    def execute_tdd_loop(self, task: Dict[str, Any], final_dir: str = None) -> bool:
        """
        核心的 TDD (Test-Driven Development) 开发与审查闭环
        返回: 是否成功解决该 Task
        """
        target_file = task.get("target_file", "")
        description = task.get("description", "")
        task_id = task.get("task_id", target_file)
        
        logger.info(f"\n🚀 开始执行任务 [{task_id}]: {target_file}")
        global_broadcaster.emit_sync("Manager", "task_start", f"开始分发执行任务: {target_file}", {"task": task})

        vfs = global_state_manager.get_vfs(self.project_id)
        
        # 如果 VFS 中没有该文件，检查磁盘是否已有（跨请求修改场景）
        if vfs.get_draft(target_file) is None and final_dir:
            disk_path = os.path.join(final_dir, target_file)
            if os.path.isfile(disk_path):
                try:
                    with open(disk_path, "r", encoding="utf-8") as f:
                        existing_code = f.read()
                    vfs.save_draft(target_file, existing_code)
                    logger.info(f"📂 从磁盘预加载已有文件到 VFS: {target_file} ({len(existing_code)} bytes)")
                except Exception as e:
                    logger.warning(f"⚠️ 磁盘文件预加载失败: {target_file} - {e}")
        feedback = None
        vfs.reset_retry(task_id)
        
        while True:
            current_retry = vfs.get_retry_count(task_id)
            
            # 1. 熔断机制判定
            if current_retry >= MAX_RETRIES:
                logger.error(f"🚨 [熔断触发] 任务 {task_id} 连续失败 {MAX_RETRIES} 次，陷入死循环！停止执行。")
                global_broadcaster.emit_sync("Manager", "task_abort", f"任务 {task_id} 发生熔断！", {})
                return False
                
            if current_retry > 2 and feedback:
                # 第二级熔断警告：连续失败3次及以上，向 Coder 施压
                logger.warning(f"⚠️ 任务 {task_id} 已失败 {current_retry} 次，正在下发强制思路转换指令！")
                feedback += "\n\n【系统级绝密警告】你已经在这个问题上失败重试了3次以上！请立刻放弃你现在的思路或引用的第三方库，采用最基础、最简单或原生的写法来实现，切勿执迷不悟！"

            # 2. Coder 生成代码 (写进内存 VFS草稿区)
            if current_retry > 0:
                logger.info(f"🔄 第 {current_retry} 次重试修复 [{task_id}]...")
                global_broadcaster.emit_sync("Manager", "task_retry", f"子任务 {task_id} 正在进行第 {current_retry} 次重试", {"attempt": current_retry})
            
            self.coder.generate_code(target_file, description, feedback)
            global_broadcaster.emit_sync("Manager", "vfs_update", f"VFS 文件树更新暂存目标: {target_file}", {"vfs": vfs.get_all_vfs()})

            # 获取 Coder 输出的代码（暂不写入记忆，等 Reviewer 测试后统一记录）
            code_draft = vfs.get_draft(target_file) or ""
            
            # 3. Reviewer 测试与审查沙盒执行
            is_pass, reviewer_feedback = self.reviewer.evaluate_draft(target_file, description)
            
            # 4. 统一写入一条带 verdict 标签的 tdd_round 事件（正面/反面教材）
            verdict = "pass" if is_pass else "fail"
            event_content = (
                f"[{verdict.upper()}] 任务 {task_id} | 文件: {target_file} | 重试: {current_retry}\n"
                f"--- 代码片段 ---\n{code_draft[:1500]}\n"
                f"--- 审查结果 ---\n{reviewer_feedback[:500] if reviewer_feedback else '审查通过'}"
            )
            append_event("tdd", f"round_{verdict}", event_content, project_id=self.project_id,
                         metadata={"task_id": task_id, "target_file": target_file,
                                   "retry": current_retry, "verdict": verdict})

            if is_pass:
                logger.info(f"🎉 任务 [{task_id}] 审查通过！完全符合要求。")
                return True
            else:
                feedback = reviewer_feedback
                vfs.increment_retry(task_id)
                logger.warning(f"🔨 任务 [{task_id}] 审查未通过，退回重写 (Current Retries: {current_retry + 1}/{MAX_RETRIES})")
        
        return False

    @staticmethod
    def _resolve_output_dir(project_name: str, out_dir: Optional[str] = None) -> str:
        """
        统一计算项目输出目录的逻辑。
        如果指定了 out_dir 则使用；否则自动在 projects/ 下按时间戳创建。
        """
        if out_dir:
            return os.path.abspath(out_dir)

        safe_proj_name = "".join(
            [c for c in project_name if c.isalnum() or c in ('_', '-')]
        ).strip()
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        auto_dir_name = f"{timestamp}_{safe_proj_name}"

        base_projects_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "projects"
        )
        return os.path.join(base_projects_dir, auto_dir_name)

    def run_project(self, user_requirement: str, out_dir: Optional[str] = None) -> Tuple[bool, str]:
        """
        项目生成统一主入口（CLI 和 Web 共用）。

        参数:
            user_requirement: 用户的需求文本
            out_dir: 可选的指定输出目录

        返回:
            (success: bool, final_dir: str) — 是否全部成功 + 最终落盘路径
        """
        # 1. 记录用户需求到事件流
        append_event("user", "prompt", user_requirement, project_id=self.project_id)
        create_project_meta(self.project_id)
        
        # 2. 清空上一轮残留状态
        vfs = global_state_manager.get_vfs(self.project_id)
        vfs.clear_state()
        global_broadcaster.emit_sync("System", "start_project", "系统重置并启动新项目生成...")

        # 3. 任务拆解
        plan = self.plan_tasks(user_requirement)
        append_event("manager", "plan", json.dumps(plan, ensure_ascii=False), project_id=self.project_id)

        # 4. 解析输出目录并执行动态重命名
        project_name = plan.get('project_name', 'Unnamed_Project').replace(" ", "_")
        
        if "新建项目" in self.project_id or "default_project" == self.project_id:
            old_project_id = self.project_id
            parts = old_project_id.split("_", 2)
            timestamp = f"{parts[0]}_{parts[1]}" if len(parts) >= 2 else time.strftime("%Y%m%d_%H%M%S")
            import re
            safe_proj_name = re.sub(r'[^\w\-\u4e00-\u9fa5]', '_', project_name)
            new_project_id = f"{timestamp}_{safe_proj_name}"
            
            base_projects_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "projects"))
            old_dir = os.path.join(base_projects_dir, old_project_id)
            new_dir = os.path.join(base_projects_dir, new_project_id)
            
            if os.path.exists(old_dir) and old_dir != new_dir:
                try:
                    os.rename(old_dir, new_dir)
                    # 更新所有作用域的 project_id
                    self.project_id = new_project_id
                    self.coder.project_id = new_project_id
                    self.reviewer.project_id = new_project_id
                    global_state_manager.rename_vfs(old_project_id, new_project_id)
                    
                    rename_project_events(old_project_id, new_project_id)
                    rename_project_meta(old_project_id, new_project_id, safe_proj_name)
                    
                    global_broadcaster.emit_sync("System", "project_renamed", f"项目正式主题生成，宇宙已重命名为: {safe_proj_name}", {
                        "old_id": old_project_id,
                        "new_id": new_project_id
                    })
                except Exception as e:
                    logger.error(f"动态重命名项目时出错: {e}")
                    new_project_id = old_project_id
            else:
                new_project_id = old_project_id
        else:
            new_project_id = self.project_id

        if out_dir:
            final_dir = os.path.abspath(out_dir)
        else:
            final_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "projects", new_project_id))
            
        os.makedirs(final_dir, exist_ok=True)
        logger.info(f"📌 项目代码最终将落盘至: {final_dir}")

        # 4. TDD 循环
        tasks = plan.get("tasks", [])
        if not tasks:
            logger.warning("任务列表为空！")
            global_broadcaster.emit_sync("System", "error", "任务拆解为空，中止进程。")
            return False, final_dir

        logger.info(f"🔥 项目开始启动！项目名: {plan.get('project_name', 'Unnamed')}")

        for idx, task in enumerate(tasks):
            logger.info(f"\n[{idx+1}/{len(tasks)}] ========================")
            success = self.execute_tdd_loop(task, final_dir=final_dir)

            if not success:
                logger.critical(f"💥 核心任务 {task.get('task_id')} 彻底失败。整个项目编译被强行终止以防止 Token 被无效消耗。")
                global_broadcaster.emit_sync("System", "error", f"💥 核心任务 {task.get('task_id')} 连续熔断。项目腰斩！")
                append_event("system", "circuit_break", f"任务 {task.get('task_id')} 熔断", project_id=self.project_id)
                update_project_status(self.project_id, "failed")
                return False, final_dir

        # 6. 全部通过，执行反思闭环 (Reflect & Memorize)
        logger.info("\n🏆 所有任务均已通过 Reviewer 测试！开始触发全局反思...")
        self._reflect_and_memorize(user_requirement, plan)
        
        vfs.commit_to_disk(final_dir)
        global_state_manager.remove_vfs(self.project_id)  # 结束后释放内存
        update_project_status(self.project_id, "success")
        logger.info(f"✨ 项目交付完成: {final_dir}")
        global_broadcaster.emit_sync("System", "success", f"✨ 项目完美生成于！{final_dir}", {"final_path": final_dir})

        return True, final_dir

    def _reflect_and_memorize(self, user_req: str, plan: dict):
        """成功后，调用 qwen3-max 对整个过程进行反思，提炼精髓存入 PG 长期记忆 (后台异步执行)"""
        
        # 收集本次 TDD 循环的关键事件作为反思素材
        recent_events = get_recent_events(project_id=self.project_id, limit=20, caller="Manager/Reflect")
        events_summary = "\n".join([
            f"[{e.role}/{e.event_type}]: {e.content[:150]}..."
            for e in recent_events
            if e.event_type in ('round_pass', 'round_fail', 'circuit_break')
        ]) or "无详细事件日志。"
        
        def _background_task():
            logger.info("🧠 正在后台线程中萃取开发经验存入长期记忆...")
            
            msg = [
                {"role": "system", "content": (
                    "你是一个资深架构师。根据用户需求、执行计划和 TDD 过程中的实际事件（代码产出、测试通过/失败记录），"
                    "提炼出 2-3 条极具价值的技术经验。重点关注：踩过的坑、失败后如何修复、关键架构决策。"
                    "字数控制在 200 字以内，直接输出干货。"
                )},
                {"role": "user", "content": (
                    f"原始需求: {user_req}\n"
                    f"执行计划: {json.dumps(plan, ensure_ascii=False)[:500]}\n"
                    f"TDD 关键事件:\n{events_summary}"
                )}
            ]
            try:
                resp = self.llm_client.chat_completion(msg, model="qwen3-max")
                distilled_knowledge = resp.content.strip()
                
                meta = {"source": "post_project_reflection", "project_name": plan.get('project_name')}
                
                # 双写：global (跨项目通用经验) + project (专属上下文)
                memorize(distilled_knowledge, scope="global", metadata=meta)
                memorize(distilled_knowledge, scope="project", project_id=self.project_id, metadata=meta)
                
                # 同时记入事件流
                append_event("system", "reflection", distilled_knowledge, project_id=self.project_id)
                
                logger.info("✨ [后台任务] 经验复盘与长时记忆写入完毕！")
            except Exception as e:
                logger.warning(f"✨ [后台任务] 经验复盘总结失败: {e}")
                
        # 抛入后台执行，不阻塞前台项目完成的 WebSocket
        global_broadcaster.emit_sync("System", "info", "🧠 所有测试通过！大模型正在后台默默回溯思考，总结并打入长时记忆库...")
        threading.Thread(target=_background_task, daemon=True).start()
