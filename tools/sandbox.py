import os
import uuid
import subprocess
import logging
import tempfile
from typing import Dict, Any
from core.state_manager import global_state_manager

logger = logging.getLogger("Sandbox")

MAX_OUTPUT_LENGTH = 1500  # Token 熔断机制：硬性截断过长的输出
EXECUTION_TIMEOUT = 60    # 单次执行超时时间（秒）

class PythonSandbox:
    """
    阅后即焚沙盒 (Ephemeral Subprocess Sandbox)
    
    核心防御逻辑：
    - 废除持久化的 workspace 目录。每次执行创建一个临时隔离舱 (tempfile.TemporaryDirectory)。
    - 将对应的内存 VFS (project_id) 全量克隆入临时舱。
    - 跑完跳出 with 块后，操作系统自动 rm -rf 擦除一切痕迹（防沙盘膨胀）。
    """
    def _truncate_output(self, text: str) -> str:
        """输出硬性截断，保留最后 N 个字符"""
        if not text:
            return ""
        if len(text) <= MAX_OUTPUT_LENGTH:
            return text
        return f"\n...[Output Truncated (输出过长已截断)]...\n{text[-MAX_OUTPUT_LENGTH:]}"

    def execute_code(self, code_string: str, project_id: str, stdin_data: str = None) -> Dict[str, Any]:
        """
        核心执行方法：建立阅后即焚容器，跑完即抛弃。
        """
        vfs = global_state_manager.get_vfs(project_id)
        
        # 使用 Python 的 tempfile.TemporaryDirectory 构建阅后即焚沙盒
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                # 1. 挂载环境：将当前项目草稿箱里的代码全尺寸拷贝进临时目录，供 import
                vfs.sync_to_sandbox(temp_dir)
                
                # 2. 生成运行的主脚本
                task_id = uuid.uuid4().hex[:8]
                script_name = f"_run_task_{task_id}.py"
                script_path = os.path.join(temp_dir, script_name)
                
                with open(script_path, "w", encoding="utf-8") as f:
                    f.write(code_string)
                logger.info(f"⏳ 正在阅后即焚沙盒中隔离执行: {script_name} (Timeout: {EXECUTION_TIMEOUT}s)...")
                
                # 3. 隔离执行
                stdin_kwargs: Dict[str, Any] = {}
                if stdin_data is not None:
                    stdin_kwargs["input"] = stdin_data # Text mode
                else:
                    stdin_kwargs["stdin"] = subprocess.DEVNULL

                env = os.environ.copy()
                env["PYTHONIOENCODING"] = "utf-8"

                result = subprocess.run(
                    ["python", script_path],
                    cwd=temp_dir,            # 绝对限制在此临时壳内
                    capture_output=True,     # 拦截全部输出
                    timeout=EXECUTION_TIMEOUT,
                    env=env,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    **stdin_kwargs
                )

                # Safe Text
                stdout_str = result.stdout or ""
                stderr_str = result.stderr or ""

                # 4. 截断防爆
                stdout_truncated = self._truncate_output(stdout_str)
                stderr_truncated = self._truncate_output(stderr_str)
                
                success = (result.returncode == 0)
                
                if success:
                    logger.info("✅ 执行成功 (Return: 0)")
                else:
                    logger.warning(f"❌ 执行失败 (Return: {result.returncode})")

                return {
                    "success": success,
                    "stdout": stdout_truncated,
                    "stderr": stderr_truncated,
                    "returncode": result.returncode
                }

        except subprocess.TimeoutExpired:
            logger.error(f"⚠️ 执行超时 (>{EXECUTION_TIMEOUT}s)")
            return {
                "success": False,
                "stdout": "",
                "stderr": f"Execution timed out after {EXECUTION_TIMEOUT} seconds. (可能存在死循环，进程已被强制终止)",
                "returncode": -1
            }
        except Exception as e:
            logger.error(f"⚠️ 沙盒框架严重异常: {e}")
            return {
                "success": False,
                "stdout": "",
                "stderr": f"Sandbox critical failure: {str(e)}",
                "returncode": -1
            }

sandbox_env = PythonSandbox()
