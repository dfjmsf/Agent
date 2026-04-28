"""
SandboxTerminalSkill — QA 特化沙盒终端技能

从原 SkillRunner._skill_run_terminal 原样提取。
包含完整的进程生命周期管理：
- _cleanup_server: 清理后台启动的服务进程
- _kill_port_occupant: 端口抢占检测与强杀（含 Werkzeug reloader 子进程治理）

⚠️ 这是有状态的 QA 特化 Skill，不可直接公共化。
"""
import os
import subprocess
import logging

from core.skills.base import BaseSkill

logger = logging.getLogger("SkillRunner")

# 默认超时
TERMINAL_TIMEOUT = 30


class SandboxTerminalSkill(BaseSkill):
    """沙盒终端 — QA Agent 专属，带进程生命周期管理"""

    def __init__(self, sandbox_dir: str, venv_python: str = "python"):
        self.sandbox_dir = os.path.abspath(sandbox_dir)
        self.venv_python = venv_python
        self._server_proc = None

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "run_terminal",
                "description": (
                    "在项目沙盒目录中执行终端命令。"
                    "用于启动服务(如 python app.py)、查看进程状态、检查日志等。"
                    "命令会在项目的 sandbox venv 环境中执行。"
                    "如果需要后台启动服务，请在命令末尾加 & (Linux) 或使用 start 参数。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "command": {
                            "type": "string",
                            "description": "要执行的终端命令（如 'python app.py'、'ls -la'）"
                        },
                        "timeout": {
                            "type": "integer",
                            "description": "命令超时秒数，默认 30 秒。后台服务建议设为 5 秒（启动后自动超时返回）",
                        },
                        "background": {
                            "type": "boolean",
                            "description": "是否后台运行（用于启动服务）。后台进程会在 QA 结束时自动清理。",
                        },
                    },
                    "required": ["command"],
                },
            },
        }

    def execute(self, **kwargs) -> str:
        command = kwargs["command"]
        timeout = kwargs.get("timeout", TERMINAL_TIMEOUT)
        background = kwargs.get("background", False)

        # 安全护栏: 替换 python → sandbox venv python
        cmd = command.strip()
        if cmd.startswith("python ") or cmd == "python":
            cmd = f'"{self.venv_python}" {cmd[7:]}'
        elif cmd.startswith("python3 "):
            cmd = f'"{self.venv_python}" {cmd[8:]}'

        logger.info(f"🔧 [Skill:run_terminal] {cmd} (bg={background}, timeout={timeout}s)")

        env = os.environ.copy()
        # 清理敏感环境变量
        for key in ("QWEN_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY",
                     "GPT_API_KEY", "DATABASE_URL"):
            env.pop(key, None)

        if background:
            return self._run_background(cmd, env)
        else:
            return self._run_foreground(cmd, env, timeout)

    def _run_background(self, cmd: str, env: dict) -> str:
        """后台启动服务"""
        try:
            self.cleanup_server()

            # 端口抢占检测
            self._kill_port_occupant(cmd)

            proc = subprocess.Popen(
                cmd, shell=True, cwd=self.sandbox_dir,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env,
            )
            self._server_proc = proc
            # 等待一小段时间看是否立即崩溃
            import time
            time.sleep(2)
            if proc.poll() is not None:
                stderr = ""
                try:
                    stderr = proc.stderr.read().decode("utf-8", errors="replace")[:1000]
                except Exception:
                    pass
                return f"服务启动失败（立即退出，退出码 {proc.returncode}）\nstderr: {stderr}"
            return f"服务已在后台启动 (PID={proc.pid})"
        except Exception as e:
            return f"后台启动失败: {e}"

    def _run_foreground(self, cmd: str, env: dict, timeout: int) -> str:
        """前台执行命令（超时时强杀进程树，防止 Windows 下 shell=True 泄漏子进程）"""
        proc = None
        try:
            proc = subprocess.Popen(
                cmd, shell=True, cwd=self.sandbox_dir,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                env=env,
            )
            stdout_bytes, stderr_bytes = proc.communicate(timeout=timeout)
            stdout = stdout_bytes.decode("utf-8", errors="replace")[:3000]
            stderr = stderr_bytes.decode("utf-8", errors="replace")[:1500]
            parts = []
            if stdout.strip():
                parts.append(f"stdout:\n{stdout}")
            if stderr.strip():
                parts.append(f"stderr:\n{stderr}")
            if not parts:
                parts.append("(无输出)")
            parts.append(f"退出码: {proc.returncode}")
            return "\n".join(parts)
        except subprocess.TimeoutExpired:
            # 强杀整棵进程树（Windows: taskkill /T, Linux: killpg）
            if proc:
                try:
                    if os.name == "nt":
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        )
                    else:
                        import signal
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    proc.wait(timeout=3)
                except Exception:
                    pass
            return f"命令超时 ({timeout}s)。如果是启动服务，请使用 background=true。"
        except Exception as e:
            if proc and proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait(timeout=2)
                except Exception:
                    pass
            return f"执行失败: {e}"

    # ============================================================
    # 进程生命周期管理
    # ============================================================

    def cleanup_server(self):
        """清理后台启动的服务进程"""
        if self._server_proc and self._server_proc.poll() is None:
            logger.info(f"🧹 清理后台服务进程 PID={self._server_proc.pid}")
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(self._server_proc.pid)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                else:
                    import signal
                    os.killpg(os.getpgid(self._server_proc.pid), signal.SIGKILL)
                self._server_proc.wait(timeout=3)
            except Exception:
                pass
            self._server_proc = None

    def _kill_port_occupant(self, cmd: str):
        """
        端口抢占检测：从启动命令中提取目标端口，检查是否被占用并强杀。
        如果无法推断，则自动清理常见的 ASTrea 端口 (5001, 3000)。
        """
        import re
        import socket

        ports_to_check = set()
        port_match = re.search(r'(?:port[=\s]+|--port\s+|-p\s+|:)(\d{4,5})', cmd)
        if port_match:
            ports_to_check.add(int(port_match.group(1)))
        else:
            if "python " in cmd or "app.py" in cmd:
                ports_to_check.add(5001)
            if "npm " in cmd or "node " in cmd:
                ports_to_check.add(3000)

        for port in ports_to_check:
            if port < 1024 or port > 65535:
                continue

            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                result = sock.connect_ex(('127.0.0.1', port))
                sock.close()
                if result != 0:
                    continue  # 端口空闲

                logger.warning(f"⚠️ [端口抢占] 端口 {port} 已被占用，尝试清理残留进程...")
                if os.name == "nt":
                    r = subprocess.run(
                        f'netstat -ano | findstr ":{port} "',
                        shell=True, capture_output=True, text=True
                    )
                    pids = set()
                    for line in r.stdout.strip().split("\n"):
                        parts = line.split()
                        if len(parts) >= 5 and "LISTENING" in line:
                            pids.add(parts[-1])
                    for pid in pids:
                        logger.warning(f"🔪 [端口抢占] Kill PID {pid}")
                        subprocess.run(
                            ["taskkill", "/F", "/T", "/PID", pid],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                        )
                    # 深度清理：Werkzeug reloader 子进程
                    subprocess.run(
                        'powershell -Command "Get-WmiObject Win32_Process | Where-Object { $_.CommandLine -match \'sandboxes.*app.py\' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"',
                        shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                else:
                    subprocess.run(
                        f"fuser -k {port}/tcp",
                        shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                import time
                time.sleep(1)
                logger.info(f"✅ [端口抢占] 端口 {port} 已清理")
            except Exception as e:
                logger.warning(f"⚠️ [端口抢占] 清理异常: {e}")
