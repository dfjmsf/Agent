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
import re
import ast
from typing import Dict, Optional, Any, List

from core.llm_client import default_llm
from core.skill_runner import SkillRunner
from core.ws_broadcaster import global_broadcaster

logger = logging.getLogger("QAAgent")

MAX_STEPS = 16       # ReAct 循环默认最大步数（可被动态覆盖）
MAX_STEPS_MIN = 10   # 动态计算下限
MAX_STEPS_MAX = 25   # 动态计算上限
FUSE_THRESHOLD = 3   # 连续相同错误 N 次 → 熔断


class QAAgent:
    """QA Agent — ReAct 循环驱动的集成测试专家"""

    def __init__(self, project_id: str):
        self.project_id = project_id
        self.model = os.getenv("MODEL_QA", "deepseek-chat")
        _et, _re = default_llm.parse_thinking_config(os.getenv("THINKING_QA", "false"))
        self.enable_thinking = _et
        self._reasoning_effort = _re

    def run_qa(self, project_spec: str, all_code: Dict[str, str],
               sandbox_dir: str, venv_python: str = "",
               focus_endpoints: list = None) -> dict:
        """
        执行 QA 验收测试。

        v4.4: 按需测试 — 有 focus_endpoints 时只测受影响端点 + 回归冒烟。

        Args:
            project_spec: Manager 规划书文本
            all_code: {文件名: 代码内容} 字典
            sandbox_dir: 沙盒目录路径
            venv_python: sandbox venv 的 python 路径
            focus_endpoints: 按需测试的端点列表（如 ["GET /stats", "POST /delete/1"]）

        Returns:
            {"passed": bool, "feedback": str, "failed_files": list, "warning": bool}
        """
        mode_hint = f"按需测试 {len(focus_endpoints)} 个端点" if focus_endpoints else "全量测试"
        logger.info(f"🧪 [QA Agent] 启动 ReAct 测试循环 ({len(all_code)} 个文件, {mode_hint})")
        global_broadcaster.emit_sync("QAAgent", "start",
            f"🧪 QA Agent 启动: {mode_hint}")

        # 初始化 SkillRunner
        skill_runner = SkillRunner(
            sandbox_dir=sandbox_dir,
            project_id=self.project_id,
            venv_python=venv_python,
        )

        try:
            return self._react_loop(project_spec, all_code, skill_runner,
                                    focus_endpoints=focus_endpoints)
        finally:
            skill_runner.cleanup()

    def _react_loop(self, project_spec: str, all_code: Dict[str, str],
                    skill_runner: SkillRunner,
                    focus_endpoints: list = None) -> dict:
        """ReAct 主循环"""

        # 构建初始消息
        system_prompt = self._build_system_prompt(project_spec, all_code,
                                                  focus_endpoints=focus_endpoints)
        user_prompt = self._build_user_prompt(all_code)

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        tool_schemas = skill_runner.get_tool_schemas()
        last_errors = []  # 用于连续错误检测
        no_tool_count = 0  # 连续无 tool_call 计数

        # v5.3: 动态 MAX_STEPS — 根据端点数量弹性计算
        estimated_endpoints = len(focus_endpoints) if focus_endpoints else self._estimate_endpoint_count(all_code)
        dynamic_max = max(MAX_STEPS_MIN, min(MAX_STEPS_MAX, 6 + estimated_endpoints * 2))
        logger.info(f"📊 [QA Agent] 动态步数上限: {dynamic_max} (预估 {estimated_endpoints} 端点)")

        for step in range(1, dynamic_max + 1):
            remaining = dynamic_max - step
            logger.info(f"🔄 [QA Agent] Step {step}/{dynamic_max} (剩余 {remaining} 步)")
            global_broadcaster.emit_sync("QAAgent", "step",
                f"🔄 QA 测试 Step {step}/{dynamic_max}")

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
                    enable_thinking=self.enable_thinking,
                    reasoning_effort=self._reasoning_effort,
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
                    return self._make_result(
                        passed,
                        feedback,
                        failed_files,
                        error_type=arguments.get("error_type", ""),
                        importer_file=arguments.get("importer_file", ""),
                        provider_file=arguments.get("provider_file", ""),
                        missing_symbols=arguments.get("missing_symbols", []),
                        repair_scope=arguments.get("repair_scope", []),
                        endpoint_results=arguments.get("endpoint_results", []),
                    )

                # 执行 Skill
                result_text = skill_runner.execute(func_name, arguments)
                logger.info(f"📤 [QA Agent] Skill 结果: {result_text[:150]}")

                # 将 tool 结果加入对话
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result_text[:800],  # 严格限制避免 context 雪球
                })

                startup_failure_result = self._handle_startup_failure(
                    func_name=func_name,
                    arguments=arguments,
                    result_text=result_text,
                    all_code=all_code,
                    messages=messages,
                    skill_runner=skill_runner,
                )
                if startup_failure_result is not None:
                    return startup_failure_result

                # 连续错误检测（仅对 http_request / run_terminal 等执行型 Skill 计数）
                # read_file / check_port 是调查行为，不应累积"相同错误"计数
                if func_name in ("http_request", "run_terminal"):
                    if "错误" in result_text or "失败" in result_text or "500" in result_text:
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
        logger.warning(f"🔥 [QA Agent] 达到最大步数 {dynamic_max}，尝试提取判定...")
        global_broadcaster.emit_sync("QAAgent", "fused",
            f"🔥 QA Agent 达到最大步数 {dynamic_max}，提取判定中...")

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
            False, f"QA Agent 熔断: 达到最大步数 {dynamic_max}，未能完成测试",
            warning=True,
        )

    # ============================================================
    # Prompt 构建
    # ============================================================

    def _build_system_prompt(self, project_spec: str, all_code: Dict[str, str],
                             focus_endpoints: list = None) -> str:
        """构建 QA Agent 的 System Prompt（v4.4: 按需测试模式支持）"""
        file_list = "\n".join(f"  - {f}" for f in sorted(all_code.keys()))

        # 检测入口文件
        entry_file = self._detect_entry_file(all_code)

        # 检测端口
        port = self._detect_port(all_code.get(entry_file, ""), project_spec)

        # v4.4: 动态测试策略
        if focus_endpoints:
            focus_list = "\n".join(f"  - {ep}" for ep in focus_endpoints)
            test_strategy = (
                f"【⚠️ 按需测试模式 — 本次仅修改了以下功能】\n"
                f"优先测试（必须逐个验证）:\n{focus_list}\n\n"
                f"回归冒烟: 从其他端点中任选 1-2 个做快速验证（如 GET / 测首页可达），确认修复未破坏已有功能。\n"
                f"测完以上端点后直接 report_result，不需要全量测试所有端点。\n"
                f"步数预算：启动服务 + check_port + 重点端点 + 1-2 个冒烟 + report_result ≈ {len(focus_endpoints) + 5} 步内完成。"
            )
        else:
            test_strategy = (
                "对项目的核心功能端点逐个发 http_request 测试\n"
                "（如 POST /add, GET /edit/1, POST /update/1, POST /delete/1 等）"
            )

        return f"""你是 QA 验收工程师 (QA Agent)。你的唯一任务是验证项目代码能否正常运行。

【你的能力】
你拥有 6 个工具。每一步必须调用工具，禁止只说话不行动。
1. run_terminal — 在项目目录执行终端命令
2. read_file — 读取项目文件（仅在遇到错误需要诊断时使用！不要预防性阅读）
3. http_request — 对 localhost 发 HTTP 请求
4. check_port — 检查端口是否在监听
5. check_ui_visuals — 利用多模态大模型判定目标页面的 UI 是否美观且无溢出
6. report_result — 提交最终判定（必须调用此工具结束测试）

【⚠️ 步数预算：你最多只有 {MAX_STEPS} 步！】

严格按以下顺序执行：
  Step 1: run_terminal(command="python {entry_file}", background=true) 启动服务
  Step 2: check_port(port={port}) 确认服务就绪
  Step 3: http_request(method="GET", url="http://127.0.0.1:{port}/") 测试首页
  Step 4: check_ui_visuals(url="http://127.0.0.1:{port}/", query="请详细审查主页排版、文字边界以及美观度，是否存在溢出等视觉缺陷") [⚠️ 如果该项目带有 Web 页面（如包含 HTML/React/Vue），必须执行此步检查！如果是纯 API 项目请跳过]
  Step 5+: {test_strategy}
  (⚠️ 提交表单时必须带 headers={{"Content-Type": "application/x-www-form-urlencoded"}})
  最后一步: report_result(...) 提交判定

【效率铁律】
- 不要在测试前 read_file！你已经有代码预览了
- 不要重复测试同一个端点
- 若 Step 1 启动即失败（ImportError / SyntaxError / cannot import name）：
  - 允许额外 read_file 1-2 次诊断
  - 立即 report_result(passed=false)，填 error_type / importer_file / provider_file / missing_symbols

【⚠️ 核心规则：测完所有端点再汇总！】
- 遇到某个端点返回 400/404/500 时，**记录下来但继续测试下一个端点**
- 只有服务完全无法启动时，才允许立即 report_result(passed=false)
- 所有端点测完后（或步数快用完时），一次性调用 report_result 汇总

【report_result 填写规则】
- passed: 所有被测端点都返回 2xx 且 UI 无严重崩塌 → true；否则 → false
- feedback: 简要总结（如 "5/7 端点通过, POST /add 返回 500, POST /update/1 返回 500"）
- **endpoint_results 必须填写！** 列出每个已测端点的逐条结果：
  - method: HTTP 方法
  - url: 完整 URL
  - status_code: 响应状态码
  - ok: 是否通过 (2xx = true)
  - detail: 失败时的简要原因（如 "TypeError: update_expense() takes 1 argument"）
- failed_files: 根据失败端点推断需修复的文件列表
- feedback 只写事实，**严禁写"问题分析"或"修复建议"！你是测试员不是修理工！**

【判定标准】
- PASS: 服务能启动 + 所有被测端点都返回 2xx + UI 无严重视觉崩塌
- FAIL（任一即判 FAIL）:
  · 服务无法启动
  · 任何端点返回非 2xx
  · UI 严重样式崩塌（白屏、代码暴露、完全错位）

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

        # v5.3: 检测 Vite 前端代理层
        vite_proxy_hint = ""
        has_vite = any(
            fname in ("vite.config.js", "vite.config.ts",
                      "vite.config.mjs", "vite.config.mts")
            for fname in all_code.keys()
        )
        if has_vite:
            vite_port = 5173  # Vite 默认端口
            for vname in ("vite.config.js", "vite.config.ts", "vite.config.mjs", "vite.config.mts"):
                if vname in all_code:
                    vite_code = all_code[vname]
                    port_match = re.search(r'port\s*[:=]\s*(\d{4,5})', vite_code)
                    if port_match:
                        vite_port = int(port_match.group(1))
                    break
            vite_proxy_hint = (
                f"\n\n【⚠️ Vite 前端代理层检测到】\n"
                f"本项目包含 Vite 前端，开发服务器端口: {vite_port}\n"
                f"后端 API 通过 Vite 代理转发。请额外验证:\n"
                f"  - run_terminal(command='npx vite --port {vite_port}', background=true)\n"
                f"  - check_port(port={vite_port}) 确认 Vite 就绪\n"
                f"  - http_request(url='http://127.0.0.1:{vite_port}/') 测试前端页面\n"
                f"  - 对比直连后端 ({port}) 和 Vite 代理 ({vite_port}) 的 API 响应是否一致"
            )

        return prompt + vite_proxy_hint

    def _build_user_prompt(self, all_code: Dict[str, str]) -> str:
        """构建首轮 user 消息 — v5.3: 入口/路由文件优先完整展示"""
        # 按优先级分类文件
        entry_names = {"app.py", "main.py", "server.py", "run.py", "wsgi.py"}
        route_names = {"routes.py", "views.py", "api.py", "urls.py"}

        priority_files = []   # 入口 + 路由：完整展示
        secondary_files = []  # 其他代码文件：截断展示

        for fname in sorted(all_code.keys()):
            if not fname.endswith((".py", ".js")):
                continue
            code = all_code[fname]
            if not code:
                continue
            basename = os.path.basename(fname)
            if basename in entry_names or basename in route_names:
                # 入口/路由文件：最多 3000 字符（确保路由定义不被截断）
                priority_files.append(f"=== {fname} (完整) ===\n{code[:3000]}")
            else:
                secondary_files.append(f"=== {fname} ===\n{code[:800]}")

        # 优先级文件最多 3 个，其余文件最多 3 个
        parts = priority_files[:3] + secondary_files[:3]
        code_summary = "\n\n".join(parts)

        return f"""项目代码已就绪，请开始 QA 验收测试。

【关键文件预览】
{code_summary[:8000]}

请从启动服务开始，按照标准测试流程逐步验证。"""

    # ============================================================
    # 辅助方法
    # ============================================================

    @staticmethod
    def _estimate_endpoint_count(all_code: Dict[str, str]) -> int:
        """从代码中预估 HTTP 端点数量（用于动态 MAX_STEPS 计算）"""
        count = 0
        # Flask/FastAPI 路由装饰器模式
        route_patterns = [
            r'@\w+\.route\(',        # Flask: @app.route(...)
            r'@\w+\.get\(',          # FastAPI: @app.get(...)
            r'@\w+\.post\(',         # FastAPI: @app.post(...)
            r'@\w+\.put\(',          # FastAPI: @app.put(...)
            r'@\w+\.delete\(',       # FastAPI: @app.delete(...)
            r'@\w+\.patch\(',        # FastAPI: @app.patch(...)
        ]
        combined = '|'.join(route_patterns)
        for fname, code in all_code.items():
            if not fname.endswith('.py') or not code:
                continue
            matches = re.findall(combined, code)
            count += len(matches)
        # 最少返回 3（首页 + 基本 CRUD），避免低估
        return max(3, count)

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
                     failed_files: list = None, warning: bool = False,
                     **extra_fields: Any) -> dict:
        """构造标准返回结构（兼容 IntegrationTester 接口）"""
        result = {
            "passed": passed,
            "feedback": feedback,
            "failed_files": failed_files or [],
            "warning": warning,
        }
        for key, value in extra_fields.items():
            if value not in (None, "", [], {}):
                result[key] = value
        return result

    def _handle_startup_failure(
        self,
        func_name: str,
        arguments: Dict[str, Any],
        result_text: str,
        all_code: Dict[str, str],
        messages: List[Dict[str, Any]],
        skill_runner: SkillRunner,
    ) -> Optional[dict]:
        """启动即失败时做一次窄范围结构化诊断，避免只返回首个报错。"""
        if func_name != "run_terminal" or not arguments.get("background"):
            return None

        if "服务启动失败" not in result_text and "后台启动失败" not in result_text:
            return None

        diagnosis = self._diagnose_startup_failure(result_text, all_code)
        if not diagnosis:
            return None

        importer_file = diagnosis.get("importer_file", "")
        provider_file = diagnosis.get("provider_file", "")
        for file_path in [importer_file, provider_file]:
            if not file_path:
                continue
            file_result = skill_runner.execute("read_file", {"file_path": file_path})
            logger.info(f"📤 [QA Agent] 启动失败补充读取 {file_path}: {file_result[:120]}")
            messages.append({
                "role": "tool",
                "tool_call_id": f"startup_diag_{file_path}",
                "content": file_result[:800],
            })

        status = "❌ 失败"
        logger.info(f"📋 [QA Agent] 启动失败特判: {status} — {diagnosis.get('feedback', '')[:100]}")
        global_broadcaster.emit_sync(
            "QAAgent",
            "failed",
            f"❌ QA 判定: {diagnosis.get('feedback', '')[:80]}",
        )
        return self._make_result(
            False,
            diagnosis["feedback"],
            diagnosis.get("failed_files", []),
            error_type=diagnosis.get("error_type", ""),
            importer_file=diagnosis.get("importer_file", ""),
            provider_file=diagnosis.get("provider_file", ""),
            missing_symbols=diagnosis.get("missing_symbols", []),
            repair_scope=diagnosis.get("repair_scope", []),
        )

    def _diagnose_startup_failure(self, result_text: str, all_code: Dict[str, str]) -> Dict[str, Any]:
        """本地解析启动失败结果，提取结构化上下文。"""
        stderr = result_text
        if "stderr:" in result_text:
            stderr = result_text.split("stderr:", 1)[1].strip()

        syntax_match = re.search(
            r"File [\"'].*?([A-Za-z0-9_./\\\\-]+\.py)[\"'].*?SyntaxError:\s*(.+)",
            stderr,
            re.DOTALL,
        )
        if syntax_match:
            importer_file = self._normalize_project_path(syntax_match.group(1), all_code)
            failed_files = [importer_file] if importer_file else []
            return {
                "error_type": "APP_BOOT_SYNTAX_ERROR",
                "feedback": f"服务启动失败，Python 语法错误：{syntax_match.group(2).strip()}",
                "failed_files": failed_files,
                "importer_file": importer_file,
                "repair_scope": failed_files,
            }

        import_match = re.search(
            r"cannot import name ['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]? from ['\"]?([A-Za-z_][A-Za-z0-9_\.]*)['\"]?",
            stderr,
        )
        if not import_match:
            return {}

        missing_symbol = import_match.group(1).strip()
        import_module = import_match.group(2).strip()
        importer_file = self._find_importer_file(all_code, import_module, missing_symbol)
        provider_file = self._resolve_provider_file(importer_file, import_module, all_code)
        imported_symbols = self._extract_imported_symbols(
            all_code.get(importer_file, ""),
            import_module,
        ) if importer_file else []
        provider_symbols = self._extract_defined_symbols(
            all_code.get(provider_file, "")
        ) if provider_file else set()

        missing_symbols = [
            symbol for symbol in imported_symbols
            if symbol not in provider_symbols
        ]
        if missing_symbol and missing_symbol not in missing_symbols:
            missing_symbols.insert(0, missing_symbol)
        if not missing_symbols:
            missing_symbols = [missing_symbol]

        repair_scope = [path for path in [provider_file, importer_file] if path]
        failed_files = repair_scope or ([importer_file] if importer_file else [])

        feedback = (
            f"服务启动失败，Python 导入错误：{importer_file or '未知文件'} 从 "
            f"{provider_file or import_module} 导入缺失符号。"
            f"缺失集合: {', '.join(missing_symbols)}。"
        )

        return {
            "error_type": "IMPORT_SYMBOL_MISSING",
            "feedback": feedback,
            "failed_files": failed_files,
            "importer_file": importer_file,
            "provider_file": provider_file,
            "missing_symbols": missing_symbols,
            "repair_scope": repair_scope,
        }

    @staticmethod
    def _normalize_project_path(raw_path: str, all_code: Dict[str, str]) -> str:
        raw_path = str(raw_path or "").replace("\\", "/")
        if raw_path in all_code:
            return raw_path
        for path in all_code.keys():
            if raw_path.endswith(path):
                return path
        return ""

    def _find_importer_file(self, all_code: Dict[str, str], import_module: str, missing_symbol: str) -> str:
        ordered_files = [path for path in all_code.keys() if path.endswith(".py")]
        for path in ordered_files:
            symbols = self._extract_imported_symbols(all_code.get(path, ""), import_module)
            if missing_symbol in symbols:
                return path
        return ordered_files[0] if ordered_files else ""

    @staticmethod
    def _resolve_provider_file(importer_file: str, import_module: str, all_code: Dict[str, str]) -> str:
        module_rel = import_module.replace(".", "/").strip("/")
        if not module_rel:
            return ""

        candidates = [
            f"{module_rel}.py",
            f"{module_rel}/__init__.py",
        ]
        if importer_file:
            importer_dir = os.path.dirname(importer_file).replace("\\", "/").strip("/")
            if importer_dir:
                candidates.extend([
                    f"{importer_dir}/{module_rel}.py",
                    f"{importer_dir}/{module_rel}/__init__.py",
                ])

        seen = set()
        for candidate in candidates:
            normalized = os.path.normpath(candidate).replace("\\", "/")
            if normalized in seen or normalized.startswith("../"):
                continue
            seen.add(normalized)
            if normalized in all_code:
                return normalized
        return ""

    @staticmethod
    def _extract_imported_symbols(importer_code: str, import_module: str) -> List[str]:
        if not importer_code:
            return []

        try:
            tree = ast.parse(importer_code)
        except SyntaxError:
            return []

        symbols: List[str] = []
        module_suffix = f".{import_module}"
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if node.module != import_module and not node.module.endswith(module_suffix):
                continue
            for alias in node.names:
                if alias.name != "*" and alias.name not in symbols:
                    symbols.append(alias.name)
        return symbols

    @staticmethod
    def _extract_defined_symbols(provider_code: str) -> set:
        if not provider_code:
            return set()

        try:
            tree = ast.parse(provider_code)
        except SyntaxError:
            return set()

        symbols = set()
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                symbols.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        symbols.add(target.id)
        return symbols
