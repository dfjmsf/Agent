"""
AstreaEngine — 状态机驱动的工作流编排引擎

v1.3 核心架构：
- 唯一的 while 循环拥有者
- 唯一的 VFS 写入者
- 通过 Blackboard 状态机驱动 Agent 唤醒
- 依赖图调度保证任务执行顺序
- Checkpoint 断点续传支持
"""
import os
import re
import json
import time
import logging
import threading
from typing import Tuple, Optional, Dict, Any, List

from core.blackboard import Blackboard, BlackboardState, TaskItem, TaskStatus, ProjectStatus
from core.code_patcher import CodePatcher, PatchFailedError, extract_xml_files
from core.vfs_utils import VfsUtils
from core.ws_broadcaster import global_broadcaster
from core.database import (
    append_event, get_recent_events, rename_project_events,
    recall, recall_project_experience, upsert_file_tree,
    create_project_meta, update_project_status, rename_project_meta,
    insert_trajectory, finalize_trajectory,
    get_recalled_memory_union, settle_memory_scores,
    get_global_round, tick_global_round,
)

logger = logging.getLogger("AstreaEngine")

MAX_RETRIES = int(os.getenv("MAX_RETRIES", 5))


class AstreaEngine:
    """
    状态机驱动的工作流编排引擎。

    核心原则：
    - Blackboard 是唯一的状态源
    - Engine 是唯一的 VFS 写入者
    - Agent 读 Blackboard，写结果回 Blackboard
    - Engine 扫描 Blackboard 状态来决定下一步动作
    """

    def __init__(self, project_id: str):
        self.blackboard = Blackboard(project_id)
        self.patcher = CodePatcher()
        self.vfs: Optional[VfsUtils] = None  # run() 时初始化
        self._shutdown = False  # 优雅退出标志

        # Agent 延迟导入（避免循环依赖）
        self._manager = None
        self._coder = None
        self._reviewer = None

    @property
    def project_id(self) -> str:
        return self.blackboard.project_id

    def _get_manager(self):
        if self._manager is None:
            from agents.manager import ManagerAgent
            self._manager = ManagerAgent(self.project_id)
        return self._manager

    def _get_coder(self):
        if self._coder is None:
            from agents.coder import CoderAgent
            self._coder = CoderAgent(self.project_id)
        return self._coder

    def _get_reviewer(self):
        if self._reviewer is None:
            from agents.reviewer import ReviewerAgent
            self._reviewer = ReviewerAgent(self.project_id)
        return self._reviewer

    # ============================================================
    # 主入口
    # ============================================================

    def run(self, user_requirement: str, out_dir: str = None) -> Tuple[bool, str]:
        """
        主入口：用户需求 → 完整项目

        Args:
            user_requirement: 用户需求文本
            out_dir: 可选指定输出目录

        Returns:
            (success, final_dir)
        """
        logger.info(f"🚀 AstreaEngine 启动: {self.project_id}")

        # 记录用户需求
        self.blackboard.state.user_requirement = user_requirement
        append_event("user", "prompt", user_requirement, project_id=self.project_id)
        create_project_meta(self.project_id)
        global_broadcaster.emit_sync("System", "start_project", "AstreaEngine 启动...")

        try:
            if self._is_existing_project():
                return self._run_patch_mode(user_requirement)

            # Phase 1: 规划（含重命名 + sandbox 预热，与 plan_tasks 并行）
            self._phase_planning(user_requirement, out_dir=out_dir)

            # 输出目录已在 _phase_planning 中设置
            final_dir = self.blackboard.state.out_dir

            # 防御：规划阶段未产出任何任务 → 直接失败（通常是网络异常）
            if not self.blackboard.state.tasks:
                logger.error("💥 规划阶段未生成任何任务，项目失败")
                self.blackboard.set_project_status(ProjectStatus.FAILED)
                update_project_status(self.project_id, "failed")
                global_broadcaster.emit_sync("System", "error", "💥 规划失败：未生成任何任务（可能是网络异常）")
                return False, out_dir or ""

            # Phase 2: 执行
            self.blackboard.set_project_status(ProjectStatus.EXECUTING)
            success = self._phase_execution()

            # Phase 3: 结算
            if success:
                self.blackboard.set_project_status(ProjectStatus.COMPLETED)
                update_project_status(self.project_id, "success")
                logger.info(f"✨ 项目交付完成: {final_dir}")
                global_broadcaster.emit_sync("System", "success",
                    f"✨ 项目完美生成！{final_dir}", {"final_path": final_dir})
            else:
                self.blackboard.set_project_status(ProjectStatus.FAILED)
                update_project_status(self.project_id, "failed")
                global_broadcaster.emit_sync("System", "error", "💥 项目存在熔断任务")

            # 后台异步结算
            self._phase_settlement(user_requirement, success)

            # 清理 checkpoint
            self.blackboard.delete_checkpoint()
            self.vfs.clean_sandbox()

            return success, final_dir

        except Exception as e:
            logger.error(f"❌ AstreaEngine 异常: {e}")
            import traceback
            traceback.print_exc()
            self.blackboard.set_project_status(ProjectStatus.FAILED)
            update_project_status(self.project_id, "failed")
            return False, out_dir or ""

    @classmethod
    def resume(cls, project_id: str) -> Optional['AstreaEngine']:
        """从 Checkpoint 恢复并继续执行"""
        bb = Blackboard.restore(project_id)
        if not bb:
            return None
        engine = cls.__new__(cls)
        engine.blackboard = bb
        engine.patcher = CodePatcher()
        engine.vfs = VfsUtils(bb.state.out_dir) if bb.state.out_dir else None
        engine._manager = None
        engine._coder = None
        engine._reviewer = None
        logger.info(f"🔄 AstreaEngine 从 Checkpoint 恢复: {project_id}")
        return engine

    # ============================================================
    # Patch Mode: 微调快速通道
    # ============================================================

    def _is_existing_project(self) -> bool:
        """判断当前项目是否是已有项目（非新建）"""
        if "新建项目" in self.project_id or self.project_id == "default_project":
            return False
        # 检查项目目录是否存在且有文件
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "projects", self.project_id))
        if not os.path.isdir(base_dir):
            return False
        # 目录非空（排除 .sandbox 等元文件）
        ignore = {'.sandbox', '.git', '__pycache__', '.venv'}
        for item in os.listdir(base_dir):
            if item not in ignore:
                return True
        return False

    def _run_patch_mode(self, user_requirement: str) -> Tuple[bool, str]:
        """
        微调快速通道：跳过 Spec 生成 + Sandbox 预热。
        Manager 只规划受影响文件，Coder 自动走 fix_with_editor 差量编辑。
        """
        logger.info(f"⚡ Patch Mode 启动: {self.project_id}")
        global_broadcaster.emit_sync("System", "start_project", f"⚡ Patch Mode: {self.project_id}")
        self.blackboard.set_project_status(ProjectStatus.PLANNING)

        # 直接使用已有目录
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "projects"))
        final_dir = os.path.join(base_dir, self.project_id)
        self.blackboard.state.out_dir = final_dir
        self.blackboard.state.project_name = self.project_id
        self.vfs = VfsUtils(final_dir)

        # Manager 精简规划（只规划受影响文件，不生成 Spec）
        manager = self._get_manager()
        plan = manager.plan_patch(user_requirement)

        self.blackboard.set_tasks(plan.get("tasks", []))

        # 设置轻量 spec（Coder fallback 时需要）
        self.blackboard.state.spec_text = (
            f"[Patch Mode] {plan.get('architecture_summary', '微调修改')}\n"
            f"用户需求: {user_requirement}"
        )

        # 记录事件
        append_event("manager", "patch_plan", json.dumps(plan, ensure_ascii=False),
                     project_id=self.project_id)

        if not self.blackboard.state.tasks:
            logger.error("💥 [Patch Mode] 未规划任何任务")
            self.blackboard.set_project_status(ProjectStatus.FAILED)
            global_broadcaster.emit_sync("System", "error", "💥 Patch Mode 规划失败")
            return False, final_dir

        logger.info(f"⚡ [Patch Mode] {len(self.blackboard.state.tasks)} 个文件需修改")

        # 复用 sandbox（已有 venv，无需重新安装依赖）
        # 不调用 _warmup_sandbox

        # Phase 2: 执行（复用现有 _phase_execution，
        # Coder 会自动走 fix_with_editor 因为 existing_code 不为空）
        self.blackboard.set_project_status(ProjectStatus.EXECUTING)
        success = self._phase_execution()

        if success:
            self.blackboard.set_project_status(ProjectStatus.COMPLETED)
            update_project_status(self.project_id, "success")
            logger.info(f"✨ [Patch Mode] 修改完成: {final_dir}")
            global_broadcaster.emit_sync("System", "success",
                f"✨ Patch Mode 修改完成！{final_dir}", {"final_path": final_dir})
        else:
            self.blackboard.set_project_status(ProjectStatus.FAILED)
            update_project_status(self.project_id, "failed")
            global_broadcaster.emit_sync("System", "error", "💥 Patch Mode 存在熔断任务")

        # 结算
        self._phase_settlement(user_requirement, success)

        # 清理
        self.blackboard.delete_checkpoint()
        if self.vfs:
            self.vfs.clean_sandbox()

        return success, final_dir

    # ============================================================
    # Phase 1: 规划 (唤醒 Manager)
    # ============================================================

    def _phase_planning(self, user_requirement: str, out_dir: str = None):
        """唤醒 Manager → 贴规划书 + 任务列表到 Blackboard → Manager 退场"""
        logger.info("📋 Phase 1: 规划阶段...")
        self.blackboard.set_project_status(ProjectStatus.PLANNING)

        manager = self._get_manager()

        # Step 1: 生成规划书（含 project_name + tech_stack）
        project_spec = manager._generate_project_spec(user_requirement)

        # Step 1.5: 从 spec 提取项目名 → 立即重命名 + 启动 sandbox 预热
        project_name = (project_spec.get("project_name", "") or "").replace(" ", "_") if project_spec else ""
        if not project_name:
            project_name = "Unnamed_Project"

        # 提前设置 project_name 到 blackboard（供 _resolve_output_dir 使用）
        self.blackboard.state.project_name = project_name

        # 立即计算输出目录 + 重命名
        final_dir = self._resolve_output_dir(out_dir)
        self.blackboard.state.out_dir = final_dir
        os.makedirs(final_dir, exist_ok=True)
        self.vfs = VfsUtils(final_dir)

        # 立即启动 sandbox 预热（异步，不阻塞 plan_tasks）
        self._warmup_sandbox(project_spec=project_spec)

        # Step 2: 拆解任务（与 sandbox warmup 并行！）
        plan = manager.plan_tasks(user_requirement, project_spec=project_spec)
        plan["project_spec"] = project_spec

        # 贴上黑板
        spec_text = json.dumps(project_spec, ensure_ascii=False, indent=2) if project_spec else "无规划书"
        self.blackboard.set_project_spec(project_spec, spec_text, project_name)
        self.blackboard.set_tasks(plan.get("tasks", []))

        # 记录事件
        append_event("manager", "plan", json.dumps(plan, ensure_ascii=False),
                      project_id=self.project_id)

        logger.info(f"📋 规划完成: {project_name}, {len(plan.get('tasks', []))} 个子任务")

    # ============================================================
    # Phase 2: 执行 (状态机主循环)
    # ============================================================

    def _phase_execution(self) -> bool:
        """
        主循环：基于依赖图调度，逐个执行 TDD。

        Returns:
            True if all tasks DONE, False if any FUSED
        """
        logger.info("⚙️ Phase 2: 执行阶段...")

        # 软删除旧轨迹
        self._archive_old_trajectories()

        task_idx = 0
        total = len(self.blackboard.state.tasks)

        while True:
            # 优雅退出检查
            if self._shutdown:
                logger.warning("🛑 Engine 检测到 shutdown 信号，停止执行")
                return False

            # 检查是否全部完成
            if self.blackboard.all_tasks_done():
                if self.blackboard.has_fused_tasks():
                    logger.error("💥 存在熔断任务")
                    return False
                logger.info("🏆 所有任务均已完成！")
                return True

            # 依赖图调度：找下一个可运行的任务
            task = self.blackboard.get_next_runnable_task()
            if task is None:
                # 没有可运行任务但也没全部完成 → 死锁检测
                logger.error("💥 依赖死锁：无可运行任务但存在未完成任务")
                return False

            task_idx += 1
            logger.info(f"\n[{task_idx}/{total}] ========================")

            # 执行单个任务的 TDD 循环
            self._execute_task(task)

            # Checkpoint
            self.blackboard.checkpoint()

    # ============================================================
    # 单任务 TDD 状态机
    # ============================================================

    def _execute_task(self, task: TaskItem):
        """
        单个任务的 TDD 状态机：

        TODO → CODING (唤醒 Coder)
        CODING → CodePatcher 缝合
            成功 → PENDING_REVIEW (写入 Sandbox 测试区)
            失败 → PATCH_FAILED (原因写入 error_logs，回到 CODING)
        PENDING_REVIEW → REVIEWING (唤醒 Reviewer)
            PASS → PASSED → commit 到 VFS 真理区 → DONE
            FAIL → REJECTED (回到 CODING)
        超过 MAX_RETRIES → FUSED
        """
        logger.info(f"🚀 开始任务 [{task.task_id}]: {task.target_file}")
        global_broadcaster.emit_sync("Engine", "task_start",
            f"开始任务: {task.target_file}", {"task_id": task.task_id})

        # 如果真理区已有文件（跨请求修改），预加载
        if self.vfs:
            existing = self.vfs.read_truth(task.target_file)
            if existing and not task.code_draft:
                task.log_action(f"从真理区预加载: {task.target_file}")

        feedback = None

        while True:
            # 优雅退出检查
            if self._shutdown:
                logger.warning("🛑 Engine 检测到 shutdown 信号，任务中断")
                return

            # 熔断检测
            if task.retry_count >= MAX_RETRIES:
                logger.error(f"🚨 [熔断] 任务 {task.task_id} 连续失败 {MAX_RETRIES} 次")
                self.blackboard.update_task_status(task.task_id, TaskStatus.FUSED)
                global_broadcaster.emit_sync("Engine", "task_abort",
                    f"任务 {task.task_id} 熔断！", {})
                return

            # 施压警告
            if task.retry_count > 2 and feedback:
                feedback += ("\n\n【系统级绝密警告】你已经在这个问题上失败重试了3次以上！"
                             "请立刻放弃你现在的思路或引用的第三方库，"
                             "采用最基础、最简单或原生的写法来实现，切勿执迷不悟！")

            # (1) 唤醒 Coder
            self.blackboard.update_task_status(task.task_id, TaskStatus.CODING)
            code_output = self._invoke_coder(task, feedback)

            if not code_output:
                task.log_error("Coder 返回空代码")
                self.blackboard.increment_retry(task.task_id)
                feedback = "Coder 返回了空代码，请重新生成完整代码。"
                continue

            # 从输出中提取代码和 action
            # 优先尝试 XML 提取（_generate_full 的标准输出格式）
            xml_files = extract_xml_files(code_output)

            # 判断是否为有效的 XML 提取结果
            has_real_xml = any(xf["path"] for xf in xml_files) if xml_files else False

            if has_real_xml:
                # 标准 XML 路径：取目标文件
                target_xml = None
                for xf in xml_files:
                    if xf["path"] == task.target_file:
                        target_xml = xf
                        break
                if not target_xml:
                    target_xml = xml_files[0]

                action = target_xml["action"]
                draft = target_xml["content"]
            else:
                # Editor 模式返回的已编辑代码（无 XML 标签）
                # 直接作为 rewrite 处理，跳过 CodePatcher 的 SEARCH/REPLACE 解析
                action = "rewrite"
                # 清洗可能的 markdown 代码块
                if xml_files and xml_files[0]["content"]:
                    draft = xml_files[0]["content"]
                else:
                    draft = code_output.strip()

            # 提交草稿到 Blackboard
            self.blackboard.submit_draft(task.task_id, draft, action)

            # (2) CodePatcher 缝合
            try:
                vfs_code = self.vfs.read_truth(task.target_file) if self.vfs else None
                merged = self.patcher.patch(vfs_code, draft, action)
                task.log_action(f"CodePatcher 缝合成功 (action={action})")
            except PatchFailedError as e:
                # 缝合失败：原因写入 error_logs，不唤醒 Reviewer
                task.log_error(f"CodePatcher 缝合失败: {e.reason}")
                self.blackboard.update_task_status(task.task_id, TaskStatus.PATCH_FAILED)
                self.blackboard.increment_retry(task.task_id)
                feedback = f"代码缝合失败: {e.reason}\n请检查你的 SEARCH 块是否与原文件一致。"
                logger.warning(f"⚠️ [{task.task_id}] 缝合失败，省下 Reviewer Token")
                continue

            # (3) 写入 Sandbox + 唤醒 Reviewer
            self.blackboard.update_task_status(task.task_id, TaskStatus.PENDING_REVIEW)

            # 写入新 VfsUtils 沙盒
            if self.vfs:
                self.vfs.write_to_sandbox({task.target_file: merged})

            # 唤醒 Reviewer
            self.blackboard.update_task_status(task.task_id, TaskStatus.REVIEWING)
            is_pass, reviewer_feedback = self._invoke_reviewer(task, merged)

            # 记录 TDD 轮次事件
            self._record_tdd_event(task, merged, is_pass, reviewer_feedback)

            if is_pass:
                # PASSED → commit 到真理区 → DONE
                self.blackboard.update_task_status(task.task_id, TaskStatus.PASSED)

                if self.vfs:
                    self.vfs.commit_to_truth(task.target_file, merged)

                # 更新文件树
                if self.vfs:
                    upsert_file_tree(self.project_id, list(self.vfs.list_truth_files().keys()))

                # 轨迹表
                recalled_ids = task.recalled_memory_ids
                insert_trajectory(
                    project_id=self.project_id, task_id=task.task_id,
                    attempt_round=task.retry_count, error_summary=None,
                    failed_code=None, recalled_memory_ids=recalled_ids,
                )
                finalize_trajectory(self.project_id, task.task_id, merged)

                self.blackboard.clear_draft(task.task_id)
                self.blackboard.update_task_status(task.task_id, TaskStatus.DONE)

                logger.info(f"🎉 [{task.task_id}] 审查通过，已 commit 到真理区")
                return
            else:
                # REJECTED → 回到 CODING
                self.blackboard.update_task_status(task.task_id, TaskStatus.REJECTED)

                # 轨迹表：记录失败
                insert_trajectory(
                    project_id=self.project_id, task_id=task.task_id,
                    attempt_round=task.retry_count,
                    error_summary=reviewer_feedback[:2000],
                    failed_code=merged,
                    recalled_memory_ids=task.recalled_memory_ids,
                )

                task.log_error(f"Reviewer 驳回: {reviewer_feedback[:200]}")
                self.blackboard.increment_retry(task.task_id)
                feedback = reviewer_feedback
                logger.warning(f"🔨 [{task.task_id}] 审查未通过 "
                               f"(retry {task.retry_count}/{MAX_RETRIES})")

    # ============================================================
    # Agent 调用
    # ============================================================

    def _invoke_coder(self, task: TaskItem, feedback: str = None) -> str:
        """唤醒 Coder，通过 Observer 预取上下文注入 task_meta"""
        coder = self._get_coder()

        # (1) 现有代码（提前获取，供精准依赖分析用）
        existing_code = ""
        if self.vfs:
            existing_code = self.vfs.read_truth(task.target_file) or ""
        if not existing_code and task.code_draft:
            existing_code = task.code_draft

        # (2) Observer 预取：项目文件树 + 精准依赖骨架
        observer_tree = ""
        observer_context = ""
        try:
            from tools.observer import Observer
            obs = Observer(self.vfs.truth_dir if self.vfs else ".")

            # 项目树
            observer_tree = obs.get_tree()

            # 精准依赖分析（传递闭包 + AST import + 兜底）
            dep_files = self._resolve_smart_deps(task, existing_code)
            if dep_files:
                context_parts = []
                for dep_path in dep_files:
                    skeleton = obs.get_skeleton(dep_path)
                    if skeleton and "Error" not in skeleton:
                        context_parts.append(f"--- [依赖文件骨架: {dep_path}] ---\n{skeleton}\n")
                    else:
                        content = obs.read_file(dep_path)
                        if content and "Error" not in content:
                            preview = content[:800] + "\n...[省略]" if len(content) > 800 else content
                            context_parts.append(f"--- [依赖文件: {dep_path}] ---\n{preview}\n")
                observer_context = "".join(context_parts)
                logger.info(f"📐 精准依赖注入: {dep_files}")
        except Exception as e:
            logger.warning(f"⚠️ Observer 预取异常: {e}")

        # (3) 构建 task_meta
        tasks_dict = [
            {"task_id": t.task_id, "target_file": t.target_file, "description": t.description}
            for t in self.blackboard.state.tasks
        ]
        task_meta = {
            "project_spec": self.blackboard.state.spec_text,
            "dependencies": task.dependencies,
            "all_tasks": tasks_dict,
            "observer_tree": observer_tree,
            "observer_context": observer_context,
            "existing_code": existing_code,
        }

        # (4) 调用 Coder
        try:
            result = coder.generate_code(
                target_file=task.target_file,
                description=task.description,
                feedback=feedback,
                task_meta=task_meta,
            )
            # 缓存 recalled IDs
            task.recalled_memory_ids = getattr(coder, '_last_recalled_ids', [])
            return result
        except Exception as e:
            err_msg = str(e)
            # 检测 interpreter shutdown
            if 'interpreter shutdown' in err_msg or 'Event loop is closed' in err_msg:
                logger.warning(f"🛑 检测到 Python 解释器关闭，Engine 将停止")
                self._shutdown = True
                return ""
            logger.error(f"❌ Coder 调用异常: {e}")
            task.log_error(f"Coder 异常: {e}")
            return ""

    def _resolve_smart_deps(self, task: TaskItem, existing_code: str = "") -> list:
        """
        三级精准依赖解析（零 LLM 成本）：
        L1: 传递闭包 — 递归展开 task.dependencies
        L2: AST import — 解析 existing_code 的 import 语句
        L3: 兜底全量 — 真理区所有源码文件（上限 6 个）
        """
        target = task.target_file
        truth_dir = self.vfs.truth_dir if self.vfs else None

        # L1: 传递闭包
        dep_files = self._resolve_transitive_deps(task)

        # L2: AST import 分析（修复模式有 existing_code）
        if existing_code and truth_dir:
            import_deps = self._resolve_imports(existing_code, truth_dir, target)
            dep_files = list(set(dep_files + import_deps))

        # L3: 兜底
        if not dep_files and truth_dir:
            dep_files = self._get_all_truth_files(truth_dir, exclude=target)

        return dep_files

    def _resolve_transitive_deps(self, task: TaskItem) -> list:
        """L1: 递归展开 task.dependencies → 传递闭包所有上游文件"""
        id_to_task = {t.task_id: t for t in self.blackboard.state.tasks}
        visited = set()

        def _walk(deps):
            for dep_id in deps:
                if dep_id in visited:
                    continue
                visited.add(dep_id)
                dep_task = id_to_task.get(dep_id)
                if dep_task:
                    _walk(dep_task.dependencies)

        _walk(task.dependencies)
        return [id_to_task[d].target_file for d in visited if d in id_to_task]

    def _resolve_imports(self, code: str, truth_dir: str, exclude: str = "") -> list:
        """L2: 从代码的 import 语句精准定位依赖文件"""
        import ast as ast_module
        try:
            tree = ast_module.parse(code)
        except SyntaxError:
            return []

        needed = []
        for node in ast_module.walk(tree):
            module = None
            if isinstance(node, ast_module.ImportFrom) and node.module:
                module = node.module
            elif isinstance(node, ast_module.Import):
                for alias in node.names:
                    module = alias.name

            if not module:
                continue

            # module "models" → 尝试 "models.py", "src/models.py" 等
            candidates = [
                module.replace(".", "/") + ".py",
                "src/" + module.replace(".", "/") + ".py",
            ]
            for c in candidates:
                if c != exclude and os.path.isfile(os.path.join(truth_dir, c)):
                    needed.append(c)
                    break

        return needed

    @staticmethod
    def _get_all_truth_files(truth_dir: str, exclude: str = "") -> list:
        """L3: 兜底 — 获取真理区所有源码文件（上限 6 个）"""
        SKIP = {'.git', '__pycache__', 'node_modules', '.venv'}
        EXTS = {'.py', '.js', '.jsx', '.ts', '.tsx', '.html', '.css'}
        result = []
        for root, dirs, files in os.walk(truth_dir):
            dirs[:] = [d for d in dirs if d not in SKIP]
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in EXTS:
                    rel = os.path.relpath(os.path.join(root, f), truth_dir).replace("\\", "/")
                    if rel != exclude:
                        result.append(rel)
        return result[:6]

    def _invoke_reviewer(self, task: TaskItem, merged_code: str) -> Tuple[bool, str]:
        """唤醒 Reviewer，传入已缝合的代码和 sandbox 目录"""
        reviewer = self._get_reviewer()
        sandbox_dir = self.vfs.sandbox_dir if self.vfs else None
        
        # 从规划书中提取 module_interfaces 契约
        module_interfaces = None
        try:
            spec = self.blackboard.state.project_spec
            if isinstance(spec, dict):
                module_interfaces = spec.get("module_interfaces")
            elif isinstance(spec, str):
                import json
                spec_dict = json.loads(spec)
                module_interfaces = spec_dict.get("module_interfaces")
        except Exception:
            pass
        
        try:
            return reviewer.evaluate_draft(task.target_file, task.description,
                                           code_content=merged_code,
                                           sandbox_dir=sandbox_dir,
                                           module_interfaces=module_interfaces)
        except Exception as e:
            err_msg = str(e)
            if 'interpreter shutdown' in err_msg or 'Event loop is closed' in err_msg:
                logger.warning(f"🛑 检测到 Python 解释器关闭，Engine 将停止")
                self._shutdown = True
                return False, "系统正在关闭"
            logger.error(f"❌ Reviewer 调用异常: {e}")
            task.log_error(f"Reviewer 异常: {e}")
            return False, f"Reviewer 执行异常: {e}"

    # ============================================================
    # Phase 3: 结算
    # ============================================================

    def _phase_settlement(self, user_requirement: str, success: bool):
        """后台异步结算：Synthesizer + Auditor + AMC"""
        logger.info("🧠 Phase 3: 后台异步结算...")
        global_broadcaster.emit_sync("System", "info",
            "🧠 正在执行经验提炼与 AMC 结算...")

        project_id = self.project_id
        bb_state = self.blackboard.state
        vfs_ref = self.vfs  # 捕获引用给后台线程用

        def _bg_settlement():
            try:
                from agents.synthesizer import SynthesizerAgent
                from agents.auditor import AuditorAgent

                synthesizer = SynthesizerAgent(project_id=project_id)

                # 构建里程碑 — 从真理区读取最终代码
                milestones_list = []
                for task in bb_state.tasks:
                    # 从真理区读取最终代码
                    final_code = ""
                    if vfs_ref:
                        final_code = vfs_ref.read_truth(task.target_file) or ""

                    milestone = {
                        "a": "",
                        "b": "\n".join(task.error_logs) if task.error_logs else "",
                        "c": final_code,
                    }
                    milestones_list.append({
                        "task": {"task_id": task.task_id, "target_file": task.target_file},
                        "milestones": milestone,
                        "success": task.status == TaskStatus.DONE,
                    })

                # Synthesizer
                plan_dict = bb_state.project_spec or {}
                for item in milestones_list:
                    tf = item["task"].get("target_file", "")
                    if item["success"]:
                        synthesizer.synthesize_success(
                            item["milestones"], user_requirement, plan_dict,
                            target_file=tf)
                    elif item["task"].get("task_id"):
                        synthesizer.synthesize_failure(
                            item["milestones"], user_requirement, plan_dict,
                            target_file=tf)
                logger.info("✨ [后台] Synthesizer 知识提炼完毕")

                # Auditor
                all_used_ids, all_ignored_ids = set(), set()
                auditor = AuditorAgent()
                from core.database import ScopedSession, Memory

                for item in milestones_list:
                    if not item["success"]:
                        continue
                    tid = item["task"]["task_id"]
                    task_memory_ids = get_recalled_memory_union(project_id, tid)
                    if not task_memory_ids:
                        continue

                    task_final_code = item["milestones"].get("c", "")
                    if not task_final_code:
                        continue

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

                    audit_result = auditor.audit(task_final_code, memories_to_audit)
                    for r in audit_result.get("results", []):
                        mid = r.get("memory_id", -1)
                        if mid > 0:
                            (all_used_ids if r.get("adopted") else all_ignored_ids).add(mid)

                if all_used_ids or all_ignored_ids:
                    settle_memory_scores(all_used_ids, all_ignored_ids, get_global_round())
                    logger.info(f"✨ [后台] AMC 结算完成: 功臣{len(all_used_ids)} 陪跑{len(all_ignored_ids)}")
                tick_global_round()

            except Exception as e:
                logger.error(f"❌ [后台] 结算异常: {e}")
                import traceback
                traceback.print_exc()

        threading.Thread(target=_bg_settlement, daemon=True).start()

    # ============================================================
    # 辅助方法
    # ============================================================

    def _resolve_output_dir(self, out_dir: str = None) -> str:
        """计算项目输出目录 + 动态重命名"""
        project_name = self.blackboard.state.project_name or "Unnamed"

        # 动态重命名逻辑
        if "新建项目" in self.project_id or "default_project" == self.project_id:
            parts = self.project_id.split("_", 2)
            timestamp = f"{parts[0]}_{parts[1]}" if len(parts) >= 2 else time.strftime("%Y%m%d_%H%M%S")
            safe_name = re.sub(r'[^\w\-\u4e00-\u9fa5]', '_', project_name)
            new_id = f"{timestamp}_{safe_name}"

            base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "projects"))
            old_dir = os.path.join(base_dir, self.project_id)
            new_dir = os.path.join(base_dir, new_id)

            if os.path.exists(old_dir) and old_dir != new_dir:
                try:
                    os.rename(old_dir, new_dir)
                    old_id = self.project_id
                    # 更新 project_id
                    self.blackboard.state.project_id = new_id
                    rename_project_events(old_id, new_id)
                    rename_project_meta(old_id, new_id, safe_name)
                    # 清理旧 Checkpoint（否则旧 "新建项目" key 永不删除）
                    from core.database import delete_checkpoint
                    delete_checkpoint(old_id)
                    global_broadcaster.emit_sync("System", "project_renamed",
                        f"项目已重命名: {safe_name}",
                        {"old_id": old_id, "new_id": new_id})
                except Exception as e:
                    logger.error(f"重命名失败: {e}")

        if out_dir:
            return os.path.abspath(out_dir)

        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "projects"))
        return os.path.join(base_dir, self.blackboard.state.project_id)

    def _warmup_sandbox(self, project_spec: dict = None):
        """预热 Sandbox（安装依赖）"""
        spec = project_spec or self.blackboard.state.project_spec
        tech_stacks = spec.get("tech_stack", []) if spec else []
        if tech_stacks:
            from tools.sandbox import sandbox_env
            pid = self.blackboard.state.project_id
            def _bg():
                sandbox_env.warm_up(pid, tech_stacks)
            threading.Thread(target=_bg, daemon=True).start()

    def _archive_old_trajectories(self):
        """软删除旧轨迹记录"""
        try:
            from core.database import TaskTrajectory, ScopedSession
            session = ScopedSession()
            try:
                stale = session.query(TaskTrajectory).filter(
                    TaskTrajectory.project_id == self.project_id,
                    TaskTrajectory.is_synthesized == False,
                ).update({"is_synthesized": True})
                session.commit()
                if stale:
                    logger.info(f"🧹 旧轨迹已归档: {stale} 条")
            except Exception:
                session.rollback()
            finally:
                ScopedSession.remove()
        except Exception:
            pass

    def _record_tdd_event(self, task: TaskItem, code: str,
                          is_pass: bool, feedback: str):
        """记录 TDD 轮次事件"""
        verdict = "pass" if is_pass else "fail"
        if is_pass:
            content = (f"[PASS] 任务 {task.task_id} | 文件: {task.target_file} | "
                       f"重试: {task.retry_count} | 审查通过")
        else:
            content = (f"[FAIL] 任务 {task.task_id} | 文件: {task.target_file} | "
                       f"重试: {task.retry_count}\n"
                       f"--- 代码片段 ---\n{code[:1500]}\n"
                       f"--- 审查结果 ---\n{feedback[:500]}")

        append_event("tdd", f"round_{verdict}", content,
                     project_id=self.project_id,
                     metadata={"task_id": task.task_id,
                               "target_file": task.target_file,
                               "retry": task.retry_count,
                               "verdict": verdict})
