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
from typing import Tuple, Optional, List, Dict, Any

from core.project_observer import ProjectObserver
from core.blackboard import Blackboard, BlackboardState, TaskStatus, ProjectStatus
from core.code_patcher import CodePatcher
from core.task_dag_builder import TaskDagBuilder, TaskDagBuildError
from core.project_scanner import scan_existing_project
from core.vfs_utils import VfsUtils
from core.ws_broadcaster import global_broadcaster
from core.database import (
    append_event, rename_project_events,
    create_project_meta, update_project_status, rename_project_meta,
)

logger = logging.getLogger("AstreaEngine")

MAX_RETRIES = int(os.getenv("MAX_RETRIES", 5))
MAX_RETRIES_PHASE = int(os.getenv("MAX_RETRIES_PHASE", 2))  # v4.0: Phase 模式下的 retry 上限

# 单一职责阈值（超过此数量的文件建议骨架先行）
_SRP_ENDPOINT_THRESHOLD = 5   # 一个文件中 API 端点数 ≥ 此值 → 复杂
_SRP_MODEL_THRESHOLD = 3      # 一个文件中数据模型数 ≥ 此值 → 复杂




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
        self._abort_requested = False  # v3.0: 一键中止标志
        self._pre_execution_git_head: Optional[str] = None  # v3.0: 执行前 Git HEAD（用于回滚）
        self._pending_project_rename: Optional[tuple[str, str, str]] = None

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

    def _next_round_number(self) -> int:
        """从 Git 历史中扫描已有的最大 Round 编号，返回 N+1。无历史则返回 1。"""
        import subprocess
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "projects", self.project_id))
        git_dir = os.path.join(base_dir, ".git")
        if not os.path.isdir(git_dir):
            return 1
        try:
            result = subprocess.run(
                ["git", "log", "--max-count=50", "--format=%s"],
                cwd=base_dir, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
            )
            if result.returncode != 0 or not result.stdout:
                return 1
            max_round = 0
            for line in result.stdout.strip().split('\n'):
                m = re.search(r"\[Round (\d+)\]", line)
                if m:
                    max_round = max(max_round, int(m.group(1)))
            return max_round + 1
        except Exception:
            return 1

    # ============================================================
    # 主入口
    # ============================================================

    def run(self, user_requirement: str, out_dir: str = None, mode: str = "auto") -> Tuple[bool, str]:
        """
        主入口：用户需求 → 完整项目

        Args:
            user_requirement: 用户需求文本
            out_dir: 可选指定输出目录
            mode: "auto" | "create" | "patch" | "extend" | "continue" | "rollback"

        Returns:
            (success, final_dir)
        """
        # 生成本次执行的全局批次/轮次标记 (递增数字 Round ID)
        self.session_id = str(self._next_round_number())
        logger.info(f"🚀 AstreaEngine 启动: {self.project_id} (mode={mode}, Round={self.session_id})")

        # v3.0: 快照当前 Git HEAD（用于 abort 回滚）
        self._pre_execution_git_head = self._get_current_git_head()
        self._abort_requested = False
        logger.info(f"📸 执行前 Git HEAD: {self._pre_execution_git_head or 'N/A'}")

        # 记录用户需求
        self.blackboard.set_user_requirement(user_requirement)
        append_event("user", "prompt", user_requirement, project_id=self.project_id)
        create_project_meta(self.project_id)
        global_broadcaster.emit_sync("System", "start_project", f"AstreaEngine 启动 (mode={mode}, Round={self.session_id})...")

        # mode 决策
        if mode == "auto":
            if self._is_existing_project():
                mode = "patch"
                logger.info("🔄 auto → patch（检测到已有项目）")
            else:
                mode = "create"
                logger.info("🆕 auto → create（新项目）")
        elif mode == "modify":
            # v3.0 透明化路由：modify 统一映射到 patch，Manager 自动区分改/加
            mode = "patch"
            logger.info("🔄 modify → patch（透明化路由）")

        try:
            if mode == "create":
                return self._run_create_mode(user_requirement, out_dir)
            elif mode == "patch":
                return self._run_patch_mode(user_requirement)
            elif mode == "extend":
                return self._run_extend_mode(user_requirement)
            elif mode == "continue":
                return self._run_continue_mode(user_requirement)
            elif mode == "rollback":
                return self._run_rollback_mode(user_requirement)
            else:
                logger.warning(f"⚠️ 未知 mode '{mode}'，降级为 auto")
                if self._is_existing_project():
                    return self._run_patch_mode(user_requirement)
                return self._run_create_mode(user_requirement, out_dir)

        except Exception as e:
            logger.error(f"❌ AstreaEngine 异常: {e}")
            import traceback
            traceback.print_exc()
            self.blackboard.set_project_status(ProjectStatus.FAILED)
            self.blackboard.record_failure_context("engine_exception", str(e))
            self._persist_blackboard_artifacts(
                self._resolve_artifact_dir(out_dir),
                failed=True,
            )
            update_project_status(self.project_id, "failed")
            return False, self._resolve_artifact_dir(out_dir)

    def _run_create_mode(self, user_requirement: str, out_dir: str = None) -> Tuple[bool, str]:
        """Create 模式：全量新建项目"""
        logger.info("🆕 Create Mode 启动")
        # v4.0: Phase 模式标志（由 server.py 在 run() 前注入，此处保留已有值）
        if not hasattr(self, '_phase_mode'):
            self._phase_mode = False

        # Phase 1: 规划（含重命名 + sandbox 预热，与 plan_tasks 并行）
        self._phase_planning(user_requirement, out_dir=out_dir)

        # 输出目录已在 _phase_planning 中设置
        final_dir = self.blackboard.state.out_dir

        if self.blackboard.state.failure_context.get("reason") in {"spec_parse_failed", "spec_contract_not_closed"}:
            logger.error("💥 规划阶段阻断：Spec 合同未闭环")
            self.blackboard.set_project_status(ProjectStatus.FAILED)
            self._persist_blackboard_artifacts(
                self._resolve_artifact_dir(final_dir or out_dir),
                failed=True,
            )
            update_project_status(self.project_id, "planning_blocked")
            global_broadcaster.emit_sync(
                "System", "error",
                "💥 规划失败：Spec 合同未闭环，未进入执行阶段"
            )
            self._finalize_project_rename()
            return False, final_dir or ""

        # 防御：规划阶段未产出任何任务 → 直接失败（通常是网络异常）
        if not self.blackboard.state.tasks:
            logger.error("💥 规划阶段未生成任何任务，项目失败")
            self.blackboard.set_project_status(ProjectStatus.FAILED)
            self.blackboard.record_failure_context("planning_failed", "规划阶段未生成任何任务")
            self._persist_blackboard_artifacts(
                self._resolve_artifact_dir(final_dir or out_dir),
                failed=True,
            )
            update_project_status(self.project_id, "failed")
            global_broadcaster.emit_sync("System", "error", "💥 规划失败：未生成任何任务（可能是网络异常）")
            self._finalize_project_rename()
            return False, out_dir or ""

        # Phase 2: 执行
        self.blackboard.set_project_status(ProjectStatus.EXECUTING)
        success = self._phase_execution()

        # Phase 2.5: 集成测试（v4.1: Phase 感知）
        delivered_with_warnings = False
        from core.integration_manager import IntegrationManager
        tester = IntegrationManager(self.blackboard, self.vfs, self.project_id)
        has_phases = getattr(self, '_phase_mode', False)
        is_final = getattr(self, '_is_final_phase', False)

        if success and tester.needs_integration_test(
            phase_mode=has_phases, is_final_phase=is_final
        ):
            integration_ok = tester.run_integration_test()
            if not integration_ok:
                if not has_phases:
                    replan_ok = tester.retry_with_replan()
                    if replan_ok:
                        success = self._phase_execution()
                        if success:
                            integration_ok = tester.run_integration_test()
                else:
                    logger.info("📦 Phase 模式跳过 replan，将结果回传给 PM")
                if not integration_ok:
                    logger.warning("⚠️ 集成测试未完全通过，降级为警告交付")
                    global_broadcaster.emit_sync("System", "integration_warning", "⚠️ 集成测试未完全通过，降级为警告交付")
                    delivered_with_warnings = True
                    success = True
        elif success and has_phases and not is_final:
            # v4.1: Phase 中间阶段 — 只做启动验证
            startup_ok = tester.run_startup_check()
            if not startup_ok:
                # v4.2: 自修复闭环 — 从 traceback 定位 guilty 文件 → 重新执行
                startup_ok = self._try_startup_self_repair(tester)
            if not startup_ok:
                logger.warning("⚠️ 启动验证修复后仍失败，降级为警告交付")
                global_broadcaster.emit_sync("System", "integration_warning", "⚠️ 启动验证失败")
                delivered_with_warnings = True

        # Phase 3: 结算
        if success:
            self.blackboard.set_project_status(ProjectStatus.COMPLETED)
            update_project_status(self.project_id, "success")
            logger.info(f"✨ 项目交付完成: {final_dir}")
            global_broadcaster.emit_sync("System", "success",
                f"✨ 项目完美生成！{final_dir}", {"final_path": final_dir})

            # Git auto-commit（不阻塞交付）
            try:
                from tools.git_ops import git_commit
                ledger_count = len(self.blackboard.state.completed_tasks)
                git_commit(final_dir, f"ASTrea: 项目交付完成 ({self.project_id}) [Ledger: {ledger_count} tasks]")
            except Exception as e:
                logger.warning(f"⚠️ Git auto-commit 失败（不影响交付）: {e}")

            self._persist_blackboard_artifacts(final_dir)
            if delivered_with_warnings:
                self.blackboard.set_project_status(ProjectStatus.DELIVERED_WITH_WARNINGS)
                self.blackboard.record_failure_context(
                    "integration_warning",
                    "集成测试未完全通过，降级为警告交付",
                    extra_context=getattr(tester, "_last_failure_context", {}) or {},
                )
                update_project_status(self.project_id, "warning")
                global_broadcaster.emit_sync("System", "integration_warning",
                    f"⚠️ 项目已交付，但集成测试未完全通过：{final_dir}", {"final_path": final_dir})
                self._persist_blackboard_artifacts(final_dir)
        else:
            self.blackboard.set_project_status(ProjectStatus.FAILED)
            self.blackboard.record_failure_context("execution_failed", "项目存在熔断任务")
            self._persist_blackboard_artifacts(final_dir, failed=True)
            update_project_status(self.project_id, "failed")
            global_broadcaster.emit_sync("System", "error", "💥 项目存在熔断任务")

        # 后台异步结算
        self._phase_settlement(user_requirement, success)

        # 清理 checkpoint
        self.blackboard.delete_checkpoint()
        self.vfs.clean_sandbox()
        self._finalize_project_rename()

        return success, self._resolve_artifact_dir(final_dir)

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
        engine.session_id = str(engine._next_round_number())
        logger.info(f"🔄 AstreaEngine 从 Checkpoint 恢复: {project_id} (Round={engine.session_id})")
        return engine


    # ============================================================
    # v3.0: 一键中止 + 自动回滚
    # ============================================================

    def _get_current_git_head(self) -> Optional[str]:
        """获取当前项目的 Git HEAD commit hash。无 git 目录时返回 None。"""
        import subprocess
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "projects", self.project_id))
        git_dir = os.path.join(base_dir, ".git")
        if not os.path.isdir(git_dir):
            return None
        try:
            result = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=base_dir, capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                return result.stdout.strip()
        except Exception as e:
            logger.warning(f"获取 Git HEAD 失败: {e}")
        return None

    def abort_and_rollback(self) -> dict:
        """
        一键中止当前执行并回滚到执行前状态。

        步骤:
        1. 设置 _abort_requested 标志（task_runner 在检查点读取此标志）
        2. 设置 _shutdown 标志（触发优雅退出）
        3. 如果有 _pre_execution_git_head，执行 git reset --hard 回滚
        4. 恢复执行前的 Blackboard 快照

        Returns:
            {"success": bool, "message": str, "rolled_back_to": str|None}
        """
        import subprocess

        self._abort_requested = True
        self._shutdown = True
        logger.warning(f"⛔ 收到中止请求: project={self.project_id}")
        global_broadcaster.emit_sync("System", "abort", f"⛔ 用户中止了执行，正在回滚...")

        rolled_back_to = None
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "projects", self.project_id))

        if self._pre_execution_git_head:
            try:
                result = subprocess.run(
                    ["git", "reset", "--hard", self._pre_execution_git_head],
                    cwd=base_dir, capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0:
                    rolled_back_to = self._pre_execution_git_head[:8]
                    logger.info(f"✅ Git 回滚成功: HEAD → {rolled_back_to}")
                    global_broadcaster.emit_sync("System", "info", f"✅ 代码已回滚到执行前状态 ({rolled_back_to})")
                else:
                    logger.error(f"Git reset 失败: {result.stderr}")
                    global_broadcaster.emit_sync("System", "error", f"❌ Git 回滚失败: {result.stderr[:200]}")
            except Exception as e:
                logger.error(f"Git reset 异常: {e}")
                global_broadcaster.emit_sync("System", "error", f"❌ Git 回滚异常: {e}")
        else:
            logger.info("无 Git HEAD 记录，跳过 Git 回滚（可能是新项目首次执行）")

        # 清理 Engine 内部状态
        self._pre_execution_git_head = None

        return {
            "success": True,
            "message": f"已中止执行" + (f"，代码已回滚到 {rolled_back_to}" if rolled_back_to else ""),
            "rolled_back_to": rolled_back_to,
        }

    # ============================================================
    # Patch Mode: 微调快速通道
    # ============================================================

    def _is_existing_project(self) -> bool:
        """判断当前项目是否是已有项目（非新建）"""
        if "新建项目" in self.project_id or "new_project" in self.project_id or self.project_id == "default_project":
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
        delivered_with_warnings = False
        self.blackboard.set_project_status(ProjectStatus.PLANNING)

        # 直接使用已有目录
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "projects"))
        final_dir = os.path.join(base_dir, self.project_id)
        self.blackboard.set_out_dir(final_dir, project_name=self.project_id)
        self.vfs = VfsUtils(final_dir)

        # Manager 精简规划（只规划受影响文件，不生成 Spec）
        # Phase 2.7: 从 user_requirement 中提取 PM 影响分析，单独传给 Manager
        pm_analysis = ""
        if "【PM 影响分析" in user_requirement:
            parts = user_requirement.split("【PM 影响分析", 1)
            pm_analysis = parts[1]
            # 移除固定前缀（精确子串匹配，不用 lstrip 以避免字符集误剥）
            _pm_prefix = "（必须采纳，包含精确的修改位置和方向）】\n"
            if pm_analysis.startswith(_pm_prefix):
                pm_analysis = pm_analysis[len(_pm_prefix):]
            elif pm_analysis.startswith("（必须采纳，包含精确的修改位置和方向）】"):
                pm_analysis = pm_analysis[len("（必须采纳，包含精确的修改位置和方向）】"):].lstrip("\n")
            # user_requirement 保留原始用户需求部分
            user_req_clean = parts[0].replace("【用户需求】\n", "").strip()
        else:
            user_req_clean = user_requirement

        # ============================================================
        # v5.0: TechLead 前置调查 — 优先复用 PM 阶段的缓存
        # PM 在 _handle_patch 中已委托 TechLead 做过一次调查，
        # 通过 server.py 注入到 engine._pm_tech_lead_diagnosis。
        # 有缓存 → 直接复用；无缓存 → 走原有本地调查逻辑。
        # ============================================================
        tech_lead_diagnosis = getattr(self, '_pm_tech_lead_diagnosis', None)

        if tech_lead_diagnosis:
            # PM 阶段已完成 TechLead 调查，直接复用（不做置信度淘汰，避免二次浪费）
            confidence = tech_lead_diagnosis.get("confidence", 0.0)
            logger.info(
                "✅ [Patch Mode] 复用 PM 阶段的 TechLead 缓存 (confidence=%.2f): %s",
                confidence,
                tech_lead_diagnosis.get("root_cause", "")[:120],
            )
            global_broadcaster.emit_sync(
                "TechLead", "patch_investigate_cached",
                f"♻️ 复用 PM 阶段的 TechLead 调查结果 (confidence={confidence:.0%})"
            )
            self._pm_tech_lead_diagnosis = None  # 一次性消费
        else:
            # 无缓存 — 启动本地 TechLead 调查（仅在 PM 未做调查时发生）
            try:
                from agents.tech_lead import TechLeadAgent
                from core.techlead_scope import resolve_target_scope

                task_context = f"【用户修改请求】\n{user_req_clean}\n"
                if pm_analysis:
                    task_context += f"\n【TechLead 诊断（需验证）】\n{pm_analysis}\n"
                task_context += (
                    "\n请逐个读取可能受影响的文件，验证影响分析是否准确，"
                    "定位需要修改的具体行号和代码。"
                    "重点关注：函数签名是否匹配、模板变量是否存在、路由路径是否正确。"
                )

                # 定向范围
                engine_scope = resolve_target_scope(final_dir, user_req_clean)
                if not engine_scope.is_resolved():
                    engine_scope = None

                logger.info("🔍 [Patch Mode] TechLead 前置调查启动...")
                global_broadcaster.emit_sync(
                    "TechLead", "patch_investigate_start",
                    "🔍 TechLead 正在白盒调查修改范围..."
                )
                tech_lead = TechLeadAgent()
                tech_lead_diagnosis = tech_lead.investigate(
                    project_dir=final_dir,
                    task_context=task_context,
                    target_scope=engine_scope,
                )
                if tech_lead_diagnosis:
                    confidence = tech_lead_diagnosis.get("confidence", 0.0)
                    logger.info(
                        "✅ [Patch Mode] TechLead 调查完成 (confidence=%.2f): %s",
                        confidence,
                        tech_lead_diagnosis.get("root_cause", "")[:120],
                    )
                else:
                    logger.warning("⚠️ [Patch Mode] TechLead 调查未产出判定，降级为纯 PM 分析")
            except Exception as e:
                logger.warning(f"⚠️ [Patch Mode] TechLead 调查异常: {e}，降级为纯 PM 分析")

        manager = self._get_manager()
        plan = manager.plan_patch(
            user_req_clean,
            pm_analysis=pm_analysis,
            tech_lead_diagnosis=tech_lead_diagnosis,
        )
        if not (plan.get("tasks") or []) and tech_lead_diagnosis:
            existing_files = []
            if os.path.isdir(final_dir):
                ignore = {'.sandbox', '.git', '__pycache__', '.venv', 'node_modules', '.idea', '.astrea'}
                for root, dirs, files in os.walk(final_dir):
                    dirs[:] = [d for d in dirs if d not in ignore]
                    for name in files:
                        rel = os.path.relpath(os.path.join(root, name), final_dir).replace("\\", "/")
                        existing_files.append(rel)
            fallback_plan = manager._build_tech_lead_patch_plan(
                user_requirement=user_req_clean,
                tech_lead_diagnosis=tech_lead_diagnosis,
                base_dir=final_dir,
                existing_files=existing_files,
            )
            if fallback_plan:
                logger.warning("⚠️ [Patch Mode] Manager 返回空任务，已启用 TechLead 快车道兜底")
                plan = fallback_plan
        plan = self._finalize_plan_with_dag(plan, project_spec={}, mode="patch")

        self.blackboard.set_tasks(plan.get("tasks", []), dag_metadata=plan.get("dag"))

        # v5.0: 显式将 TechLead / PM 诊断作为 `tech_lead_feedback` 注入任务
        # 解决 Coder fallback 时 feedback=None 导致保守覆写原样输出的致命 Bug
        if tech_lead_diagnosis:
            tl_feedback = f"【TechLead 根因诊断】\n{tech_lead_diagnosis.get('root_cause', '')}\n\n【TechLead 修复指令】\n{tech_lead_diagnosis.get('fix_instruction', '')}"
            for task in self.blackboard.state.tasks:
                task.tech_lead_feedback = tl_feedback
        elif pm_analysis:
            for task in self.blackboard.state.tasks:
                task.tech_lead_feedback = f"【PM 需求约束】\n{pm_analysis}"

        # 设置轻量 spec（Coder fallback 时需要）
        self.blackboard.set_project_spec(
            spec={},
            spec_text=(
                f"[Patch Mode] {plan.get('architecture_summary', '微调修改')}\n"
                f"用户需求: {user_requirement}"
            ),
        )

        # 🔧 Patch Mode 关键补丁：从已有项目文件推断 tech_stack
        # 否则 playbook_loader 匹配不到任何 playbook → Coder 不遵守编码规范
        inferred_tech_stack = self._infer_tech_stack(final_dir)
        if inferred_tech_stack:
            if not self.blackboard.state.project_spec:
                self.blackboard.set_project_spec(
                    spec={"tech_stack": inferred_tech_stack},
                    spec_text=self.blackboard.state.spec_text or "",
                )
            else:
                self.blackboard.state.project_spec["tech_stack"] = inferred_tech_stack
                self.blackboard._touch()
            logger.info(f"🔍 [Patch Mode] 推断 tech_stack: {inferred_tech_stack}")

        # 记录事件
        append_event("manager", "patch_plan", json.dumps(plan, ensure_ascii=False),
                     project_id=self.project_id)

        if not self.blackboard.state.tasks:
            logger.error("💥 [Patch Mode] 未规划任何任务")
            self.blackboard.set_project_status(ProjectStatus.FAILED)
            global_broadcaster.emit_sync("System", "error", "💥 Patch Mode 规划失败")
            self.blackboard.record_failure_context("patch_planning_failed", "Patch Mode 未规划任何任务")
            self._persist_blackboard_artifacts(
                self._resolve_artifact_dir(final_dir),
                failed=True,
            )
            return False, final_dir

        logger.info(f"⚡ [Patch Mode] {len(self.blackboard.state.tasks)} 个文件需修改")

        # 复用 sandbox（已有 venv，无需重新安装依赖）
        # 不调用 _warmup_sandbox

        # Phase 2: 执行（复用现有 _phase_execution，
        # Coder 会自动走 fix_with_editor 因为 existing_code 不为空）
        self.blackboard.set_project_status(ProjectStatus.EXECUTING)
        success = self._phase_execution()

        if success:
            mini_qa_ok, mini_qa_warning = self._run_patch_mini_qa_gate(
                user_requirement=user_req_clean,
                tech_lead_diagnosis=tech_lead_diagnosis,
                final_dir=final_dir,
                plan=plan,
            )
            delivered_with_warnings = delivered_with_warnings or mini_qa_warning
            success = success and mini_qa_ok

        # Phase 2.5: 集成测试（v4.1: Phase 感知）
        from core.integration_manager import IntegrationManager
        tester = IntegrationManager(self.blackboard, self.vfs, self.project_id)
        has_phases = getattr(self, '_phase_mode', False)
        is_final = getattr(self, '_is_final_phase', False)

        if success and tester.needs_integration_test(
            phase_mode=has_phases, is_final_phase=is_final
        ):
            # v4.4: Patch 模式按需测试 — 从修改的文件推断受影响端点
            patch_focus = self._infer_focus_endpoints(plan, final_dir)
            integration_ok = tester.run_integration_test(focus_endpoints=patch_focus)
            if not integration_ok:
                if not has_phases:
                    replan_ok = tester.retry_with_replan()
                    if replan_ok:
                        success = self._phase_execution()
                        if success:
                            integration_ok = tester.run_integration_test(focus_endpoints=patch_focus)
                else:
                    logger.info("📦 Phase 模式跳过 replan")
                if not integration_ok:
                    logger.warning("⚠️ [Patch] 集成测试未通过，降级为警告交付")
                    global_broadcaster.emit_sync("System", "integration_warning", "⚠️ 集成测试未完全通过，降级为警告交付")
                    delivered_with_warnings = True
                    success = True
        elif success and has_phases and not is_final:
            startup_ok = tester.run_startup_check()
            if not startup_ok:
                startup_ok = self._try_startup_self_repair(tester)
            if not startup_ok:
                delivered_with_warnings = True

        if success:
            self.blackboard.set_project_status(ProjectStatus.COMPLETED)
            update_project_status(self.project_id, "success")
            logger.info(f"✨ [Patch Mode] 修改完成: {final_dir}")
            global_broadcaster.emit_sync("System", "success",
                f"✨ Patch Mode 修改完成！{final_dir}", {"final_path": final_dir})

            # Git auto-commit（不阻塞交付）
            try:
                from tools.git_ops import git_commit
                git_commit(final_dir, f"ASTrea Patch: {user_requirement[:60]}")
            except Exception as e:
                logger.warning(f"⚠️ Git auto-commit 失败（不影响交付）: {e}")

            self._persist_blackboard_artifacts(final_dir)
            if delivered_with_warnings:
                self.blackboard.set_project_status(ProjectStatus.DELIVERED_WITH_WARNINGS)
                self.blackboard.record_failure_context(
                    "integration_warning",
                    "集成测试未完全通过，降级为警告交付",
                    extra_context=getattr(tester, "_last_failure_context", {}) or {},
                )
                update_project_status(self.project_id, "warning")
                global_broadcaster.emit_sync("System", "integration_warning",
                    f"⚠️ Patch Mode 已交付，但集成测试未完全通过：{final_dir}", {"final_path": final_dir})
                self._persist_blackboard_artifacts(final_dir)
        else:
            self.blackboard.set_project_status(ProjectStatus.FAILED)
            self.blackboard.record_failure_context("patch_execution_failed", "Patch Mode 存在熔断任务")
            self._persist_blackboard_artifacts(final_dir, failed=True)
            update_project_status(self.project_id, "failed")
            global_broadcaster.emit_sync("System", "error", "💥 Patch Mode 存在熔断任务")

        # 结算
        self._phase_settlement(user_requirement, success)

        # 清理
        self.blackboard.delete_checkpoint()
        if self.vfs:
            self.vfs.clean_sandbox()

        return success, final_dir

    def _run_patch_mini_qa_gate(self, user_requirement: str,
                                tech_lead_diagnosis: dict,
                                final_dir: str,
                                plan: dict) -> Tuple[bool, bool]:
        """Patch Mode 后置最小浏览器 QA。返回 (是否允许继续交付, 是否降级警告)。"""
        try:
            from core.patch_mini_qa import (
                build_patch_mini_qa_plan,
                choose_patch_mini_qa_repair_target,
                run_patch_mini_qa,
            )
        except Exception as e:
            logger.warning("⚠️ Patch Mini QA 模块不可用: %s", e)
            return True, True

        qa_plan = build_patch_mini_qa_plan(
            user_requirement=user_requirement,
            tech_lead_diagnosis=tech_lead_diagnosis,
            project_dir=final_dir,
        )
        if not qa_plan:
            return True, False

        global_broadcaster.emit_sync(
            "System", "patch_mini_qa",
            f"🧪 Patch Mini QA: 执行 {len(qa_plan)} 条局部交互验证"
        )
        logger.info("🧪 [Patch Mini QA] 计划: %s", qa_plan)

        changed_files = [
            str(t.get("target_file", "")).replace("\\", "/")
            for t in (plan.get("tasks") or [])
            if t.get("target_file")
        ]
        max_retries = int(os.getenv("PATCH_MINI_QA_RETRIES", "1"))

        for attempt in range(max_retries + 1):
            result = run_patch_mini_qa(
                project_dir=final_dir,
                qa_plan=qa_plan,
                project_id=self.project_id,
            )
            feedback = str(result.get("feedback", "Patch Mini QA 未返回反馈"))

            if result.get("passed"):
                logger.info("✅ [Patch Mini QA] 通过")
                global_broadcaster.emit_sync("System", "patch_mini_qa_pass", "✅ Patch Mini QA 通过")
                return True, False

            if result.get("env_failed"):
                logger.warning("⚠️ [Patch Mini QA] 环境失败，降级为警告: %s", feedback[:300])
                self.blackboard.record_failure_context(
                    "patch_mini_qa_env_failed",
                    feedback,
                    extra_context=result,
                )
                global_broadcaster.emit_sync(
                    "System", "integration_warning",
                    f"⚠️ Patch Mini QA 环境失败，降级为警告: {feedback[:120]}"
                )
                return True, True

            logger.warning("❌ [Patch Mini QA] 失败: %s", feedback[:300])
            global_broadcaster.emit_sync(
                "System", "patch_mini_qa_fail",
                f"❌ Patch Mini QA 未通过: {feedback[:160]}"
            )

            if attempt >= max_retries:
                self.blackboard.record_failure_context(
                    "patch_mini_qa_failed",
                    feedback,
                    extra_context=result,
                )
                return False, False

            target = choose_patch_mini_qa_repair_target(
                project_dir=final_dir,
                qa_plan=qa_plan,
                changed_files=changed_files,
            )
            # 优先使用 TechLead 的 guilty_file 作为修复目标
            tl_guilty = (tech_lead_diagnosis or {}).get("guilty_file", "")
            if tl_guilty and os.path.isfile(os.path.join(final_dir, tl_guilty)):
                target = tl_guilty
            if not target:
                self.blackboard.record_failure_context(
                    "patch_mini_qa_no_target",
                    feedback,
                    extra_context=result,
                )
                return False, False

            # 将 TechLead 的精确修复指令注入 Coder（而非只告诉它"QA 失败了"）
            tl_fix = (tech_lead_diagnosis or {}).get("fix_instruction", "")
            tl_root_cause = (tech_lead_diagnosis or {}).get("root_cause", "")
            fix_instruction = (
                "【Patch Mini QA 失败】\n"
                f"{feedback}\n\n"
                "【必须修复到通过的局部交互断言】\n"
                f"{json.dumps(qa_plan, ensure_ascii=False)}\n\n"
            )
            if tl_fix:
                fix_instruction += (
                    "【TechLead 精确修复指令（已验证，必须严格执行）】\n"
                    f"{tl_fix}\n\n"
                )
            if tl_root_cause:
                fix_instruction += (
                    "【TechLead 根因分析】\n"
                    f"{tl_root_cause}\n\n"
                )
            fix_instruction += "请只修改当前目标文件，确保浏览器中执行点击后目标元素真实可见。"

            self.blackboard.inject_targeted_fix_task(
                target_file=target,
                description=f"[PATCH_MINI_QA_FIX] 修复 Patch Mini QA 失败: {feedback[:180]}",
                fix_instruction=fix_instruction,
                source_task_id="patch_mini_qa",
            )
            changed_files.append(target)
            repair_success = self._phase_execution()
            if not repair_success:
                self.blackboard.record_failure_context(
                    "patch_mini_qa_repair_failed",
                    "Patch Mini QA 修复任务执行失败",
                    extra_context=result,
                )
                return False, False

        return False, False

    def _run_continue_mode(self, user_requirement: str) -> Tuple[bool, str]:
        """Continue 模式：基于上一轮 QA 失败上下文做定向继续修复。"""
        logger.info(f"🔁 Continue Mode 启动: {self.project_id}")
        global_broadcaster.emit_sync("System", "start_project", f"🔁 Continue Mode: {self.project_id}")

        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "projects"))
        final_dir = os.path.join(base_dir, self.project_id)
        self.vfs = VfsUtils(final_dir)

        loaded_state = BlackboardState.load_from_disk(final_dir)
        if loaded_state:
            loaded_state.project_id = self.project_id
            loaded_state.out_dir = final_dir
            self.blackboard._state = loaded_state
        else:
            self.blackboard.set_out_dir(final_dir, project_name=self.project_id)

        self.blackboard.set_user_requirement(user_requirement)
        failure_context = self.blackboard.state.failure_context or {}
        endpoint_results = failure_context.get("endpoint_results") or []
        failed_endpoints = [
            ep for ep in endpoint_results
            if isinstance(ep, dict) and not ep.get("ok")
        ]

        if not endpoint_results or not failed_endpoints:
            self.blackboard.set_project_status(ProjectStatus.FAILED)
            self.blackboard.record_failure_context(
                "continue_context_missing",
                "Continue Mode 缺少上一轮 QA 失败端点上下文，拒绝自动修复",
                extra_context={
                    "available_failure_context_keys": sorted(failure_context.keys()),
                    "endpoint_results": endpoint_results,
                },
            )
            self._persist_blackboard_artifacts(final_dir, failed=True)
            update_project_status(self.project_id, "failed")
            global_broadcaster.emit_sync(
                "System",
                "error",
                "Continue Mode 缺少上一轮 QA 失败端点上下文，已拒绝自动修复",
            )
            return False, final_dir

        # Phase 5.5: 注入烂账账本到 Manager
        open_issues_text = self.blackboard.get_open_issues_text()

        # ============================================================
        # v4.4: TechLead 前置调查 — ReAct 多步白盒排障
        # 替代 Manager 的单次 LLM 盲猜诊断，通过 read_file/grep_project
        # 主动读取代码、追踪导入链、定位根因，输出行级精确修复指令。
        # ============================================================
        tech_lead_diagnosis = None
        try:
            from agents.tech_lead import TechLeadAgent

            # 构建失败端点摘要（供 TechLead 调查使用）
            _failed_eps = [
                ep for ep in endpoint_results
                if isinstance(ep, dict) and not ep.get("ok")
            ]
            _passed_eps = [
                ep for ep in endpoint_results
                if isinstance(ep, dict) and ep.get("ok")
            ]
            _ep_summary = "\n".join(
                f"  ❌ {ep.get('method', '?')} {ep.get('url', '?')} -> "
                f"{ep.get('status_code', '?')} {ep.get('detail', '')}".strip()
                for ep in _failed_eps[:6]
            ) or "无具体失败端点"
            _passed_summary = "\n".join(
                f"  ✅ {ep.get('method', '?')} {ep.get('url', '?')} -> {ep.get('status_code', '?')}"
                for ep in _passed_eps[:8]
            )
            _feedback = str(
                failure_context.get("feedback")
                or failure_context.get("error_message")
                or ""
            )[:2000]

            # 从 failure_context 提取修复范围文件
            _repair_scope = failure_context.get("repair_scope") or []
            if isinstance(_repair_scope, str):
                _repair_scope = [_repair_scope]
            _repair_files = []
            for item in _repair_scope:
                if isinstance(item, str):
                    _repair_files.append(item)
                elif isinstance(item, dict):
                    for key in ("target_file", "file", "path"):
                        if item.get(key):
                            _repair_files.append(str(item[key]))

            task_context = (
                f"【QA 集成测试失败 — Continue Mode 修复前调查】\n\n"
                f"【失败端点】\n{_ep_summary}\n\n"
                f"【QA 反馈】\n{_feedback}\n\n"
            )
            if _passed_summary:
                task_context += f"【已通过端点 — 修复时不得破坏】\n{_passed_summary}\n\n"
            if _repair_files:
                task_context += f"【修复范围】\n{', '.join(_repair_files)}\n\n"
            if open_issues_text:
                task_context += f"【历史问题台账】\n{open_issues_text}\n\n"
            task_context += (
                "请逐个读取修复范围内的文件，追踪每个失败端点的请求链路，定位根因。\n"
                "重点关注：函数命名冲突（import 被同名定义覆盖）、数据库查询错误、模板变量不匹配。"
            )

            logger.info("🔍 [Continue Mode] TechLead 前置调查启动...")
            global_broadcaster.emit_sync(
                "TechLead", "continue_investigate_start",
                "🔍 TechLead 正在白盒调查 QA 失败根因..."
            )
            tech_lead = TechLeadAgent()
            tech_lead_diagnosis = tech_lead.investigate(
                project_dir=final_dir,
                task_context=task_context,
                max_steps=10,
            )
            if tech_lead_diagnosis:
                confidence = tech_lead_diagnosis.get("confidence", 0.0)
                logger.info(
                    "✅ [Continue Mode] TechLead 调查完成 (confidence=%.2f): %s",
                    confidence,
                    tech_lead_diagnosis.get("root_cause", "")[:120],
                )
                # 低置信度时降级（TechLead 调查不充分）
                if confidence < 0.3:
                    logger.warning("⚠️ [Continue Mode] TechLead 置信度过低 (%.2f)，降级为 Manager LLM 诊断", confidence)
                    tech_lead_diagnosis = None
            else:
                logger.warning("⚠️ [Continue Mode] TechLead 调查未产出判定，降级为 Manager LLM 诊断")
        except Exception as e:
            logger.warning(f"⚠️ [Continue Mode] TechLead 调查异常: {e}，降级为 Manager LLM 诊断")

        manager = self._get_manager()
        plan = manager.plan_continue(
            failure_context,
            open_issues_text=open_issues_text,
            tech_lead_diagnosis=tech_lead_diagnosis,
        )
        tasks = plan.get("tasks", []) or []
        if not tasks:
            self.blackboard.set_project_status(ProjectStatus.FAILED)
            self.blackboard.record_failure_context(
                "continue_planning_failed",
                "Continue Mode 未能从 repair_scope/failed_files 生成修复任务",
                extra_context=failure_context,
            )
            self._persist_blackboard_artifacts(final_dir, failed=True)
            update_project_status(self.project_id, "failed")
            global_broadcaster.emit_sync("System", "error", "Continue Mode 未生成任何修复任务")
            return False, final_dir

        group_id = f"continue:{self.session_id}"
        normalized_tasks = []
        for idx, task in enumerate(tasks, start=1):
            item = dict(task)
            target_file = item.get("target_file", "")
            item["task_id"] = f"continue_{self.session_id}_{idx}"
            item["group_id"] = group_id
            item["dependencies"] = []
            item["write_targets"] = item.get("write_targets") or ([target_file] if target_file else [])
            normalized_tasks.append(item)

        self.blackboard.append_tasks(
            normalized_tasks,
            group_id=group_id,
            dag_metadata={
                "mode": "continue",
                "node_count": len(normalized_tasks),
                "edge_count": 0,
                "source": "failure_context",
            },
        )

        project_spec = self.blackboard.state.project_spec or {}
        inferred_tech_stack = self._infer_tech_stack(final_dir)
        if inferred_tech_stack and not project_spec.get("tech_stack"):
            project_spec = dict(project_spec)
            project_spec["tech_stack"] = inferred_tech_stack
        self.blackboard.set_project_spec(
            spec=project_spec,
            spec_text=(
                f"{self.blackboard.state.spec_text or ''}\n\n"
                f"[Continue Mode]\n{plan.get('architecture_summary', '')}\n"
                f"用户要求: {user_requirement}\n"
                f"修复范围: {', '.join(t.get('target_file', '') for t in normalized_tasks)}"
            ).strip(),
            project_name=self.project_id,
        )
        append_event(
            "manager",
            "continue_plan",
            json.dumps(plan, ensure_ascii=False),
            project_id=self.project_id,
        )

        self.blackboard.set_project_status(ProjectStatus.EXECUTING)
        success = self._phase_execution(group_id=group_id)

        delivered_with_warnings = False
        tester = None
        if success:
            from core.integration_manager import IntegrationManager
            tester = IntegrationManager(self.blackboard, self.vfs, self.project_id)
            has_phases = getattr(self, '_phase_mode', False)
            is_final = getattr(self, '_is_final_phase', False)
            if tester.needs_integration_test(phase_mode=has_phases, is_final_phase=is_final):
                # v4.4: Continue 模式按需测试 — 只重测上一轮失败的端点
                focus_eps = [
                    f"{ep.get('method', 'GET')} {ep.get('url', '')}"
                    for ep in failed_endpoints
                    if isinstance(ep, dict) and ep.get("url")
                ] or None
                integration_ok = tester.run_integration_test(focus_endpoints=focus_eps)
                if not integration_ok:
                    delivered_with_warnings = True
                    success = True
                    global_broadcaster.emit_sync(
                        "System",
                        "integration_warning",
                        "⚠️ Continue Mode 修复已执行，但 QA 仍未完全通过",
                    )

        if success:
            if delivered_with_warnings:
                self.blackboard.set_project_status(ProjectStatus.DELIVERED_WITH_WARNINGS)
                self.blackboard.record_failure_context(
                    "integration_warning",
                    "Continue Mode 后集成测试仍未完全通过",
                    extra_context=getattr(tester, "_last_failure_context", {}) or {},
                )
                update_project_status(self.project_id, "warning")
                global_broadcaster.emit_sync(
                    "System",
                    "integration_warning",
                    f"⚠️ Continue Mode 已交付，但 QA 仍未完全通过：{final_dir}",
                    {"final_path": final_dir},
                )
            else:
                self.blackboard.set_project_status(ProjectStatus.COMPLETED)
                update_project_status(self.project_id, "success")
                global_broadcaster.emit_sync(
                    "System",
                    "success",
                    f"✅ Continue Mode 修复完成：{final_dir}",
                    {"final_path": final_dir},
                )

            try:
                from tools.git_ops import git_commit
                git_commit(final_dir, f"ASTrea Continue: {user_requirement[:60]}")
            except Exception as e:
                logger.warning(f"⚠️ Git auto-commit 失败（不影响交付）: {e}")
            self._persist_blackboard_artifacts(final_dir)
        else:
            self.blackboard.set_project_status(ProjectStatus.FAILED)
            self.blackboard.record_failure_context(
                "continue_execution_failed",
                "Continue Mode 存在熔断任务",
                extra_context=failure_context,
            )
            self._persist_blackboard_artifacts(final_dir, failed=True)
            update_project_status(self.project_id, "failed")
            global_broadcaster.emit_sync("System", "error", "Continue Mode 存在熔断任务")

        self._phase_settlement(user_requirement, success)
        self.blackboard.delete_checkpoint()
        if self.vfs:
            self.vfs.clean_sandbox()
        return success, final_dir

    def _normalize_extend_plan(self, plan: dict, existing_context: dict) -> dict:
        """对 Extend 规划做代码层归一化，强制落实 new_file / weld 约束。"""
        plan = dict(plan or {})
        existing_context = existing_context or {}
        existing_files = {
            str(path).replace("\\", "/").lstrip("/")
            for path in (existing_context.get("file_tree") or [])
            if path
        }
        route_blacklist = sorted({
            str(item.get("path", "")).strip()
            for item in (existing_context.get("existing_routes") or [])
            if isinstance(item, dict) and str(item.get("path", "")).strip()
        })

        extend_context = dict(plan.get("extend_context") or {})
        extend_context["route_blacklist"] = route_blacklist

        raw_tasks = plan.get("tasks") or []
        normalized_tasks: List[Dict[str, Any]] = []
        new_files: List[str] = []
        weld_targets: List[str] = []
        new_task_ids: List[str] = []
        seen_targets = set()

        for idx, task in enumerate(raw_tasks, start=1):
            if not isinstance(task, dict):
                continue
            target_file = str(task.get("target_file", "")).replace("\\", "/").lstrip("/")
            if not target_file or target_file in seen_targets:
                continue
            seen_targets.add(target_file)

            item = dict(task)
            item["task_id"] = str(item.get("task_id") or f"extend_{self.session_id}_{idx}")
            item["target_file"] = target_file
            item["dependencies"] = [str(dep) for dep in (item.get("dependencies") or []) if dep]

            if target_file in existing_files:
                item["task_type"] = "weld"
                item["draft_action"] = "modify"
                item["write_targets"] = [target_file]
                if target_file not in weld_targets:
                    weld_targets.append(target_file)
            else:
                item["task_type"] = "new_file"
                item.pop("draft_action", None)
                item["write_targets"] = [target_file]
                if target_file not in new_files:
                    new_files.append(target_file)
                new_task_ids.append(item["task_id"])

            normalized_tasks.append(item)

        if new_task_ids:
            for item in normalized_tasks:
                if item.get("task_type") != "weld":
                    continue
                deps: List[str] = []
                for dep in list(item.get("dependencies") or []) + new_task_ids:
                    if dep and dep not in deps:
                        deps.append(dep)
                item["dependencies"] = deps

        normalized_routes: List[Dict[str, str]] = []
        seen_routes = set()
        for route in extend_context.get("new_routes") or []:
            if not isinstance(route, dict):
                continue
            path = str(route.get("path", "")).strip()
            if not path:
                continue
            method = str(route.get("method", "GET") or "GET").upper()
            file_path = str(route.get("file", "") or "").replace("\\", "/").lstrip("/")
            route_key = (method, path, file_path)
            if route_key in seen_routes:
                continue
            seen_routes.add(route_key)
            normalized_routes.append({
                "method": method,
                "path": path,
                "file": file_path,
            })

        extend_context["new_files"] = new_files
        extend_context["weld_targets"] = weld_targets
        extend_context["new_routes"] = normalized_routes
        plan["extend_context"] = extend_context
        plan["tasks"] = normalized_tasks
        return plan

    def _find_extend_route_conflicts(self, plan: dict, existing_context: dict) -> List[str]:
        """检查 Extend 规划中的新路由是否与已有路由冲突。"""
        existing_paths = {
            str(item.get("path", "")).strip()
            for item in (existing_context.get("existing_routes") or [])
            if isinstance(item, dict) and str(item.get("path", "")).strip()
        }
        conflicts = {
            str(item.get("path", "")).strip()
            for item in (plan.get("extend_context", {}).get("new_routes") or [])
            if isinstance(item, dict) and str(item.get("path", "")).strip() in existing_paths
        }
        return sorted(conflicts)

    def _scan_realized_extend_route_conflicts(self, project_dir: str, plan: dict,
                                              existing_context: dict) -> List[str]:
        """执行后扫描真实新增文件路由，防止实现阶段偏离规划。"""
        existing_paths = {
            str(item.get("path", "")).strip()
            for item in (existing_context.get("existing_routes") or [])
            if isinstance(item, dict) and str(item.get("path", "")).strip()
        }
        if not existing_paths or not os.path.isdir(project_dir):
            return []

        from tools.observer import Observer

        observer = Observer(project_dir)
        candidate_files = [
            str(path).replace("\\", "/").lstrip("/")
            for path in (plan.get("extend_context", {}).get("new_files") or [])
            if path
        ]
        if not candidate_files:
            candidate_files = [
                str(task.get("target_file", "")).replace("\\", "/").lstrip("/")
                for task in (plan.get("tasks") or [])
                if task.get("task_type") == "new_file"
            ]

        realized_paths = set()
        for file_path in candidate_files:
            abs_path = os.path.join(project_dir, file_path)
            if not os.path.isfile(abs_path):
                continue
            try:
                routes = observer.extract_routes(file_path) or []
            except Exception:
                routes = []
            for route in routes:
                if isinstance(route, dict) and str(route.get("path", "")).strip():
                    realized_paths.add(str(route.get("path", "")).strip())

        return sorted(realized_paths & existing_paths)

    def _run_extend_mode(self, user_requirement: str) -> Tuple[bool, str]:
        """Extend 模式：在已有项目基础上新增完整模块。"""
        logger.info(f"🧩 Extend Mode 启动: {self.project_id}")
        global_broadcaster.emit_sync("System", "start_project", f"🧩 Extend Mode: {self.project_id}")

        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "projects"))
        final_dir = os.path.join(base_dir, self.project_id)
        if not os.path.isdir(final_dir):
            self.blackboard.set_project_status(ProjectStatus.FAILED)
            self.blackboard.record_failure_context(
                "extend_project_missing",
                "Extend Mode 只能用于已有项目，目标项目目录不存在",
            )
            self._persist_blackboard_artifacts(final_dir, failed=True)
            update_project_status(self.project_id, "failed")
            global_broadcaster.emit_sync("System", "error", "Extend Mode 只能用于已有项目")
            return False, final_dir

        self.vfs = VfsUtils(final_dir)
        loaded_state = BlackboardState.load_from_disk(final_dir)
        if loaded_state:
            loaded_state.project_id = self.project_id
            loaded_state.out_dir = final_dir
            self.blackboard._state = loaded_state
        else:
            self.blackboard.set_out_dir(final_dir, project_name=self.project_id)
        self.blackboard.state.project_id = self.project_id
        self.blackboard.state.out_dir = final_dir
        self.blackboard.set_user_requirement(user_requirement)
        self.blackboard.set_project_status(ProjectStatus.PLANNING)

        try:
            from tools.git_ops import git_commit

            if not git_commit(final_dir, f"Auto-backup before extend [Round {self.session_id}]"):
                raise RuntimeError("git auto-backup failed")
        except Exception as e:
            logger.error(f"❌ [Extend] Git 安全快照失败: {e}")
            self.blackboard.set_project_status(ProjectStatus.FAILED)
            self.blackboard.record_failure_context(
                "extend_backup_failed",
                "Extend Mode 执行前 Git 安全快照失败",
                extra_context={"error": str(e)},
            )
            self._persist_blackboard_artifacts(final_dir, failed=True)
            update_project_status(self.project_id, "failed")
            global_broadcaster.emit_sync("System", "error", "Extend Mode 启动失败：Git 安全快照失败")
            return False, final_dir

        existing_context = scan_existing_project(
            final_dir,
            blackboard_state=self.blackboard.state.model_dump(),
        )
        inferred_tech_stack = existing_context.get("tech_stack") or self._infer_tech_stack(final_dir)

        from core.playbook_loader import PlaybookLoader

        manager_playbook = PlaybookLoader().load_for_manager(inferred_tech_stack or [])
        manager = self._get_manager()

        # Phase 5.5: 注入烂账账本到 Extend 规划
        open_issues_text = self.blackboard.get_open_issues_text()

        plan = manager.plan_extend(
            user_requirement,
            existing_context,
            manager_playbook=manager_playbook,
            replan_feedback=None,
            open_issues_text=open_issues_text,
        )
        plan = self._normalize_extend_plan(plan, existing_context)

        route_conflicts = self._find_extend_route_conflicts(plan, existing_context)
        if route_conflicts:
            replan_feedback = {
                "reason": "route_conflict",
                "conflicts": route_conflicts,
                "route_blacklist": plan.get("extend_context", {}).get("route_blacklist", []),
            }
            plan = manager.plan_extend(
                user_requirement,
                existing_context,
                manager_playbook=manager_playbook,
                replan_feedback=replan_feedback,
                open_issues_text=open_issues_text,
            )
            plan = self._normalize_extend_plan(plan, existing_context)
            route_conflicts = self._find_extend_route_conflicts(plan, existing_context)

        tasks = plan.get("tasks") or []
        if route_conflicts:
            self.blackboard.set_project_status(ProjectStatus.FAILED)
            self.blackboard.record_failure_context(
                "extend_route_conflict",
                "新增模块路由与已有路由冲突，已拒绝执行",
                extra_context={
                    "conflicts": route_conflicts,
                    "existing_routes": existing_context.get("existing_routes", []),
                },
            )
            self._persist_blackboard_artifacts(final_dir, failed=True)
            update_project_status(self.project_id, "failed")
            global_broadcaster.emit_sync(
                "System",
                "error",
                f"Extend Mode 路由冲突：{', '.join(route_conflicts)}",
            )
            return False, final_dir

        if not tasks:
            self.blackboard.set_project_status(ProjectStatus.FAILED)
            self.blackboard.record_failure_context(
                "extend_planning_failed",
                "Extend Mode 未生成任何任务",
                extra_context={"existing_context": existing_context},
            )
            self._persist_blackboard_artifacts(final_dir, failed=True)
            update_project_status(self.project_id, "failed")
            global_broadcaster.emit_sync("System", "error", "Extend Mode 未生成任何任务")
            return False, final_dir

        current_spec = dict(self.blackboard.state.project_spec or {})
        if inferred_tech_stack and not current_spec.get("tech_stack"):
            current_spec["tech_stack"] = inferred_tech_stack

        plan = self._finalize_plan_with_dag(plan, project_spec=current_spec, mode="extend")
        plan = self._normalize_extend_plan(plan, existing_context)
        tasks = plan.get("tasks") or []
        if not tasks:
            self.blackboard.set_project_status(ProjectStatus.FAILED)
            self.blackboard.record_failure_context(
                "extend_dag_empty",
                "Extend Mode 经 DAG 归一化后无可执行任务",
            )
            self._persist_blackboard_artifacts(final_dir, failed=True)
            update_project_status(self.project_id, "failed")
            global_broadcaster.emit_sync("System", "error", "Extend Mode DAG 归一化后无任务")
            return False, final_dir

        group_id = f"extend:{self.session_id}"
        normalized_tasks = []
        for task in tasks:
            item = dict(task)
            target_file = str(item.get("target_file", "")).replace("\\", "/").lstrip("/")
            item["target_file"] = target_file
            item["group_id"] = group_id
            item["write_targets"] = item.get("write_targets") or ([target_file] if target_file else [])
            normalized_tasks.append(item)

        self.blackboard.append_tasks(
            normalized_tasks,
            group_id=group_id,
            dag_metadata=plan.get("dag") or {
                "mode": "extend",
                "node_count": len(normalized_tasks),
                "edge_count": 0,
            },
        )

        extend_context = plan.get("extend_context", {})
        extend_spec_text = (
            f"{self.blackboard.state.spec_text or ''}\n\n"
            f"[Extend Mode]\n"
            f"{plan.get('architecture_summary', '新增模块')}\n"
            f"用户要求: {user_requirement}\n"
            f"新增文件: {', '.join(extend_context.get('new_files', [])) or '无'}\n"
            f"焊接文件: {', '.join(extend_context.get('weld_targets', [])) or '无'}"
        ).strip()
        self.blackboard.set_project_spec(
            spec=current_spec,
            spec_text=extend_spec_text,
            project_name=self.project_id,
        )
        append_event(
            "manager",
            "extend_plan",
            json.dumps(plan, ensure_ascii=False),
            project_id=self.project_id,
        )

        self.blackboard.set_project_status(ProjectStatus.EXECUTING)
        success = self._phase_execution(group_id=group_id)
        delivered_with_warnings = False
        tester = None

        if success:
            realized_conflicts = self._scan_realized_extend_route_conflicts(final_dir, plan, existing_context)
            if realized_conflicts:
                success = False
                self.blackboard.set_project_status(ProjectStatus.FAILED)
                self.blackboard.record_failure_context(
                    "extend_route_conflict_realized",
                    "新增模块实现后的真实路由与已有路由冲突",
                    extra_context={
                        "conflicts": realized_conflicts,
                        "new_files": extend_context.get("new_files", []),
                    },
                )
                global_broadcaster.emit_sync(
                    "System",
                    "error",
                    f"Extend Mode 执行后检测到路由冲突：{', '.join(realized_conflicts)}",
                )

        if success:
            from core.integration_manager import IntegrationManager

            tester = IntegrationManager(self.blackboard, self.vfs, self.project_id)
            has_phases = getattr(self, '_phase_mode', False)
            is_final = getattr(self, '_is_final_phase', False)
            if tester.needs_integration_test(phase_mode=has_phases, is_final_phase=is_final):
                integration_ok = tester.run_integration_test()
                if not integration_ok:
                    delivered_with_warnings = True
                    success = True
                    global_broadcaster.emit_sync(
                        "System",
                        "integration_warning",
                        "⚠️ Extend Mode 已完成，但集成测试仍未完全通过",
                    )
            elif has_phases and not is_final:
                startup_ok = tester.run_startup_check()
                if not startup_ok:
                    startup_ok = self._try_startup_self_repair(tester)
                if not startup_ok:
                    delivered_with_warnings = True

        if success:
            if delivered_with_warnings:
                self.blackboard.set_project_status(ProjectStatus.DELIVERED_WITH_WARNINGS)
                self.blackboard.record_failure_context(
                    "integration_warning",
                    "Extend Mode 后集成测试未完全通过",
                    extra_context=getattr(tester, "_last_failure_context", {}) or {},
                )
                update_project_status(self.project_id, "warning")
                global_broadcaster.emit_sync(
                    "System",
                    "integration_warning",
                    f"⚠️ Extend Mode 已交付，但 QA 仍未完全通过：{final_dir}",
                    {"final_path": final_dir},
                )
            else:
                self.blackboard.set_project_status(ProjectStatus.COMPLETED)
                update_project_status(self.project_id, "success")
                global_broadcaster.emit_sync(
                    "System",
                    "success",
                    f"✅ Extend Mode 构建完成：{final_dir}",
                    {"final_path": final_dir},
                )

            global_broadcaster.emit_sync(
                "System",
                "extend_complete",
                "新增模块已完成",
                {
                    "final_path": final_dir,
                    "new_files": extend_context.get("new_files", []),
                    "weld_targets": extend_context.get("weld_targets", []),
                },
            )
            try:
                from tools.git_ops import git_commit

                git_commit(final_dir, f"ASTrea Extend: {user_requirement[:60]}")
            except Exception as e:
                logger.warning(f"⚠️ Git auto-commit 失败（不影响交付）: {e}")
            self._persist_blackboard_artifacts(final_dir)
        else:
            if self.blackboard.state.project_status != ProjectStatus.FAILED:
                self.blackboard.set_project_status(ProjectStatus.FAILED)
                self.blackboard.record_failure_context(
                    "extend_execution_failed",
                    "Extend Mode 存在熔断任务或执行后校验失败",
                    extra_context=plan.get("extend_context", {}),
                )
            self._persist_blackboard_artifacts(final_dir, failed=True)
            update_project_status(self.project_id, "failed")
            global_broadcaster.emit_sync("System", "error", "Extend Mode 执行失败")

        self._phase_settlement(user_requirement, success)
        self.blackboard.delete_checkpoint()
        if self.vfs:
            self.vfs.clean_sandbox()
        return success, final_dir

    # ============================================================
    # Tech Stack 推断（Patch Mode 用）
    # ============================================================

    def _infer_tech_stack(self, project_dir: str) -> list:
        """
        从已有项目文件推断 tech_stack（用于 Patch Mode 加载 Playbook）。
        规则：按文件扩展名、关键 import 语句和配置文件嗅探。
        确保 PlaybookLoader 的 Addon Assembly 能在 Patch Mode 下正确注入补丁。
        """
        stack = set()
        if not os.path.isdir(project_dir):
            return []

        for root, dirs, files in os.walk(project_dir):
            # 跳过隐藏目录和缓存
            dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__' and d != 'node_modules']
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                fpath = os.path.join(root, fname)

                if ext == '.py':
                    stack.add("Python")
                    try:
                        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                            head = f.read(2000)
                        # 后端框架嗅探
                        if "from flask" in head or "import flask" in head:
                            stack.add("Flask")
                        if "from django" in head or "import django" in head:
                            stack.add("Django")
                        if "from fastapi" in head or "import fastapi" in head:
                            stack.add("FastAPI")
                        # 数据库嗅探
                        if "import sqlite3" in head:
                            stack.add("SQLite")
                        if "from sqlalchemy" in head or "import sqlalchemy" in head:
                            stack.add("SQLAlchemy")
                    except Exception:
                        pass
                elif ext == '.html':
                    stack.add("HTML")
                elif ext == '.css':
                    stack.add("CSS")
                elif ext in ('.js', '.jsx'):
                    stack.add("JavaScript")
                elif ext in ('.ts', '.tsx'):
                    stack.add("TypeScript")
                elif ext == '.vue':
                    stack.add("Vue3")

                # 配置文件嗅探
                fname_lower = fname.lower()
                if fname_lower == 'package.json':
                    try:
                        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                            pkg = json.loads(f.read())
                        all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                        if "react" in all_deps:
                            stack.add("React")
                        if "vue" in all_deps:
                            stack.add("Vue3")
                        if "next" in all_deps:
                            stack.add("Next.js")
                    except Exception:
                        pass
                elif fname_lower in ('tailwind.config.js', 'tailwind.config.ts', 'tailwind.config.mjs'):
                    stack.add("Tailwind")
                elif fname_lower == 'next.config.js' or fname_lower == 'next.config.mjs':
                    stack.add("Next.js")

        return sorted(stack)

    # ============================================================
    # Rollback Mode: 版本回退
    # ============================================================

    def _run_rollback_mode(self, user_requirement: str) -> Tuple[bool, str]:
        """
        Rollback 模式：从 git log 中定位 commit，执行 git revert。
        """
        logger.info(f"⏪ Rollback Mode 启动: {self.project_id}")
        global_broadcaster.emit_sync("System", "start_project", f"⏪ Rollback Mode: {self.project_id}")

        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "projects", self.project_id))
        git_dir = os.path.join(base_dir, ".git")

        if not os.path.isdir(git_dir):
            logger.error("💥 [Rollback] 项目没有 Git 仓库，无法回滚")
            global_broadcaster.emit_sync("System", "error", "💥 项目没有 Git 仓库，无法回滚")
            return False, base_dir

        try:
            import subprocess
            # 从 user_requirement 中提取关键词，搜索 git log
            result = subprocess.run(
                ["git", "log", "--max-count=20", "--format=%H|%s|%ai"],
                cwd=base_dir, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
            )
            if result.returncode != 0 or not result.stdout:
                logger.error(f"💥 [Rollback] git log 失败: {result.stderr}")
                return False, base_dir

            commits = []
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                parts = line.split('|', 2)
                if len(parts) == 3:
                    commits.append({"hash": parts[0].strip(), "message": parts[1].strip(), "date": parts[2].strip()})

            if not commits:
                logger.warning("⚠️ [Rollback] 没有可回滚的 commit")
                global_broadcaster.emit_sync("System", "error", "没有可回滚的 commit 记录")
                return False, base_dir

            # 解析指令：判断是否是批量批次回滚
            req_lower = user_requirement.lower()
            if req_lower.startswith("rollback round:"):
                # 提取 round_id
                round_id = user_requirement.split(":", 1)[1].strip()
                logger.info(f"⏪ [Rollback] 收到批次回退请求: Round {round_id}")
                
                # 找出同属于该批次的所有 commit
                round_commits = [c for c in commits if f"[Round {round_id}]" in c["message"]]
                if not round_commits:
                    logger.warning(f"⚠️ [Rollback] 找不到包含 [Round {round_id}] 的 commit")
                    global_broadcaster.emit_sync("System", "error", f"找不到批次 {round_id} 的提交记录")
                    return False, base_dir
                
                # git log 输出是最新的在前（时间倒序）。所以 round_commits 已经是从新到旧排序了。
                # 级联回退：依次 git revert --no-commit
                global_broadcaster.emit_sync("System", "info", f"⏪ 正在级联回退批次: Round {round_id} (共 {len(round_commits)} 条记录)...")
                for c in round_commits:
                    commit_hash = c["hash"]
                    logger.info(f"⏪ [Rollback] revert {commit_hash[:8]}: {c['message']}")
                    res = subprocess.run(
                        ["git", "revert", "--no-commit", commit_hash],
                        cwd=base_dir, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15
                    )
                    if res.returncode != 0:
                        logger.error(f"❌ [Rollback] git revert 冲突: {res.stderr}")
                        subprocess.run(["git", "revert", "--abort"], cwd=base_dir, capture_output=True, timeout=5)
                        global_broadcaster.emit_sync("System", "error", "❌ 级联回退时发生冲突，已中止。")
                        return False, base_dir
                
                # 最终一笔提交
                res = subprocess.run(
                    ["git", "commit", "-m", f"⏪ Rollback [Round {round_id}]"],
                    cwd=base_dir, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10
                )
                if res.returncode == 0:
                    logger.info(f"✅ [Rollback] 批次回退成功: Round {round_id}")
                    global_broadcaster.emit_sync("System", "success", f"✅ 已成功将批次 Round {round_id} 的全部修改连根拔除")
                    update_project_status(self.project_id, "success")
                    return True, base_dir
                else:
                    logger.error(f"❌ [Rollback] 提交回退记录失败: {res.stderr}")
                    return False, base_dir

            else:
                # 兼容旧单笔回退或回退到指定 commit
                target_commit = None
                if req_lower.startswith("rollback commit:"):
                    commit_hash = user_requirement.split(":", 1)[1].strip()
                    for c in commits:
                        if c["hash"].startswith(commit_hash):
                            target_commit = c
                            break
                else:
                    for c in commits:
                        if any(kw in c["message"].lower() for kw in req_lower.split() if len(kw) > 1):
                            target_commit = c
                            break

                if not target_commit:
                    target_commit = commits[0]
                    logger.info(f"⏪ [Rollback] 未匹配到关键词，回退最近的 commit: {target_commit['hash'][:8]}")

                commit_hash = target_commit["hash"]
                logger.info(f"⏪ [Rollback] 执行 git revert {commit_hash[:8]}: {target_commit['message']}")
                global_broadcaster.emit_sync("System", "info", f"⏪ 正在回退: {target_commit['message']} ({target_commit['date'][:10]})")

                revert_result = subprocess.run(
                    ["git", "revert", "--no-edit", commit_hash],
                    cwd=base_dir, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                )

                if revert_result.returncode == 0:
                    logger.info(f"✅ [Rollback] 回退成功: {commit_hash[:8]}")
                    global_broadcaster.emit_sync("System", "success", f"✅ 已成功回退到 {target_commit['message']} 之前的状态")
                    update_project_status(self.project_id, "success")
                    return True, base_dir
                else:
                    logger.error(f"❌ [Rollback] git revert 冲突: {revert_result.stderr}")
                    subprocess.run(["git", "revert", "--abort"], cwd=base_dir, capture_output=True, timeout=5)
                    global_broadcaster.emit_sync("System", "error", "❌ 回退时发生冲突，已自动中止。可能需要手动处理。")
                    return False, base_dir

        except Exception as e:
            logger.error(f"❌ [Rollback] 异常: {e}")
            global_broadcaster.emit_sync("System", "error", f"❌ 回退异常: {str(e)}")
            return False, base_dir

    # _needs_integration_test, _phase_integration_test, _retry_from_integration
    # → 已迁移至 core/integration_manager.py IntegrationManager

    # ============================================================
    # Phase 1: 规划 (唤醒 Manager)
    # ============================================================

    def _phase_planning(self, user_requirement: str, out_dir: str = None):
        """唤醒 Manager → 贴规划书 + 任务列表到 Blackboard → Manager 退场"""
        logger.info("📋 Phase 1: 规划阶段...")
        self.blackboard.set_project_status(ProjectStatus.PLANNING)

        manager = self._get_manager()

        # Step 0.5: 读取 plan.md（如果存在，由 PlannerLite 在 PM 确认阶段生成）
        # ⚠️ Phase 模式下跳过：磁盘 plan.md 是完整版（含所有阶段），
        #    而 user_requirement 已经是 PM 裁剪后的 Phase 工单，
        #    注入完整 plan 会导致 Spec/Manager 看到非本阶段的技术栈和模块。
        has_phases = getattr(self, '_phase_mode', False)
        if has_phases:
            plan_md_content = None
            logger.info("📋 [Phase 模式] 跳过磁盘 plan.md，使用 PM 编译的 Phase 工单作为唯一输入")
        else:
            plan_md_content = self._read_plan_md()
        if plan_md_content:
            logger.info(f"plan.md 已读取 ({len(plan_md_content)} 字符)，将作为合同约束注入 Manager")

        # Step 0.6: 提取 Playbook 铁律（防止 Manager 规划出被禁止的技术栈）
        playbook_hint = ""
        try:
            from core.playbook_loader import PlaybookLoader
            _pb = PlaybookLoader()
            # 先用用户需求推测技术栈（spec 还没生成，无法确定）
            # 默认加载 Flask（最常见）+ 通用规则
            for tech_guess in [["Flask"], ["FastAPI"]]:
                full_pb = _pb.load_for_coder(tech_guess, "app.py")
                if full_pb:
                    iron_rules = [line for line in full_pb.split("\n")
                                  if any(k in line for k in ("禁止", "严禁", "铁律", "绝对不", "MUST NOT"))]
                    if iron_rules:
                        playbook_hint += "\n".join(iron_rules[:15]) + "\n"
            if playbook_hint:
                logger.info("📜 [Phase 1] Playbook 铁律已提取，将注入 Spec 生成")
        except Exception as e:
            logger.warning(f"⚠️ Playbook 铁律提取失败: {e}")

        # Step 1: 生成规划书（含 project_name + tech_stack），注入 plan.md 合同 + Playbook 铁律
        project_spec = manager._generate_project_spec(
            user_requirement, plan_md=plan_md_content, playbook_hint=playbook_hint
        )

        if manager.has_spec_parse_failure():
            self.blackboard.set_project_spec({}, "{}", self.project_id)
            self._record_planning_failure(
                "spec_parse_failed",
                f"规划书 JSON 解析失败: {manager.last_spec_parse_error}",
                out_dir=out_dir,
                extra_context={
                    "raw_spec_response": manager.last_spec_raw_response,
                    "validation_warnings": [repr(w) for w in manager.last_spec_warnings],
                },
            )
            return

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

        # v4.1: Phase 中间阶段降级合同校验（子集 spec 不完整是预期的）
        has_phases = getattr(self, '_phase_mode', False)
        is_final = getattr(self, '_is_final_phase', False)

        if manager.has_blocking_spec_validation():
            if has_phases and not is_final:
                # Phase 中间阶段：error 降级为 warning，不阻断
                logger.warning(
                    "⚠️ [Phase 中间阶段] 合同校验有 error 但降级为 warning（子集 spec 不完整是预期的）: %s",
                    " | ".join(repr(w) for w in manager.last_spec_warnings if getattr(w, "severity", "") == "error")
                )
                global_broadcaster.emit_sync(
                    "Engine", "spec_validation_warning",
                    "⚠️ Phase 中间阶段: 合同校验降级为 warning，继续执行",
                )
            else:
                blocking_warnings = [
                    repr(w) for w in manager.last_spec_warnings
                    if getattr(w, "severity", "warning") == "error"
                ] or [repr(w) for w in manager.last_spec_warnings]
                spec_text = json.dumps(project_spec, ensure_ascii=False, indent=2) if project_spec else "无规划书"
                self.blackboard.set_project_spec(project_spec, spec_text, project_name)
                self._record_planning_failure(
                    "spec_contract_not_closed",
                    " | ".join(blocking_warnings),
                    out_dir=final_dir,
                    extra_context={
                        "raw_spec": manager.last_raw_spec or {},
                        "compiled_spec": manager.last_compiled_spec or project_spec or {},
                        "validation_warnings": [repr(w) for w in manager.last_spec_warnings],
                        "compiler_metadata": (project_spec or {}).get("compiler_metadata", {}),
                    },
                )
                logger.error("❌ 规划阶段阻断：Spec 合同未闭环")
                global_broadcaster.emit_sync(
                    "Engine",
                    "planning_blocked",
                    "❌ 规划阶段阻断：Spec 合同未闭环，未进入 Task DAG",
                    {"warnings": [repr(w) for w in manager.last_spec_warnings]},
                )
                return

        # 立即启动 sandbox 预热（异步，不阻塞 plan_tasks）
        self._warmup_sandbox(project_spec=project_spec)

        # Step 2: 拆解任务（与 sandbox warmup 并行！）
        # 加载 Manager Playbook（按技术栈动态注入）
        from core.playbook_loader import PlaybookLoader
        _pb_loader = PlaybookLoader()
        _tech_stack = (project_spec or {}).get("tech_stack", [])
        manager_playbook = _pb_loader.load_for_manager(_tech_stack)

        # Step 2.0: 构建 Phase 约束（如果处于 Phase 模式）
        phase_constraint = getattr(self, '_current_phase_info', None)
        if phase_constraint:
            logger.info(f"📋 Phase 约束已注入: P{phase_constraint.get('index')}:{phase_constraint.get('name')} "
                        f"[{phase_constraint.get('scope_type', '?')}]")

        # Step 2.1: 预估文件数，判断是否启用两阶段规划
        estimated_files = ProjectObserver.estimate_file_count(project_spec)
        TWO_STAGE_THRESHOLD = 12

        if estimated_files >= TWO_STAGE_THRESHOLD:
            # ═══ 两阶段规划（大项目） ═══
            logger.info(f"🧩 预估 {estimated_files} 文件 ≥ {TWO_STAGE_THRESHOLD}，启动两阶段规划")
            plan = self._two_stage_planning(
                manager, user_requirement, project_spec, manager_playbook
            )
        else:
            # ═══ 单阶段规划（常规项目，不变） ═══
            logger.info(f"📋 预估 {estimated_files} 文件 < {TWO_STAGE_THRESHOLD}，单阶段规划")
            # 生成复杂文件提示（辅助 Manager 决策 sub_tasks）
            complex_hint = ProjectObserver.build_complex_files_hint(project_spec)
            plan = manager.plan_tasks(
                user_requirement, project_spec=project_spec,
                manager_playbook=manager_playbook,
                complex_files_hint=complex_hint,
                plan_md=plan_md_content,
                phase_constraint=phase_constraint,
            )

        # 防御：plan_tasks 返回 0 tasks → 自动重试一次（应对 LLM 间歇性 JSON 解析失败）
        if not plan.get("tasks"):
            logger.warning(
                "⚠️ plan_tasks 首次返回 0 tasks (plan=%s)，自动重试一次...",
                {k: v for k, v in plan.items() if k != "project_spec"},
            )
            global_broadcaster.emit_sync(
                "Engine", "plan_retry",
                "⚠️ 任务拆解首次为空，正在重试...",
            )
            complex_hint = ProjectObserver.build_complex_files_hint(project_spec)
            plan = manager.plan_tasks(
                user_requirement, project_spec=project_spec,
                manager_playbook=manager_playbook,
                complex_files_hint=complex_hint,
                plan_md=plan_md_content,
                phase_constraint=phase_constraint,
            )
            if plan.get("tasks"):
                logger.info(f"✅ 重试成功: {len(plan['tasks'])} 个 tasks")
            else:
                logger.error("❌ 重试仍为 0 tasks，将触发规划失败")

        plan["project_spec"] = project_spec

        # 贴上黑板
        spec_text = json.dumps(project_spec, ensure_ascii=False, indent=2) if project_spec else "无规划书"
        plan = self._finalize_plan_with_dag(plan, project_spec=project_spec, mode="create")
        plan["project_spec"] = project_spec

        self.blackboard.set_project_spec(project_spec, spec_text, project_name)
        self.blackboard.set_tasks(plan.get("tasks", []), dag_metadata=plan.get("dag"))

        # 记录事件
        append_event("manager", "plan", json.dumps(plan, ensure_ascii=False),
                      project_id=self.project_id)

        logger.info(f"📋 规划完成: {project_name}, {len(plan.get('tasks', []))} 个子任务")

    def _read_plan_md(self) -> str:
        """读取项目目录下的 plan.md（由 PlannerLite 在 PM 确认阶段生成）"""
        project_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "projects", self.project_id
        )
        plan_path = os.path.join(project_dir, ".astrea", "plan.md")
        if os.path.isfile(plan_path):
            try:
                with open(plan_path, "r", encoding="utf-8") as f:
                    return f.read()
            except Exception as e:
                logger.warning(f"plan.md 读取失败: {e}")
        return None



    def _two_stage_planning(self, manager, user_requirement: str,
                            project_spec: dict, manager_playbook: str) -> dict:
        """两阶段规划：Stage 1 分模块组 → Stage 2 逐组规划 tasks → 合并"""
        global_broadcaster.emit_sync("Engine", "two_stage_start",
            "🧩 大型项目: 启动两阶段规划...")

        # Stage 1: 模块分组
        module_groups = manager.plan_module_groups(user_requirement, project_spec)

        if not module_groups:
            # 降级：Stage 1 失败，回退到单阶段
            logger.warning("⚠️ 两阶段 Stage 1 失败，降级为单阶段规划")
            complex_hint = ProjectObserver.build_complex_files_hint(project_spec)
            return manager.plan_tasks(
                user_requirement, project_spec=project_spec,
                manager_playbook=manager_playbook,
                complex_files_hint=complex_hint
            )

        # Stage 2: 逐模块组规划 tasks（此阶段仍产出 raw tasks，最终依赖图交给 DAG Builder）
        complex_hint = ProjectObserver.build_complex_files_hint(project_spec)
        all_tasks = []

        for group in module_groups:
            group_tasks = manager.plan_group_tasks(
                user_requirement, project_spec, group,
                manager_playbook=manager_playbook,
                complex_files_hint=complex_hint
            )
            for t in group_tasks:
                task = dict(t)
                task["_dag_group_id"] = group.get("group_id")
                all_tasks.append(task)

        logger.info(f"✅ [两阶段] Raw 规划完成: {len(module_groups)} 组 → {len(all_tasks)} 个 raw tasks")
        global_broadcaster.emit_sync("Engine", "two_stage_done",
            f"🧩 两阶段规划完成: {len(all_tasks)} 个 raw tasks，等待 DAG 归一化")

        # 取第一组的 project_name（或从 spec 取）
        project_name = (project_spec or {}).get("project_name", "AutoGen_Project")
        return {
            "project_name": project_name,
            "architecture_summary": f"两阶段规划: {len(module_groups)} 模块组",
            "tasks": all_tasks,
            "module_groups": module_groups,
        }

    def _finalize_plan_with_dag(
        self,
        plan: dict,
        project_spec: Optional[dict] = None,
        mode: str = "create",
    ) -> dict:
        """将 Manager 原始计划统一归一化为确定性 DAG 结果。"""
        plan = dict(plan or {})
        raw_tasks = plan.get("tasks", []) or []
        module_groups = plan.get("module_groups", []) or []

        try:
            dag_plan = TaskDagBuilder.build_plan(
                raw_tasks=raw_tasks,
                project_spec=project_spec,
                module_groups=module_groups,
                mode=mode,
            )
        except TaskDagBuildError as exc:
            # 降级：DAG 构建失败时回退到 LLM 原始顺序
            logger.warning(f"⚠️ DAG 构建失败，降级为 LLM 原序: {exc}")
            global_broadcaster.emit_sync("Engine", "dag_fallback",
                f"⚠️ DAG 构建失败，降级为 LLM 原始顺序: {exc}")
            fallback_tasks = []
            for idx, t in enumerate(raw_tasks):
                t = dict(t or {})
                t["task_id"] = f"task_{idx + 1}"
                t["dependencies"] = []  # 清空不可靠的依赖
                t.pop("_dag_group_id", None)
                fallback_tasks.append(t)
            plan["tasks"] = fallback_tasks
            plan["dag"] = {"mode": mode, "node_count": len(fallback_tasks),
                           "edge_count": 0, "fallback": True,
                           "fallback_reason": str(exc)}
            return plan

        plan["tasks"] = dag_plan["tasks"]
        plan["dag"] = dag_plan["dag"]

        dag_summary = {
            "node_count": dag_plan["dag"].get("node_count", 0),
            "edge_count": dag_plan["dag"].get("edge_count", 0),
            "ready_batches": len(dag_plan["dag"].get("ready_batches", [])),
            "warnings": len(dag_plan["dag"].get("warnings", [])),
        }
        logger.info(
            "🧭 确定性 DAG 已生成: %s 节点 / %s 边 / %s ready batches",
            dag_summary["node_count"],
            dag_summary["edge_count"],
            dag_summary["ready_batches"],
        )
        global_broadcaster.emit_sync(
            "Engine",
            "dag_ready",
            (
                f"🧭 DAG 已生成: {dag_summary['node_count']} 节点 / "
                f"{dag_summary['edge_count']} 边 / {dag_summary['ready_batches']} 个 ready 批次"
            ),
            dag_summary,
        )
        return plan

    # ============================================================
    # v4.2: 启动验证自修复
    # ============================================================

    def _try_startup_self_repair(self, tester) -> bool:
        """启动验证失败后的自修复闭环。
        从 stderr 提取 guilty 文件 → reopen 任务 → 重新执行 → 再次验证。
        最多修复 1 轮，防止无限循环。"""
        from core.integration_manager import IntegrationManager

        failure_ctx = tester._last_failure_context or {}
        stderr = failure_ctx.get("feedback", "")
        if not stderr:
            logger.warning("⚠️ 启动验证无错误信息，跳过自修复")
            return False

        # 从 traceback 提取 guilty 文件
        truth_dir = self.blackboard.state.out_dir or ""
        guilty_file = IntegrationManager.extract_guilty_file_from_stderr(stderr, truth_dir)
        if not guilty_file:
            logger.warning("⚠️ 无法从 traceback 定位出错文件，跳过自修复")
            return False

        # 找到对应的 DONE 任务
        guilty_task = self.blackboard.find_task_by_file(guilty_file)
        if not guilty_task:
            logger.warning(f"⚠️ 出错文件 {guilty_file} 不在任务列表中，跳过自修复")
            return False

        logger.info(f"🔧 [启动自修复] 定位到出错文件: {guilty_file} (task: {guilty_task.task_id})")
        global_broadcaster.emit_sync("System", "self_repair",
            f"🔧 启动验证失败，定位到 {guilty_file}，尝试自修复...")

        # Reopen 任务，注入 stderr 作为修复指令
        fix_instruction = (
            f"【启动验证失败 — 进程崩溃】\n"
            f"错误输出:\n{stderr[:1500]}\n\n"
            f"请修复上述错误，确保项目能正常启动运行。"
        )
        self.blackboard.reopen_task(
            guilty_task.task_id,
            fix_instruction=fix_instruction,
        )

        # 重新执行（只跑被 reopen 的任务）
        repair_success = self._phase_execution()
        if not repair_success:
            logger.warning("⚠️ 自修复执行失败（任务熔断）")
            return False

        # 再次启动验证
        startup_ok = tester.run_startup_check()
        if startup_ok:
            logger.info("✅ 自修复成功，启动验证通过")
            global_broadcaster.emit_sync("System", "self_repair_passed",
                "✅ 自修复成功，启动验证通过")
        else:
            logger.warning("⚠️ 自修复后启动验证仍失败")

        return startup_ok

    # ============================================================
    # Phase 2: 执行 (状态机主循环)
    # ============================================================

    @staticmethod
    def _infer_focus_endpoints(plan: dict, project_dir: str) -> list | None:
        """
        v4.4: 从 Patch 修改的文件反向推断受影响的 HTTP 端点。

        策略:
        - 模板文件 (.html): grep 所有 .py 找 render_template('name') → 提取 @route
        - Python 文件 (.py): 直接提取 @app.route / @bp.route 装饰器
        - 静态资源 (.css/.js): 降级为首页冒烟 (GET /)
        - 无法推断时返回 None → 走全量测试
        """
        import re

        target_files = [
            t.get("target_file", "") for t in plan.get("tasks", [])
            if t.get("target_file")
        ]
        if not target_files:
            return None

        # 收集所有 .py 文件内容（用于 grep）
        py_files = {}
        if os.path.isdir(project_dir):
            ignore = {'.sandbox', '.git', '__pycache__', '.venv', 'node_modules', '.astrea'}
            for root, dirs, files in os.walk(project_dir):
                dirs[:] = [d for d in dirs if d not in ignore]
                for f in files:
                    if f.endswith('.py'):
                        fpath = os.path.join(root, f)
                        rel = os.path.relpath(fpath, project_dir).replace('\\', '/')
                        try:
                            with open(fpath, 'r', encoding='utf-8') as fh:
                                py_files[rel] = fh.read()
                        except Exception:
                            pass

        focus = set()
        # 匹配 @app.route / @bp.route / @blueprint.route 等
        route_pattern = re.compile(
            r"@\w+\.route\(\s*['\"]([^'\"]+)['\"]"
            r"(?:.*?methods\s*=\s*\[([^\]]+)\])?"
        , re.DOTALL)

        # 动态检测端口
        port = 5001
        for content in py_files.values():
            m = re.search(r'port\s*=\s*(\d{4,5})', content, re.IGNORECASE)
            if m:
                port = int(m.group(1))
                break

        for target in target_files:
            basename = os.path.basename(target)
            ext = os.path.splitext(target)[1].lower()

            if ext in ('.html', '.htm'):
                # 模板文件：grep render_template('basename') 找路由
                for py_rel, py_content in py_files.items():
                    if f"'{basename}'" in py_content or f'"{basename}"' in py_content:
                        # 找到引用此模板的 .py 文件，提取 route
                        lines = py_content.split('\n')
                        last_route = None
                        last_methods = ['GET']
                        for line in lines:
                            rm = route_pattern.search(line)
                            if rm:
                                last_route = rm.group(1)
                                methods_str = rm.group(2)
                                if methods_str:
                                    last_methods = [
                                        m.strip().strip("'\"")
                                        for m in methods_str.split(',')
                                    ]
                                else:
                                    last_methods = ['GET']
                            # 检查这行或下面几行是否引用了模板
                            if (f"render_template('{basename}'" in line or
                                    f'render_template("{basename}"' in line):
                                if last_route:
                                    # 替换动态参数为示例值
                                    route_url = re.sub(r'<\w+:\w+>', '1', last_route)
                                    route_url = re.sub(r'<\w+>', '1', route_url)
                                    for method in last_methods:
                                        focus.add(f"{method.upper()} http://127.0.0.1:{port}{route_url}")

            elif ext == '.py':
                # Python 文件：直接提取该文件中的 route
                content = py_files.get(target, '')
                if content:
                    for rm in route_pattern.finditer(content):
                        route_path = rm.group(1)
                        methods_str = rm.group(2)
                        if methods_str:
                            methods = [m.strip().strip("'\"") for m in methods_str.split(',')]
                        else:
                            methods = ['GET']
                        route_url = re.sub(r'<\w+:\w+>', '1', route_path)
                        route_url = re.sub(r'<\w+>', '1', route_url)
                        for method in methods:
                            focus.add(f"{method.upper()} http://127.0.0.1:{port}{route_url}")

            elif ext in ('.css', '.js', '.scss', '.less'):
                # 静态资源修改：至少做首页冒烟
                focus.add(f"GET http://127.0.0.1:{port}/")

        if focus:
            result = sorted(focus)
            logger.info(f"🎯 [Patch Mode] 推断受影响端点: {result}")
            return result

        logger.info("🎯 [Patch Mode] 无法推断受影响端点，走全量测试")
        return None

    def _phase_execution(self, group_id: Optional[str] = None) -> bool:
        """
        主循环：基于依赖图调度，逐个执行 TDD。

        Returns:
            True if all tasks DONE, False if any FUSED
        """
        # v5.1: 存储当前 group_id，供 server.py 调用 get_execution_summary 时过滤
        self._last_group_id = group_id
        logger.info("⚙️ Phase 2: 执行阶段%s...", f" (group={group_id})" if group_id else "")

        # 软删除旧轨迹
        from core.settlement import SettlementEngine
        SettlementEngine(self.project_id, self.blackboard.state, self.vfs).archive_old_trajectories()

        task_idx = 0
        total = len([
            t for t in self.blackboard.state.tasks
            if group_id is None or t.group_id == group_id
        ])

        while True:
            # 优雅退出检查
            if self._shutdown:
                logger.warning("🛑 Engine 检测到 shutdown 信号，停止执行")
                return False

            # 检查是否全部完成
            if self.blackboard.all_tasks_done(group_id=group_id):
                if self.blackboard.has_fused_tasks(group_id=group_id):
                    logger.error("💥 存在熔断任务")
                    return False
                logger.info("🏆 所有任务均已完成！")
                return True

            # 熔断即停：任何任务熔断 → 整体终止，不再浪费 Token
            if self.blackboard.has_fused_tasks(group_id=group_id):
                fused_tasks = [t.target_file for t in self.blackboard.state.tasks
                               if t.status == TaskStatus.FUSED and (group_id is None or t.group_id == group_id)]
                remaining = [t.target_file for t in self.blackboard.state.tasks
                             if t.status not in (TaskStatus.DONE, TaskStatus.FUSED)
                             and (group_id is None or t.group_id == group_id)]
                logger.error(f"🛑 熔断即停：{fused_tasks} 已熔断，跳过剩余 {len(remaining)} 个任务 {remaining}")
                global_broadcaster.emit_sync("Engine", "project_fused",
                    f"项目因 {', '.join(fused_tasks)} 熔断而终止", {})
                return False

            # 依赖图调度：找下一个可运行的任务
            task = self.blackboard.get_next_runnable_task(group_id=group_id)
            if task is None:
                # 没有可运行任务但也没全部完成 → 死锁检测
                logger.error("💥 依赖死锁：无可运行任务但存在未完成任务")
                return False

            task_idx += 1
            logger.info(f"\n[{task_idx}/{total}] ========================")

            # 执行单个任务的 TDD 循环
            # 执行单个任务的 TDD 循环 (委托 TaskRunner)
            from core.task_runner import TaskRunner
            runner = TaskRunner(
                blackboard=self.blackboard,
                vfs=self.vfs,
                patcher=self.patcher,
                project_id=self.project_id,
                session_id=getattr(self, "session_id", "local"),
                shutdown_flag=lambda: self._shutdown,
                phase_mode=getattr(self, '_phase_mode', False),
            )
            runner.execute(task)

            # Checkpoint
            self.blackboard.checkpoint()


    # ============================================================
    # Phase 3: 结算
    # ============================================================

    def _phase_settlement(self, user_requirement: str, success: bool):
        """后台异步结算：先同步写入实时地图 + 演进脚印，再委托 SettlementEngine"""
        # Phase 5.5a: 实时地图 — 将 project_scanner 结果写入 BlackboardState
        project_dir = self.blackboard.state.out_dir
        if project_dir and os.path.isdir(project_dir):
            try:
                from core.project_scanner import scan_existing_project
                snapshot = scan_existing_project(
                    project_dir,
                    blackboard_state=self.blackboard.state.model_dump(),
                )
                self.blackboard.state.project_snapshot = snapshot
                file_count = len(snapshot.get("file_tree", []))
                logger.info(f"🗺️ [实时地图] project_snapshot 已刷新 ({file_count} 文件)")
            except Exception as e:
                logger.warning(f"⚠️ [实时地图] project_snapshot 刷新失败: {e}")

        # Phase 5.5b: 演进脚印 — 追加当轮结构化 summary
        current_round = len(self.blackboard.state.round_history) + 1
        mode = "create"
        qa_extra = {}
        fc = self.blackboard.state.failure_context
        if fc:
            qa_extra["qa_passed_count"] = fc.get("passed_count", 0)
            qa_extra["qa_failed_count"] = fc.get("failed_count", 0)
        self.blackboard.append_round_summary(
            mode=mode,
            user_intent=user_requirement or "",
            current_round=current_round,
            extra=qa_extra if qa_extra.get("qa_passed_count") or qa_extra.get("qa_failed_count") else None,
        )

        # 落盘（确保 snapshot + round_history 持久化）
        if project_dir:
            self._persist_blackboard_artifacts(project_dir)

        from core.settlement import SettlementEngine
        settler = SettlementEngine(self.project_id, self.blackboard.state, self.vfs)
        settler.run_async(user_requirement, success)

    # ============================================================
    # 辅助方法
    # ============================================================

    def _resolve_artifact_dir(self, fallback_dir: str = None) -> str:
        """解析用于保存本地黑板快照的目录。"""
        candidate = self.blackboard.state.out_dir or fallback_dir
        if candidate:
            return os.path.abspath(candidate)

        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "projects"))
        project_id = self.blackboard.state.project_id or self.project_id
        return os.path.join(base_dir, project_id)

    def _persist_blackboard_artifacts(self, project_dir: str, failed: bool = False):
        """将当前黑板状态落盘；失败时额外保留一份失败态快照。"""
        if not project_dir:
            return

        try:
            self.blackboard.state.save_to_disk(project_dir)
            if failed:
                self.blackboard.state.save_to_disk(project_dir, "blackboard_state.failed.json")
        except Exception as e:
            logger.warning(f"⚠️ Blackboard 持久化失败: {e}")

    def _record_planning_failure(self, reason: str, error_message: str,
                                 out_dir: str = None,
                                 extra_context: Optional[dict] = None):
        """规划阶段失败统一收口，禁止空 spec 继续下沉到执行链。"""
        artifact_dir = self._resolve_artifact_dir(out_dir)
        self.blackboard.set_project_status(ProjectStatus.FAILED)
        self.blackboard.record_failure_context(reason, error_message)
        if extra_context:
            self.blackboard.state.failure_context.update(extra_context)
        self.blackboard._touch()
        self._persist_blackboard_artifacts(artifact_dir, failed=True)
        update_project_status(self.project_id, "planning_blocked")
        global_broadcaster.emit_sync(
            "System", "error",
            f"规划阶段已阻断: {error_message}",
        )

    def _finalize_project_rename(self):
        """在主流程尾部再次尝试项目目录重命名，降低 Windows 目录锁导致的失败率。"""
        if not self._pending_project_rename:
            return

        old_id, new_id, safe_name = self._pending_project_rename
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "projects"))
        old_dir = os.path.join(base_dir, old_id)
        new_dir = os.path.join(base_dir, new_id)

        if not os.path.exists(old_dir) or old_dir == new_dir:
            self._pending_project_rename = None
            return

        deadline = time.time() + 3.0
        last_error = None
        while time.time() < deadline:
            try:
                os.rename(old_dir, new_dir)
                self.blackboard.state.project_id = new_id
                self.blackboard.state.out_dir = new_dir
                rename_project_events(old_id, new_id)
                rename_project_meta(old_id, new_id, safe_name)
                global_broadcaster.emit_sync(
                    "System", "project_renamed",
                    f"项目已重命名: {safe_name}",
                    {"old_id": old_id, "new_id": new_id},
                )
                self._pending_project_rename = None
                return
            except Exception as e:
                last_error = e
                time.sleep(0.2)

        logger.warning(f"⚠️ 延迟重命名仍失败，保留原目录 ID: {last_error}")
        rename_project_meta(old_id, old_id, safe_name)
        self._pending_project_rename = None

    def _resolve_output_dir(self, out_dir: str = None) -> str:
        """计算项目输出目录 + 动态重命名"""
        project_name = self.blackboard.state.project_name or "Unnamed"

        # 动态重命名逻辑
        if "新建项目" in self.project_id or "new_project" in self.project_id or "default_project" == self.project_id:
            parts = self.project_id.split("_", 2)
            timestamp = f"{parts[0]}_{parts[1]}" if len(parts) >= 2 else time.strftime("%Y%m%d_%H%M%S")
            # 目录名仅允许 ASCII（中文项目名仅存入 project_meta 表）
            safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', project_name)
            safe_name = re.sub(r'_+', '_', safe_name).strip('_') or "Unnamed"
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
                    logger.warning(f"⚠️ 立即重命名失败，登记延迟重试: {e}")
                    self._pending_project_rename = (self.project_id, new_id, safe_name)
                    rename_project_meta(self.project_id, self.project_id, safe_name)

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
