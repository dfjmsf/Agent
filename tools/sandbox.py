import os
import uuid
import subprocess
import logging
from typing import Dict, Any

logger = logging.getLogger("Sandbox")

# 沙盒运行的相关配置
WORKSPACE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "workspace")
MAX_OUTPUT_LENGTH = 1500  # Token 熔断机制：硬性截断过长的输出
EXECUTION_TIMEOUT = 60    # 单次执行超时时间（秒）

class PythonSandbox:
    """
    绝对安全审查沙盒 (Subprocess Sandbox)
    
    核心防御逻辑：
    - 将大模型生成的任意命令或代码，强制封装写入临时文件 (形如 _run_task_*.py)
    - 独立唤起 python 子进程执行，通过 capture_output=True 捕获输出，杜绝直接 CMD 注入带来的 UAC 弹窗死锁。
    - 自带输出截断功能，防止死循环和巨型堆栈拉断 Token 池。
    """
    
    def __init__(self):
        if not os.path.exists(WORKSPACE_DIR):
            os.makedirs(WORKSPACE_DIR)

    def _truncate_output(self, text: str) -> str:
        """输出硬性截断，保留最后 N 个字符 (通常是最底层的报错堆栈)"""
        if not text:
            return ""
        if len(text) <= MAX_OUTPUT_LENGTH:
            return text
        return f"\n...[Output Truncated (输出过长已截断)]...\n{text[-MAX_OUTPUT_LENGTH:]}"

    def _cleanup_old_task_scripts(self, keep: int = 10):
        """滚动清理过期的沙盒脚本，防止硬盘爆满"""
        try:
            files = []
            for f in os.listdir(WORKSPACE_DIR):
                if f.startswith("_run_task_") and f.endswith(".py"):
                    full_path = os.path.join(WORKSPACE_DIR, f)
                    if os.path.isfile(full_path):
                        files.append((full_path, os.path.getmtime(full_path)))
            
            # 按修改时间降序排列（索引 0 是最新文件）
            files.sort(key=lambda x: x[1], reverse=True)
            
            # 删除老文件
            if len(files) > keep:
                for file_path, _ in files[keep:]:
                    try:
                        os.remove(file_path)
                    except Exception as clean_e:
                        logger.warning(f"无法清理旧沙盒文件 {file_path}: {clean_e}")
        except Exception as e:
            logger.error(f"执行沙盒空间清理时发生异常: {e}")

    def execute_code(self, code_string: str, stdin_data: str = None) -> Dict[str, Any]:
        """
        核心执行方法：将代码写入临时文件并安全执行。

        参数:
            code_string: 要执行的 Python 代码
            stdin_data: 可选的 stdin 输入数据。如果不提供，stdin 将被切断（DEVNULL），
                        含 input() 的代码会立即抛 EOFError 而非永久阻塞。

        返回值包含: success (布尔), stdout, stderr, returncode
        """
        # 1. 执行前先滚动清理旧包袱
        self._cleanup_old_task_scripts(keep=10)

        # 2. 生成符合规范的临时脚本名 (前缀必须是 _run_ 以避开 git 追踪)
        task_id = uuid.uuid4().hex[:8]
        script_name = f"_run_task_{task_id}.py"
        script_path = os.path.join(WORKSPACE_DIR, script_name)

        # 2. 写入沙盒工作区
        try:
            with open(script_path, "w", encoding="utf-8") as f:
                f.write(code_string)
            logger.info(f"已创建沙盒临时任务脚本: {script_name}")
        except Exception as e:
            logger.error(f"写入沙盒脚本当场失败: {e}")
            return {
                "success": False,
                "stdout": "",
                "stderr": f"Failed to write script to workspace: {e}",
                "returncode": -1
            }

        # 3. 隔离执行 (Subprocess 核心)
        try:
            logger.info(f"⏳ 正在沙盒中隔离执行: {script_name} (Timeout: {EXECUTION_TIMEOUT}s)...")
            # 构建 stdin 参数：有数据则管道喂入，无数据则切断防阻塞
            stdin_kwargs: Dict[str, Any] = {}
            if stdin_data is not None:
                stdin_kwargs["input"] = stdin_data.encode("utf-8")
            else:
                stdin_kwargs["stdin"] = subprocess.DEVNULL

            result = subprocess.run(
                ["python", script_path],
                cwd=WORKSPACE_DIR,       # 强制将工作目录限制在 workspace 下
                capture_output=True,     # 拦截标准输出和错误
                timeout=EXECUTION_TIMEOUT, # 防死循环超时保护
                **stdin_kwargs
            )

            # Manually decode the output streams using fallback (utf-8 -> gbk -> replace)
            def safe_decode(bts: bytes) -> str:
                if not bts: return ""
                try:
                    return bts.decode("utf-8")
                except UnicodeDecodeError:
                    try:
                        return bts.decode("gbk")
                    except UnicodeDecodeError:
                        return bts.decode("utf-8", errors="replace")
            
            stdout_str = safe_decode(result.stdout)
            stderr_str = safe_decode(result.stderr)

            # 4. 提取并截断结果
            stdout_truncated = self._truncate_output(stdout_str)
            stderr_truncated = self._truncate_output(stderr_str)
            
            success = (result.returncode == 0)
            
            if success:
                logger.info(f"✅ 执行成功 (Return: 0)")
            else:
                logger.warning(f"❌ 执行失败 (Return: {result.returncode})")

            return {
                "success": success,
                "stdout": stdout_truncated,
                "stderr": stderr_truncated,
                "returncode": result.returncode
            }

        except subprocess.TimeoutExpired:
            logger.error(f"⚠️ 执行超时 (>{EXECUTION_TIMEOUT}s): {script_name}")
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

# 全局单例
sandbox_env = PythonSandbox()
