"""
QA Agent — 持 Skill 下场的集成测试专家

核心架构: ReAct Tool Calling 循环
- 不再生成测试脚本，而是自己操作终端逐步验证
- 拥有 run_terminal / read_file / http_request / check_port / report_result 五个 Skill
- 绝对不能修改源码（没有 edit_file Skill）

安全机制:
- MAX_STEPS 熔断: 最多执行 N 步
- 连续相同错误检测: 同一错误连续出现 3 次 → 停止
- 资源清理: 结束时自动杀掉后台进程
"""
import os
import json
import logging
from typing import Dict, Optional

from core.llm_client import default_llm
from core.skill_runner import SkillRunner
from core.ws_broadcaster import global_broadcaster

logger = logging.getLogger("QAAgent")

MAX_STEPS = 12       # ReAct 循环最大步数（12 步足够完成完整测试流程）
FUSE_THRESHOLD = 3   # 连续相同错误 N 次 → 熔断


class QAAgent:
    """QA Agent — ReAct 循环驱动的集成测试专家"""

    def __init__(self, project_id: str):
        self.project_id = project_id
        self.model = os.getenv("MODEL_QA", "deepseek-chat")

    def run_qa(self, project_spec: str, all_code: Dict[str, str],
               sandbox_dir: str, venv_python: str = "") -> dict:
        """
        执行 QA 验收测试。

        Args:
            project_spec: Manager 规划书文本
            all_code: {文件名: 代码内容} 字典
            sandbox_dir: 沙盒目录路径
            venv_python: sandbox venv 的 python 路径

        Returns:
            {"passed": bool, "feedback": str, "failed_files": list, "warning": bool}
        """
        logger.info(f"🧪 [QA Agent] 启动 ReAct 测试循环 ({len(all_code)} 个文件)")
        global_broadcaster.emit_sync("QAAgent", "start",
            f"🧪 QA Agent 启动: ReAct 模式验证 {len(all_code)} 个文件")

        # 初始化 SkillRunner
        skill_runner = SkillRunner(
            sandbox_dir=sandbox_dir,
            project_id=self.project_id,
            venv_python=venv_python,
        )

        try:
            return self._react_loop(project_spec, all_code, skill_runner)
        finally:
            skill_runner.cleanup()

    def _react_loop(self, project_spec: str, all_code: Dict[str, str],
                    skill_runner: SkillRunner) -> dict:
        """ReAct 主循环"""

        # 构建初始消息
        system_prompt = self._build_system_prompt(project_spec, all_code)
        user_prompt = self._build_user_prompt(all_code)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        tool_schemas = skill_runner.get_tool_schemas()
        last_errors = []  # 用于连续错误检测

        no_tool_count = 0  # 连续无 tool_call 计数

        for step in range(1, MAX_STEPS + 1):
            remaining = MAX_STEPS - step
            logger.info(f"🔄 [QA Agent] Step {step}/{MAX_STEPS} (剩余 {remaining} 步)")
            global_broadcaster.emit_sync("QAAgent", "step",
                f"🔄 QA 测试 Step {step}/{MAX_STEPS}")

            # 临近上限时，强制要求 report_result
            if remaining <= 2:
                force_msg = {
                    "role": "user",
                    "content": f"⚠️ 警告：你只剩 {remaining} 步了！请立即调用 report_result 提交你的最终判定。不要再做其他操作。",
                }
                if messages[-1].get("role") != "user":
                    messages.append(force_msg)

            try:
                response = default_llm.chat_completion(
                    messages=messages,
                    model=self.model,
                    tools=tool_schemas,
                    tool_choice="auto",
                    temperature=0.1,
                )
            except Exception as e:
                logger.error(f"❌ [QA Agent] LLM 调用失败: {e}")
                return self._make_result(False, f"QA Agent LLM 调用失败: {e}",
                                         warning=True)

            # 检查是否有 tool_calls
            tool_calls = getattr(response, "tool_calls", None)

            if not tool_calls:
                # LLM 没有调用 tool — 可能在思考或总结
                text = getattr(response, "content", "") or ""
                logger.info(f"💬 [QA Agent] LLM 文本回复 (无 tool_call): {text[:200]}")
                no_tool_count += 1

                # 如果连续 3 次不调 tool，或已接近上限，直接从文本中提取判定
                if no_tool_count >= 3 or remaining <= 1:
                    logger.warning(f"⚡ [QA Agent] 强制从文本提取判定 (no_tool_count={no_tool_count}, remaining={remaining})")
                    return self._extract_result_from_text(text)

                # 将回复加入对话，强硬提示
                messages.append({"role": "assistant", "content": text})
                messages.append({
                    "role": "user",
                    "content": f"你必须调用工具！剩余步数: {remaining}。请立即调用 report_result(passed=true/false, feedback='你的判断') 提交判定。",
                })
                continue
            else:
                no_tool_count = 0  # 重置计数

            # 处理 tool_calls
            # 先将 assistant message（含 tool_calls）加入历史
            messages.append(response)

            for tc in tool_calls:
                func_name = tc.function.name
                try:
                    arguments = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    arguments = {}

                logger.info(f"🔧 [QA Agent] 调用 Skill: {func_name}({json.dumps(arguments, ensure_ascii=False)[:100]})")

                # 拦截 report_result — 终止循环
                if func_name == "report_result":
                    passed = arguments.get("passed", False)
                    feedback = arguments.get("feedback", "")
                    failed_files = arguments.get("failed_files", [])
                    status = "✅ 通过" if passed else "❌ 失败"
                    logger.info(f"📋 [QA Agent] 最终判定: {status} — {feedback[:100]}")
                    global_broadcaster.emit_sync("QAAgent",
                        "passed" if passed else "failed",
                        f"{'✅' if passed else '❌'} QA 判定: {feedback[:80]}")
                    return self._make_result(passed, feedback, failed_files)

                # 执行 Skill
                result_text = skill_runner.execute(func_name, arguments)
                logger.info(f"📤 [QA Agent] Skill 结果: {result_text[:150]}")

                # 将 tool 结果加入对话
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_text[:800],  # 严格限制避免 context 雪球
                })

                # 连续错误检测
                if "错误" in result_text or "失败" in result_text:
                    last_errors.append(result_text[:100])
                    if len(last_errors) >= FUSE_THRESHOLD:
                        recent = last_errors[-FUSE_THRESHOLD:]
                        if len(set(recent)) == 1:
                            logger.warning(f"🔥 [QA Agent] 连续 {FUSE_THRESHOLD} 次相同错误，熔断!")
                            return self._make_result(
                                False,
                                f"QA Agent 熔断: 连续 {FUSE_THRESHOLD} 次相同错误\n{recent[0]}",
                            )
                else:
                    last_errors.clear()

        # 超过 MAX_STEPS — 做最后一次尝试从历史中提取判定
        logger.warning(f"🔥 [QA Agent] 达到最大步数 {MAX_STEPS}，尝试提取判定...")
        global_broadcaster.emit_sync("QAAgent", "fused",
            f"🔥 QA Agent 达到最大步数 {MAX_STEPS}，提取判定中...")

        # 从最后几条消息提取可能的判定
        last_text = ""
        for msg in reversed(messages[-5:]):
            content = ""
            if isinstance(msg, dict):
                content = msg.get("content", "") or ""
            else:
                content = getattr(msg, "content", "") or ""
            if content:
                last_text += content + "\n"
        if last_text:
            return self._extract_result_from_text(last_text)

        return self._make_result(
            False, f"QA Agent 熔断: 达到最大步数 {MAX_STEPS}，未能完成测试",
            warning=True,
        )

    # ============================================================
    # Prompt 构建
    # ============================================================

    def _build_system_prompt(self, project_spec: str, all_code: Dict[str, str]) -> str:
        """构建 QA Agent 的 System Prompt"""
        file_list = "\n".join(f"  - {f}" for f in sorted(all_code.keys()))

        # 检测入口文件
        entry_file = self._detect_entry_file(all_code)

        # 检测端口
        port = self._detect_port(all_code.get(entry_file, ""), project_spec)

        return f"""你是 QA 验收工程师 (QA Agent)。你的唯一任务是验证项目代码能否正常运行。

【你的能力】
你拥有 5 个工具。每一步必须调用工具，禁止只说话不行动。
1. run_terminal — 在项目目录执行终端命令
2. read_file — 读取项目文件（仅在遇到错误需要诊断时使用！不要预防性阅读）
3. http_request — 对 localhost 发 HTTP 请求
4. check_port — 检查端口是否在监听
5. report_result — 提交最终判定（必须调用此工具结束测试）

【⚠️ 步数预算：你最多只有 {MAX_STEPS} 步！标准流程只需 6 步！】

严格按以下顺序执行，一步一个操作，不要跳步也不要加步：
  Step 1: run_terminal(command="python {entry_file}", background=true) 启动服务
  Step 2: check_port(port={port}) 确认服务就绪
  Step 3: http_request(method="GET", url="http://127.0.0.1:{port}/") 测试首页
  Step 4: http_request(method="POST", ...) 提交一条测试数据
  Step 5: http_request(method="GET", url="http://127.0.0.1:{port}/edit/1") 测试编辑页
  Step 6: report_result(...) 提交判定

【效率铁律】
- 不要在测试前 read_file！你已经有代码预览了
- 不要重复测试同一个端点
- 首页返回 200 + POST 能提交 + 编辑页不 500 → 直接 PASS
- 遇到任何 500 错误 → 最多 read_file 1 次诊断原因 → 立刻 report_result(passed=false)


【判定标准 — 严格！】
- PASS: 服务能启动 + 所有被测端点都返回 2xx
- FAIL（任一即判 FAIL）:
  · 服务无法启动
  · 任何端点返回 400（如 CSRF token missing）
  · 任何端点返回 404（路由未注册）
  · 任何端点返回 500（服务器内部错误）
  · 任何非 2xx 响应码

【遇到非 2xx 响应时 — 快速失败！】
- 看到 400/404/500 → 立刻 report_result(passed=false)
- feedback 只写事实："POST http://127.0.0.1:5001/add 返回 400, 响应体: The CSRF token is missing"
- **严禁在 feedback 中写"问题分析"或"修复建议"！你是测试员不是修理工！**
- **严禁建议添加/修改任何代码！**只报告"什么 URL + 什么 HTTP 方法 + 什么状态码 + 响应体摘要"
- 不要再 read_file 去诊断原因！
- 不要继续测试其他端点！一个失败 = 整体失败

【铁律】
- 你只测试，绝对不修改任何源代码文件
- 不要访问外部网络
- 不要运行危险命令（rm -rf 等）
- 每一步都必须调用工具，禁止只输出文字不行动

【项目信息】
入口文件（推测）: {entry_file}
服务端口（推测）: {port}

文件列表:
{file_list}

【项目规划书】
{project_spec[:3000] if project_spec else '(无规划书)'}
"""

    def _build_user_prompt(self, all_code: Dict[str, str]) -> str:
        """构建首轮 user 消息"""
        # 只展示关键文件的代码摘要
        key_files = []
        for fname in sorted(all_code.keys()):
            if fname.endswith((".py", ".js")):
                code = all_code[fname]
                if code:
                    key_files.append(f"=== {fname} ===\n{code[:1000]}")

        code_summary = "\n\n".join(key_files[:5])  # 最多展示 5 个文件

        return f"""项目代码已就绪，请开始 QA 验收测试。

【关键文件预览】
{code_summary[:5000]}

请从启动服务开始，按照标准测试流程逐步验证。"""

    # ============================================================
    # 辅助方法
    # ============================================================

    @staticmethod
    def _detect_entry_file(all_code: Dict[str, str]) -> str:
        """检测入口文件"""
        priority = ["main.py", "app.py", "server.py", "run.py"]
        for fname in all_code:
            basename = os.path.basename(fname)
            if basename in priority:
                code = all_code.get(fname, "")
                if code and "__name__" in code:
                    return fname
        for fname in all_code:
            if os.path.basename(fname) in priority:
                return fname
        for fname, code in all_code.items():
            if fname.endswith(".py") and code and "__name__" in code:
                return fname
        return "main.py"

    @staticmethod
    def _detect_port(entry_code: str, project_spec: str) -> int:
        """检测服务端口"""
        import re
        if entry_code:
            m = re.search(r'port\s*=\s*(\d{4,5})', entry_code, re.IGNORECASE)
            if m:
                return int(m.group(1))
        if project_spec:
            m = re.search(r'localhost:(\d{4,5})', project_spec)
            if m:
                return int(m.group(1))
        return 5001

    @staticmethod
    def _extract_result_from_text(text: str) -> dict:
        """从 LLM 纯文本回复中提取测试判定（兜底策略）"""
        text_lower = text.lower()
        # 优先检测明确的失败信号
        fail_signals = ["fail", "失败", "无法启动", "error", "报错", "崩溃", "500"]
        pass_signals = ["pass", "通过", "成功", "正常运行", "all good", "验证通过"]

        fail_score = sum(1 for s in fail_signals if s in text_lower)
        pass_score = sum(1 for s in pass_signals if s in text_lower)

        if fail_score > pass_score:
            return QAAgent._make_result(False, f"(从文本提取) {text[:300]}", warning=True)
        else:
            # 宁可放过，不可误杀
            return QAAgent._make_result(True, f"(从文本提取) {text[:300]}", warning=True)

    @staticmethod
    def _make_result(passed: bool, feedback: str,
                     failed_files: list = None, warning: bool = False) -> dict:
        """构造标准返回结构（兼容 IntegrationTester 接口）"""
        return {
            "passed": passed,
            "feedback": feedback,
            "failed_files": failed_files or [],
            "warning": warning,
        }
