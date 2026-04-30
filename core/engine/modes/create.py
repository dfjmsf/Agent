"""
engine/modes/create.py — Create 模式：全量新建项目
"""
import json
import logging
from typing import Tuple

from core.blackboard import ProjectStatus
from core.database import update_project_status
from core.ws_broadcaster import global_broadcaster

from core.engine.helpers import (
    resolve_artifact_dir,
    persist_blackboard_artifacts,
    finalize_project_rename,
)
from core.engine.pipeline import (
    phase_planning,
    phase_execution,
    phase_settlement,
    try_startup_self_repair,
)

logger = logging.getLogger("AstreaEngine")


def run_create_mode(engine, user_requirement: str, out_dir: str = None) -> Tuple[bool, str]:
    """Create 模式：全量新建项目"""
    logger.info("🆕 Create Mode 启动")
    # v4.0: Phase 模式标志
    if not hasattr(engine, '_phase_mode'):
        engine._phase_mode = False

    # Phase 1: 规划
    phase_planning(engine, user_requirement, out_dir=out_dir)

    final_dir = engine.blackboard.state.out_dir

    if engine.blackboard.state.failure_context.get("reason") in {"spec_parse_failed", "spec_contract_not_closed"}:
        logger.error("💥 规划阶段阻断：Spec 合同未闭环")
        engine.blackboard.set_project_status(ProjectStatus.FAILED)
        persist_blackboard_artifacts(
            engine,
            resolve_artifact_dir(engine, final_dir or out_dir),
            failed=True,
        )
        update_project_status(engine.project_id, "planning_blocked")
        global_broadcaster.emit_sync(
            "System", "error",
            "💥 规划失败：Spec 合同未闭环，未进入执行阶段"
        )
        finalize_project_rename(engine)
        return False, final_dir or ""

    # 防御：规划阶段未产出任何任务
    if not engine.blackboard.state.tasks:
        logger.error("💥 规划阶段未生成任何任务，项目失败")
        engine.blackboard.set_project_status(ProjectStatus.FAILED)
        engine.blackboard.record_failure_context("planning_failed", "规划阶段未生成任何任务")
        persist_blackboard_artifacts(
            engine,
            resolve_artifact_dir(engine, final_dir or out_dir),
            failed=True,
        )
        update_project_status(engine.project_id, "failed")
        global_broadcaster.emit_sync("System", "error", "💥 规划失败：未生成任何任务（可能是网络异常）")
        finalize_project_rename(engine)
        return False, out_dir or ""

    # Phase 2: 执行
    engine.blackboard.set_project_status(ProjectStatus.EXECUTING)
    success = phase_execution(engine)

    # Phase 2.5: 集成测试
    delivered_with_warnings = False
    from core.integration_manager import IntegrationManager
    tester = IntegrationManager(engine.blackboard, engine.vfs, engine.project_id)
    has_phases = getattr(engine, '_phase_mode', False)
    is_final = getattr(engine, '_is_final_phase', False)

    if success and tester.needs_integration_test(
        phase_mode=has_phases, is_final_phase=is_final
    ):
        integration_ok = tester.run_integration_test()
        if not integration_ok:
            if not has_phases:
                replan_ok = tester.retry_with_replan()
                if replan_ok:
                    success = phase_execution(engine)
                    if success:
                        integration_ok = tester.run_integration_test()
            else:
                logger.info("📦 Phase 模式跳过 replan，将结果回传给 PM")
            if not integration_ok:
                logger.warning("⚠️ 集成测试未完全通过，降级为警告交付")
                global_broadcaster.emit_sync("System", "integration_warning",
                    "⚠️ 集成测试未完全通过，降级为警告交付")
                delivered_with_warnings = True
                success = True
    elif success and has_phases and not is_final:
        startup_ok = tester.run_startup_check()
        if not startup_ok:
            startup_ok = try_startup_self_repair(engine, tester)
        if not startup_ok:
            logger.warning("⚠️ 启动验证修复后仍失败，降级为警告交付")
            global_broadcaster.emit_sync("System", "integration_warning", "⚠️ 启动验证失败")
            delivered_with_warnings = True

    # Phase 3: 结算
    if success:
        engine.blackboard.set_project_status(ProjectStatus.COMPLETED)
        update_project_status(engine.project_id, "success")
        logger.info(f"✨ 项目交付完成: {final_dir}")
        global_broadcaster.emit_sync("System", "success",
            f"✨ 项目完美生成！{final_dir}", {"final_path": final_dir})

        try:
            from tools.git_ops import git_commit
            ledger_count = len(engine.blackboard.state.completed_tasks)
            git_commit(final_dir, f"ASTrea: 项目交付完成 ({engine.project_id}) [Ledger: {ledger_count} tasks]")
        except Exception as e:
            logger.warning(f"⚠️ Git auto-commit 失败（不影响交付）: {e}")

        persist_blackboard_artifacts(engine, final_dir)
        if delivered_with_warnings:
            engine.blackboard.set_project_status(ProjectStatus.DELIVERED_WITH_WARNINGS)
            engine.blackboard.record_failure_context(
                "integration_warning",
                "集成测试未完全通过，降级为警告交付",
                extra_context=getattr(tester, "_last_failure_context", {}) or {},
            )
            update_project_status(engine.project_id, "warning")
            global_broadcaster.emit_sync("System", "integration_warning",
                f"⚠️ 项目已交付，但集成测试未完全通过：{final_dir}", {"final_path": final_dir})
            persist_blackboard_artifacts(engine, final_dir)
    else:
        engine.blackboard.set_project_status(ProjectStatus.FAILED)
        engine.blackboard.record_failure_context("execution_failed", "项目存在熔断任务")
        persist_blackboard_artifacts(engine, final_dir, failed=True)
        update_project_status(engine.project_id, "failed")
        global_broadcaster.emit_sync("System", "error", "💥 项目存在熔断任务")

    # 后台异步结算
    phase_settlement(engine, user_requirement, success)

    # 清理
    engine.blackboard.delete_checkpoint()
    engine.vfs.clean_sandbox()
    finalize_project_rename(engine)

    return success, resolve_artifact_dir(engine, final_dir)
