"""
TaskRunner — 单任务 TDD 执行器

从 engine.py 拆解而来 (Engine A-1 Phase C)。
职责：
- 单个 TaskItem 的完整 TDD 生命周期
- Coder / Reviewer / TechLead Agent 唤醒与交互
- CodePatcher 缝合
- 骨架先行（skeleton-first）
- 跨文件冲突仲裁
- 变动率检查（Patch Mode 质量守卫）

设计原则：
- 不持有独立状态，所有状态通过 Blackboard 读写
- 通过依赖注入获取 blackboard, vfs, patcher 等外部资源
"""
import os
import json
import logging
import difflib
import ast
from typing import Optional, Tuple

from core.blackboard import Blackboard, TaskItem, TaskStatus
from core.code_patcher import CodePatcher, PatchFailedError, extract_xml_files
from core.techlead_scope import build_scope_from_cross_file_signal, parse_cross_file_signal
from core.ws_broadcaster import global_broadcaster
from core.llm_client import default_llm
from core.database import append_event, insert_trajectory, finalize_trajectory

logger = logging.getLogger("TaskRunner")

MAX_RETRIES = int(os.getenv("MAX_RETRIES", 5))
MAX_RETRIES_PHASE = int(os.getenv("MAX_RETRIES_PHASE", 5))  # v4.1: 2→5, 低上限导致 L0.15/L0.2 乒乓必然熔断


class TaskRunner:
    """单任务 TDD 状态机执行器"""

    def __init__(self, blackboard: Blackboard, vfs, patcher: CodePatcher,
                 project_id: str, session_id: str = "local",
                 shutdown_flag=None, phase_mode: bool = False):
        self.blackboard = blackboard
        self.vfs = vfs
        self.patcher = patcher
        self.project_id = project_id
        self.session_id = session_id
        # shutdown_flag 是一个 callable，返回 bool（来自 Engine._shutdown）
        self._is_shutdown = shutdown_flag or (lambda: False)
        # v4.0: Phase 模式下降低 retry 上限
        self._max_retries = MAX_RETRIES_PHASE if phase_mode else MAX_RETRIES

        # Agent 延迟导入
        self._coder = None
        self._reviewer = None

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
    # 对外唯一入口
    # ============================================================

    def execute(self, task: TaskItem):
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

        # === Phase 0: skeleton 验收闸门 ===
        if task.sub_tasks and task.current_sub_task_index == 0:
            skeleton_sub = next((s for s in task.sub_tasks if s.get("type") == "skeleton"), None)
            if skeleton_sub:
                logger.info(f"🧱 [{task.task_id}] skeleton 先行: {skeleton_sub.get('description', '')}")
                global_broadcaster.emit_sync(
                    "Engine", "task_skeleton",
                    f"skeleton 生成: {task.target_file}", {"task_id": task.task_id}
                )
                if not self._run_skeleton_stage(task):
                    return

        # 如果 TechLead 注入了修复指令，作为初始 feedback
        feedback = task.tech_lead_feedback
        if feedback:
            logger.info(f"⚖️ [{task.task_id}] 使用 TechLead 修复指令: {feedback[:80]}...")
            task.tech_lead_feedback = None

        if task.sub_tasks and task.current_sub_task_index == 0:
            logger.info(f"↩️ [{task.task_id}] 回到 skeleton 阶段")
            if not self._run_skeleton_stage(task, feedback):
                return
            feedback = None

        while True:
            if self._is_shutdown():
                logger.warning("🛑 Engine 检测到 shutdown 信号，任务中断")
                return

            # 熔断检测
            if task.sub_tasks and task.current_sub_task_index == 0:
                logger.info(f"↩️ [{task.task_id}] 重新进入 skeleton 阶段")
                if not self._run_skeleton_stage(task, feedback):
                    return
                feedback = None

            if task.retry_count >= self._max_retries:
                logger.error(f"🚨 [熔断] 任务 {task.task_id} 连续失败 {self._max_retries} 次")
                self.blackboard.update_task_status(task.task_id, TaskStatus.FUSED)
                # Phase 5.5: 烂账账本 — 记录熔断
                fuse_summary = f"任务 {task.task_id} 熔断 ({task.target_file})"
                if task.error_logs:
                    fuse_summary += f": {task.error_logs[-1][:100]}"
                current_round = len(self.blackboard.state.round_history) + 1
                self.blackboard.upsert_issue(
                    category="fuse",
                    summary=fuse_summary,
                    related_files=[task.target_file],
                    current_round=current_round,
                )
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
            xml_files = extract_xml_files(code_output)
            has_real_xml = any(xf["path"] for xf in xml_files) if xml_files else False

            if has_real_xml:
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
                action = "rewrite"
                if xml_files and xml_files[0]["content"]:
                    draft = xml_files[0]["content"]
                else:
                    draft = code_output.strip()

            coder_mode = self._get_last_coder_mode()
            fill_guard_pass, fill_guard_feedback = self._validate_fill_rewrite_draft(
                task, draft, coder_mode
            )
            if not fill_guard_pass:
                logger.warning(f"⚠️ [{task.task_id}] {fill_guard_feedback}")
                task.log_error(fill_guard_feedback)
                self.blackboard.update_task_status(task.task_id, TaskStatus.PATCH_FAILED)
                self.blackboard.increment_retry(task.task_id)
                feedback = fill_guard_feedback
                continue

            if self._should_reject_force_modify_draft(task, action, coder_mode, draft):
                reason = (
                    f"[MODIFY_GUARD] {task.target_file}: weld/modify 任务禁止使用 "
                    f"Coder 降级覆写产物 (action={action}, coder_mode={coder_mode})。"
                    "请使用 edit_file 的 start_line/end_line 做局部编辑；"
                    "若确需重写前端结构，请使用 start_line=1/end_line=EOF 的受控整文件替换。"
                )
                logger.warning(f"⚠️ [{task.task_id}] {reason}")
                task.log_error(reason)
                self.blackboard.update_task_status(task.task_id, TaskStatus.PATCH_FAILED)
                self.blackboard.increment_retry(task.task_id)
                feedback = reason
                continue

            # 提交草稿到 Blackboard
            self.blackboard.submit_draft(task.task_id, draft, action)

            # (2) CodePatcher 缝合
            try:
                vfs_code = self.vfs.read_truth(task.target_file) if self.vfs else None
                merged = self.patcher.patch(vfs_code, draft, action)
                task.log_action(f"CodePatcher 缝合成功 (action={action})")
            except PatchFailedError as e:
                task.log_error(f"CodePatcher 缝合失败: {e.reason}")
                self.blackboard.update_task_status(task.task_id, TaskStatus.PATCH_FAILED)
                self.blackboard.increment_retry(task.task_id)
                feedback = f"代码缝合失败: {e.reason}\n请检查你的 SEARCH 块是否与原文件一致。"
                logger.warning(f"⚠️ [{task.task_id}] 缝合失败，省下 Reviewer Token")
                continue

            # (3) 写入 Sandbox + 唤醒 Reviewer
            self.blackboard.update_task_status(task.task_id, TaskStatus.PENDING_REVIEW)

            if self.vfs:
                self.vfs.write_to_sandbox({task.target_file: merged})

            self.blackboard.update_task_status(task.task_id, TaskStatus.REVIEWING)
            is_pass, reviewer_feedback = self._invoke_reviewer(task, merged)

            # P2: SOFT_PASS 累计检测 — 连续 3 次软放行降级为 FAIL
            if is_pass and "[SOFT_PASS]" in reviewer_feedback:
                soft_pass_count = sum(
                    1 for log in (task.action_trajectory or [])
                    if "[SOFT_PASS]" in str(log)
                )
                task.log_action(f"[SOFT_PASS] L1 软放行 (累计 {soft_pass_count + 1})")
                if soft_pass_count >= 2:
                    is_pass = False
                    reviewer_feedback = (
                        f"[P2 连续软放行降级] 该文件已连续 {soft_pass_count + 1} 次因 LLM 异常被放行，"
                        f"降级为 FAIL 以防止缺陷累积。原始反馈: {reviewer_feedback}"
                    )
                    logger.warning(f"⚠️ [{task.task_id}] 连续 SOFT_PASS → 降级 FAIL")

            # P3: 连续相同 L0 失败降级 — 同一 L0 错误连续 3 次重复，
            # 说明 Coder 无法修复该检查项（可能是规则过严），降级为 warning 放行
            _SAME_L0_THRESHOLD = 3
            if not is_pass and "[L0." in reviewer_feedback:
                # 提取最近 N 条 Reviewer 驳回记录
                _recent_l0_errors = [
                    log for log in (task.error_logs or [])[-_SAME_L0_THRESHOLD:]
                    if isinstance(log, str) and log.startswith("Reviewer 驳回:")
                ]
                if (len(_recent_l0_errors) >= _SAME_L0_THRESHOLD
                        and len(set(_recent_l0_errors)) == 1):
                    logger.warning(
                        f"⚠️ [{task.task_id}] 连续 {_SAME_L0_THRESHOLD} 次相同 L0 失败，"
                        f"降级为 warning 放行: {reviewer_feedback[:100]}"
                    )
                    is_pass = True
                    reviewer_feedback = (
                        f"[L0_DEGRADED] 连续 {_SAME_L0_THRESHOLD} 次相同 L0 失败，"
                        f"降级为 warning 放行。原始错误: {reviewer_feedback[:300]}"
                    )

            # 记录 TDD 轮次事件
            self._record_tdd_event(task, merged, is_pass, reviewer_feedback)

            if is_pass:
                # PASSED → commit 到真理区 → DONE
                self.blackboard.update_task_status(task.task_id, TaskStatus.PASSED)

                if self.vfs:
                    self.vfs.commit_to_truth(task.target_file, merged)
                    self.blackboard.update_global_snapshot(
                        task.target_file, self.vfs.truth_dir
                    )

                # 更新文件树
                if self.vfs:
                    from core.database import upsert_file_tree
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

                # 逐任务 Git commit + Ledger 记录
                git_hash = None
                if self.vfs:
                    try:
                        from tools.git_ops import git_commit, get_head_hash
                        commit_msg = f"[Round {self.session_id}] [{task.task_id}] {task.target_file}: {task.description[:60]}"
                        git_commit(self.vfs.truth_dir, commit_msg)
                        git_hash = get_head_hash(self.vfs.truth_dir)
                    except Exception as e:
                        logger.warning(f"⚠️ 逐任务 Git commit 失败: {e}")

                self.blackboard.record_completed_task(
                    task_id=task.task_id,
                    target_file=task.target_file,
                    description=task.description,
                    git_hash=git_hash,
                )

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
                if self._should_rollback_fill_to_skeleton(task, reviewer_feedback):
                    feedback = self._rollback_fill_to_skeleton(task, reviewer_feedback)
                    continue

                self.blackboard.increment_retry(task.task_id)
                feedback = reviewer_feedback

                pivot_result = self._handle_cross_file_signal(
                    task,
                    reviewer_feedback,
                    stage="review",
                    pivot_source="task_runner_review",
                )
                if pivot_result["pause"]:
                    return
                if pivot_result["feedback"]:
                    feedback = pivot_result["feedback"]

                logger.warning(f"🔨 [{task.task_id}] 审查未通过 "
                               f"(retry {task.retry_count}/{self._max_retries})")

    # ============================================================
    # Agent 调用
    # ============================================================

    @staticmethod
    def _is_fill_mode_task(task: TaskItem) -> bool:
        return bool(task.sub_tasks) and task.current_sub_task_index >= 1

    def _get_last_coder_mode(self) -> str:
        coder = self._coder
        return str(getattr(coder, "_last_coder_mode", "unknown") or "unknown")

    @staticmethod
    def _is_force_modify_task(task: TaskItem) -> bool:
        return task.task_type == "weld" or task.draft_action == "modify"

    @staticmethod
    def _allows_controlled_full_file_modify(task: TaskItem, action: str, draft: str) -> bool:
        ext = os.path.splitext(task.target_file)[1].lower()
        if ext not in {'.html', '.htm', '.vue', '.css', '.scss', '.less'}:
            return False
        if action not in {"rewrite", "create"}:
            return False
        if not draft or len(draft.strip()) < 20:
            return False

        text = f"{task.description or ''}\n{task.tech_lead_feedback or ''}".lower()
        markers = (
            "重写", "重构", "重新设计", "整体", "布局", "结构", "html结构",
            "dom", "容器", "区域", "rewrite", "rebuild", "restructure", "layout",
        )
        if not any(marker in text for marker in markers):
            return False

        if ext in {'.html', '.htm'}:
            lowered = draft.lower()
            return "<html" in lowered or "<body" in lowered or "<!doctype" in lowered
        if ext == '.vue':
            lowered = draft.lower()
            return "<template" in lowered or "<script" in lowered
        return True

    @staticmethod
    def _should_disable_ast_slice_for_feedback(feedback: str) -> bool:
        text = feedback or ""
        markers = (
            "L0.0",
            "骨架残留",
            "L0.C1",
            "路由未注册",
            "L0.VUE",
            "Invalid end tag",
            "At least one <template> or <script>",
            "标签不闭合",
            "SFC结构缺失",
            "CSS花括号错误",
            "Unexpected }",
            # v4.4: Scope Expansion 反馈信号 — Reviewer 报告缺少函数/未定义时，
            # 说明 Coder 被 AST 切片困住无法新增定义，必须清除切片
            "缺少函数",
            "缺少方法",
            "函数未定义",
            "未定义函数",
            "未实现",
            "missing function",
            "undefined function",
            "not defined",
            "is not defined",
            "新增函数",
            "添加函数",
            "需要实现",
        )
        return any(marker in text for marker in markers)

    @staticmethod
    def _python_top_level_functions(code: str) -> Optional[set]:
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return None
        return {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

    @staticmethod
    def _python_ellipsis_functions(code: str) -> Optional[list]:
        try:
            tree = ast.parse(code)
        except SyntaxError:
            return None

        residual = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = list(node.body)
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                body = body[1:]
            if len(body) == 1:
                stmt = body[0]
                if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                    if stmt.value.value is ...:
                        residual.append(node.name)
        return residual

    def _validate_fill_rewrite_draft(self, task: TaskItem, draft: str,
                                     coder_mode: str) -> Tuple[bool, str]:
        if not self._is_fill_mode_task(task):
            return True, ""
        if not task.target_file.endswith(".py"):
            return True, ""
        if coder_mode not in {"fill_retry_rewrite", "fallback_rewrite"}:
            return True, ""

        old_code = self.vfs.read_truth(task.target_file) if self.vfs else ""
        if not old_code or not draft:
            return True, ""

        old_funcs = self._python_top_level_functions(old_code)
        new_funcs = self._python_top_level_functions(draft)
        if old_funcs is None or new_funcs is None:
            return True, ""

        missing = sorted(old_funcs - new_funcs)
        if missing:
            return False, (
                f"[FILL_GUARD 结构漂移] {task.target_file}: 全文件 fill 删除或重命名了既有函数: "
                f"{missing}。请保留所有骨架函数名，只补全函数体。"
            )

        residual = self._python_ellipsis_functions(draft)
        if residual:
            return False, (
                f"[FILL_GUARD 骨架残留] {task.target_file}: 全文件 fill 后仍有 `...` 占位函数: "
                f"{residual}。请一次性补完所有占位函数。"
            )

        return True, ""

    @classmethod
    def _should_reject_force_modify_draft(cls, task: TaskItem,
                                          action: str,
                                          coder_mode: str,
                                          draft: str = "") -> bool:
        if not cls._is_force_modify_task(task):
            return False

        if action == "modify":
            return False

        # Editor/Slice 会返回完整合并后的代码，TaskRunner 兼容为 rewrite。
        # 其他 rewrite/create 代表 Coder 绕过了局部编辑约束，必须重试。
        if action == "rewrite" and coder_mode in {"editor", "slice"}:
            return False

        if (
            coder_mode == "controlled_full_rewrite"
            and cls._allows_controlled_full_file_modify(task, action, draft)
        ):
            logger.info(
                "✅ [MODIFY_GUARD] 受控整文件修改放行: %s (action=%s)",
                task.target_file,
                action,
            )
            return False

        return True

    def _should_rollback_fill_to_skeleton(self, task: TaskItem, reviewer_feedback: str) -> bool:
        """仅在明确的结构性失败上触发 fill -> skeleton 回退。"""
        if not self._is_fill_mode_task(task):
            return False

        feedback = (reviewer_feedback or "").lower()
        rollback_markers = (
            # 仅保留真正的 skeleton 结构性缺陷（函数签名/导入/语法层面）
            "[l0.2",            # 导入缺失 → skeleton 漏了 import
            "[l0.5",            # 语法错误 → skeleton 自身有语法问题
            "结构缺失",
            "缺少规划书中定义的",
            "路由致命错误",
            # 注意：[l0.0] (骨架残留) 和 [l0.c1] (路由未注册) 是 fill 阶段的问题，
            # 不应触发 skeleton 回退——skeleton 本来就是 `...` 占位。
        )
        if any(marker in feedback for marker in rollback_markers):
            return True

        l1_hard_markers = (
            "致命缺陷",
            "接口签名不匹配",
            "签名不匹配",
            "运行时崩溃",
            "未导入",
            "未定义",
            "nameerror",
            "attributeerror",
        )
        return (
            task.retry_count >= 1
            and "[l1 合约审计]" in feedback
            and any(marker in feedback for marker in l1_hard_markers)
        )

    def _rollback_fill_to_skeleton(self, task: TaskItem, reviewer_feedback: str) -> str:
        """执行 fill -> skeleton 回退，并返回交给 skeleton coder 的反馈。"""
        self.blackboard.increment_retry(task.task_id)
        self.blackboard.rollback_fill_to_skeleton(task.task_id, reviewer_feedback)
        logger.warning(f"↩️ [{task.task_id}] 检测到坏 skeleton，回退到 skeleton 阶段")
        return reviewer_feedback

    def _invoke_coder(self, task: TaskItem, feedback: str = None) -> str:
        """唤醒 Coder，通过 ProjectObserver 一站式组装上下文"""
        coder = self._get_coder()
        force_modify = task.task_type == "weld" or task.draft_action == "modify"

        existing_code = ""
        if self.vfs:
            existing_code = self.vfs.read_truth(task.target_file) or ""
        if not existing_code and task.code_draft:
            existing_code = task.code_draft
        if force_modify and not existing_code and self.vfs:
            disk_path = os.path.join(self.vfs.truth_dir, task.target_file)
            if os.path.isfile(disk_path):
                try:
                    with open(disk_path, "r", encoding="utf-8", errors="replace") as f:
                        existing_code = f.read()
                except Exception as e:
                    logger.warning(f"⚠️ 读取 weld 目标失败: {task.target_file} - {e}")
        if force_modify and not existing_code:
            msg = (
                f"焊接任务缺少旧文件内容，禁止退化为全量生成: {task.target_file} "
                f"(task_type={task.task_type}, draft_action={task.draft_action})"
            )
            logger.error(f"❌ {msg}")
            task.log_error(msg)
            return ""

        # 委托 ProjectObserver 一站式组装 task_meta
        from core.project_observer import ProjectObserver
        observer = ProjectObserver(self.blackboard, self.vfs)
        task_meta = observer.build_task_meta(task, existing_code, feedback)

        if feedback and task_meta.get("ast_slice"):
            if "L0.3A" in feedback:
                logger.info(f"🔧 [{task.task_id}] L0.3A 架构违规检测到，清除 ast_slice 强制全量重写")
                task_meta.pop("ast_slice", None)
            elif self._should_disable_ast_slice_for_feedback(feedback):
                logger.info(f"🔧 [{task.task_id}] 前端结构错误检测到，清除 ast_slice 强制 Editor 行号编辑")
                task_meta.pop("ast_slice", None)

        try:
            result = coder.generate_code(
                target_file=task.target_file,
                description=task.description,
                feedback=feedback,
                task_meta=task_meta,
            )
            task.recalled_memory_ids = getattr(coder, '_last_recalled_ids', [])
            return result
        except Exception as e:
            err_msg = str(e)
            if 'interpreter shutdown' in err_msg or 'Event loop is closed' in err_msg:
                logger.warning("🛑 检测到 Python 解释器关闭")
                return ""
            logger.error(f"❌ Coder 调用异常: {e}")
            task.log_error(f"Coder 异常: {e}")
            return ""

    def _run_skeleton_stage(self, task: TaskItem, feedback: str = None) -> bool:
        """skeleton 阶段：先生成，再跑 Reviewer L0 验收，通过后才写入真理区。"""
        while task.sub_tasks and task.current_sub_task_index == 0:
            if self._is_shutdown():
                logger.warning("🛑 Engine 检测到 shutdown 信号，skeleton 阶段中断")
                return False

            if task.retry_count >= self._max_retries:
                logger.error(f"🧨 [熔断] skeleton 阶段任务 {task.task_id} 连续失败 {self._max_retries} 次")
                self.blackboard.update_task_status(task.task_id, TaskStatus.FUSED)
                # Phase 5.5: 烂账账本 — 记录 skeleton 阶段熔断
                fuse_summary = f"任务 {task.task_id} skeleton 阶段熔断 ({task.target_file})"
                if task.error_logs:
                    fuse_summary += f": {task.error_logs[-1][:100]}"
                current_round = len(self.blackboard.state.round_history) + 1
                self.blackboard.upsert_issue(
                    category="fuse",
                    summary=fuse_summary,
                    related_files=[task.target_file],
                    current_round=current_round,
                )
                global_broadcaster.emit_sync(
                    "Engine", "task_abort",
                    f"任务 {task.task_id} skeleton 阶段熔断", {}
                )
                return False

            skeleton_code = self._invoke_coder_skeleton(task, feedback)
            if not skeleton_code:
                task.log_error("[SKELETON_REJECTED] Coder 未生成有效 skeleton")
                self.blackboard.increment_retry(task.task_id)
                feedback = "上轮 skeleton 为空，请输出完整骨架，不要返回空内容。"
                continue

            if self.vfs:
                self.vfs.write_to_sandbox({task.target_file: skeleton_code})

            is_pass, reviewer_feedback = self._invoke_skeleton_reviewer(task, skeleton_code)
            if not is_pass:
                task.log_error(reviewer_feedback[:2000])
                self.blackboard.increment_retry(task.task_id)
                feedback = reviewer_feedback
                pivot_result = self._handle_cross_file_signal(
                    task,
                    reviewer_feedback,
                    stage="skeleton",
                    pivot_source="task_runner_skeleton",
                )
                if pivot_result["pause"]:
                    return False
                if pivot_result["feedback"]:
                    feedback = pivot_result["feedback"]
                continue

            if self.vfs:
                self.vfs.commit_to_truth(task.target_file, skeleton_code)
                self.blackboard.update_global_snapshot(task.target_file, self.vfs.truth_dir)

            task.log_action(
                f"Skeleton 已通过 Reviewer L0 验收并写入真理区 ({len(skeleton_code)} chars)"
            )
            task.current_sub_task_index = 1
            logger.info(f"🧱 [{task.task_id}] skeleton 验收通过，进入 fill 阶段")
            return True

        return True

    @staticmethod
    def _build_skeleton_symbol_contract(target_file: str, expected_symbols: list) -> str:
        symbols = [str(item).strip() for item in (expected_symbols or []) if str(item).strip()]
        if not symbols:
            return ""

        symbol_lines = "\n".join(f"- {name}" for name in symbols)
        return (
            f"【Skeleton 符号硬契约：{target_file}】\n"
            "Reviewer L0 将按以下顶层符号验收，本轮输出必须逐一定义，名称完全一致：\n"
            f"{symbol_lines}\n"
            "禁止用同义名称替代，禁止合并函数，禁止遗漏函数。"
        )

    def _invoke_coder_skeleton(self, task: TaskItem, feedback: str = None) -> str:
        """骨架先行：生成函数签名骨架。"""
        from core.prompt import Prompts
        from core.playbook_loader import PlaybookLoader

        project_spec = self.blackboard.state.spec_text or "无规划书"
        raw_project_spec = self.blackboard.state.project_spec or {}
        if isinstance(raw_project_spec, dict):
            project_spec_dict = raw_project_spec
        elif isinstance(raw_project_spec, str):
            try:
                project_spec_dict = json.loads(raw_project_spec)
            except Exception:
                project_spec_dict = {}
        else:
            project_spec_dict = {}

        _pb_loader = PlaybookLoader()
        _tech_stack = project_spec_dict.get("tech_stack", [])
        _arch_contract = project_spec_dict.get("architecture_contract")
        playbook_content = _pb_loader.load_for_coder(
            _tech_stack, task.target_file,
            architecture_contract=_arch_contract,
        )

        expected_symbols = []
        try:
            from core.route_topology import extract_expected_symbols_for_target
            expected_symbols = extract_expected_symbols_for_target(
                project_spec_dict,
                task.target_file,
                project_spec_dict.get("module_interfaces", {}) or {},
            )
        except Exception as e:
            logger.warning(f"⚠️ skeleton 符号契约提取异常: {e}")

        # Observer 依赖注入
        dep_context = ""
        try:
            from tools.observer import Observer
            from core.project_observer import (
                ProjectObserver,
                build_route_module_contract_hint,
                build_architecture_contract_hint,
            )
            if self.vfs:
                architecture_hint = build_architecture_contract_hint(project_spec_dict, task.target_file)
                if architecture_hint:
                    dep_context += "\n\n" + architecture_hint
                route_module_hint = build_route_module_contract_hint(project_spec_dict, task.target_file)
                if route_module_hint:
                    dep_context += "\n\n" + route_module_hint
                obs = Observer(self.vfs.truth_dir)
                po = ProjectObserver(self.blackboard, self.vfs)
                dep_files = po.resolve_smart_deps(task)
                if dep_files:
                    parts = []
                    for dep_path in dep_files:
                        skeleton = obs.get_skeleton(dep_path)
                        if skeleton and "Error" not in skeleton:
                            parts.append(f"--- [依赖文件: {dep_path}] ---\n{skeleton}")
                    if parts:
                        dep_context += "\n\n【依赖文件签名（你的函数签名必须与这些接口对齐）】\n" + "\n\n".join(parts)
                        logger.info(f"🦴 骨架依赖注入: {dep_files}")
        except Exception as e:
            logger.warning(f"⚠️ 骨架依赖注入异常: {e}")

        system_content = Prompts.CODER_SKELETON_SYSTEM.format(
            target_file=task.target_file,
            description=task.description,
            project_spec=project_spec,
            coder_playbook=playbook_content,
        )
        if dep_context:
            system_content += dep_context
        symbol_contract = self._build_skeleton_symbol_contract(task.target_file, expected_symbols)
        if symbol_contract:
            system_content += "\n\n" + symbol_contract

        if os.path.basename(task.target_file) == "models.py" and "TaskTag" in project_spec:
            system_content += (
                "\n\n【SQLAlchemy ORM 强约束】\n"
                "如果规划书中声明了 `class TaskTag(...)`，则必须将 TaskTag 实现为显式 ORM 模型类。\n"
                "禁止同时再定义 `task_tags = db.Table(...)` 或 `Table('task_tags', ...)`。\n"
                "对于同一个中间表，只能二选一：显式模型类 或 裸 association table。\n"
                "本项目按规划书要求，必须保留 `class TaskTag`，不要生成裸 `task_tags` 关联表。\n"
            )
        if dep_context and "路由模块实现契约" in dep_context:
            system_content += (
                "\n\n【路由模块强约束】\n"
                "如果 app 侧已经通过 `url_prefix` 挂载 blueprint，则本文件里的 `@bp.route(...)` 只能写局部相对路径。\n"
                "禁止再次写完整 `/api/...` 前缀。\n"
                "如果契约声明为 `init_function`，则 `init_*_routes` / `register_*_routes` 只是挂载 helper，绝不是 HTTP handler。\n"
            )

        user_prompt = "请生成该文件的完整代码骨架。只输出函数签名和占位符，不写任何业务实现。"
        if expected_symbols:
            user_prompt += "\n\n【本轮硬性验收清单】\n" + "\n".join(
                f"- 必须定义 `{name}`" for name in expected_symbols
            )
        if feedback:
            user_prompt += f"\n\n【上轮 skeleton 验收反馈】\n{feedback}\n\n请修正这些结构问题后重新输出完整 skeleton。"

        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_prompt}
        ]

        try:
            model = os.getenv("MODEL_CODER", "qwen3-coder-plus")
            response_msg = default_llm.chat_completion(
                messages=messages,
                model=model,
            )
            raw = response_msg.content if hasattr(response_msg, 'content') else response_msg.get("content", "")

            xml_files = extract_xml_files(raw)
            if xml_files and xml_files[0].get("content"):
                code = xml_files[0]["content"]
            else:
                import re
                md_match = re.search(r"```(?:python|py)?\s*(.*?)\s*```", raw, re.DOTALL)
                code = md_match.group(1).strip() if md_match else raw.strip()

            logger.info(f"🦴 骨架生成完毕: {len(code)} chars")
            return code
        except Exception as e:
            logger.error(f"❌ 骨架生成异常: {e}")
            return ""

    def _invoke_skeleton_reviewer(self, task: TaskItem, skeleton_code: str) -> Tuple[bool, str]:
        """唤醒 Reviewer 对 skeleton 做轻量 L0 验收。"""
        reviewer = self._get_reviewer()
        sandbox_dir = self.vfs.sandbox_dir if self.vfs else None

        module_interfaces = None
        project_spec = None
        try:
            spec = self.blackboard.state.project_spec
            if isinstance(spec, dict):
                module_interfaces = spec.get("module_interfaces")
                project_spec = spec
            elif isinstance(spec, str):
                spec_dict = json.loads(spec)
                module_interfaces = spec_dict.get("module_interfaces")
                project_spec = spec_dict
        except Exception:
            pass

        try:
            return reviewer.evaluate_skeleton(
                task.target_file,
                code_content=skeleton_code,
                sandbox_dir=sandbox_dir,
                module_interfaces=module_interfaces,
                project_spec=project_spec,
            )
        except Exception as e:
            err_msg = str(e)
            if 'interpreter shutdown' in err_msg or 'Event loop is closed' in err_msg:
                logger.warning("🛑 检测到 Python 解释器关闭")
                return False, "[SKELETON_REJECTED] 系统正在关闭"
            logger.error(f"❌ Skeleton Reviewer 调用异常: {e}")
            task.log_error(f"Skeleton Reviewer 异常: {e}")
            return False, f"[SKELETON_REJECTED] Reviewer 执行异常: {e}"

    def _invoke_reviewer(self, task: TaskItem, merged_code: str) -> Tuple[bool, str]:
        """唤醒 Reviewer"""
        reviewer = self._get_reviewer()
        sandbox_dir = self.vfs.sandbox_dir if self.vfs else None

        module_interfaces = None
        project_spec = None
        try:
            spec = self.blackboard.state.project_spec
            if isinstance(spec, dict):
                module_interfaces = spec.get("module_interfaces")
                project_spec = spec
            elif isinstance(spec, str):
                spec_dict = json.loads(spec)
                module_interfaces = spec_dict.get("module_interfaces")
                project_spec = spec_dict
        except Exception:
            pass

        try:
            # 提取 DAG 中尚未完成的任务文件集，供 L0.F 前端 import 容错
            pending_files = set()
            try:
                from core.blackboard import TaskStatus
                for t in self.blackboard.state.tasks:
                    if t.status not in {TaskStatus.DONE, TaskStatus.FUSED}:
                        tf = t.target_file.replace("\\", "/").lstrip("/")
                        if tf:
                            pending_files.add(tf)
            except Exception:
                pass

            return reviewer.evaluate_draft(task.target_file, task.description,
                                           code_content=merged_code,
                                           sandbox_dir=sandbox_dir,
                                           module_interfaces=module_interfaces,
                                           project_spec=project_spec,
                                           task_id=task.task_id,
                                           pending_files=pending_files or None)
        except Exception as e:
            err_msg = str(e)
            if 'interpreter shutdown' in err_msg or 'Event loop is closed' in err_msg:
                logger.warning("🛑 检测到 Python 解释器关闭")
                return False, "系统正在关闭"
            logger.error(f"❌ Reviewer 调用异常: {e}")
            task.log_error(f"Reviewer 异常: {e}")
            return False, f"Reviewer 执行异常: {e}"

    # ============================================================
    # TechLead 跨文件仲裁
    # ============================================================

    def _handle_cross_file_signal(self, task: TaskItem, feedback: str,
                                  stage: str, pivot_source: str) -> dict:
        signal = parse_cross_file_signal(feedback, stage=stage)
        if not signal:
            return {"pause": False, "feedback": ""}

        if not signal.importer_file:
            signal.importer_file = task.target_file

        pivot_count = getattr(task, "_pivot_count", 0)
        if task.tech_lead_invoked or pivot_count >= 1:
            return {"pause": False, "feedback": ""}

        logger.info(
            "⚖️ 检测到跨文件信号: importer=%s provider=%s stage=%s",
            signal.importer_file,
            signal.provider_file,
            signal.stage,
        )
        global_broadcaster.emit_sync(
            "Engine", "tech_lead",
            f"⚖️ 唤醒技术骨干定向追因: {signal.importer_file} ↔ {signal.provider_file}"
        )

        verdict = self._invoke_tech_lead(task, signal, feedback)
        task.tech_lead_invoked = True

        action = "no_verdict"
        resolved = False
        next_feedback = ""

        if verdict:
            guilty_file = verdict.get("guilty_file") or signal.provider_file
            verdict["guilty_file"] = guilty_file
            if guilty_file == task.target_file:
                action = "self_fix"
                resolved = True
                next_feedback = self._merge_tech_lead_feedback(verdict, feedback)
            else:
                task._pivot_count = pivot_count + 1
                action = self._apply_cross_file_pivot(task, signal, verdict)
                resolved = action in {
                    "reopened_done_task",
                    "nudged_existing_task",
                    "injected_minimal_task",
                }

        self.blackboard.record_cross_file_pivot(
            source_task_id=task.task_id,
            importer_file=signal.importer_file or task.target_file,
            provider_file=verdict.get("guilty_file") if verdict else signal.provider_file,
            missing_symbol=signal.missing_symbol,
            pivot_stage=signal.stage,
            pivot_source=pivot_source,
            verdict_type=(verdict or {}).get("root_cause_type", ""),
            provider_task_action=action,
            resolved=resolved,
        )

        return {
            "pause": resolved and action != "self_fix",
            "feedback": next_feedback,
        }

    def _invoke_tech_lead(self, task: TaskItem, signal, feedback: str) -> Optional[dict]:
        """唤醒 TechLead 进行白盒排障调查（A-1 ReAct 模式）。"""
        try:
            from agents.tech_lead import TechLeadAgent
            tech_lead = TechLeadAgent()

            # 提取项目目录
            project_dir = self.vfs.truth_dir if self.vfs else ""
            sandbox_dir = self.vfs.sandbox_dir if self.vfs else None

            # 构建调查上下文
            user_req = self.blackboard.state.user_requirement or ""
            signal_scope = build_scope_from_cross_file_signal(project_dir, signal)
            # 双保险：将当前草稿代码直接注入 TechLead 上下文
            # 即使 sandbox read_file 因时序问题读不到，TechLead 也能在 prompt 中看到草稿
            draft_snippet = (task.code_draft or "")[:3000]
            draft_section = ""
            if draft_snippet:
                draft_section = (
                    f"\n\n【当前草稿代码（已写入 sandbox，read_file 可直接读取）】\n"
                    f"文件: {task.target_file}\n"
                    f"{draft_snippet}"
                )

            task_context = (
                f"【TDD 排障】\n"
                f"当前任务: {task.target_file}\n"
                f"任务描述: {task.description}\n"
                f"已重试: {task.retry_count} 次\n"
                f"跨文件冲突: {signal.importer_file or task.target_file} ↔ {signal.provider_file}\n\n"
                f"【Reviewer 报错】\n{feedback[:2000]}"
                f"{draft_section}\n\n"
                f"【用户需求】\n{user_req[:500]}"
            )

            # ReAct 调查
            result = tech_lead.investigate(
                project_dir=project_dir,
                task_context=task_context,
                sandbox_dir=sandbox_dir,
                max_steps=10,
                target_scope=signal_scope,
                signal={
                    "provider_file": signal.provider_file,
                    "importer_file": signal.importer_file or task.target_file,
                    "missing_symbol": signal.missing_symbol,
                    "stage": signal.stage,
                },
            )

            if result:
                return {
                    "guilty_file": result.get("guilty_file", signal.provider_file or task.target_file),
                    "fix_instruction": result.get("fix_instruction", ""),
                    "reasoning": result.get("root_cause", ""),
                    "root_cause_type": result.get("root_cause_type", "wrong_target"),
                    "recommended_target_files": result.get("recommended_target_files", []),
                    "confidence": result.get("confidence", 0.0),
                }
            return None

        except Exception as e:
            logger.error(f"❌ TechLead 排障失败: {e}")
            return None

    def _apply_cross_file_pivot(self, blocked_task: TaskItem, signal, verdict: dict) -> str:
        guilty_file = verdict["guilty_file"]
        guilty_task = self.blackboard.find_task_by_file(guilty_file)
        fix_instruction = verdict.get("fix_instruction", "")

        # 辅助：给 blocked_task 注入对 guilty_task 的运行时依赖，
        # 确保 get_next_runnable_task 不会在 guilty_task 完成前再次调度 blocked_task
        def _inject_dependency(provider_task_id: str):
            if provider_task_id and provider_task_id not in blocked_task.dependencies:
                blocked_task.dependencies.append(provider_task_id)
                logger.info(
                    "⚖️ 注入运行时依赖: %s → %s",
                    blocked_task.task_id, provider_task_id,
                )

        primary_action = "no_action"

        if guilty_task:
            if guilty_task.status == TaskStatus.DONE:
                logger.info(
                    "⚖️ TechLead 仲裁: 打回 %s (原因: %s)",
                    guilty_file,
                    verdict.get("reasoning", "无")[:60],
                )
                global_broadcaster.emit_sync(
                    "Engine", "tech_lead_reopen",
                    f"⚖️ 打回有罪文件: {guilty_file}",
                    {"guilty": guilty_file, "blocked": blocked_task.target_file},
                )
                self.blackboard.reopen_task(
                    guilty_task.task_id,
                    fix_instruction=fix_instruction,
                    reset_retry=False,
                    reorder_before=blocked_task.task_id,
                )
                _inject_dependency(guilty_task.task_id)
                self.blackboard.reopen_task(blocked_task.task_id, reset_retry=False)
                primary_action = "reopened_done_task"
            else:
                logger.info(
                    "⚖️ TechLead 仲裁: 未完成 provider 任务注入修复指令 %s -> %s",
                    guilty_task.task_id,
                    guilty_file,
                )
                self.blackboard.nudge_task_for_tech_lead(
                    guilty_task.task_id,
                    fix_instruction=fix_instruction,
                    reorder_before=blocked_task.task_id,
                )
                _inject_dependency(guilty_task.task_id)
                self.blackboard.reopen_task(blocked_task.task_id, reset_retry=False)
                primary_action = "nudged_existing_task"
        else:
            logger.info("⚖️ TechLead 仲裁: 注入最小修复任务 -> %s", guilty_file)
            injected_id = self.blackboard.inject_targeted_fix_task(
                target_file=guilty_file,
                description=(
                    f"[TECHLEAD_PIVOT_FIX] 修复 {guilty_file} 对"
                    f" `{signal.missing_symbol or '跨文件契约'}` 的提供，解除 {blocked_task.target_file} 阻塞"
                ),
                fix_instruction=fix_instruction,
                reorder_before=blocked_task.task_id,
                source_task_id=blocked_task.task_id,
            )
            _inject_dependency(injected_id)
            self.blackboard.reopen_task(blocked_task.task_id, reset_retry=False)
            primary_action = "injected_minimal_task"

        # ============================================================
        # 多文件联动修复：消费 recommended_target_files
        # 对 guilty_file 以外的推荐文件，注入辅助修复任务（上限 3 个防膨胀）
        # ============================================================
        recommended = verdict.get("recommended_target_files") or []
        _MAX_EXTRA_PIVOTS = 3
        extra_count = 0
        for rec_file in recommended:
            rec_file = str(rec_file).replace("\\", "/").strip("/")
            if not rec_file or rec_file == guilty_file:
                continue
            if extra_count >= _MAX_EXTRA_PIVOTS:
                break
            # 跳过已有活跃任务的文件（避免重复注入）
            existing = self.blackboard.find_task_by_file(rec_file)
            if existing and existing.status not in {TaskStatus.DONE, TaskStatus.FUSED}:
                continue
            if existing and existing.status == TaskStatus.DONE:
                # 已完成的推荐文件 → 打回重审
                self.blackboard.reopen_task(
                    existing.task_id,
                    fix_instruction=f"[TECHLEAD_RECOMMENDED] TechLead 建议联动检查此文件，确保与 {guilty_file} 的修改一致。",
                    reset_retry=False,
                    reorder_before=blocked_task.task_id,
                )
                _inject_dependency(existing.task_id)
                extra_count += 1
                logger.info("⚖️ 多文件联动: 打回推荐文件 %s", rec_file)
            else:
                # 不存在任务 → 注入辅助修复任务
                rec_id = self.blackboard.inject_targeted_fix_task(
                    target_file=rec_file,
                    description=(
                        f"[TECHLEAD_RECOMMENDED_FIX] 联动检查 {rec_file}，"
                        f"确保与 {guilty_file} 的修复一致，解除 {blocked_task.target_file} 阻塞"
                    ),
                    fix_instruction=f"TechLead 建议联动修复此文件。主要修复在 {guilty_file}，请确保本文件的接口/引用与之对齐。",
                    reorder_before=blocked_task.task_id,
                    source_task_id=blocked_task.task_id,
                )
                _inject_dependency(rec_id)
                extra_count += 1
                logger.info("⚖️ 多文件联动: 注入推荐文件修复任务 %s", rec_file)

        if extra_count > 0:
            logger.info("⚖️ 多文件联动: 共注入 %d 个额外修复任务", extra_count)
            global_broadcaster.emit_sync(
                "Engine", "tech_lead_multi_pivot",
                f"⚖️ TechLead 多文件联动: 主修 {guilty_file} + {extra_count} 个关联文件",
            )

        return primary_action

    @staticmethod
    def _merge_tech_lead_feedback(verdict: dict, original_feedback: str) -> str:
        fix_instruction = verdict.get("fix_instruction", "").strip()
        if not fix_instruction:
            return original_feedback
        return f"{fix_instruction}\n\n原始审查: {original_feedback}"

    # ============================================================
    # 辅助
    # ============================================================

    @staticmethod
    def check_change_rate(target_file: str, old_code: str, new_code: str,
                          coder_mode: str = "unknown") -> bool:
        """检查代码变动率，防止 patch 模式下 Coder 抽风重写。"""
        if not old_code or not new_code:
            return True

        old_lines = old_code.splitlines()
        new_lines = new_code.splitlines()
        ratio = difflib.SequenceMatcher(None, old_lines, new_lines).ratio()
        change_rate = 1.0 - ratio

        logger.info(f"📊 变动率检查: {target_file} | rate={change_rate:.1%} | coder_mode={coder_mode}")

        if coder_mode == "fallback_rewrite":
            logger.info("✅ 合法降级覆写 (fallback_rewrite)，放行")
            return True

        if change_rate > 0.8:
            logger.warning(f"⚠️ 变动率过高 ({change_rate:.1%})，疑似全量重写而非增量修改")
            return False

        return True

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
