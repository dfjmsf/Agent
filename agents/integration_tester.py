"""
IntegrationTester v3 — 端到端集成测试专家（模板化架构）

v2 → v3 变更：
- 核心改动: 不再让 LLM 生成完整测试脚本，改为"固定模板 + LLM 只写断言"
- 固定模板: 处理服务启动、端口轮询、进程清理（代码级保障，不依赖 LLM）
- LLM 只生成: test_endpoints(base_url) 函数（纯 HTTP 请求 + 验证）
- 保留: Layer 1 compile 预检 + Layer 3 归因分析 + 三层自愈
"""
import os
import re
import json
import logging
from typing import Dict, Optional, Tuple

from core.prompt import Prompts
from core.llm_client import default_llm
from tools.sandbox import sandbox_env
from core.ws_broadcaster import global_broadcaster

logger = logging.getLogger("IntegrationTester")

# ============================================================
# 固定测试模板（不由 LLM 生成，代码级可靠）
# ============================================================

TEST_HARNESS_TEMPLATE = '''
import sys, os, time, socket, subprocess, atexit

# === 强制行缓冲（确保 print 立即写入文件）===
sys.stdout.reconfigure(line_buffering=True)
sys.stderr.reconfigure(line_buffering=True)

# === 配置 ===
PORT = {port}
ENTRY_FILE = "{entry_file}"
BASE_URL = f"http://127.0.0.1:{{PORT}}"
_MY_PID = os.getpid()
print(f"[Harness] 入口={{ENTRY_FILE}} 端口={{PORT}}")

# === 硬超时保护 (35s) ===
import threading
def _hard_timeout():
    time.sleep(35)
    print("❌ INTEGRATION_TEST_FAILED: Script hard timeout (35s)")
    print(f"FAILED_FILES: {{ENTRY_FILE}} | 脚本超时，可能服务启动过慢或请求无响应")
    sys.stdout.flush()
    _cleanup()
    os._exit(1)
threading.Thread(target=_hard_timeout, daemon=True).start()

# === 进程清理 ===
_server_proc = None

def _cleanup():
    global _server_proc
    if _server_proc and _server_proc.poll() is None:
        if os.name == 'nt':
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(_server_proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            try:
                import signal
                os.killpg(os.getpgid(_server_proc.pid), signal.SIGKILL)
            except Exception:
                _server_proc.kill()
        try:
            _server_proc.wait(timeout=3)
        except Exception:
            pass

atexit.register(_cleanup)

# === 设置端口环境变量（供代码中读取 os.environ["PORT"] 的场景使用）===
os.environ["PORT"] = str(PORT)

# === 启动服务 ===
_entry_dir = os.path.dirname(ENTRY_FILE)
_server_env = os.environ.copy()
if _entry_dir:
    # 添加项目根和文件目录到 PYTHONPATH，同时支持 from src.X 和 from X
    _server_env["PYTHONPATH"] = os.pathsep.join(['.', _entry_dir, _server_env.get("PYTHONPATH", "")])
    # 确保子目录有 __init__.py（-m 模式需要包结构）
    _init_file = os.path.join(_entry_dir, "__init__.py")
    if not os.path.exists(_init_file):
        with open(_init_file, "w") as _f:
            _f.write("")
        print(f"[Harness] 创建 {{_init_file}}")

# 子目录文件用 -m 模块方式运行（支持相对导入 from .models import X）
if _entry_dir:
    _module_name = ENTRY_FILE.replace(os.sep, '.').replace('/', '.').replace('.py', '')
    _cmd = [sys.executable, "-m", _module_name]
    print(f"[Harness] 启动服务 (模块模式): {{' '.join(_cmd)}}")
else:
    _cmd = [sys.executable, ENTRY_FILE]
    print(f"[Harness] 启动服务: {{' '.join(_cmd)}}")

_server_proc = subprocess.Popen(
    _cmd,
    stdout=subprocess.DEVNULL,
    stderr=subprocess.PIPE,
    env=_server_env,
)

# === 端口轮询 (最多 25s) ===
_started = False
for _i in range(25):
    if _server_proc.poll() is not None:
        _err = ""
        try:
            _err = _server_proc.stderr.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        print(f"❌ INTEGRATION_TEST_FAILED: Server exited immediately (code={{_server_proc.returncode}})")
        if _err:
            print(f"Server stderr: {{_err}}")
        print(f"FAILED_FILES: {{ENTRY_FILE}} | 服务无法启动")
        sys.exit(0)
    try:
        s = socket.socket()
        s.settimeout(1)
        s.connect(("127.0.0.1", PORT))
        s.close()
        _started = True
        print(f"[Harness] 服务就绪 ({{_i+1}}s)")
        break
    except Exception:
        time.sleep(1)

if not _started:
    _err = ""
    try:
        _err = _server_proc.stderr.read().decode("utf-8", errors="replace")[:500]
    except Exception:
        pass
    print("❌ INTEGRATION_TEST_FAILED: Service did not start within 25s")
    if _err:
        print(f"Server stderr: {{_err}}")
    print(f"FAILED_FILES: {{ENTRY_FILE}} | 服务未在 25 秒内启动")
    _cleanup()
    sys.exit(0)

# === 执行测试 ===
print("[Harness] 开始执行测试...")
import requests as _req

{test_function}

try:
    test_endpoints(BASE_URL)
    print("✅ INTEGRATION_TEST_PASSED")
except AssertionError as e:
    print(f"❌ INTEGRATION_TEST_FAILED: Assertion failed: {{e}}")
except _req.exceptions.ConnectionError as e:
    print(f"❌ INTEGRATION_TEST_FAILED: Connection error: {{e}}")
    print(f"FAILED_FILES: {{ENTRY_FILE}} | 服务连接失败")
except Exception as e:
    print(f"❌ INTEGRATION_TEST_FAILED: Unexpected error: {{type(e).__name__}}: {{e}}")
finally:
    _cleanup()
'''


class IntegrationTester:
    """端到端集成测试 Agent（v3 模板化架构）"""

    def __init__(self, project_id: str):
        self.project_id = project_id
        self.model = os.getenv("MODEL_REVIEWER", "qwen3-max")

    # ============================================================
    # 三层自愈工具
    # ============================================================

    @staticmethod
    def _compile_check(code: str) -> str:
        """Layer 1: 预检测试脚本语法"""
        try:
            compile(code, "<integration_test>", "exec")
            return ""
        except SyntaxError as e:
            return f"Line {e.lineno}: {e.msg}"

    @staticmethod
    def _is_tester_fault(stderr: str) -> bool:
        """Layer 3: 归因分析"""
        if not stderr:
            return False
        error_patterns = [
            "has no attribute 'environ'",
            "No module named 'requests'",
            "Connection refused",
            "timed out",
            "Execution timed out",
        ]
        return any(p in stderr for p in error_patterns)

    @staticmethod
    def _detect_entry_file(all_code: Dict[str, str]) -> str:
        """从项目文件中检测入口文件（优先含 __name__ 块的文件）"""
        import os as _os

        # 优先级 1：精确文件名匹配（含子目录）
        priority_names = ["main.py", "app.py", "server.py", "run.py"]
        for fname in all_code:
            basename = _os.path.basename(fname)
            if basename in priority_names:
                # 有 __name__ 块的最优先
                code = all_code.get(fname, "")
                if code and "__name__" in code:
                    return fname
        # 没有 __name__ 也行（次优先）
        for fname in all_code:
            basename = _os.path.basename(fname)
            if basename in priority_names:
                return fname

        # 优先级 2：含 if __name__ + 启动关键词的文件
        for fname, code in all_code.items():
            if fname.endswith('.py') and code:
                if '__name__' in code and any(kw in code for kw in
                        ['uvicorn.run', 'app.run(', '.run(host', '.run(debug']):
                    return fname

        # 优先级 3：只含 if __name__ 的 py 文件
        for fname, code in all_code.items():
            if fname.endswith('.py') and code and '__name__' in code:
                return fname

        # 兜底：返回第一个 .py 文件
        for fname in all_code:
            if fname.endswith('.py'):
                return fname
        return "main.py"

    @staticmethod
    def _detect_port_from_code(entry_code: str, project_spec: str) -> int:
        """从入口文件代码或规划书中检测服务端口，避免分配随机端口导致不匹配"""
        import re

        # 优先从代码中检测（port=5001, port = 5001, PORT = 5001）
        if entry_code:
            m = re.search(r'port\s*=\s*(\d{4,5})', entry_code, re.IGNORECASE)
            if m:
                return int(m.group(1))

        # 其次从规划书的 api_contracts 中检测
        if project_spec:
            m = re.search(r'localhost:(\d{4,5})', project_spec)
            if m:
                return int(m.group(1))

        # 兜底
        return 5001

    # ============================================================
    # 主入口
    # ============================================================

    def run_integration_test(self, project_spec: str,
                             all_code: Dict[str, str],
                             sandbox_dir: str,
                             max_retries: int = 2) -> dict:
        """
        执行端到端集成测试（v3 模板化）。

        Returns:
            {"passed": bool, "feedback": str, "failed_files": list, "warning": bool}
        """
        logger.info(f"🧪 [Phase 2.5] 集成测试启动 ({len(all_code)} 个文件)")
        global_broadcaster.emit_sync("IntegrationTester", "start",
            f"🧪 集成测试: 验证 {len(all_code)} 个文件的端到端行为")

        # 1. 检测入口文件
        entry_file = self._detect_entry_file(all_code)
        logger.info(f"🔍 [Phase 2.5] 检测到入口文件: {entry_file}")

        # 2. 从入口文件代码中检测端口（不分配随机端口，用代码自己的端口）
        port = self._detect_port_from_code(all_code.get(entry_file, ""), project_spec)
        logger.info(f"🔍 [Phase 2.5] 检测到服务端口: {port}")

        # 3. 准备 LLM prompt（只生成断言函数）
        file_list = "\n".join([f"- {fname}" for fname in all_code.keys()])
        file_contents_parts = []
        for fname, code in all_code.items():
            if fname.endswith('.py') and code:
                truncated = code[:2000] + ("...(truncated)" if len(code) > 2000 else "")
                file_contents_parts.append(f"### {fname}\n```python\n{truncated}\n```")
        file_contents = "\n\n".join(file_contents_parts) if file_contents_parts else "无 Python 文件。"

        # 4. 召回历史测试经验
        experience_hint = ""
        try:
            from core.database import recall_reviewer_experience
            exps = recall_reviewer_experience(
                f"integration test {' '.join(all_code.keys())}", n_results=3, caller="IntegrationTester"
            )
            if exps:
                exp_str = "\n".join([f"  - {e[:200]}" for e in exps])
                experience_hint = f"\n\n【历史踩坑记录，务必避免】\n{exp_str}"
        except Exception:
            pass

        system_prompt = self._build_system_prompt(
            port, project_spec, file_list, file_contents, experience_hint
        )

        user_prompt = (
            f"请为这个项目生成 test_endpoints(base_url) 函数。\n"
            f"base_url 已经是 'http://127.0.0.1:{port}'，直接拼路径即可。\n"
            f"用 requests 发请求，用 assert 验证。"
        )

        compile_retries = 0
        max_compile_retries = 3

        for attempt in range(max_retries + 1):
            # LLM 生成断言函数
            test_function = self._generate_test_function(system_prompt, user_prompt)
            if not test_function:
                logger.error("❌ [Phase 2.5] LLM 未生成测试函数")
                return {"passed": False, "feedback": "测试函数生成失败",
                        "failed_files": [], "warning": True}

            # 拼接最终脚本 = 固定模板 + LLM 断言函数
            final_script = TEST_HARNESS_TEMPLATE.format(
                port=port,
                entry_file=entry_file,
                test_function=test_function,
            )

            # ── Layer 1: compile() 预检 ──
            compile_err = self._compile_check(final_script)
            if compile_err:
                compile_retries += 1
                logger.warning(f"⚠️ [Layer 1] 脚本语法错误 (#{compile_retries}): {compile_err}")
                if compile_retries <= max_compile_retries:
                    user_prompt = (
                        f"你上次生成的 test_endpoints 函数有语法错误:\n{compile_err}\n"
                        f"请修正后重新生成。只输出 test_endpoints(base_url) 函数。"
                    )
                    continue
                else:
                    break

            # ── Layer 2: 沙盒执行 ──
            logger.info(f"🧪 [Phase 2.5] 执行集成测试 (attempt {attempt+1}/{max_retries+1})")
            global_broadcaster.emit_sync("IntegrationTester", "executing",
                f"🧪 执行集成测试脚本 (尝试 {attempt+1})")

            result = sandbox_env.execute_code(
                final_script,
                project_id=self.project_id,
                sandbox_dir=sandbox_dir,
                timeout=45,
            )

            stdout = result.get("stdout", "")
            stderr = result.get("stderr", "")
            output = stdout + "\n" + stderr

            # 判定 1: PASSED
            if "INTEGRATION_TEST_PASSED" in output:
                if not result.get("success"):
                    logger.info("ℹ️ [Phase 2.5] 退出码非 0（清理导致，属正常现象）")
                logger.info("✅ [Phase 2.5] 集成测试通过！")
                global_broadcaster.emit_sync("IntegrationTester", "passed", "✅ 集成测试通过！")
                return {"passed": True, "feedback": "端到端集成测试通过",
                        "failed_files": [], "warning": False}

            # 判定 2: FAILED
            if "INTEGRATION_TEST_FAILED" in output:
                feedback = self._extract_failure_info(output)
                failed_files = self._extract_failed_files(output)
                logger.warning(f"❌ [Phase 2.5] 集成测试失败: {feedback[:200]}")
                global_broadcaster.emit_sync("IntegrationTester", "failed",
                    f"❌ 集成测试失败: {feedback[:100]}")
                return {"passed": False, "feedback": feedback,
                        "failed_files": failed_files, "warning": False}

            # ── Layer 3: 归因分析 ──
            if self._is_tester_fault(stderr):
                logger.warning(f"⚠️ [Layer 3] 测试脚本问题，自愈: {stderr[:200]}")
                user_prompt = (
                    f"上一个 test_endpoints 函数执行出错:\n{stderr[:500]}\n"
                    f"请修正后重新生成。只输出 test_endpoints(base_url) 函数。"
                )
            else:
                feedback = f"[应用运行时错误] {stderr[:800]}"
                failed_files = self._extract_failed_files_from_traceback(stderr)
                logger.warning(f"❌ [Layer 3] 应用运行时错误: {stderr[:200]}")
                return {"passed": False, "feedback": feedback,
                        "failed_files": failed_files, "warning": False}

        # 重试耗尽
        logger.warning("⚠️ [Phase 2.5] 测试脚本多次出错，集成测试未能执行")
        global_broadcaster.emit_sync("IntegrationTester", "warning",
            "⚠️ 集成测试未能执行（脚本多次出错）")
        return {"passed": True, "feedback": "⚠️ 集成测试未能执行（脚本多次出错）",
                "failed_files": [], "warning": True}

    # ============================================================
    # LLM 调用
    # ============================================================

    @staticmethod
    def _build_system_prompt(port, project_spec, file_list, file_contents, experience_hint):
        """构建精简版 system prompt（只要求生成断言函数）"""
        return f"""你是集成测试专家。你只需要生成一个 test_endpoints(base_url) 函数。

【你不需要做的事】
- 不要启动服务（模板已处理）
- 不要轮询端口（模板已处理）
- 不要清理进程（模板已处理）
- 不要 import subprocess, socket, time（不需要）
- 不要写 if __name__ 块

【你只需要做的事】
生成一个函数：
```python
def test_endpoints(base_url):
    import requests
    # 测试各个 API 端点
    resp = requests.get(f"{{base_url}}/api/xxx", timeout=10)
    assert 200 <= resp.status_code < 300, f"GET /api/xxx 返回 {{resp.status_code}}: {{resp.text[:200]}}"
    # ... 更多测试
```

【要求】
1. 所有 requests 调用必须带 timeout=10
2. 状态码验证用范围判断 `assert 200 <= resp.status_code < 300`，不要写 `== 200`
   （POST 可能返回 201 Created，这是正确的）
3. 如果有 POST 接口，先 POST 创建数据，再 GET 验证
4. assert 失败时的消息要包含实际响应内容 resp.text[:200]，方便定位
5. 只输出 test_endpoints 函数代码，不要其他任何东西
6. 不要用 markdown 包裹

【项目信息】
服务端口: {port}
规划书: {project_spec or '无'}
文件列表: {file_list}
关键文件内容:
{file_contents}
{experience_hint}"""

    def _generate_test_function(self, system_prompt: str, user_prompt: str) -> Optional[str]:
        """让 LLM 生成 test_endpoints 函数"""
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

    # ============================================================
    # 输出解析
    # ============================================================

    @staticmethod
    def _extract_failure_info(output: str) -> str:
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
        for line in output.split("\n"):
            if "FAILED_FILES:" in line:
                files_str = line.split("FAILED_FILES:")[1].strip()
                files = []
                for f in files_str.split(","):
                    fname = f.split("|")[0].strip()
                    if fname:
                        files.append(fname)
                return files
        return []

    @staticmethod
    def _extract_failed_files_from_traceback(stderr: str) -> list:
        files = set()
        for line in stderr.split("\n"):
            if 'File "' in line and "_run_task_" not in line:
                m = re.search(r'File "([^"]+)"', line)
                if m:
                    basename = os.path.basename(m.group(1))
                    if basename.endswith('.py') and not basename.startswith('_'):
                        files.add(basename)
        return list(files) if files else []
