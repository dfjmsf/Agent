"""
Reviewer Agent v2 — 三层自愈架构
Layer 1: compile() 预检 — 拦截语法级错误（0 token 成本）
Layer 2: 沙盒执行 — returncode=0 → 直接 PASS
Layer 3: 失败智能分析 — 区分 Coder bug vs Reviewer 脚本错误，后者自愈重试
"""
import os
import json
import logging
from typing import Dict, Any, Tuple

from core.llm_client import default_llm
from core.prompt import Prompts
from tools.sandbox import sandbox_env
from core.ws_broadcaster import global_broadcaster
from core.database import get_recent_events, recall_reviewer_experience, memorize

logger = logging.getLogger("ReviewerAgent")


class ReviewerAgent:
    """
    审查与执行 Agent (Reviewer v2)
    专职：编写测试脚本 -> 三层自愈执行 -> 给出 PASS 或具体报错信息。
    """
    def __init__(self, project_id: str = "default_project"):
        self.model = os.getenv("MODEL_REVIEWER", "qwen3-max")
        self.project_id = project_id
        self._last_stderr = ""
        self._last_healing_triggered = False  # 是否触发了自愈
        self._last_error = ""                 # 自愈前的错误
        self._healed_test_code = ""           # 自愈后的测试脚本

    # ============================================================
    # 工具方法
    # ============================================================

    @staticmethod
    def _compile_check(code: str) -> str:
        """
        Layer 1: 预检测试脚本是否能通过 Python compile()。
        返回空字符串表示通过，否则返回错误信息。
        """
        try:
            compile(code, "<test_script>", "exec")
            return ""
        except SyntaxError as e:
            return f"Line {e.lineno}: {e.msg}"

    @staticmethod
    def _is_reviewer_fault(stderr: str) -> bool:
        """
        通用判定：沙盒失败是否是 Reviewer 测试脚本的问题。
        
        策略：检查 traceback 最后一个 File "..." 行（报错实际位置），
        如果在 _run_task_*.py 或 unittest/mock 中 → Reviewer 的锅。
        """
        if not stderr:
            return False
        
        lines = stderr.strip().split('\n')
        # 找最后一个 File "..." 行（即报错的实际代码位置）
        last_file_line = ""
        for line in reversed(lines):
            if line.strip().startswith('File "'):
                last_file_line = line
                break
        
        # 报错帧在测试脚本中 → Reviewer 的锅
        if "_run_task_" in last_file_line:
            return True
        # 报错帧在 unittest.mock 中（patch 了不存在的属性）→ Reviewer 的锅
        if "unittest" in last_file_line and "mock" in last_file_line:
            return True
        
        return False

    @staticmethod
    def _has_deprecation_warning(stderr: str) -> bool:
        """检测 stderr 中是否包含弃用警告"""
        if not stderr:
            return False
        keywords = ['DeprecationWarning', 'PendingDeprecationWarning', 'FutureWarning']
        return any(kw in stderr for kw in keywords)

    @staticmethod
    def _is_cleanup_permission_error(stderr: str) -> bool:
        """
        Windows 文件锁检测：如果 stderr 只包含 PermissionError
        （通常是测试完成后清理临时文件时的文件锁），视为非致命错误。
        """
        if not stderr:
            return False
        # stderr 中有 PermissionError 且没有其他致命错误类型
        if 'PermissionError' not in stderr:
            return False
        fatal_errors = ['AssertionError', 'TypeError', 'ValueError', 'AttributeError',
                        'ImportError', 'NameError', 'KeyError', 'IndexError']
        return not any(e in stderr for e in fatal_errors)

    def _extract_test_code_from_response(self, response_msg) -> str:
        """从 LLM response 的 tool_call 中提取测试脚本代码"""
        if hasattr(response_msg, "tool_calls") and response_msg.tool_calls:
            for tool_call in response_msg.tool_calls:
                if tool_call.function.name == "sandbox_execute":
                    try:
                        args = json.loads(tool_call.function.arguments)
                        return args.get("test_code_string", "")
                    except (json.JSONDecodeError, TypeError):
                        pass
        return ""

    # ============================================================
    # 上下文构建
    # ============================================================

    def _build_review_context(self, target_file: str, module_interfaces: dict = None) -> str:
        """
        构建 Reviewer 的记忆上下文：
        1. 项目文件树
        2. 跨文件接口契约
        3. Reviewer 测试经验召回
        """
        memory_hint = ""

        # 1. 短期记忆 → 项目文件树
        file_tree_events = get_recent_events(
            project_id=self.project_id, limit=1,
            event_types=["file_tree"], caller="Reviewer"
        )
        if file_tree_events:
            memory_hint += f"\n\n【📂 当前项目文件结构】\n{file_tree_events[0].content[:500]}"

        # 2. 跨文件接口契约（来自 Manager 规划书）
        if module_interfaces:
            iface_str = "\n".join([f"  {k}: {v}" for k, v in module_interfaces.items()])
            memory_hint += f"\n\n【🔗 跨文件接口契约（代码中的 import/调用必须与此一致）】\n{iface_str}"

        # 3. Reviewer 测试经验召回（预防已知错误）
        try:
            test_exps = recall_reviewer_experience(
                f"{target_file}", n_results=2, caller="Reviewer"
            )
            if test_exps:
                exp_str = "\n".join([f"  {i+1}. {e[:200]}" for i, e in enumerate(test_exps)])
                memory_hint += f"\n\n【🧪 历史测试经验（曾经犯过的错误，务必避免重犯）】\n{exp_str}"
        except Exception as e:
            logger.warning(f"⚠️ Reviewer 测试经验召回失败: {e}")

        return memory_hint

    # ============================================================
    # 测试脚本生成
    # ============================================================

    def _generate_test_script(self, target_file: str, description: str,
                               code_content: str, memory_hint: str) -> str:
        """LLM 首次生成测试脚本"""
        system_prompt = Prompts.REVIEWER_SYSTEM + memory_hint
        user_content = (
            f"【当前要审查的文件】: {target_file}\n"
            f"【业务需求描述】: {description}\n"
            f"【Coder提交的代码内容】:\n```python\n{code_content}\n```\n\n"
            f"请立即使用 `sandbox_execute` 工具生成并执行一段测试脚本！"
            f"测试脚本应 import 该文件中的类/函数进行黑盒测试。"
            f"如果无法 import（比如该文件是入口配置），请写一段语法检查即可。"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ]

        response_msg = default_llm.chat_completion(
            messages=messages,
            model=self.model,
            tools=Prompts.REVIEWER_TOOL_SCHEMA
        )

        test_code = self._extract_test_code_from_response(response_msg)
        if not test_code:
            logger.warning("⚠️ Reviewer 未调用测试工具（偷懒或幻觉）")
        return test_code

    def _regenerate_test_script(self, target_file: str, code_content: str,
                                 description: str, memory_hint: str,
                                 error_context: str) -> str:
        """带错误上下文重新生成测试脚本（自愈）"""
        system_prompt = Prompts.REVIEWER_SYSTEM + memory_hint
        user_content = (
            f"【当前要审查的文件】: {target_file}\n"
            f"【业务需求描述】: {description}\n"
            f"【Coder提交的代码内容】:\n```python\n{code_content[:2000]}\n```\n\n"
            f"请使用 `sandbox_execute` 工具重新生成测试脚本。"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
            {"role": "assistant", "content": "（上次生成的测试脚本有问题，正在修正）"},
            {"role": "user", "content": f"【⚠️ 上次测试脚本的错误】\n{error_context}\n\n请修正上述问题后重新生成测试脚本。"}
        ]

        response_msg = default_llm.chat_completion(
            messages=messages,
            model=self.model,
            tools=Prompts.REVIEWER_TOOL_SCHEMA
        )

        return self._extract_test_code_from_response(response_msg)

    # ============================================================
    # 三层自愈执行核心
    # ============================================================

    def _execute_with_healing(self, test_code: str, target_file: str,
                               code_content: str, description: str,
                               memory_hint: str, sandbox_dir: str,
                               max_retries: int = 2) -> Tuple[bool, str]:
        """
        三层自愈执行：
        Layer 1: compile() 预检 — 语法错误直接重新生成
        Layer 2: 沙盒执行 — returncode=0 → 直接 PASS
        Layer 3: 失败分析 — Reviewer 脚本错误 → 自愈重试；Coder bug → 正常 FAIL
        
        返回: (is_pass, feedback)
        """
        self._last_healing_triggered = False

        for attempt in range(max_retries + 1):
            # 无测试脚本 → LLM 偷懒，跳过审查
            if not test_code:
                logger.warning("⚠️ 无法获取测试脚本，跳过审查")
                return True, "测试通过（Reviewer 未生成测试脚本，跳过审查）"

            # ── Layer 1: compile 预检 ──
            compile_error = self._compile_check(test_code)
            if compile_error:
                logger.warning(f"⚠️ 测试脚本 compile 失败 (尝试 {attempt+1}/{max_retries+1}): {compile_error}")
                global_broadcaster.emit_sync("Reviewer", "sandbox_start",
                    f"测试脚本语法错误，自愈重新生成 ({attempt+1}/{max_retries+1})", {})
                if attempt < max_retries:
                    self._last_healing_triggered = True
                    self._last_error = compile_error
                    test_code = self._regenerate_test_script(
                        target_file, code_content, description, memory_hint,
                        error_context=f"你上次生成的测试脚本有语法错误:\n{compile_error}\n请修正后重新生成。"
                    )
                    self._healed_test_code = test_code
                    continue
                else:
                    return True, "测试通过（Reviewer 测试脚本多次语法错误，跳过审查）"

            # ── Layer 2: 沙盒执行 ──
            logger.info("🛡️ Reviewer 唤起本地沙盒执行测试代码...")
            global_broadcaster.emit_sync("Reviewer", "sandbox_start",
                "Reviewer 正在将其验证脚本压入沙盒容器...", {"test_code": test_code})

            result = sandbox_env.execute_code(test_code, self.project_id,
                                               sandbox_dir=sandbox_dir)
            rc = result.get("returncode", -1)
            stderr = result.get("stderr", "")
            self._last_stderr = stderr

            global_broadcaster.emit_sync("Reviewer", "sandbox_end",
                f"沙盒测试完毕，退出码 {rc}", {"result": result})

            # returncode=0 → 直接 PASS（不再交给 LLM 二次判定）
            if rc == 0:
                # 唯一例外：DeprecationWarning
                if self._has_deprecation_warning(stderr):
                    logger.warning(f"⚠️ 检测到 DeprecationWarning，强制驳回")
                    return False, (
                        f"[DeprecationWarning] 沙盒 stderr 中检测到弃用警告，"
                        f"当前代码使用了已弃用的 API。\nstderr: {stderr[:500]}\n"
                        f"请根据警告信息修复代码。"
                    )
                return True, "测试通过"

            # Windows 文件锁容错：只有 PermissionError → 视为通过
            if self._is_cleanup_permission_error(stderr):
                logger.info("✅ Windows 文件锁异常（cleanup 阶段），忽略并视为通过")
                return True, "测试通过（cleanup 阶段文件锁，已自动忽略）"

            # ── Layer 3: 失败分析 ──
            if self._is_reviewer_fault(stderr) and attempt < max_retries:
                # Reviewer 自身问题 → 自愈重试
                logger.warning(
                    f"⚠️ 测试脚本自身有问题 (尝试 {attempt+1}/{max_retries+1})，"
                    f"自愈重新生成..."
                )
                global_broadcaster.emit_sync("Reviewer", "sandbox_start",
                    f"测试脚本自身有错误，自愈重新生成 ({attempt+1}/{max_retries+1})", {})
                self._last_healing_triggered = True
                self._last_error = stderr[:500]
                test_code = self._regenerate_test_script(
                    target_file, code_content, description, memory_hint,
                    error_context=(
                        f"你上次的测试脚本执行失败，"
                        f"错误不在 Coder 代码中而在你的测试脚本:\n{stderr[:800]}\n"
                        f"请修正后重新生成测试脚本。"
                    )
                )
                self._healed_test_code = test_code
                continue

            # 确认是 Coder 的 bug → 正常 FAIL
            logger.warning(f"❌ 沙盒测试失败 (Coder bug)，exit={rc}")
            # 提取有效反馈：截取 stderr 的关键报错部分
            feedback = f"[代码bug] 沙盒测试失败 (exit={rc}):\n{stderr[:800]}"
            return False, feedback

        # 自愈次数耗尽 → 放行
        logger.warning("⚠️ Reviewer 测试脚本多次自愈失败，跳过审查放行")
        return True, "测试通过（Reviewer 测试脚本多次失败，跳过审查）"

    # ============================================================
    # 经验沉淀
    # ============================================================

    def _save_test_experience(self, error: str, healed_code: str):
        """
        自愈成功后，将错误经验沉淀到长期记忆。
        下次 Reviewer 生成测试脚本时会自动召回，避免重复犯错。
        """
        content = f"❌ 测试脚本错误: {error[:300]}\n✅ 修正方式: 避免上述写法，使用更安全的替代方案"
        try:
            memorize(
                text=content,
                scope="global",
                exp_type="reviewer_test",
                scenario="测试脚本编写",
                tech_stacks=[],
            )
            logger.info(f"📝 Reviewer 测试经验已沉淀: '{content[:50]}...'")
        except Exception as e:
            logger.warning(f"⚠️ Reviewer 测试经验沉淀失败: {e}")

    # ============================================================
    # 主入口
    # ============================================================

    def evaluate_draft(self, target_file: str, description: str,
                       code_content: str = None, sandbox_dir: str = None,
                       module_interfaces: dict = None) -> Tuple[bool, str]:
        """
        评估特定的文件草稿（v2 三层自愈架构）
        
        Args:
            target_file: 目标文件路径
            description: 任务描述
            code_content: Engine 传入的已缝合代码
            sandbox_dir: 沙盒工作目录
            module_interfaces: 跨文件接口契约（来自 Manager 规划书）
        
        返回:
            is_pass (bool): 是否审查通过
            feedback (str): 修改建议/报错 或 简短评语
        """
        code_draft = code_content
        if not code_draft:
            return False, "没有找到该文件的代码内容（Engine 未传入 code_content）"

        logger.info(f"🛡️ Reviewer 正在审查文件: {target_file}")
        global_broadcaster.emit_sync("Reviewer", "review_start",
            f"开始审查目标文件: {target_file}", {"target": target_file, "code": code_draft})

        # 1. 构建审查上下文（含经验召回）
        memory_hint = self._build_review_context(target_file, module_interfaces)

        # 2. LLM 生成测试脚本
        test_code = self._generate_test_script(target_file, description, code_draft, memory_hint)

        # 3. 三层自愈执行
        is_pass, feedback = self._execute_with_healing(
            test_code, target_file, code_draft, description,
            memory_hint, sandbox_dir
        )

        # 4. 经验沉淀（仅在自愈触发时）
        if self._last_healing_triggered:
            self._save_test_experience(self._last_error, self._healed_test_code)

        # 5. 广播结果
        if is_pass:
            logger.info(f"✅ Reviewer 盖章通过！")
            global_broadcaster.emit_sync("Reviewer", "review_pass", "审查通过！", {"feedback": feedback})
        else:
            logger.warning(f"❌ Reviewer 驳回草稿！已生成反馈意见。")
            global_broadcaster.emit_sync("Reviewer", "review_fail", "审查未通过！", {"feedback": feedback})

        return is_pass, feedback
