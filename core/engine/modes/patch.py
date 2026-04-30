"""
engine/modes/patch.py — Patch 模式：微调快速通道
"""
import os
import json
import logging
from typing import Tuple, List, Dict, Any

from core.blackboard import BlackboardState, ProjectStatus
from core.database import append_event, update_project_status
from core.vfs_utils import VfsUtils
from core.ws_broadcaster import global_broadcaster

from core.engine.helpers import (
    persist_blackboard_artifacts,
    resolve_artifact_dir,
    infer_tech_stack,
    infer_focus_endpoints,
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

MAX_RETRIES = int(os.getenv("MAX_RETRIES", 5))


def run_patch_mode(engine, user_requirement: str) -> Tuple[bool, str]:
    """
    微调快速通道：跳过 Spec 生成 + Sandbox 预热。
    Manager 只规划受影响文件，Coder 自动走 fix_with_editor 差量编辑。
    """
    logger.info(f"⚡ Patch Mode 启动: {engine.project_id}")
    global_broadcaster.emit_sync("System", "start_project", f"⚡ Patch Mode: {engine.project_id}")
    delivered_with_warnings = False
    engine.blackboard.set_project_status(ProjectStatus.PLANNING)

    # 直接使用已有目录
    final_dir = os.path.join(_PROJECTS_DIR, engine.project_id)
    engine.blackboard.set_out_dir(final_dir, project_name=engine.project_id)
    engine.vfs = VfsUtils(final_dir)

    # 从 user_requirement 中提取 PM 影响分析
    pm_analysis = ""
    if "【PM 影响分析" in user_requirement:
        parts = user_requirement.split("【PM 影响分析", 1)
        pm_analysis = parts[1]
        _pm_prefix = "（必须采纳，包含精确的修改位置和方向）】\n"
        if pm_analysis.startswith(_pm_prefix):
            pm_analysis = pm_analysis[len(_pm_prefix):]
        elif pm_analysis.startswith("（必须采纳，包含精确的修改位置和方向）】"):
            pm_analysis = pm_analysis[len("（必须采纳，包含精确的修改位置和方向）】"):].lstrip("\n")
        user_req_clean = parts[0].replace("【用户需求】\n", "").strip()
    else:
        user_req_clean = user_requirement

    # TechLead 前置调查 — 优先复用 PM 阶段的缓存
    tech_lead_diagnosis = getattr(engine, '_pm_tech_lead_diagnosis', None)

    if tech_lead_diagnosis:
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
        engine._pm_tech_lead_diagnosis = None  # 一次性消费
    else:
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

    manager = get_manager(engine)
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
    plan = finalize_plan_with_dag(engine, plan, project_spec={}, mode="patch")

    engine.blackboard.set_tasks(plan.get("tasks", []), dag_metadata=plan.get("dag"))

    # 注入 TechLead / PM 诊断
    if tech_lead_diagnosis:
        tl_feedback = (
            f"【TechLead 根因诊断】\n{tech_lead_diagnosis.get('root_cause', '')}\n\n"
            f"【TechLead 修复指令】\n{tech_lead_diagnosis.get('fix_instruction', '')}"
        )
        for task in engine.blackboard.state.tasks:
            task.tech_lead_feedback = tl_feedback
    elif pm_analysis:
        for task in engine.blackboard.state.tasks:
            task.tech_lead_feedback = f"【PM 需求约束】\n{pm_analysis}"

    # 设置轻量 spec
    engine.blackboard.set_project_spec(
        spec={},
        spec_text=(
            f"[Patch Mode] {plan.get('architecture_summary', '微调修改')}\n"
            f"用户需求: {user_requirement}"
        ),
    )

    # 推断 tech_stack
    inferred_tech_stack = infer_tech_stack(None, final_dir)
    if inferred_tech_stack:
        if not engine.blackboard.state.project_spec:
            engine.blackboard.set_project_spec(
                spec={"tech_stack": inferred_tech_stack},
                spec_text=engine.blackboard.state.spec_text or "",
            )
        else:
            engine.blackboard.state.project_spec["tech_stack"] = inferred_tech_stack
            engine.blackboard._touch()
        logger.info(f"🔍 [Patch Mode] 推断 tech_stack: {inferred_tech_stack}")

    append_event("manager", "patch_plan", json.dumps(plan, ensure_ascii=False),
                 project_id=engine.project_id)

    if not engine.blackboard.state.tasks:
        logger.error("💥 [Patch Mode] 未规划任何任务")
        engine.blackboard.set_project_status(ProjectStatus.FAILED)
        global_broadcaster.emit_sync("System", "error", "💥 Patch Mode 规划失败")
        engine.blackboard.record_failure_context("patch_planning_failed", "Patch Mode 未规划任何任务")
        persist_blackboard_artifacts(
            engine,
            resolve_artifact_dir(engine, final_dir),
            failed=True,
        )
        return False, final_dir

    logger.info(f"⚡ [Patch Mode] {len(engine.blackboard.state.tasks)} 个文件需修改")

    # Phase 2: 执行
    engine.blackboard.set_project_status(ProjectStatus.EXECUTING)
    success = phase_execution(engine)

    if success:
        mini_qa_ok, mini_qa_warning = _run_patch_mini_qa_gate(
            engine,
            user_requirement=user_req_clean,
            tech_lead_diagnosis=tech_lead_diagnosis,
            final_dir=final_dir,
            plan=plan,
        )
        delivered_with_warnings = delivered_with_warnings or mini_qa_warning
        success = success and mini_qa_ok

    # Phase 2.5: 集成测试
    from core.integration_manager import IntegrationManager
    tester = IntegrationManager(engine.blackboard, engine.vfs, engine.project_id)
    has_phases = getattr(engine, '_phase_mode', False)
    is_final = getattr(engine, '_is_final_phase', False)

    if success and tester.needs_integration_test(
        phase_mode=has_phases, is_final_phase=is_final
    ):
        patch_focus = infer_focus_endpoints(plan, final_dir)
        integration_ok = tester.run_integration_test(focus_endpoints=patch_focus)
        if not integration_ok:
            if not has_phases:
                replan_ok = tester.retry_with_replan()
                if replan_ok:
                    success = phase_execution(engine)
                    if success:
                        integration_ok = tester.run_integration_test(focus_endpoints=patch_focus)
            else:
                logger.info("📦 Phase 模式跳过 replan")
            if not integration_ok:
                logger.warning("⚠️ [Patch] 集成测试未通过，降级为警告交付")
                global_broadcaster.emit_sync("System", "integration_warning",
                    "⚠️ 集成测试未完全通过，降级为警告交付")
                delivered_with_warnings = True
                success = True
    elif success and has_phases and not is_final:
        startup_ok = tester.run_startup_check()
        if not startup_ok:
            startup_ok = try_startup_self_repair(engine, tester)
        if not startup_ok:
            delivered_with_warnings = True

    if success:
        engine.blackboard.set_project_status(ProjectStatus.COMPLETED)
        update_project_status(engine.project_id, "success")
        logger.info(f"✨ [Patch Mode] 修改完成: {final_dir}")
        global_broadcaster.emit_sync("System", "success",
            f"✨ Patch Mode 修改完成！{final_dir}", {"final_path": final_dir})

        try:
            from tools.git_ops import git_commit
            git_commit(final_dir, f"ASTrea Patch: {user_requirement[:60]}")
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
                f"⚠️ Patch Mode 已交付，但集成测试未完全通过：{final_dir}", {"final_path": final_dir})
            persist_blackboard_artifacts(engine, final_dir)
    else:
        engine.blackboard.set_project_status(ProjectStatus.FAILED)
        engine.blackboard.record_failure_context("patch_execution_failed", "Patch Mode 存在熔断任务")
        persist_blackboard_artifacts(engine, final_dir, failed=True)
        update_project_status(engine.project_id, "failed")
        global_broadcaster.emit_sync("System", "error", "💥 Patch Mode 存在熔断任务")

    # 结算
    phase_settlement(engine, user_requirement, success)

    # 清理
    engine.blackboard.delete_checkpoint()
    if engine.vfs:
        engine.vfs.clean_sandbox()

    return success, final_dir


def _run_patch_mini_qa_gate(engine, user_requirement: str,
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
            project_id=engine.project_id,
        )
        feedback = str(result.get("feedback", "Patch Mini QA 未返回反馈"))

        if result.get("passed"):
            logger.info("✅ [Patch Mini QA] 通过")
            global_broadcaster.emit_sync("System", "patch_mini_qa_pass", "✅ Patch Mini QA 通过")
            return True, False

        if result.get("env_failed"):
            logger.warning("⚠️ [Patch Mini QA] 环境失败，降级为警告: %s", feedback[:300])
            engine.blackboard.record_failure_context(
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
            engine.blackboard.record_failure_context(
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
        tl_guilty = (tech_lead_diagnosis or {}).get("guilty_file", "")
        if tl_guilty and os.path.isfile(os.path.join(final_dir, tl_guilty)):
            target = tl_guilty
        if not target:
            engine.blackboard.record_failure_context(
                "patch_mini_qa_no_target",
                feedback,
                extra_context=result,
            )
            return False, False

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

        engine.blackboard.inject_targeted_fix_task(
            target_file=target,
            description=f"[PATCH_MINI_QA_FIX] 修复 Patch Mini QA 失败: {feedback[:180]}",
            fix_instruction=fix_instruction,
            source_task_id="patch_mini_qa",
        )
        changed_files.append(target)
        repair_success = phase_execution(engine)
        if not repair_success:
            engine.blackboard.record_failure_context(
                "patch_mini_qa_repair_failed",
                "Patch Mini QA 修复任务执行失败",
                extra_context=result,
            )
            return False, False

    return False, False
