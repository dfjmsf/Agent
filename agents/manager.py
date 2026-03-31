import os
import re
import json
import time
import logging
import threading
from typing import List, Dict, Any, Optional, Tuple
from core.llm_client import default_llm
from core.prompt import Prompts
from core.ws_broadcaster import global_broadcaster
from agents.coder import CoderAgent
from agents.reviewer import ReviewerAgent
from core.database import (
    append_event, get_recent_events, rename_project_events,
    recall, upsert_file_tree,
    create_project_meta, update_project_status, rename_project_meta,
    insert_trajectory, finalize_trajectory,
    get_recalled_memory_union, settle_memory_scores,
    get_global_round, tick_global_round,
)
from agents.synthesizer import SynthesizerAgent
from agents.auditor import AuditorAgent

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

    def _generate_project_spec(self, user_requirement: str) -> dict:
        """
        步骤 1: 生成或增量更新项目规划书 (Project Spec)。
        - 新项目: 全量生成
        - 已有规划书: 注入旧 spec，让 LLM 判断是否需要修改（允许原样输出）
        """
        logger.info("📋 Manager 正在生成/更新项目规划书...")
        
        # 检查是否已有规划书
        existing_spec_events = get_recent_events(
            project_id=self.project_id, limit=1,
            event_types=["project_spec"], caller="Manager/Spec"
        )
        
        if existing_spec_events:
            # 已有规划书 → 增量更新（全量覆写模式，prompt 提供"可以不改"选项）
            old_spec_content = existing_spec_events[0].content
            system_prompt = Prompts.MANAGER_SPEC_UPDATE_SYSTEM.format(existing_spec=old_spec_content)
            logger.info("📋 检测到已有规划书，进入增量更新模式")
        else:
            # 新项目 → 全量生成
            system_prompt = Prompts.MANAGER_SPEC_SYSTEM
            logger.info("📋 新项目，全量生成规划书")
        
        user_prompt = f"主人的开发需求：\n{user_requirement}\n请输出项目规划书 JSON。"
        
        try:
            raw_response = self.llm_client.chat_completion(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]
            )
            json_str = raw_response.content
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0].strip()
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0].strip()
            
            spec = json.loads(json_str)
            spec_text = json.dumps(spec, ensure_ascii=False, indent=2)
            
            # upsert 语义：删旧写新
            from sqlalchemy import text as sql_text
            from core.database import ScopedSession, SessionEvent
            session = ScopedSession()
            try:
                session.query(SessionEvent).filter(
                    SessionEvent.project_id == self.project_id,
                    SessionEvent.event_type == "project_spec"
                ).delete()
                session.commit()
            finally:
                ScopedSession.remove()
            
            append_event("manager", "project_spec", spec_text, project_id=self.project_id)
            
            logger.info(f"✅ 项目规划书生成完毕 ({len(spec_text)} bytes)")
            global_broadcaster.emit_sync("Manager", "project_spec_ready", "项目规划书已就绪", {"spec": spec})
            
            return spec
            
        except json.JSONDecodeError as e:
            logger.error(f"规划书 JSON 解析失败: {e}")
            return {}
        except Exception as e:
            logger.error(f"规划书生成异常: {e}")
            return {}

    def plan_tasks(self, user_requirement: str, project_spec: dict = None,
                   manager_playbook: str = "") -> dict:
        """
        步骤 2: 基于规划书拆解任务列表。
        """
        logger.info("🧠 Manager 正在基于规划书拆解任务...")
        
        # 1. 查询短期记忆 — 只读 prompt 类事件（精简：不注入 TDD 噪音）
        recent_events = get_recent_events(
            project_id=self.project_id, limit=5,
            event_types=["prompt"], caller="Manager"
        )
        history_str = "\n".join([f"[用户需求]: {e.content[:150]}..." for e in recent_events]) if recent_events else "无近期对话。"
        
        # 2. 查询长期经验记忆 (RAG)
        past_experience = recall(user_requirement, n_results=3, project_id=self.project_id, caller="Manager")
        experience_str = "\n".join([e["content"] for e in past_experience]) if past_experience else "无相关历史经验。"

        # 3. 构造环境变量声明
        is_new_project = "新建项目" in self.project_id or self.project_id == "default_project"
        if is_new_project:
            env_context = "【项目环境】\n这是全新项目。项目名称已在规划书中确定。请专注于拆解任务列表，JSON 中的 `project_name` 使用规划书中的名称即可。"
        else:
            # v1.3: 直接从项目目录读取文件列表（不依赖 StateManager VFS）
            existing_files = []
            base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "projects", self.project_id))
            if os.path.isdir(base_dir):
                ignore = {'.sandbox', '.git', '__pycache__', '.venv', 'node_modules', '.idea'}
                for root, dirs, files in os.walk(base_dir):
                    dirs[:] = [d for d in dirs if d not in ignore]
                    for f in files:
                        rel = os.path.relpath(os.path.join(root, f), base_dir).replace("\\", "/")
                        existing_files.append(rel)
            file_tree = "\n".join([f"- {f}" for f in existing_files]) if existing_files else "目录暂空。"
            env_context = (
                f"【项目环境】\n当前项目宇宙已永久命名并固化为: `{self.project_id}`\n"
                f"你 MUST 且只能将 JSON 里的 `project_name` 固定为 `{self.project_id}`，绝对不允许修改项目名！！！\n"
                f"【已有文件架构】:\n{file_tree}\n"
                f"这是当前项目内存里的所有文件，你可以在此基础上规划对旧文件的修改（覆盖），或者指派增加全新的子模块文件。"
            )

        # 4. 注入规划书（如果有）
        spec_context = ""
        if project_spec:
            spec_str = json.dumps(project_spec, ensure_ascii=False, indent=2)
            spec_context = f"\n\n【项目规划书 — 你必须基于此架构拆解任务】\n{spec_str}"

        # 5. 注入 Playbook（技术栈专用拆分规则）
        manager_system = Prompts.MANAGER_SYSTEM.format(manager_playbook=manager_playbook)
        system_prompt = manager_system + f"\n\n【近期用户需求历史】\n{history_str}\n\n【RAG 检索到的过往血泪经验】\n{experience_str}\n\n{env_context}{spec_context}"
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
            
            # 去重：防止 LLM 将同一文件拆成多个 task
            seen_files = set()
            deduped_tasks = []
            for task in plan["tasks"]:
                tf = task.get("target_file", "")
                if tf not in seen_files:
                    seen_files.add(tf)
                    deduped_tasks.append(task)
                else:
                    logger.warning(f"⚠️ 去重: 跳过重复文件 task: {tf}")
            if len(deduped_tasks) < len(plan["tasks"]):
                logger.warning(f"⚠️ Manager 去重: {len(plan['tasks'])} → {len(deduped_tasks)} 个 task")
            plan["tasks"] = deduped_tasks
                
            logger.info(f"✅ 任务拆解完成: {plan.get('project_name')}")
            global_broadcaster.emit_sync("Manager", "plan_ready", f"成功拆解任务: {plan.get('project_name')}", {"plan": plan})
            
            return plan
            
        except json.JSONDecodeError as e:
            logger.error(f"Manager 返回的并不是标准的 JSON 格式：\n{raw_response}")
            return {"project_name": "Error_Project", "architecture_summary": "解析失败", "tasks": []}
        except Exception as e:
            logger.error(f"Manager 拆解任务时发生异常: {e}")
            return {"project_name": "Error_Project", "architecture_summary": "API异常", "tasks": []}

    def plan_patch(self, user_requirement: str) -> dict:
        """
        Patch Mode 精简规划：读取项目文件树 + Observer 骨架，
        只规划需要修改的文件（跳过 Spec 生成）。
        """
        logger.info("⚡ [Patch Mode] Manager 精简规划启动...")
        global_broadcaster.emit_sync("Manager", "patch_plan_start", "Patch Mode: 分析需修改的文件...")

        # 1. 读取项目文件树
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "projects", self.project_id))
        existing_files = []
        if os.path.isdir(base_dir):
            ignore = {'.sandbox', '.git', '__pycache__', '.venv', 'node_modules', '.idea'}
            for root, dirs, files in os.walk(base_dir):
                dirs[:] = [d for d in dirs if d not in ignore]
                for f in files:
                    rel = os.path.relpath(os.path.join(root, f), base_dir).replace("\\", "/")
                    existing_files.append(rel)
        file_tree = "\n".join([f"- {f}" for f in existing_files]) if existing_files else "目录暂空。"

        # 2. 提取所有源文件的 Observer 骨架
        skeleton_parts = []
        try:
            from tools.observer import Observer
            obs = Observer(base_dir)
            source_exts = {'.py', '.js', '.jsx', '.ts', '.tsx', '.html', '.css', '.vue'}
            for f in existing_files:
                ext = os.path.splitext(f)[1].lower()
                if ext in source_exts:
                    skeleton = obs.get_skeleton(f)
                    if skeleton and "Error" not in skeleton:
                        skeleton_parts.append(skeleton)
        except Exception as e:
            logger.warning(f"⚠️ [Patch Mode] Observer 骨架提取异常: {e}")

        file_skeletons = "\n\n".join(skeleton_parts) if skeleton_parts else "无骨架信息。"

        # 3. 构建 prompt
        system_prompt = Prompts.MANAGER_PATCH_SYSTEM.format(
            project_id=self.project_id,
            file_tree=file_tree,
            file_skeletons=file_skeletons,
        )
        user_prompt = f"主人的修改需求：\n{user_requirement}\n请严格按照 JSON Schema 输出。"

        # 4. 调用 LLM
        try:
            raw_response = self.llm_client.chat_completion(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]
            )

            json_str = raw_response.content
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0].strip()
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0].strip()

            plan = json.loads(json_str)

            plan["project_name"] = self.project_id  # 强制锁定
            if "tasks" not in plan:
                plan["tasks"] = []

            # 去重
            seen_files = set()
            deduped = []
            for task in plan["tasks"]:
                tf = task.get("target_file", "")
                if tf not in seen_files:
                    seen_files.add(tf)
                    deduped.append(task)
            plan["tasks"] = deduped

            logger.info(f"⚡ [Patch Mode] 精简规划完成: {len(plan['tasks'])} 个文件需修改")
            global_broadcaster.emit_sync("Manager", "patch_plan_ready",
                f"Patch Mode: {len(plan['tasks'])} 个文件需修改", {"plan": plan})
            return plan

        except json.JSONDecodeError:
            logger.error(f"[Patch Mode] Manager 返回非 JSON: {raw_response.content[:200]}")
            return {"project_name": self.project_id, "tasks": []}
        except Exception as e:
            logger.error(f"[Patch Mode] Manager 规划异常: {e}")
            return {"project_name": self.project_id, "tasks": []}

    def execute_tdd_loop(self, task: Dict[str, Any], final_dir: str = None, task_meta: dict = None) -> Tuple[bool, Dict[str, str]]:
        """
        核心的 TDD (Test-Driven Development) 开发与审查闭环
        返回: (success: bool, milestones: dict)
        milestones: {"a": 初始代码, "b": 报错摘要流, "c": 最终代码}
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
        
        # 里程碑收集器
        milestones = {"a": "", "b": "", "c": ""}
        error_trail = []  # 报错摘要流
        
        feedback = None
        vfs.reset_retry(task_id)
        
        while True:
            current_retry = vfs.get_retry_count(task_id)
            
            # 1. 熔断机制判定
            if current_retry >= MAX_RETRIES:
                logger.error(f"🚨 [熔断触发] 任务 {task_id} 连续失败 {MAX_RETRIES} 次，陷入死循环！停止执行。")
                global_broadcaster.emit_sync("Manager", "task_abort", f"任务 {task_id} 发生熔断！", {})
                # 熔断时也收集里程碑 B
                milestones["b"] = "\n".join(error_trail) if error_trail else "无报错记录"
                return False, milestones
                
            if current_retry > 2 and feedback:
                # 第二级熔断警告：连续失败3次及以上，向 Coder 施压
                logger.warning(f"⚠️ 任务 {task_id} 已失败 {current_retry} 次，正在下发强制思路转换指令！")
                feedback += "\n\n【系统级绝密警告】你已经在这个问题上失败重试了3次以上！请立刻放弃你现在的思路或引用的第三方库，采用最基础、最简单或原生的写法来实现，切勿执迷不悟！"

            # 2. Coder 生成代码 (写进内存 VFS草稿区)
            if current_retry > 0:
                logger.info(f"🔄 第 {current_retry} 次重试修复 [{task_id}]...")
                global_broadcaster.emit_sync("Manager", "task_retry", f"子任务 {task_id} 正在进行第 {current_retry} 次重试", {"attempt": current_retry})
            
            self.coder.generate_code(target_file, description, feedback, task_meta=task_meta)
            recalled_ids = getattr(self.coder, '_last_recalled_ids', [])
            global_broadcaster.emit_sync("Manager", "vfs_update", f"VFS 文件树更新暂存目标: {target_file}", {"vfs": vfs.get_all_vfs()})

            # 获取 Coder 输出的代码
            code_draft = vfs.get_draft(target_file) or ""
            
            # 里程碑 A：记录第一次的代码（“初始错误直觉”）
            if current_retry == 0:
                milestones["a"] = code_draft
            
            # 更新 File Tree
            file_list = list(vfs.get_all_vfs().keys())
            upsert_file_tree(self.project_id, file_list)
            
            # 3. Reviewer 测试与审查沙盒执行
            is_pass, reviewer_feedback = self.reviewer.evaluate_draft(target_file, description)
            
            # 4. 统一写入 tdd_round 事件
            verdict = "pass" if is_pass else "fail"
            if is_pass:
                # round_pass 精简：不含代码（与 Synthesizer 经验重复），只记摘要
                event_content = f"[PASS] 任务 {task_id} | 文件: {target_file} | 重试: {current_retry} | 审查通过"
            else:
                # round_fail 保留完整代码和报错（Coder 需要用来修复）
                event_content = (
                    f"[FAIL] 任务 {task_id} | 文件: {target_file} | 重试: {current_retry}\n"
                    f"--- 代码片段 ---\n{code_draft[:1500]}\n"
                    f"--- 审查结果 ---\n{reviewer_feedback[:500]}"
                )
            append_event("tdd", f"round_{verdict}", event_content, project_id=self.project_id,
                         metadata={"task_id": task_id, "target_file": target_file,
                                   "retry": current_retry, "verdict": verdict})

            if is_pass:
                # 里程碑 C：最终通过的代码（"通关密码"）
                milestones["c"] = code_draft
                milestones["b"] = "\n".join(error_trail) if error_trail else "（一次通过，无报错记录）"
                # 轨迹表：写入成功轨迹（含 recalled IDs）+ 回填最终代码
                insert_trajectory(
                    project_id=self.project_id, task_id=task_id,
                    attempt_round=current_retry, error_summary=None,
                    failed_code=None, recalled_memory_ids=recalled_ids,
                )
                finalize_trajectory(self.project_id, task_id, code_draft)
                logger.info(f"🎉 任务 [{task_id}] 审查通过！recalled_ids={recalled_ids}")
                return True, milestones
            else:
                # 里程碑 B：累积报错摘要
                error_trail.append(f"尝试{current_retry + 1}: {reviewer_feedback[:200]}")
                # 轨迹表：写入本轮失败快照 + 实际 recalled IDs
                insert_trajectory(
                    project_id=self.project_id, task_id=task_id,
                    attempt_round=current_retry,
                    error_summary=reviewer_feedback[:2000],
                    failed_code=code_draft,
                    recalled_memory_ids=recalled_ids,
                )
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

        # 3. 生成规划书 → 任务拆解
        project_spec = self._generate_project_spec(user_requirement)
        plan = self.plan_tasks(user_requirement, project_spec=project_spec)
        
        # 将 spec 存入 plan 供后续传递
        plan["project_spec"] = project_spec
        spec_text = json.dumps(project_spec, ensure_ascii=False, indent=2) if project_spec else "无规划书"
        append_event("manager", "plan", json.dumps(plan, ensure_ascii=False), project_id=self.project_id)

        # 4. 解析输出目录并执行动态重命名
        project_name = plan.get('project_name', 'Unnamed_Project').replace(" ", "_")
        
        if "新建项目" in self.project_id or "default_project" == self.project_id:
            old_project_id = self.project_id
            parts = old_project_id.split("_", 2)
            timestamp = f"{parts[0]}_{parts[1]}" if len(parts) >= 2 else time.strftime("%Y%m%d_%H%M%S")
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

        # 5. 重命名完成后异步预热 sandbox（用最终 project_id）
        from tools.sandbox import sandbox_env
        tech_stacks = project_spec.get("tech_stack", []) if project_spec else []
        if tech_stacks:
            final_project_id = new_project_id
            def _bg_warmup():
                sandbox_env.warm_up(final_project_id, tech_stacks)
            threading.Thread(target=_bg_warmup, daemon=True).start()

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

        all_milestones = []  # 收集所有任务的里程碑
        synthesizer = SynthesizerAgent(project_id=self.project_id)

        # 软删除该 project 的旧轨迹记录（标记 is_synthesized=True），确保本次审计范围干净
        from core.database import TaskTrajectory
        from core.database import ScopedSession as _SS
        _session = _SS()
        try:
            staled = _session.query(TaskTrajectory).filter(
                TaskTrajectory.project_id == self.project_id,
                TaskTrajectory.is_synthesized == False,
            ).update({"is_synthesized": True})
            _session.commit()
            if staled:
                logger.info(f"🧹 旧轨迹已归档: {staled} 条 (project={self.project_id})")
        except Exception:
            _session.rollback()
        finally:
            _SS.remove()

        for idx, task in enumerate(tasks):
            logger.info(f"\n[{idx+1}/{len(tasks)}] ========================")
            # 构建 task_meta 传递给 Coder：规划书 + 依赖列表
            task_meta = {
                "project_spec": spec_text,
                "dependencies": task.get("dependencies", []),
                "all_tasks": tasks,  # 用于按 dependencies 查找依赖文件
            }
            success, milestones = self.execute_tdd_loop(task, final_dir=final_dir, task_meta=task_meta)
            all_milestones.append({"task": task, "milestones": milestones, "success": success})

            if not success:
                logger.critical(f"💥 核心任务 {task.get('task_id')} 彻底失败。")
                global_broadcaster.emit_sync("System", "error", f"💥 核心任务 {task.get('task_id')} 连续熔断。项目腰斩！")
                append_event("system", "circuit_break", f"任务 {task.get('task_id')} 熔断", project_id=self.project_id)
                update_project_status(self.project_id, "failed")
                
                # 熔断时也调用 Synthesizer 提炼 Anti-pattern
                global_broadcaster.emit_sync("System", "info", "🧠 熔断了！正在提炼失败教训...")
                def _bg_failure():
                    synthesizer.synthesize_failure(milestones, user_requirement, plan)
                threading.Thread(target=_bg_failure, daemon=True).start()
                
                return False, final_dir

        # 6. 全部通过，调用 Synthesizer 提炼 Contrastive Pair
        logger.info("\n🏆 所有任务均已通过 Reviewer 测试！触发 Synthesizer 知识提炼...")
        
        vfs.commit_to_disk(final_dir)
        global_state_manager.remove_vfs(self.project_id)
        update_project_status(self.project_id, "success")
        logger.info(f"✨ 项目交付完成: {final_dir}")
        global_broadcaster.emit_sync("System", "success", f"✨ 项目完美生成！{final_dir}", {"final_path": final_dir})

        # 后台异步: Synthesizer 蒸馏 + Auditor 按 task 粒度审计 + AMC 延迟结算
        global_broadcaster.emit_sync("System", "info", "🧠 所有测试通过！正在执行经验提炼与 AMC 结算...")
        project_id = self.project_id
        def _bg_settlement():
            try:
                for item in all_milestones:
                    if item["success"]:
                        synthesizer.synthesize_success(item["milestones"], user_requirement, plan)
                logger.info("✨ [后台] Synthesizer 知识提炼完毕")

                # 按 task 粒度审计：每个 task 的 recalled IDs 只对该 task 的 final_code 审计
                all_used_ids, all_ignored_ids = set(), set()
                auditor = AuditorAgent()
                from core.database import ScopedSession, Memory

                for item in all_milestones:
                    if not item["success"]:
                        continue
                    tid = item["task"].get("task_id", "")
                    task_memory_ids = get_recalled_memory_union(project_id, tid)
                    if not task_memory_ids:
                        continue

                    task_final_code = item["milestones"].get("c", "")
                    if not task_final_code:
                        continue

                    # 填充记忆内容
                    memories_to_audit = [{"id": mid, "content": ""} for mid in task_memory_ids]
                    session = ScopedSession()
                    try:
                        for m in memories_to_audit:
                            if m["id"] > 0:
                                row = session.query(Memory).filter(Memory.id == m["id"]).first()
                                if row:
                                    m["content"] = row.content[:300]
                    finally:
                        ScopedSession.remove()

                    logger.info(f"📋 [后台] Auditor 审计 [{tid}]: {len(memories_to_audit)} 条记忆 vs {len(task_final_code)} bytes 代码")
                    audit_result = auditor.audit(task_final_code, memories_to_audit)

                    for r in audit_result.get("results", []):
                        mid = r.get("memory_id", -1)
                        if mid > 0:
                            (all_used_ids if r.get("adopted") else all_ignored_ids).add(mid)

                if all_used_ids or all_ignored_ids:
                    settle_memory_scores(all_used_ids, all_ignored_ids, get_global_round())
                    logger.info(f"✨ [后台] AMC 延迟结算完成: 功臣{len(all_used_ids)} 陪跑{len(all_ignored_ids)}")
                else:
                    logger.info("📋 [后台] 无召回记忆需要结算，跳过")
                tick_global_round()
            except Exception as e:
                logger.error(f"❌ [后台] 结算流程异常: {e}")
        threading.Thread(target=_bg_settlement, daemon=True).start()

        return True, final_dir
