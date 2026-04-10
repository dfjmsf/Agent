"""
SkillRunner — QA Agent 的 Skill 执行引擎

将 LLM 的 tool_call 翻译为真实沙盒操作。
所有操作限定在 sandbox_dir 内部，绝不允许越狱。

安全护栏:
- run_terminal: cwd 强制锁定 sandbox_dir, 使用 sandbox venv python
- read_file: 路径校验禁止 ../
- http_request: 只允许 127.0.0.1 / localhost
- 每个 Skill 有独立超时
"""
import os
import re
import json
import socket
import subprocess
import logging
from typing import Optional
from urllib.parse import urlparse

logger = logging.getLogger("SkillRunner")

# Skill 超时配置
TERMINAL_TIMEOUT = 30
HTTP_TIMEOUT = 10
FILE_READ_MAX = 50000  # 字符


class SkillRunner:
    """Skill 执行引擎 — QA Agent 专属"""

    def __init__(self, sandbox_dir: str, project_id: str, venv_python: str = ""):
        """
        Args:
            sandbox_dir: 沙盒目录（QA 的全部操作边界）
            project_id: 项目 ID
            venv_python: sandbox venv 的 python 可执行文件路径
        """
        self.sandbox_dir = os.path.abspath(sandbox_dir)
        self.project_id = project_id
        self.venv_python = venv_python or "python"
        self._server_proc = None  # 跟踪 QA 启动的后台服务进程

    # ============================================================
    # Tool Schemas — 喂给 LLM 的函数签名
    # ============================================================

    @staticmethod
    def get_tool_schemas() -> list:
        """返回 QA Agent 可用的所有 Skill 的 JSON Schema"""
        return [
            {
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
            },
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "description": "读取项目中指定文件的内容。用于查看源代码、配置文件、日志等。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "file_path": {
                                "type": "string",
                                "description": "相对于项目根目录的文件路径（如 'app.py'、'templates/index.html'）"
                            },
                        },
                        "required": ["file_path"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "http_request",
                    "description": (
                        "向本地运行的服务发送 HTTP 请求。"
                        "用于验证 API 端点是否正常工作。只能请求 localhost。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "method": {
                                "type": "string",
                                "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"],
                                "description": "HTTP 方法"
                            },
                            "url": {
                                "type": "string",
                                "description": "完整 URL（如 'http://127.0.0.1:5001/api/items'）"
                            },
                            "body": {
                                "type": "object",
                                "description": "请求体（JSON 格式，用于 POST/PUT）"
                            },
                            "headers": {
                                "type": "object",
                                "description": "自定义请求头"
                            },
                        },
                        "required": ["method", "url"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "check_port",
                    "description": "检查指定端口是否正在监听。用于确认服务是否已启动就绪。",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "port": {
                                "type": "integer",
                                "description": "要检查的端口号（如 5001）"
                            },
                        },
                        "required": ["port"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "report_result",
                    "description": (
                        "提交最终测试判定。调用此工具将终止测试循环。"
                        "必须在充分测试后才调用。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "passed": {
                                "type": "boolean",
                                "description": "测试是否通过"
                            },
                            "feedback": {
                                "type": "string",
                                "description": "测试结果的详细描述（通过时写测试摘要，失败时写具体错误和修复建议）"
                            },
                            "failed_files": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "失败时需要修复的文件列表（如 ['app.py', 'models.py']）"
                            },
                        },
                        "required": ["passed", "feedback"],
                    },
                },
            },
        ]

    # ============================================================
    # 统一执行入口
    # ============================================================

    def execute(self, tool_name: str, arguments: dict) -> str:
        """
        执行一个 Skill，返回文本结果。

        Args:
            tool_name: Skill 名称
            arguments: LLM 传入的参数字典

        Returns:
            执行结果的文本描述
        """
        dispatch = {
            "run_terminal": self._skill_run_terminal,
            "read_file": self._skill_read_file,
            "http_request": self._skill_http_request,
            "check_port": self._skill_check_port,
            "report_result": self._skill_report_result,
        }

        handler = dispatch.get(tool_name)
        if not handler:
            return f"错误: 未知的 Skill '{tool_name}'"

        try:
            return handler(**arguments)
        except TypeError as e:
            return f"错误: 参数不匹配 — {e}"
        except Exception as e:
            logger.error(f"Skill '{tool_name}' 执行异常: {e}")
            return f"错误: {type(e).__name__}: {e}"

    # ============================================================
    # Skill 实现
    # ============================================================

    def _skill_run_terminal(self, command: str, timeout: int = TERMINAL_TIMEOUT,
                            background: bool = False) -> str:
        """在 sandbox 中执行终端命令"""
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
            # 后台启动服务
            try:
                self._cleanup_server()  # 清理之前的服务

                # 端口抢占检测：如果目标端口被外部进程占用，先干掉它
                # 防止 QA 测到旧项目的残留服务（这是"幽灵 CSRF 400"的根因）
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
        else:
            # 前台执行
            try:
                result = subprocess.run(
                    cmd, shell=True, cwd=self.sandbox_dir,
                    capture_output=True, timeout=timeout, env=env,
                )
                stdout = result.stdout.decode("utf-8", errors="replace")[:3000]
                stderr = result.stderr.decode("utf-8", errors="replace")[:1500]
                parts = []
                if stdout.strip():
                    parts.append(f"stdout:\n{stdout}")
                if stderr.strip():
                    parts.append(f"stderr:\n{stderr}")
                if not parts:
                    parts.append("(无输出)")
                parts.append(f"退出码: {result.returncode}")
                return "\n".join(parts)
            except subprocess.TimeoutExpired:
                return f"命令超时 ({timeout}s)。如果是启动服务，请使用 background=true。"
            except Exception as e:
                return f"执行失败: {e}"

    def _skill_read_file(self, file_path: str) -> str:
        """读取 sandbox 中的文件"""
        # 安全护栏: 路径校验
        normalized = os.path.normpath(file_path)
        if normalized.startswith("..") or os.path.isabs(normalized):
            return f"错误: 禁止访问项目目录之外的路径 '{file_path}'"

        full_path = os.path.join(self.sandbox_dir, normalized)
        abs_full = os.path.abspath(full_path)

        # 二次校验: 确认在 sandbox 内
        if not abs_full.startswith(self.sandbox_dir):
            return f"错误: 路径越界 '{file_path}'"

        if not os.path.isfile(abs_full):
            return f"文件不存在: {file_path}"

        try:
            with open(abs_full, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(FILE_READ_MAX)
            if len(content) >= FILE_READ_MAX:
                content += "\n... (文件过大，已截断)"
            return content
        except Exception as e:
            return f"读取失败: {e}"

    def _skill_http_request(self, method: str, url: str,
                            body: dict = None, headers: dict = None) -> str:
        """对 localhost 发 HTTP 请求"""
        import urllib.request
        import urllib.error

        # 安全护栏: 只允许 localhost
        parsed = urlparse(url)
        if parsed.hostname not in ("127.0.0.1", "localhost", "0.0.0.0"):
            return f"错误: 只允许请求 localhost，不允许 '{parsed.hostname}'"

        try:
            req_headers = {"Content-Type": "application/json"}
            if headers:
                req_headers.update(headers)

            data = None
            if body and method.upper() in ("POST", "PUT", "PATCH"):
                data = json.dumps(body).encode("utf-8")

            req = urllib.request.Request(
                url, data=data, headers=req_headers, method=method.upper()
            )

            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                status = resp.status
                resp_body = resp.read().decode("utf-8", errors="replace")[:500]
                return f"HTTP {status} OK\n{resp_body}"

        except urllib.error.HTTPError as e:
            body_text = ""
            try:
                body_text = e.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            return f"⚠️ HTTP {e.code} {e.reason} (非200响应！)\n{body_text}"
        except urllib.error.URLError as e:
            return f"连接失败: {e.reason}"
        except TimeoutError:
            return f"请求超时 ({HTTP_TIMEOUT}s)"
        except Exception as e:
            return f"请求异常: {type(e).__name__}: {e}"

    def _skill_check_port(self, port: int) -> str:
        """检查端口是否在监听"""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            result = s.connect_ex(("127.0.0.1", port))
            s.close()
            if result == 0:
                return f"端口 {port} 正在监听 ✅"
            else:
                return f"端口 {port} 未监听 ❌"
        except Exception as e:
            return f"端口检查失败: {e}"

    def _skill_report_result(self, passed: bool, feedback: str,
                             failed_files: list = None) -> str:
        """提交最终测试判定（此方法的返回值不重要，由 QA Agent 拦截处理）"""
        return json.dumps({
            "passed": passed,
            "feedback": feedback,
            "failed_files": failed_files or [],
        })

    # ============================================================
    # 资源清理
    # ============================================================

    def _cleanup_server(self):
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
            # 如果命令行里没写 port，通常是在代码里写死的，Astrea 平台最常见的端口是 5001 和 3000
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
                    # Windows: 深度清理！因为 Werkzeug reloader 生成的子进程经常脱离进程树，
                    # 导致父进程死了但子进程依然霸占端口。我们将所有在 sandboxes 中运行的 app.py 统统杀掉。
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

    def cleanup(self):
        """QA Agent 结束时调用的总清理"""
        self._cleanup_server()


if __name__ == "__main__":
    """简单自测"""
    import tempfile

    print("=== SkillRunner 自测 ===\n")

    with tempfile.TemporaryDirectory() as td:
        runner = SkillRunner(sandbox_dir=td, project_id="test", venv_python="python")

        # Test 1: get_tool_schemas
        schemas = runner.get_tool_schemas()
        assert len(schemas) == 5
        names = {s["function"]["name"] for s in schemas}
        assert names == {"run_terminal", "read_file", "http_request", "check_port", "report_result"}
        print(f"✅ Test 1: 5 个 Skill Schema 完整")

        # Test 2: run_terminal
        result = runner.execute("run_terminal", {"command": "echo hello_world"})
        assert "hello_world" in result
        print(f"✅ Test 2: run_terminal echo → {result.strip()[:50]}")

        # Test 3: read_file (存在的文件)
        with open(os.path.join(td, "test.txt"), "w") as f:
            f.write("hello from file")
        result = runner.execute("read_file", {"file_path": "test.txt"})
        assert "hello from file" in result
        print(f"✅ Test 3: read_file → '{result.strip()[:30]}'")

        # Test 4: read_file (越狱防护)
        result = runner.execute("read_file", {"file_path": "../../etc/passwd"})
        assert "禁止" in result or "错误" in result
        print(f"✅ Test 4: 越狱防护 → '{result.strip()[:50]}'")

        # Test 5: check_port (未监听的端口)
        result = runner.execute("check_port", {"port": 59999})
        assert "未监听" in result
        print(f"✅ Test 5: check_port 59999 → '{result.strip()}'")

        # Test 6: http_request (localhost 校验)
        result = runner.execute("http_request", {"method": "GET", "url": "http://evil.com/api"})
        assert "错误" in result
        print(f"✅ Test 6: 外部 URL 拦截 → '{result.strip()[:50]}'")

        # Test 7: report_result
        result = runner.execute("report_result", {"passed": True, "feedback": "all good"})
        parsed = json.loads(result)
        assert parsed["passed"] is True
        print(f"✅ Test 7: report_result → {parsed}")

        # Test 8: 未知 Skill
        result = runner.execute("edit_file", {"path": "x"})
        assert "未知" in result
        print(f"✅ Test 8: 未知 Skill 拦截 → '{result.strip()[:50]}'")

        runner.cleanup()

    print("\n🎉 SkillRunner 全部自测通过！")
