"""
engine/modes/continue_mode.py — Continue 模式：基于上一轮 QA 失败上下文做定向继续修复
"""
import os
import json
import logging
from typing import Tuple

from core.blackboard import BlackboardState, ProjectStatus
from core.database import append_event, update_project_status
from core.vfs_utils import VfsUtils
from core.ws_broadcaster import global_broadcaster

from core.engine.helpers import (
    persist_blackboard_artifacts,
    infer_tech_stack,
)
from core.engine.lifecycle import get_manager
from core.engine.pipeline import phase_execution, phase_settlement

logger = logging.getLogger("AstreaEngine")

_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_PROJECTS_DIR = os.path.join(_ROOT_DIR, "projects")


def run_continue_mode(engine, user_requirement: str) -> Tuple[bool, str]:
    """Continue 模式：基于上一轮 QA 失败上下文做定向继续修复。"""
    logger.info(f"🔁 Continue Mode 启动: {engine.project_id}")
    global_broadcaster.emit_sync("System", "start_project", f"🔁 Continue Mode: {engine.project_id}")

    final_dir = os.path.join(_PROJECTS_DIR, engine.project_id)
    engine.vfs = VfsUtils(final_dir)

    loaded_state = BlackboardState.load_from_disk(final_dir)
    if loaded_state:
        loaded_state.project_id = engine.project_id
        loaded_state.out_dir = final_dir
        engine.blackboard._state = loaded_state
    else:
        engine.blackboard.set_out_dir(final_dir, project_name=engine.project_id)

    engine.blackboard.set_user_requirement(user_requirement)
    failure_context = engine.blackboard.state.failure_context or {}
    endpoint_results = failure_context.get("endpoint_results") or []
    failed_endpoints = [
        ep for ep in endpoint_results
        if isinstance(ep, dict) and not ep.get("ok")
    ]

    if not endpoint_results or not failed_endpoints:
        engine.blackboard.set_project_status(ProjectStatus.FAILED)
        engine.blackboard.record_failure_context(
            "continue_context_missing",
            "Continue Mode 缺少上一轮 QA 失败端点上下文，拒绝自动修复",
            extra_context={
                "available_failure_context_keys": sorted(failure_context.keys()),
                "endpoint_results": endpoint_results,
            },
        )
        persist_blackboard_artifacts(engine, final_dir, failed=True)
        update_project_status(engine.project_id, "failed")
        global_broadcaster.emit_sync(
            "System", "error",
            "Continue Mode 缺少上一轮 QA 失败端点上下文，已拒绝自动修复",
        )
        return False, final_dir

    # Phase 5.5: 注入烂账账本到 Manager
    open_issues_text = engine.blackboard.get_open_issues_text()

    # ============================================================
    # TechLead 前置调查
    # ============================================================
    tech_lead_diagnosis = None
    try:
        from agents.tech_lead import TechLeadAgent

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
            if confidence < 0.3:
                logger.warning("⚠️ [Continue Mode] TechLead 置信度过低 (%.2f)，降级为 Manager LLM 诊断", confidence)
                tech_lead_diagnosis = None
        else:
            logger.warning("⚠️ [Continue Mode] TechLead 调查未产出判定，降级为 Manager LLM 诊断")
    except Exception as e:
        logger.warning(f"⚠️ [Continue Mode] TechLead 调查异常: {e}，降级为 Manager LLM 诊断")

    manager = get_manager(engine)
    plan = manager.plan_continue(
        failure_context,
        open_issues_text=open_issues_text,
        tech_lead_diagnosis=tech_lead_diagnosis,
    )
    tasks = plan.get("tasks", []) or []
    if not tasks:
        engine.blackboard.set_project_status(ProjectStatus.FAILED)
        engine.blackboard.record_failure_context(
            "continue_planning_failed",
            "Continue Mode 未能从 repair_scope/failed_files 生成修复任务",
            extra_context=failure_context,
        )
        persist_blackboard_artifacts(engine, final_dir, failed=True)
        update_project_status(engine.project_id, "failed")
        global_broadcaster.emit_sync("System", "error", "Continue Mode 未生成任何修复任务")
        return False, final_dir

    group_id = f"continue:{engine.session_id}"
    normalized_tasks = []
    for idx, task in enumerate(tasks, start=1):
        item = dict(task)
        target_file = item.get("target_file", "")
        item["task_id"] = f"continue_{engine.session_id}_{idx}"
        item["group_id"] = group_id
        item["dependencies"] = []
        item["write_targets"] = item.get("write_targets") or ([target_file] if target_file else [])
        normalized_tasks.append(item)

    engine.blackboard.append_tasks(
        normalized_tasks,
        group_id=group_id,
        dag_metadata={
            "mode": "continue",
            "node_count": len(normalized_tasks),
            "edge_count": 0,
            "source": "failure_context",
        },
    )

    project_spec = engine.blackboard.state.project_spec or {}
    inferred_tech_stack = infer_tech_stack(None, final_dir)
    if inferred_tech_stack and not project_spec.get("tech_stack"):
        project_spec = dict(project_spec)
        project_spec["tech_stack"] = inferred_tech_stack
    engine.blackboard.set_project_spec(
        spec=project_spec,
        spec_text=(
            f"{engine.blackboard.state.spec_text or ''}\n\n"
            f"[Continue Mode]\n{plan.get('architecture_summary', '')}\n"
            f"用户要求: {user_requirement}\n"
            f"修复范围: {', '.join(t.get('target_file', '') for t in normalized_tasks)}"
        ).strip(),
        project_name=engine.project_id,
    )
    append_event(
        "manager", "continue_plan",
        json.dumps(plan, ensure_ascii=False),
        project_id=engine.project_id,
    )

    engine.blackboard.set_project_status(ProjectStatus.EXECUTING)
    success = phase_execution(engine, group_id=group_id)

    delivered_with_warnings = False
    tester = None
    if success:
        from core.integration_manager import IntegrationManager
        tester = IntegrationManager(engine.blackboard, engine.vfs, engine.project_id)
        has_phases = getattr(engine, '_phase_mode', False)
        is_final = getattr(engine, '_is_final_phase', False)
        if tester.needs_integration_test(phase_mode=has_phases, is_final_phase=is_final):
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
                    "System", "integration_warning",
                    "⚠️ Continue Mode 修复已执行，但 QA 仍未完全通过",
                )

    if success:
        if delivered_with_warnings:
            engine.blackboard.set_project_status(ProjectStatus.DELIVERED_WITH_WARNINGS)
            engine.blackboard.record_failure_context(
                "integration_warning",
                "Continue Mode 后集成测试仍未完全通过",
                extra_context=getattr(tester, "_last_failure_context", {}) or {},
            )
            update_project_status(engine.project_id, "warning")
            global_broadcaster.emit_sync(
                "System", "integration_warning",
                f"⚠️ Continue Mode 已交付，但 QA 仍未完全通过：{final_dir}",
                {"final_path": final_dir},
            )
        else:
            engine.blackboard.set_project_status(ProjectStatus.COMPLETED)
            update_project_status(engine.project_id, "success")
            global_broadcaster.emit_sync(
                "System", "success",
                f"✅ Continue Mode 修复完成：{final_dir}",
                {"final_path": final_dir},
            )

        try:
            from tools.git_ops import git_commit
            git_commit(final_dir, f"ASTrea Continue: {user_requirement[:60]}")
        except Exception as e:
            logger.warning(f"⚠️ Git auto-commit 失败（不影响交付）: {e}")
        persist_blackboard_artifacts(engine, final_dir)
    else:
        engine.blackboard.set_project_status(ProjectStatus.FAILED)
        engine.blackboard.record_failure_context(
            "continue_execution_failed",
            "Continue Mode 存在熔断任务",
            extra_context=failure_context,
        )
        persist_blackboard_artifacts(engine, final_dir, failed=True)
        update_project_status(engine.project_id, "failed")
        global_broadcaster.emit_sync("System", "error", "Continue Mode 存在熔断任务")

    phase_settlement(engine, user_requirement, success)
    engine.blackboard.delete_checkpoint()
    if engine.vfs:
        engine.vfs.clean_sandbox()
    return success, final_dir
