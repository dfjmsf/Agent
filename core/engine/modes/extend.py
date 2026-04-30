"""
engine/modes/extend.py — Extend 模式：在已有项目基础上新增完整模块
"""
import os
import json
import logging
from typing import Tuple, List, Dict, Any

from core.blackboard import BlackboardState, ProjectStatus
from core.project_scanner import scan_existing_project
from core.database import append_event, update_project_status
from core.vfs_utils import VfsUtils
from core.ws_broadcaster import global_broadcaster

from core.engine.helpers import (
    persist_blackboard_artifacts,
    infer_tech_stack,
)
from core.engine.lifecycle import get_manager
from core.engine.pipeline import (
    finalize_plan_with_dag,
    phase_execution,
    phase_settlement,
    try_startup_self_repair,
)

logger = logging.getLogger("AstreaEngine")

_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_PROJECTS_DIR = os.path.join(_ROOT_DIR, "projects")


# ============================================================
# Extend 规划归一化
# ============================================================

def _normalize_extend_plan(engine, plan: dict, existing_context: dict) -> dict:
    """对 Extend 规划做代码层归一化，强制落实 new_file / weld 约束。

    注意：不强制注入 weld→new 全量依赖。依赖关系完全由 LLM 声明 +
    DAG builder 确定性规则决定，遗漏的跨文件依赖由 TechLead 运行时 pivot 补偿。
    暴力注入会与 SSR 模板规则产生环依赖，导致 DAG 构建失败。
    """
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
    seen_targets = set()

    for idx, task in enumerate(raw_tasks, start=1):
        if not isinstance(task, dict):
            continue
        target_file = str(task.get("target_file", "")).replace("\\", "/").lstrip("/")
        if not target_file or target_file in seen_targets:
            continue
        seen_targets.add(target_file)

        item = dict(task)
        item["task_id"] = str(item.get("task_id") or f"extend_{engine.session_id}_{idx}")
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

        normalized_tasks.append(item)

    # 不再暴力注入 new_task_ids 到所有 weld 任务。
    # 旧逻辑会让每个 weld 任务依赖所有 new file，与 SSR 模板→路由
    # 确定性边形成环（如 expenses_ops.py ↔ models.py 双向依赖）。
    # LLM 声明的 weld→new 依赖保留不变，由 DAG builder 负责排序。
    # 遗漏的跨文件依赖由 TechLead 运行时 pivot 机制补偿。

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


def _normalize_extend_plan_metadata_only(plan: dict, existing_context: dict) -> dict:
    """DAG 归一化后的轻量元数据刷新。

    仅更新 extend_context 中的 new_files / weld_targets 分类列表，
    不触碰 tasks 的 dependencies（避免覆盖 DAG builder 的确定性排序
    或在降级场景下重新注入已清零的依赖）。
    """
    plan = dict(plan or {})
    existing_context = existing_context or {}
    existing_files = {
        str(path).replace("\\", "/").lstrip("/")
        for path in (existing_context.get("file_tree") or [])
        if path
    }

    extend_context = dict(plan.get("extend_context") or {})
    new_files: List[str] = []
    weld_targets: List[str] = []

    for task in plan.get("tasks") or []:
        if not isinstance(task, dict):
            continue
        target_file = str(task.get("target_file", "")).replace("\\", "/").lstrip("/")
        if not target_file:
            continue
        if target_file in existing_files:
            if target_file not in weld_targets:
                weld_targets.append(target_file)
        else:
            if target_file not in new_files:
                new_files.append(target_file)

    extend_context["new_files"] = new_files
    extend_context["weld_targets"] = weld_targets
    plan["extend_context"] = extend_context
    return plan


def _find_extend_route_conflicts(plan: dict, existing_context: dict) -> List[str]:
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


def _scan_realized_extend_route_conflicts(project_dir: str, plan: dict,
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


# ============================================================
# Extend Mode 主入口
# ============================================================

def run_extend_mode(engine, user_requirement: str) -> Tuple[bool, str]:
    """Extend 模式：在已有项目基础上新增完整模块。"""
    logger.info(f"🧩 Extend Mode 启动: {engine.project_id}")
    global_broadcaster.emit_sync("System", "start_project", f"🧩 Extend Mode: {engine.project_id}")

    final_dir = os.path.join(_PROJECTS_DIR, engine.project_id)
    if not os.path.isdir(final_dir):
        engine.blackboard.set_project_status(ProjectStatus.FAILED)
        engine.blackboard.record_failure_context(
            "extend_project_missing",
            "Extend Mode 只能用于已有项目，目标项目目录不存在",
        )
        persist_blackboard_artifacts(engine, final_dir, failed=True)
        update_project_status(engine.project_id, "failed")
        global_broadcaster.emit_sync("System", "error", "Extend Mode 只能用于已有项目")
        return False, final_dir

    engine.vfs = VfsUtils(final_dir)
    loaded_state = BlackboardState.load_from_disk(final_dir)
    if loaded_state:
        loaded_state.project_id = engine.project_id
        loaded_state.out_dir = final_dir
        engine.blackboard._state = loaded_state
    else:
        engine.blackboard.set_out_dir(final_dir, project_name=engine.project_id)
    engine.blackboard.state.project_id = engine.project_id
    engine.blackboard.state.out_dir = final_dir
    engine.blackboard.set_user_requirement(user_requirement)
    engine.blackboard.set_project_status(ProjectStatus.PLANNING)

    try:
        from tools.git_ops import git_commit
        if not git_commit(final_dir, f"Auto-backup before extend [Round {engine.session_id}]"):
            raise RuntimeError("git auto-backup failed")
    except Exception as e:
        logger.error(f"❌ [Extend] Git 安全快照失败: {e}")
        engine.blackboard.set_project_status(ProjectStatus.FAILED)
        engine.blackboard.record_failure_context(
            "extend_backup_failed",
            "Extend Mode 执行前 Git 安全快照失败",
            extra_context={"error": str(e)},
        )
        persist_blackboard_artifacts(engine, final_dir, failed=True)
        update_project_status(engine.project_id, "failed")
        global_broadcaster.emit_sync("System", "error", "Extend Mode 启动失败：Git 安全快照失败")
        return False, final_dir

    existing_context = scan_existing_project(
        final_dir,
        blackboard_state=engine.blackboard.state.model_dump(),
    )
    inferred_tech_stack_list = existing_context.get("tech_stack") or infer_tech_stack(None, final_dir)

    from core.playbook_loader import PlaybookLoader
    manager_playbook = PlaybookLoader().load_for_manager(inferred_tech_stack_list or [])
    manager = get_manager(engine)

    # Phase 5.5: 注入烂账账本
    open_issues_text = engine.blackboard.get_open_issues_text()

    plan = manager.plan_extend(
        user_requirement, existing_context,
        manager_playbook=manager_playbook,
        replan_feedback=None,
        open_issues_text=open_issues_text,
    )
    plan = _normalize_extend_plan(engine, plan, existing_context)

    route_conflicts = _find_extend_route_conflicts(plan, existing_context)
    if route_conflicts:
        replan_feedback = {
            "reason": "route_conflict",
            "conflicts": route_conflicts,
            "route_blacklist": plan.get("extend_context", {}).get("route_blacklist", []),
        }
        plan = manager.plan_extend(
            user_requirement, existing_context,
            manager_playbook=manager_playbook,
            replan_feedback=replan_feedback,
            open_issues_text=open_issues_text,
        )
        plan = _normalize_extend_plan(engine, plan, existing_context)
        route_conflicts = _find_extend_route_conflicts(plan, existing_context)

    tasks = plan.get("tasks") or []
    if route_conflicts:
        engine.blackboard.set_project_status(ProjectStatus.FAILED)
        engine.blackboard.record_failure_context(
            "extend_route_conflict",
            "新增模块路由与已有路由冲突，已拒绝执行",
            extra_context={
                "conflicts": route_conflicts,
                "existing_routes": existing_context.get("existing_routes", []),
            },
        )
        persist_blackboard_artifacts(engine, final_dir, failed=True)
        update_project_status(engine.project_id, "failed")
        global_broadcaster.emit_sync(
            "System", "error",
            f"Extend Mode 路由冲突：{', '.join(route_conflicts)}",
        )
        return False, final_dir

    if not tasks:
        engine.blackboard.set_project_status(ProjectStatus.FAILED)
        engine.blackboard.record_failure_context(
            "extend_planning_failed",
            "Extend Mode 未生成任何任务",
            extra_context={"existing_context": existing_context},
        )
        persist_blackboard_artifacts(engine, final_dir, failed=True)
        update_project_status(engine.project_id, "failed")
        global_broadcaster.emit_sync("System", "error", "Extend Mode 未生成任何任务")
        return False, final_dir

    current_spec = dict(engine.blackboard.state.project_spec or {})
    if inferred_tech_stack_list and not current_spec.get("tech_stack"):
        current_spec["tech_stack"] = inferred_tech_stack_list

    plan = finalize_plan_with_dag(engine, plan, project_spec=current_spec, mode="extend")
    # DAG 归一化后只刷新 metadata，不重新注入依赖
    # （二次全量归一化会覆盖 DAG 的确定性排序或在降级场景下重新制造环依赖）
    plan = _normalize_extend_plan_metadata_only(plan, existing_context)
    tasks = plan.get("tasks") or []
    if not tasks:
        engine.blackboard.set_project_status(ProjectStatus.FAILED)
        engine.blackboard.record_failure_context(
            "extend_dag_empty",
            "Extend Mode 经 DAG 归一化后无可执行任务",
        )
        persist_blackboard_artifacts(engine, final_dir, failed=True)
        update_project_status(engine.project_id, "failed")
        global_broadcaster.emit_sync("System", "error", "Extend Mode DAG 归一化后无任务")
        return False, final_dir

    group_id = f"extend:{engine.session_id}"
    normalized_tasks = []
    for task in tasks:
        item = dict(task)
        target_file = str(item.get("target_file", "")).replace("\\", "/").lstrip("/")
        item["target_file"] = target_file
        item["group_id"] = group_id
        item["write_targets"] = item.get("write_targets") or ([target_file] if target_file else [])
        normalized_tasks.append(item)

    engine.blackboard.append_tasks(
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
        f"{engine.blackboard.state.spec_text or ''}\n\n"
        f"[Extend Mode]\n"
        f"{plan.get('architecture_summary', '新增模块')}\n"
        f"用户要求: {user_requirement}\n"
        f"新增文件: {', '.join(extend_context.get('new_files', [])) or '无'}\n"
        f"焊接文件: {', '.join(extend_context.get('weld_targets', [])) or '无'}"
    ).strip()
    engine.blackboard.set_project_spec(
        spec=current_spec,
        spec_text=extend_spec_text,
        project_name=engine.project_id,
    )
    append_event(
        "manager", "extend_plan",
        json.dumps(plan, ensure_ascii=False),
        project_id=engine.project_id,
    )

    engine.blackboard.set_project_status(ProjectStatus.EXECUTING)
    success = phase_execution(engine, group_id=group_id)
    delivered_with_warnings = False
    tester = None

    if success:
        realized_conflicts = _scan_realized_extend_route_conflicts(final_dir, plan, existing_context)
        if realized_conflicts:
            success = False
            engine.blackboard.set_project_status(ProjectStatus.FAILED)
            engine.blackboard.record_failure_context(
                "extend_route_conflict_realized",
                "新增模块实现后的真实路由与已有路由冲突",
                extra_context={
                    "conflicts": realized_conflicts,
                    "new_files": extend_context.get("new_files", []),
                },
            )
            global_broadcaster.emit_sync(
                "System", "error",
                f"Extend Mode 执行后检测到路由冲突：{', '.join(realized_conflicts)}",
            )

    if success:
        from core.integration_manager import IntegrationManager
        tester = IntegrationManager(engine.blackboard, engine.vfs, engine.project_id)
        has_phases = getattr(engine, '_phase_mode', False)
        is_final = getattr(engine, '_is_final_phase', False)
        if tester.needs_integration_test(phase_mode=has_phases, is_final_phase=is_final):
            integration_ok = tester.run_integration_test()
            if not integration_ok:
                delivered_with_warnings = True
                success = True
                global_broadcaster.emit_sync(
                    "System", "integration_warning",
                    "⚠️ Extend Mode 已完成，但集成测试仍未完全通过",
                )
        elif has_phases and not is_final:
            startup_ok = tester.run_startup_check()
            if not startup_ok:
                startup_ok = try_startup_self_repair(engine, tester)
            if not startup_ok:
                delivered_with_warnings = True

    if success:
        if delivered_with_warnings:
            engine.blackboard.set_project_status(ProjectStatus.DELIVERED_WITH_WARNINGS)
            engine.blackboard.record_failure_context(
                "integration_warning",
                "Extend Mode 后集成测试未完全通过",
                extra_context=getattr(tester, "_last_failure_context", {}) or {},
            )
            update_project_status(engine.project_id, "warning")
            global_broadcaster.emit_sync(
                "System", "integration_warning",
                f"⚠️ Extend Mode 已交付，但 QA 仍未完全通过：{final_dir}",
                {"final_path": final_dir},
            )
        else:
            engine.blackboard.set_project_status(ProjectStatus.COMPLETED)
            update_project_status(engine.project_id, "success")
            global_broadcaster.emit_sync(
                "System", "success",
                f"✅ Extend Mode 构建完成：{final_dir}",
                {"final_path": final_dir},
            )

        global_broadcaster.emit_sync(
            "System", "extend_complete",
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
        persist_blackboard_artifacts(engine, final_dir)
    else:
        if engine.blackboard.state.project_status != ProjectStatus.FAILED:
            engine.blackboard.set_project_status(ProjectStatus.FAILED)
            engine.blackboard.record_failure_context(
                "extend_execution_failed",
                "Extend Mode 存在熔断任务或执行后校验失败",
                extra_context=plan.get("extend_context", {}),
            )
        persist_blackboard_artifacts(engine, final_dir, failed=True)
        update_project_status(engine.project_id, "failed")
        global_broadcaster.emit_sync("System", "error", "Extend Mode 执行失败")

    phase_settlement(engine, user_requirement, success)
    engine.blackboard.delete_checkpoint()
    if engine.vfs:
        engine.vfs.clean_sandbox()
    return success, final_dir
