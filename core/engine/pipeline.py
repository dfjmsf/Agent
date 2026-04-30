"""
engine/pipeline.py — AstreaEngine 核心执行管道

包含：_phase_planning, _phase_execution, _phase_settlement,
      _two_stage_planning, _finalize_plan_with_dag, _try_startup_self_repair,
      _read_plan_md
"""
import os
import json
import logging
from typing import Optional, Tuple

from core.blackboard import ProjectStatus, TaskStatus
from core.project_observer import ProjectObserver
from core.task_dag_builder import TaskDagBuilder, TaskDagBuildError
from core.database import append_event, update_project_status
from core.ws_broadcaster import global_broadcaster

from core.engine.helpers import (
    resolve_artifact_dir,
    resolve_output_dir,
    persist_blackboard_artifacts,
    record_planning_failure,
    warmup_sandbox,
)

logger = logging.getLogger("AstreaEngine")

# 路径基准
_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_PROJECTS_DIR = os.path.join(_ROOT_DIR, "projects")


# ============================================================
# Phase 1: 规划 (唤醒 Manager)
# ============================================================

def phase_planning(engine, user_requirement: str, out_dir: str = None):
    """唤醒 Manager → 贴规划书 + 任务列表到 Blackboard → Manager 退场"""
    from core.engine.lifecycle import get_manager

    logger.info("📋 Phase 1: 规划阶段...")
    engine.blackboard.set_project_status(ProjectStatus.PLANNING)

    manager = get_manager(engine)

    # Step 0.5: 读取 plan.md（Phase 模式下跳过）
    has_phases = getattr(engine, '_phase_mode', False)
    if has_phases:
        plan_md_content = None
        logger.info("📋 [Phase 模式] 跳过磁盘 plan.md，使用 PM 编译的 Phase 工单作为唯一输入")
    else:
        plan_md_content = read_plan_md(engine)
    if plan_md_content:
        logger.info(f"plan.md 已读取 ({len(plan_md_content)} 字符)，将作为合同约束注入 Manager")

    # Step 0.6: 提取 Playbook 铁律
    playbook_hint = ""
    try:
        from core.playbook_loader import PlaybookLoader
        _pb = PlaybookLoader()
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

    # Step 1: 生成规划书
    project_spec = manager._generate_project_spec(
        user_requirement, plan_md=plan_md_content, playbook_hint=playbook_hint
    )

    if manager.has_spec_parse_failure():
        engine.blackboard.set_project_spec({}, "{}", engine.project_id)
        record_planning_failure(
            engine,
            "spec_parse_failed",
            f"规划书 JSON 解析失败: {manager.last_spec_parse_error}",
            out_dir=out_dir,
            extra_context={
                "raw_spec_response": manager.last_spec_raw_response,
                "validation_warnings": [repr(w) for w in manager.last_spec_warnings],
            },
        )
        return

    # Step 1.5: 从 spec 提取项目名 → 重命名 + sandbox 预热
    project_name = (project_spec.get("project_name", "") or "").replace(" ", "_") if project_spec else ""
    if not project_name:
        project_name = "Unnamed_Project"

    engine.blackboard.state.project_name = project_name

    final_dir = resolve_output_dir(engine, out_dir)
    engine.blackboard.state.out_dir = final_dir
    os.makedirs(final_dir, exist_ok=True)
    from core.vfs_utils import VfsUtils
    engine.vfs = VfsUtils(final_dir)

    # v4.1: Phase 中间阶段降级合同校验
    has_phases = getattr(engine, '_phase_mode', False)
    is_final = getattr(engine, '_is_final_phase', False)

    if manager.has_blocking_spec_validation():
        if has_phases and not is_final:
            logger.warning(
                "⚠️ [Phase 中间阶段] 合同校验有 error 但降级为 warning: %s",
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
            engine.blackboard.set_project_spec(project_spec, spec_text, project_name)
            record_planning_failure(
                engine,
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

    # 启动 sandbox 预热
    warmup_sandbox(engine, project_spec=project_spec)

    # Step 2: 拆解任务
    from core.playbook_loader import PlaybookLoader
    _pb_loader = PlaybookLoader()
    _tech_stack = (project_spec or {}).get("tech_stack", [])
    manager_playbook = _pb_loader.load_for_manager(_tech_stack)

    # Phase 约束
    phase_constraint = getattr(engine, '_current_phase_info', None)
    if phase_constraint:
        logger.info(f"📋 Phase 约束已注入: P{phase_constraint.get('index')}:{phase_constraint.get('name')} "
                    f"[{phase_constraint.get('scope_type', '?')}]")

    # 预估文件数
    estimated_files = ProjectObserver.estimate_file_count(project_spec)
    TWO_STAGE_THRESHOLD = 12

    if estimated_files >= TWO_STAGE_THRESHOLD:
        logger.info(f"🧩 预估 {estimated_files} 文件 ≥ {TWO_STAGE_THRESHOLD}，启动两阶段规划")
        plan = two_stage_planning(
            engine, manager, user_requirement, project_spec, manager_playbook
        )
    else:
        logger.info(f"📋 预估 {estimated_files} 文件 < {TWO_STAGE_THRESHOLD}，单阶段规划")
        complex_hint = ProjectObserver.build_complex_files_hint(project_spec)
        plan = manager.plan_tasks(
            user_requirement, project_spec=project_spec,
            manager_playbook=manager_playbook,
            complex_files_hint=complex_hint,
            plan_md=plan_md_content,
            phase_constraint=phase_constraint,
        )

    # 防御重试
    if not plan.get("tasks"):
        logger.warning(
            "⚠️ plan_tasks 首次返回 0 tasks (plan=%s)，自动重试一次...",
            {k: v for k, v in plan.items() if k != "project_spec"},
        )
        global_broadcaster.emit_sync("Engine", "plan_retry", "⚠️ 任务拆解首次为空，正在重试...")
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
    plan = finalize_plan_with_dag(engine, plan, project_spec=project_spec, mode="create")
    plan["project_spec"] = project_spec

    engine.blackboard.set_project_spec(project_spec, spec_text, project_name)
    engine.blackboard.set_tasks(plan.get("tasks", []), dag_metadata=plan.get("dag"))

    # 记录事件
    append_event("manager", "plan", json.dumps(plan, ensure_ascii=False),
                  project_id=engine.project_id)

    logger.info(f"📋 规划完成: {project_name}, {len(plan.get('tasks', []))} 个子任务")


def read_plan_md(engine) -> Optional[str]:
    """读取项目目录下的 plan.md（由 PlannerLite 在 PM 确认阶段生成）"""
    plan_path = os.path.join(_PROJECTS_DIR, engine.project_id, ".astrea", "plan.md")
    if os.path.isfile(plan_path):
        try:
            with open(plan_path, "r", encoding="utf-8") as f:
                return f.read()
        except Exception as e:
            logger.warning(f"plan.md 读取失败: {e}")
    return None


# ============================================================
# 两阶段规划
# ============================================================

def two_stage_planning(engine, manager, user_requirement: str,
                       project_spec: dict, manager_playbook: str) -> dict:
    """两阶段规划：Stage 1 分模块组 → Stage 2 逐组规划 tasks → 合并"""
    global_broadcaster.emit_sync("Engine", "two_stage_start",
        "🧩 大型项目: 启动两阶段规划...")

    module_groups = manager.plan_module_groups(user_requirement, project_spec)

    if not module_groups:
        logger.warning("⚠️ 两阶段 Stage 1 失败，降级为单阶段规划")
        complex_hint = ProjectObserver.build_complex_files_hint(project_spec)
        return manager.plan_tasks(
            user_requirement, project_spec=project_spec,
            manager_playbook=manager_playbook,
            complex_files_hint=complex_hint
        )

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

    project_name = (project_spec or {}).get("project_name", "AutoGen_Project")
    return {
        "project_name": project_name,
        "architecture_summary": f"两阶段规划: {len(module_groups)} 模块组",
        "tasks": all_tasks,
        "module_groups": module_groups,
    }


# ============================================================
# DAG 归一化
# ============================================================

def finalize_plan_with_dag(
    engine,
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
        logger.warning(f"⚠️ DAG 构建失败，降级为 LLM 原序: {exc}")
        global_broadcaster.emit_sync("Engine", "dag_fallback",
            f"⚠️ DAG 构建失败，降级为 LLM 原始顺序: {exc}")
        fallback_tasks = []
        for idx, t in enumerate(raw_tasks):
            t = dict(t or {})
            t["task_id"] = f"task_{idx + 1}"
            t["dependencies"] = []
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
# Phase 2: 执行 (状态机主循环)
# ============================================================

def phase_execution(engine, group_id: Optional[str] = None) -> bool:
    """
    主循环：基于依赖图调度，逐个执行 TDD。

    Returns:
        True if all tasks DONE, False if any FUSED
    """
    # v5.1: 存储当前 group_id
    engine._last_group_id = group_id
    logger.info("⚙️ Phase 2: 执行阶段%s...", f" (group={group_id})" if group_id else "")

    # 软删除旧轨迹
    from core.settlement import SettlementEngine
    SettlementEngine(engine.project_id, engine.blackboard.state, engine.vfs).archive_old_trajectories()

    task_idx = 0
    total = len([
        t for t in engine.blackboard.state.tasks
        if group_id is None or t.group_id == group_id
    ])

    while True:
        # 优雅退出检查
        if engine._shutdown:
            logger.warning("🛑 Engine 检测到 shutdown 信号，停止执行")
            return False

        # 检查是否全部完成
        if engine.blackboard.all_tasks_done(group_id=group_id):
            if engine.blackboard.has_fused_tasks(group_id=group_id):
                logger.error("💥 存在熔断任务")
                return False
            logger.info("🏆 所有任务均已完成！")
            return True

        # 熔断即停
        if engine.blackboard.has_fused_tasks(group_id=group_id):
            fused_tasks = [t.target_file for t in engine.blackboard.state.tasks
                           if t.status == TaskStatus.FUSED and (group_id is None or t.group_id == group_id)]
            remaining = [t.target_file for t in engine.blackboard.state.tasks
                         if t.status not in (TaskStatus.DONE, TaskStatus.FUSED)
                         and (group_id is None or t.group_id == group_id)]
            logger.error(f"🛑 熔断即停：{fused_tasks} 已熔断，跳过剩余 {len(remaining)} 个任务 {remaining}")
            global_broadcaster.emit_sync("Engine", "project_fused",
                f"项目因 {', '.join(fused_tasks)} 熔断而终止", {})
            return False

        # 依赖图调度
        task = engine.blackboard.get_next_runnable_task(group_id=group_id)
        if task is None:
            logger.error("💥 依赖死锁：无可运行任务但存在未完成任务")
            return False

        task_idx += 1
        logger.info(f"\n[{task_idx}/{total}] ========================")

        # 执行单个任务的 TDD 循环
        from core.task_runner import TaskRunner
        runner = TaskRunner(
            blackboard=engine.blackboard,
            vfs=engine.vfs,
            patcher=engine.patcher,
            project_id=engine.project_id,
            session_id=getattr(engine, "session_id", "local"),
            shutdown_flag=lambda: engine._shutdown,
            phase_mode=getattr(engine, '_phase_mode', False),
        )
        runner.execute(task)

        # Checkpoint
        engine.blackboard.checkpoint()


# ============================================================
# Phase 3: 结算
# ============================================================

def phase_settlement(engine, user_requirement: str, success: bool):
    """后台异步结算：先同步写入实时地图 + 演进脚印，再委托 SettlementEngine"""
    project_dir = engine.blackboard.state.out_dir
    if project_dir and os.path.isdir(project_dir):
        try:
            from core.project_scanner import scan_existing_project
            snapshot = scan_existing_project(
                project_dir,
                blackboard_state=engine.blackboard.state.model_dump(),
            )
            engine.blackboard.state.project_snapshot = snapshot
            file_count = len(snapshot.get("file_tree", []))
            logger.info(f"🗺️ [实时地图] project_snapshot 已刷新 ({file_count} 文件)")
        except Exception as e:
            logger.warning(f"⚠️ [实时地图] project_snapshot 刷新失败: {e}")

    # 演进脚印
    current_round = len(engine.blackboard.state.round_history) + 1
    mode = "create"
    qa_extra = {}
    fc = engine.blackboard.state.failure_context
    if fc:
        qa_extra["qa_passed_count"] = fc.get("passed_count", 0)
        qa_extra["qa_failed_count"] = fc.get("failed_count", 0)
    engine.blackboard.append_round_summary(
        mode=mode,
        user_intent=user_requirement or "",
        current_round=current_round,
        extra=qa_extra if qa_extra.get("qa_passed_count") or qa_extra.get("qa_failed_count") else None,
    )

    # 落盘
    if project_dir:
        persist_blackboard_artifacts(engine, project_dir)

    from core.settlement import SettlementEngine as SE
    settler = SE(engine.project_id, engine.blackboard.state, engine.vfs)
    settler.run_async(user_requirement, success)


# ============================================================
# 启动验证自修复
# ============================================================

def try_startup_self_repair(engine, tester) -> bool:
    """启动验证失败后的自修复闭环。最多修复 1 轮。"""
    from core.integration_manager import IntegrationManager

    failure_ctx = tester._last_failure_context or {}
    stderr = failure_ctx.get("feedback", "")
    if not stderr:
        logger.warning("⚠️ 启动验证无错误信息，跳过自修复")
        return False

    truth_dir = engine.blackboard.state.out_dir or ""
    guilty_file = IntegrationManager.extract_guilty_file_from_stderr(stderr, truth_dir)
    if not guilty_file:
        logger.warning("⚠️ 无法从 traceback 定位出错文件，跳过自修复")
        return False

    guilty_task = engine.blackboard.find_task_by_file(guilty_file)
    if not guilty_task:
        logger.warning(f"⚠️ 出错文件 {guilty_file} 不在任务列表中，跳过自修复")
        return False

    logger.info(f"🔧 [启动自修复] 定位到出错文件: {guilty_file} (task: {guilty_task.task_id})")
    global_broadcaster.emit_sync("System", "self_repair",
        f"🔧 启动验证失败，定位到 {guilty_file}，尝试自修复...")

    fix_instruction = (
        f"【启动验证失败 — 进程崩溃】\n"
        f"错误输出:\n{stderr[:1500]}\n\n"
        f"请修复上述错误，确保项目能正常启动运行。"
    )
    engine.blackboard.reopen_task(
        guilty_task.task_id,
        fix_instruction=fix_instruction,
    )

    repair_success = phase_execution(engine)
    if not repair_success:
        logger.warning("⚠️ 自修复执行失败（任务熔断）")
        return False

    startup_ok = tester.run_startup_check()
    if startup_ok:
        logger.info("✅ 自修复成功，启动验证通过")
        global_broadcaster.emit_sync("System", "self_repair_passed",
            "✅ 自修复成功，启动验证通过")
    else:
        logger.warning("⚠️ 自修复后启动验证仍失败")

    return startup_ok
