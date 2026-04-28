"""
SettlementEngine — 后台异步结算引擎

从 engine.py 拆解而来 (Engine A-1 Phase A)。
职责：
- 经验提炼（Synthesizer + Auditor 并行）
- AMC 分数结算
- 旧轨迹归档

设计原则：
- 仅通过 Blackboard 和 VFS 接口与外部交互
- 所有计算在后台线程中执行，不阻塞主流程
"""
import logging
import threading

from core.blackboard import BlackboardState, TaskStatus
from core.database import (
    get_recalled_memory_union, settle_memory_scores,
    get_global_round, tick_global_round,
    TaskTrajectory, ScopedSession,
)

logger = logging.getLogger("SettlementEngine")


class SettlementEngine:
    """后台异步结算：Synthesizer + Auditor + AMC"""

    def __init__(self, project_id: str, blackboard_state: BlackboardState, vfs):
        self.project_id = project_id
        self.bb_state = blackboard_state
        self.vfs = vfs

    def archive_old_trajectories(self):
        """软删除旧轨迹记录"""
        try:
            session = ScopedSession()
            try:
                stale = session.query(TaskTrajectory).filter(
                    TaskTrajectory.project_id == self.project_id,
                    TaskTrajectory.is_synthesized == False,
                ).update({"is_synthesized": True})
                session.commit()
                if stale:
                    logger.info(f"🧹 旧轨迹已归档: {stale} 条")
            except Exception as e:
                logger.warning(f"⚠️ 旧轨迹归档失败（已 rollback）: {e}")
                session.rollback()
            finally:
                ScopedSession.remove()
        except Exception as e:
            logger.warning(f"⚠️ 旧轨迹归档 session 创建失败: {e}")

    def run_async(self, user_requirement: str, success: bool):
        """启动后台异步结算线程"""
        logger.info("🧠 Phase 3: 后台异步结算...")
        from core.ws_broadcaster import global_broadcaster
        global_broadcaster.emit_sync("System", "info",
            "🧠 正在执行经验提炼与 AMC 结算...")

        # 捕获引用给后台线程用
        project_id = self.project_id
        bb_state = self.bb_state
        vfs_ref = self.vfs

        def _bg_settlement():
            try:
                from agents.synthesizer import SynthesizerAgent
                from agents.auditor import AuditorAgent
                from concurrent.futures import ThreadPoolExecutor, as_completed

                synthesizer = SynthesizerAgent(project_id=project_id)

                # 构建里程碑 — 从真理区读取最终代码
                milestones_list = []
                for task in bb_state.tasks:
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

                # ── 并行化：Synthesizer + Auditor 同时跑 ──

                def _run_synthesizer():
                    """Synthesizer: 所有 task 并行提炼"""
                    plan_dict = bb_state.project_spec or {}

                    def _synth_one(item):
                        tf = item["task"].get("target_file", "")
                        try:
                            if item["success"]:
                                synthesizer.synthesize_success(
                                    item["milestones"], user_requirement, plan_dict,
                                    target_file=tf)
                            elif item["task"].get("task_id"):
                                synthesizer.synthesize_failure(
                                    item["milestones"], user_requirement, plan_dict,
                                    target_file=tf)
                        except Exception as e:
                            logger.warning(f"⚠️ Synthesizer 单 task 异常 ({tf}): {e}")

                    with ThreadPoolExecutor(max_workers=min(len(milestones_list), 4)) as pool:
                        list(pool.map(_synth_one, milestones_list))
                    logger.info("✨ [后台] Synthesizer 知识提炼完毕")

                def _run_auditor():
                    """Auditor: 所有 task 的审计并行执行"""
                    from core.database import ScopedSession as _ScopedSession, Memory

                    auditor = AuditorAgent()
                    all_used_ids, all_ignored_ids = set(), set()

                    # 收集需要审计的 task
                    audit_tasks = []
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
                        audit_tasks.append((tid, task_memory_ids, task_final_code))

                    if not audit_tasks:
                        return set(), set()

                    def _audit_one(args):
                        tid, memory_ids, final_code = args
                        memories_to_audit = [{"id": mid, "content": ""} for mid in memory_ids]
                        session = _ScopedSession()
                        try:
                            for m in memories_to_audit:
                                if m["id"] > 0:
                                    row = session.query(Memory).filter(Memory.id == m["id"]).first()
                                    if row:
                                        m["content"] = row.content[:300]
                        finally:
                            _ScopedSession.remove()

                        result = auditor.audit(final_code, memories_to_audit)
                        used, ignored = set(), set()
                        for r in result.get("results", []):
                            mid = r.get("memory_id", -1)
                            if mid > 0:
                                (used if r.get("adopted") else ignored).add(mid)
                        return used, ignored

                    # 并行审计所有 task
                    with ThreadPoolExecutor(max_workers=min(len(audit_tasks), 4)) as pool:
                        futures = [pool.submit(_audit_one, t) for t in audit_tasks]
                        for fut in as_completed(futures):
                            try:
                                used, ignored = fut.result()
                                all_used_ids |= used
                                all_ignored_ids |= ignored
                            except Exception as e:
                                logger.warning(f"⚠️ 单 task 审计异常: {e}")

                    return all_used_ids, all_ignored_ids

                # Synthesizer 和 Auditor 并行执行
                with ThreadPoolExecutor(max_workers=2) as pool:
                    synth_future = pool.submit(_run_synthesizer)
                    audit_future = pool.submit(_run_auditor)

                    # 等待 Synthesizer 完成（不需要返回值）
                    synth_future.result()

                    # 等待 Auditor 完成并做 AMC 结算
                    all_used_ids, all_ignored_ids = audit_future.result()
                    if all_used_ids or all_ignored_ids:
                        settle_memory_scores(all_used_ids, all_ignored_ids, get_global_round())
                        logger.info(f"✨ [后台] AMC 结算完成: 功臣{len(all_used_ids)} 陪跑{len(all_ignored_ids)}")
                    tick_global_round()

            except Exception as e:
                logger.error(f"❌ [后台] 结算异常: {e}")
                import traceback
                traceback.print_exc()

        threading.Thread(target=_bg_settlement, daemon=True).start()
