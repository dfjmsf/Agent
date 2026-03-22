import os
import json
import logging
from typing import Dict, Any, Tuple
from core.llm_client import default_llm
from core.prompt import Prompts
from tools.sandbox import sandbox_env
from core.ws_broadcaster import global_broadcaster
from core.database import get_recent_events

logger = logging.getLogger("ReviewerAgent")

class ReviewerAgent:
    """
    审查与执行 Agent (Reviewer)
    专职：编写测试脚本 -> 调用沙盒 -> 拿到结果 -> 给出 PASS 或是具体报错信息。
    """
    def __init__(self, project_id: str = "default_project"):
        self.model = os.getenv("MODEL_REVIEWER", "qwen3-max")
        self.project_id = project_id
        self._last_stderr = ""  # 沙盒 stderr 缓存（用于 DeprecationWarning 硬判）

    def _execute_sandbox_tool(self, tool_call, sandbox_dir: str = None) -> str:
        """解析 LLM 传来的工具请求，并在物理沙盒中执行"""
        try:
            args = json.loads(tool_call.function.arguments)
            test_code = args.get("test_code_string", "")
            
            logger.info("🛡️ Reviewer 唤起本地沙盒执行测试代码...")
            global_broadcaster.emit_sync("Reviewer", "sandbox_start", "Reviewer 正在将其验证脚本压入沙盒容器...", {"test_code": test_code})
            
            result = sandbox_env.execute_code(test_code, self.project_id,
                                              sandbox_dir=sandbox_dir)
            
            # 捕获 stderr 用于硬判（DeprecationWarning 检测）
            self._last_stderr = result.get("stderr", "")
            
            # 格式化沙盒返回结果给大模型看
            global_broadcaster.emit_sync("Reviewer", "sandbox_end", f"沙盒测试完毕，退出码 {result.get('returncode', 'Unknown')}", {"result": result})
            return json.dumps(result, ensure_ascii=False)
                
        except Exception as e:
            return f"工具调用解析失败: {e}"

    @staticmethod
    def _is_reviewer_script_error(stderr: str) -> bool:
        """
        检测沙盒失败是否由 Reviewer 自己写的测试脚本引起（而非 Coder 代码 bug）。
        
        匹配模式：stderr 中包含指向 _run_task_*.py 的 SyntaxError/IndentationError。
        这些错误说明测试脚本本身就不是合法 Python，不应归咎于 Coder。
        """
        if not stderr:
            return False
        
        # 测试脚本文件名特征
        script_pattern = "_run_task_"
        # Reviewer 测试脚本自身的典型错误类型
        script_errors = ["SyntaxError", "IndentationError", "TabError"]
        
        # 检测 stderr 中是否同时包含测试脚本文件名和语法错误
        has_script_file = script_pattern in stderr
        has_syntax_error = any(err in stderr for err in script_errors)
        
        return has_script_file and has_syntax_error

    def evaluate_draft(self, target_file: str, description: str,
                       code_content: str = None, sandbox_dir: str = None) -> Tuple[bool, str]:
        """
        评估特定的文件草稿
        
        Args:
            target_file: 目标文件路径
            description: 任务描述
            code_content: v1.3 新参数 — Engine 传入的已缝合代码（优先使用）
        
        返回:
            is_pass (bool): 是否审查通过
            feedback (str): 如果没通过，具体的修改建议和报错；如果通过，则为空或简短评语。
        """
        # v1.3: 使用 Engine 传入的代码
        code_draft = code_content
        if not code_draft:
            return False, "没有找到该文件的代码内容（Engine 未传入 code_content）"

        logger.info(f"🛡️ Reviewer 正在审查文件: {target_file}")
        global_broadcaster.emit_sync("Reviewer", "review_start", f"开始审查目标文件: {target_file}", {"target": target_file, "code": code_draft})

        # 记忆注入（Reviewer 仅保留文件树，用于生成测试脚本时的 import 参考）
        memory_hint = ""

        # 短期记忆 → 项目文件树
        file_tree_events = get_recent_events(
            project_id=self.project_id, limit=1,
            event_types=["file_tree"], caller="Reviewer"
        )
        if file_tree_events:
            memory_hint += f"\n\n【📂 当前项目文件结构】\n{file_tree_events[0].content[:500]}"

        system_prompt = Prompts.REVIEWER_SYSTEM + memory_hint
        user_content = f"【当前要审查的文件】: {target_file}\n【业务需求描述】: {description}\n【Coder提交的代码内容】:\n```python\n{code_draft}\n```\n\n请立即使用 `sandbox_execute` 工具生成并执行一段测试脚本！测试脚本应 import 该文件中的类/函数进行黑盒测试。如果无法 import（比如该文件是入口配置），请写一段语法检查即可。"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ]

        # 第一轮请求：要求 LLM 使用测试工具
        response_msg = default_llm.chat_completion(
            messages=messages,
            model=self.model,
            tools=Prompts.REVIEWER_TOOL_SCHEMA
        )

        messages.append(response_msg) # 把 AI 的回复原样加进历史

        # 如果大模型乖乖调用了沙盒
        sandbox_failed = False       # 退出码硬判标志
        sandbox_passed = False       # 沙盒通过标志（returncode=0）
        reviewer_script_error = False  # Reviewer 测试脚本自身错误标志
        if hasattr(response_msg, "tool_calls") and response_msg.tool_calls:
            for tool_call in response_msg.tool_calls:
                if tool_call.function.name == "sandbox_execute":
                    tool_result_str = self._execute_sandbox_tool(tool_call, sandbox_dir=sandbox_dir)
                    
                    # 记录沙盒退出码，用于后续硬判
                    try:
                        _result = json.loads(tool_result_str)
                        rc = _result.get("returncode", -1)
                        stderr_text = _result.get("stderr", "")
                        
                        if rc != 0:
                            # 检测是否是 Reviewer 测试脚本自身的语法错误
                            if self._is_reviewer_script_error(stderr_text):
                                reviewer_script_error = True
                                logger.warning("⚠️ Reviewer 测试脚本自身有语法错误，不归咒 Coder")
                            else:
                                sandbox_failed = True
                        else:
                            sandbox_passed = True
                    except (json.JSONDecodeError, TypeError):
                        sandbox_failed = True
                    
                    # 将沙盒结果附加到消息流中
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.function.name,
                        "content": tool_result_str
                    })
                    
            # 拿到测试结果后，构建轻量级消息发起第二轮裁定
            # 优化: 不重复完整 system_prompt，但注入被测代码摘要供 LLM 参考
            code_summary = code_draft[:800] if len(code_draft) > 800 else code_draft
            verdict_messages = [
                {"role": "system", "content": "你是代码审查裁定官。根据沙盒测试结果判定代码质量。严格返回 JSON：{\"status\": \"PASS\" 或 \"FAIL\", \"feedback\": \"原因或改进建议\"}。注意：如果 stderr 中出现 DeprecationWarning/FutureWarning，必须判 FAIL 并在 feedback 中说明修复方案。"},
                {"role": "user", "content": f"被测文件: {target_file}\n\n【被测代码摘要】:\n```\n{code_summary}\n```\n\n【沙盒测试结果】:\n{tool_result_str}\n\n请分析结果并返回 JSON 裁定。切勿返回其他冗余内容。"}
            ]
            
            logger.info("🕵️ Reviewer 正在根据沙盒结果出具最终报告...")
            final_response = default_llm.chat_completion(
                messages=verdict_messages,
                model=self.model
            )
            report_text = final_response.content
            
        else:
            # 大模型没调用工具，直接给出结果了 (幻觉或偷懒)
            logger.warning("⚠️ Reviewer 未调用测试沙盒，只进行了代码静态肉眼白盒审查。")
            report_text = response_msg.content

        # 解析最终裁定
        # 防止 LLM 加了 ```json 等标签
        report_text = report_text.replace("```json", "").replace("```", "").strip()
        
        try:
            report_dict = json.loads(report_text)
            status = report_dict.get("status", "FAIL")
            feedback = report_dict.get("feedback", report_text)
            
            is_pass = (status.upper() == "PASS")
            
            # 硬判 1：沙盒退出码非零时，强制覆盖 LLM 的 PASS 为 FAIL
            if sandbox_failed and is_pass:
                logger.warning("⚠️ 沙盒退出码非零但 LLM 判定 PASS → 强制覆盖为 FAIL（禁止放水）")
                is_pass = False
                feedback = f"[代码bug] {feedback}"

            # 硬判 2：沙盒退出码为零时，强制覆盖 LLM 的 FAIL 为 PASS（禁止冤枉）
            if sandbox_passed and not is_pass:
                logger.warning(f"⚠️ 沙盒退出码=0 但 LLM 判定 FAIL → 强制覆盖为 PASS（测试已通过，禁止冤枉 Coder）")
                logger.warning(f"   LLM 原始理由: {feedback[:200]}")
                is_pass = True
                feedback = "测试通过"

            # 硬判 3：Reviewer 测试脚本自身语法错误 → 不归咒 Coder，强制 PASS
            if reviewer_script_error and not is_pass:
                logger.warning("⚠️ Reviewer 测试脚本自身有语法错误，强制 PASS（这是 Reviewer 的错，不是 Coder 的错）")
                is_pass = True
                feedback = "测试通过（Reviewer 测试脚本自身有语法错误，跳过本次审查）"

            # 硬判 4：检测 stderr 中的弃用警告（退出码可能为 0，优先级最高）
            if is_pass and hasattr(self, '_last_stderr') and self._last_stderr:
                deprecation_keywords = ['DeprecationWarning', 'PendingDeprecationWarning', 'FutureWarning']
                for kw in deprecation_keywords:
                    if kw in self._last_stderr:
                        logger.warning(f"⚠️ 检测到 {kw}，强制驳回")
                        is_pass = False
                        feedback = f"[系统强制驳回] 沙盒 stderr 中检测到 {kw}，当前代码使用了已弃用的 API。\nstderr: {self._last_stderr[:500]}\n请根据警告信息修复代码。"
                        break
            
            if is_pass:
                logger.info(f"✅ Reviewer 盖章通过！")
                global_broadcaster.emit_sync("Reviewer", "review_pass", "审查通过！", {"feedback": feedback})
            else:
                logger.warning(f"❌ Reviewer 驳回草稿！已生成反馈意见。")
                global_broadcaster.emit_sync("Reviewer", "review_fail", "审查未通过！", {"feedback": feedback})
                
            return is_pass, feedback
            
        except json.JSONDecodeError:
            # 兼容 LLM 没有按要求输出 JSON 的情况
            logger.error(f"Reviewer 输出了非标准 JSON 报告: {report_text[:100]}...")
            if "PASS" in report_text and "FAIL" not in report_text:
                global_broadcaster.emit_sync("Reviewer", "review_pass", "审查通过！(非标准JSON)", {"feedback": report_text})
                return True, report_text
            global_broadcaster.emit_sync("Reviewer", "review_fail", "审查未通过！(非标准JSON)", {"feedback": report_text})
            return False, report_text
