import os
import json
import logging
from typing import Dict, Any, Tuple
from core.llm_client import default_llm
from core.prompt import Prompts
from core.state_manager import global_state_manager
from tools.sandbox import sandbox_env
from core.ws_broadcaster import global_broadcaster
from core.database import recall

logger = logging.getLogger("ReviewerAgent")

class ReviewerAgent:
    """
    审查与执行 Agent (Reviewer)
    专职：编写测试脚本 -> 调用沙盒 -> 拿到结果 -> 给出 PASS 或是具体报错信息。
    """
    def __init__(self, project_id: str = "default_project"):
        self.model = os.getenv("MODEL_REVIEWER", "qwen3-max")
        self.project_id = project_id

    def _execute_sandbox_tool(self, tool_call) -> str:
        """解析 LLM 传来的工具请求，并在物理沙盒中执行"""
        try:
            args = json.loads(tool_call.function.arguments)
            test_code = args.get("test_code_string", "")
            
            logger.info("🛡️ Reviewer 唤起本地沙盒执行测试代码...")
            global_broadcaster.emit_sync("Reviewer", "sandbox_start", "Reviewer 正在将其验证脚本压入沙盒容器...", {"test_code": test_code})
            # 【重要闭环】在运行测试前，必须确保当前 VFS 中的所有草稿文件
            # 已经被物理写入了 Sandbox 的目录下，否则测试脚本里的 import 会报错。
            # 这部分现在由 Ephemeral Sandbox 内部的 temporary_directory 处理了。
            
            result = sandbox_env.execute_code(test_code, self.project_id)
            
            # 格式化沙盒返回结果给大模型看
            global_broadcaster.emit_sync("Reviewer", "sandbox_end", f"沙盒测试完毕，退出码 {result.get('returncode', 'Unknown')}", {"result": result})
            return json.dumps(result, ensure_ascii=False)
                
        except Exception as e:
            return f"工具调用解析失败: {e}"

    def evaluate_draft(self, target_file: str, description: str) -> Tuple[bool, str]:
        """
        评估特定的文件草稿
        
        返回:
            is_pass (bool): 是否审查通过
            feedback (str): 如果没通过，具体的修改建议和报错；如果通过，则为空或简短评语。
        """
        vfs = global_state_manager.get_vfs(self.project_id)
        code_draft = vfs.get_draft(target_file)
        if not code_draft:
            return False, "VFS 中没有找到该文件的代码草稿"

        logger.info(f"🛡️ Reviewer 正在审查文件: {target_file}")
        global_broadcaster.emit_sync("Reviewer", "review_start", f"开始审查目标文件: {target_file}", {"target": target_file, "code": code_draft})

        # 召回历史测试经验 (RAG 长期记忆)
        past_tips = recall(f"测试 {target_file} {description}", n_results=2, project_id=self.project_id, caller="Reviewer")
        memory_hint = ""
        if past_tips:
            memory_hint = "\n\n【历史测试经验 (RAG 长期记忆)】\n" + "\n".join([f"- {tip}" for tip in past_tips])

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
        if hasattr(response_msg, "tool_calls") and response_msg.tool_calls:
            for tool_call in response_msg.tool_calls:
                if tool_call.function.name == "sandbox_execute":
                    tool_result_str = self._execute_sandbox_tool(tool_call)
                    
                    # 将沙盒结果附加到消息流中
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "name": tool_call.function.name,
                        "content": tool_result_str
                    })
                    
            # 拿到测试结果后，发起第二轮交互，让 LLM 做最终裁定
            messages.append({
                "role": "user", 
                "content": "你已经拿到了沙盒测试结果。请分析结果，并严格返回 JSON 格式：\n{\"status\": \"PASS\" 或 \"FAIL\", \"feedback\": \"原因或改进建议\"}。\n切勿返回其他冗余内容。"
            })
            
            logger.info("🕵️ Reviewer 正在根据沙盒结果出具最终报告...")
            final_response = default_llm.chat_completion(
                messages=messages,
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
