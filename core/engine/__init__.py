"""
core/engine/__init__.py — AstreaEngine Facade

所有外部引用统一通过 `from core.engine import AstreaEngine`，
Facade 将方法代理到各子模块（lifecycle, pipeline, modes, helpers）。
"""
import logging
import traceback
from typing import Tuple, Optional

from core.blackboard import ProjectStatus
from core.database import (
    append_event, create_project_meta, update_project_status,
)
from core.ws_broadcaster import global_broadcaster

from core.engine import lifecycle
from core.engine import pipeline
from core.engine.helpers import resolve_artifact_dir, persist_blackboard_artifacts

logger = logging.getLogger("AstreaEngine")


class AstreaEngine:
    """
    状态机驱动的工作流编排引擎 (v1.7.0 Unlimited 架构)。

    核心原则：
    - Blackboard 是唯一的状态源
    - Engine 是唯一的 VFS 写入者
    - Agent 读 Blackboard，写结果回 Blackboard
    - Engine 扫描 Blackboard 状态来决定下一步动作

    本 Facade 将所有业务逻辑代理到子模块，自身仅保留路由分发和属性定义。
    """

    def __init__(self, project_id: str):
        lifecycle.init_engine(self, project_id)

    @property
    def project_id(self) -> str:
        return self.blackboard.project_id

    # ----------------------------------------------------------
    # 类方法
    # ----------------------------------------------------------

    @classmethod
    def resume(cls, project_id: str) -> Optional["AstreaEngine"]:
        """从 Checkpoint 恢复引擎实例"""
        return lifecycle.resume_engine(cls, project_id)

    # ----------------------------------------------------------
    # 主入口
    # ----------------------------------------------------------

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
        # 生成本次执行的全局批次/轮次标记
        self.session_id = str(lifecycle.next_round_number(self))
        logger.info(f"🚀 AstreaEngine 启动: {self.project_id} (mode={mode}, Round={self.session_id})")

        # 快照当前 Git HEAD（用于 abort 回滚）
        self._pre_execution_git_head = lifecycle.get_current_git_head(self)
        self._abort_requested = False
        logger.info(f"📸 执行前 Git HEAD: {self._pre_execution_git_head or 'N/A'}")

        # 记录用户需求
        self.blackboard.set_user_requirement(user_requirement)
        append_event("user", "prompt", user_requirement, project_id=self.project_id)
        create_project_meta(self.project_id)
        global_broadcaster.emit_sync(
            "System", "start_project",
            f"AstreaEngine 启动 (mode={mode}, Round={self.session_id})...",
        )

        # mode 决策
        if mode == "auto":
            if lifecycle.is_existing_project(self):
                mode = "patch"
                logger.info("🔄 auto → patch（检测到已有项目）")
            else:
                mode = "create"
                logger.info("🆕 auto → create（新项目）")
        elif mode == "modify":
            mode = "patch"
            logger.info("🔄 modify → patch（透明化路由）")

        try:
            if mode == "create":
                from core.engine.modes.create import run_create_mode
                return run_create_mode(self, user_requirement, out_dir)
            elif mode == "patch":
                from core.engine.modes.patch import run_patch_mode
                return run_patch_mode(self, user_requirement)
            elif mode == "extend":
                from core.engine.modes.extend import run_extend_mode
                return run_extend_mode(self, user_requirement)
            elif mode == "continue":
                from core.engine.modes.continue_mode import run_continue_mode
                return run_continue_mode(self, user_requirement)
            elif mode == "rollback":
                from core.engine.modes.rollback import run_rollback_mode
                return run_rollback_mode(self, user_requirement)
            else:
                logger.warning(f"⚠️ 未知 mode '{mode}'，降级为 auto")
                if lifecycle.is_existing_project(self):
                    from core.engine.modes.patch import run_patch_mode
                    return run_patch_mode(self, user_requirement)
                from core.engine.modes.create import run_create_mode
                return run_create_mode(self, user_requirement, out_dir)

        except Exception as e:
            logger.error(f"❌ AstreaEngine 异常: {e}")
            traceback.print_exc()
            self.blackboard.set_project_status(ProjectStatus.FAILED)
            self.blackboard.record_failure_context("engine_exception", str(e))
            persist_blackboard_artifacts(
                self,
                resolve_artifact_dir(self, out_dir),
                failed=True,
            )
            update_project_status(self.project_id, "failed")
            return False, resolve_artifact_dir(self, out_dir)

    # ----------------------------------------------------------
    # 公开方法代理（供 server.py 直接调用）
    # ----------------------------------------------------------

    def abort_and_rollback(self) -> dict:
        """一键中止当前执行并回滚到执行前状态"""
        return lifecycle.abort_and_rollback(self)

    def _phase_execution(self, group_id: Optional[str] = None) -> bool:
        """执行阶段（供 resume 端点调用）"""
        return pipeline.phase_execution(self, group_id=group_id)

    # ----------------------------------------------------------
    # 兼容桩：Agent 延迟获取（供可能的外部直接调用）
    # ----------------------------------------------------------

    def _get_manager(self):
        return lifecycle.get_manager(self)

    def _get_coder(self):
        return lifecycle.get_coder(self)

    def _get_reviewer(self):
        return lifecycle.get_reviewer(self)
