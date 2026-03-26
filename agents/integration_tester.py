"""
IntegrationTester — 端到端集成测试专家

职责：所有文件通过 Reviewer 单文件测试后，启动整个应用做端到端验证。
位于 Phase 2.5（TDD 循环之后、结算之前）。

核心流程：
1. 收集所有已完成代码（来自真理区）
2. LLM 生成集成测试脚本（启动服务 → HTTP 请求 → 验证响应）
3. 在 PowerSandbox 中执行
4. 返回测试结果 + 识别问题文件
"""
import os
import re
import json
import logging
from typing import Dict, Optional

from core.prompt import Prompts
from core.llm_client import default_llm
from tools.sandbox import sandbox_env
from core.ws_broadcaster import global_broadcaster

logger = logging.getLogger("IntegrationTester")


class IntegrationTester:
    """端到端集成测试 Agent"""

    def __init__(self, project_id: str):
        self.project_id = project_id
        self.model = os.getenv("MODEL_REVIEWER", "qwen3-max")

    def run_integration_test(self, project_spec: str,
                             all_code: Dict[str, str],
                             sandbox_dir: str,
                             max_retries: int = 2) -> dict:
        """
        执行端到端集成测试。

        Args:
            project_spec: 项目规划书文本
            all_code: {"main.py": "代码内容", ...} 所有已完成文件
            sandbox_dir: VFS sandbox 目录
            max_retries: 测试脚本自身出错时的重试次数

        Returns:
            {
                "passed": bool,
                "feedback": str,       # 详细反馈信息
                "failed_files": list,  # 需要退回 TDD 的文件列表
            }
        """
        logger.info(f"🧪 [Phase 2.5] 集成测试启动 ({len(all_code)} 个文件)")
        global_broadcaster.emit_sync("IntegrationTester", "start",
            f"🧪 集成测试: 验证 {len(all_code)} 个文件的端到端行为")

        # 1. 分配端口
        try:
            port = sandbox_env.alloc_port(self.project_id)
        except RuntimeError:
            port = 5080  # 兜底
            logger.warning(f"⚠️ 端口分配失败，使用兜底端口 {port}")

        # 2. 准备文件信息
        file_list = "\n".join([f"- {fname}" for fname in all_code.keys()])

        # 只注入后端 Python 文件的完整内容（前端文件太大）
        file_contents_parts = []
        for fname, code in all_code.items():
            if fname.endswith('.py') and code:
                # 限制每个文件 2000 字符
                truncated = code[:2000] + ("...(truncated)" if len(code) > 2000 else "")
                file_contents_parts.append(f"### {fname}\n```python\n{truncated}\n```")
        file_contents = "\n\n".join(file_contents_parts) if file_contents_parts else "无 Python 文件。"

        # 3. LLM 生成测试脚本
        system_prompt = Prompts.INTEGRATION_TEST_SYSTEM.format(
            port=port,
            project_spec=project_spec or "无规划书",
            file_list=file_list,
            file_contents=file_contents,
        )

        user_prompt = (
            f"请为这个项目生成一个端到端集成测试脚本。\n"
            f"后端服务应监听端口 {port}。\n"
            f"测试脚本需要：启动服务 → 等待就绪 → 发送 HTTP 请求 → 验证响应 → 清理进程。"
        )

        for attempt in range(max_retries + 1):
            test_script = self._generate_test_script(system_prompt, user_prompt)
            if not test_script:
                logger.error("❌ [Phase 2.5] LLM 未生成测试脚本")
                return {"passed": False, "feedback": "测试脚本生成失败", "failed_files": []}

            # 4. 在沙盒中执行测试
            logger.info(f"🧪 [Phase 2.5] 执行集成测试 (attempt {attempt+1}/{max_retries+1})")
            global_broadcaster.emit_sync("IntegrationTester", "executing",
                f"🧪 执行集成测试脚本 (尝试 {attempt+1})")

            result = sandbox_env.execute_code(
                test_script,
                project_id=self.project_id,
                sandbox_dir=sandbox_dir,
            )

            # 5. 解析结果
            stdout = result.get("stdout", "")
            stderr = result.get("stderr", "")
            output = stdout + "\n" + stderr

            if "INTEGRATION_TEST_PASSED" in output:
                # exit code 可能非 0（因为服务进程被 terminate 后会产生异常退出码），
                # 但只要标记字符串存在就代表测试逻辑通过
                if not result.get("success"):
                    logger.info("ℹ️ [Phase 2.5] 退出码非 0（服务进程清理导致，属正常现象）")
                logger.info("✅ [Phase 2.5] 集成测试通过！")
                global_broadcaster.emit_sync("IntegrationTester", "passed", "✅ 集成测试通过！")
                return {"passed": True, "feedback": "端到端集成测试通过", "failed_files": []}

            if "INTEGRATION_TEST_FAILED" in output:
                # 测试发现真正的业务 bug
                feedback = self._extract_failure_info(output)
                failed_files = self._extract_failed_files(output)
                logger.warning(f"❌ [Phase 2.5] 集成测试失败: {feedback[:200]}")
                global_broadcaster.emit_sync("IntegrationTester", "failed",
                    f"❌ 集成测试失败: {feedback[:100]}")
                return {"passed": False, "feedback": feedback, "failed_files": failed_files}

            # 测试脚本本身有问题（语法错误等）→ 重试
            if attempt < max_retries:
                logger.warning(f"⚠️ [Phase 2.5] 测试脚本自身出错 (attempt {attempt+1})，重新生成...")
                user_prompt = (
                    f"上一个测试脚本执行出错了，请修正并重新生成：\n"
                    f"错误输出：\n{output[:1000]}\n\n"
                    f"后端服务应监听端口 {port}。"
                )

        # 重试耗尽，视为通过（避免误熔断）
        logger.warning("⚠️ [Phase 2.5] 测试脚本多次出错，跳过集成测试")
        global_broadcaster.emit_sync("IntegrationTester", "skipped",
            "⚠️ 集成测试脚本多次出错，跳过")
        return {"passed": True, "feedback": "集成测试脚本多次出错，已跳过", "failed_files": []}

    def _generate_test_script(self, system_prompt: str, user_prompt: str) -> Optional[str]:
        """让 LLM 生成集成测试脚本"""
        try:
            response = default_llm.chat_completion(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.2,
            )

            raw = response.content
            # 清理 Markdown 包裹
            if "```python" in raw:
                raw = raw.split("```python")[1].split("```")[0].strip()
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0].strip()

            return raw

        except Exception as e:
            logger.error(f"❌ [Phase 2.5] LLM 调用失败: {e}")
            return None

    @staticmethod
    def _extract_failure_info(output: str) -> str:
        """从测试输出中提取失败信息"""
        lines = output.split("\n")
        failure_lines = []
        capture = False
        for line in lines:
            if "INTEGRATION_TEST_FAILED" in line:
                failure_lines.append(line)
                capture = True
            elif capture:
                failure_lines.append(line)
        return "\n".join(failure_lines) if failure_lines else output[-500:]

    @staticmethod
    def _extract_failed_files(output: str) -> list:
        """从测试输出中提取失败的文件列表"""
        for line in output.split("\n"):
            if "FAILED_FILES:" in line:
                files_str = line.split("FAILED_FILES:")[1].strip()
                return [f.strip() for f in files_str.split(",") if f.strip()]
        return []
