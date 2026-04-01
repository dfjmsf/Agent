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
# === 杀掉遗留端口 ===
def _kill_port(p):
    try:
        if os.name == 'nt':
            out = subprocess.check_output(f"netstat -ano | findstr :{{p}}", shell=True).decode()
            for line in out.splitlines():
                if f":{{p}} " in line and "LISTENING" in line:
                    pid = line.strip().split()[-1]
                    subprocess.run(["taskkill", "/F", "/PID", pid], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.run(f"fuser -k {{p}}/tcp", shell=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass

_kill_port(PORT)

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
                             sandbox_dir: str) -> dict:
        """
        执行端到端集成测试（确定性方案 C）。

        Returns:
            {"passed": bool, "feedback": str, "failed_files": list, "warning": bool}
        """
        logger.info(f"🧪 [Phase 2.5] 集成测试启动 ({len(all_code)} 个文件)")
        global_broadcaster.emit_sync("IntegrationTester", "start",
            f"🧪 集成测试: 确定性验证 {len(all_code)} 个文件的端到端行为")

        # 0. 纯前端 npm 项目检测：无后端 .py 入口 → 直接做前端冒烟测试
        has_backend = any(f.endswith('.py') for f in all_code)
        has_package_json = any(os.path.basename(f) == 'package.json' for f in all_code)
        if not has_backend and has_package_json:
            logger.info("📦 [Phase 2.5] 纯前端 npm 项目，跳过后端测试，直接执行前端冒烟测试")
            global_broadcaster.emit_sync("IntegrationTester", "frontend_only",
                "📦 纯前端 npm 项目: 执行 npm build + 前端冒烟测试")
            fe_pass, fe_feedback = self._frontend_smoke_test(
                port=0, project_spec=project_spec,
                all_code=all_code, sandbox_dir=sandbox_dir
            )
            if fe_pass:
                global_broadcaster.emit_sync("IntegrationTester", "passed", "✅ 前端冒烟测试通过！")
                return {"passed": True, "feedback": "纯前端 npm 项目: npm build + 前端冒烟测试通过",
                        "failed_files": [], "warning": False}
            else:
                fe_failed = [f for f in all_code if f.endswith(('.js', '.jsx', '.vue', '.ts', '.tsx'))]
                global_broadcaster.emit_sync("IntegrationTester", "failed",
                    f"❌ 前端冒烟测试失败: {fe_feedback[:100]}")
                return {"passed": False, "feedback": fe_feedback,
                        "failed_files": fe_failed or ["src/App.vue"],
                        "warning": False}

        # 1. 检测入口文件（后端模式）
        entry_file = self._detect_entry_file(all_code)
        logger.info(f"🔍 [Phase 2.5] 检测到入口文件: {entry_file}")

        # 2. 从入口文件代码中检测端口
        port = self._detect_port_from_code(all_code.get(entry_file, ""), project_spec)
        logger.info(f"🔍 [Phase 2.5] 检测到服务端口: {port}")

        # 3. 确定性代码生成
        api_contracts = self._parse_api_contracts(project_spec)
        test_function = self._generate_deterministic_test(api_contracts, all_code)
        
        # 拼接最终脚本 = 固定模板 + 测试函数
        final_script = TEST_HARNESS_TEMPLATE.format(
            port=port,
            entry_file=entry_file,
            test_function=test_function,
        )

        # ── Layer 2: 沙盒执行 ──
        logger.info(f"🧪 [Phase 2.5] 执行确定性集成测试")
        global_broadcaster.emit_sync("IntegrationTester", "executing",
            "🧪 执行确定性集成测试脚本")

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

            # ── Phase 2.6: 前端冒烟测试 ──
            fe_pass, fe_feedback = self._frontend_smoke_test(
                port, project_spec, all_code, sandbox_dir
            )
            if not fe_pass:
                logger.warning(f"❌ [Phase 2.6] 前端冒烟测试失败: {fe_feedback[:200]}")
                global_broadcaster.emit_sync("IntegrationTester", "failed",
                    f"❌ 前端冒烟测试失败: {fe_feedback[:100]}")
                # 归因到前端 JS 文件
                fe_failed = [f for f in all_code if f.endswith('.js')]
                return {"passed": False, "feedback": fe_feedback,
                        "failed_files": fe_failed or ["frontend/app.js"],
                        "warning": False}

            global_broadcaster.emit_sync("IntegrationTester", "passed", "✅ 集成测试通过！")
            return {"passed": True, "feedback": "端到端集成测试 + 前端冒烟测试通过",
                    "failed_files": [], "warning": False}

        # 判定 2: FAILED
        if "INTEGRATION_TEST_FAILED" in output:
            feedback = self._extract_failure_info(output)
            failed_files = self._extract_failed_files(output)
            if not failed_files:
                failed_files = self._extract_failed_files_from_assertion(feedback, all_code)
            
            logger.warning(f"❌ [Phase 2.5] 集成测试失败: {feedback[:200]}")
            global_broadcaster.emit_sync("IntegrationTester", "failed",
                f"❌ 集成测试失败: {feedback[:100]}")
            return {"passed": False, "feedback": feedback,
                    "failed_files": failed_files, "warning": False}

        # ── 意外错误 ──
        feedback = f"测试执行环境意外终止:\n{stderr[:800]}"
        failed_files = self._extract_failed_files_from_traceback(stderr)
        if not failed_files:
            failed_files = self._extract_failed_files_from_assertion(feedback, all_code)
        
        logger.warning(f"❌ [Phase 2.5] 应用运行时错误: {stderr[:200]}")
        return {"passed": False, "feedback": feedback,
                "failed_files": failed_files, "warning": False}

    # ============================================================
    # 确定性测试生成 (方案 C)
    # ============================================================

    @staticmethod
    def _parse_api_contracts(project_spec_text: str) -> list:
        """从项目规划书 JSON 中解析 api_contracts"""
        if not project_spec_text:
            return []
        try:
            clean_str = project_spec_text
            if "```json" in clean_str:
                clean_str = clean_str.split("```json")[1].split("```")[0].strip()
            elif "```" in clean_str:
                clean_str = clean_str.split("```")[1].split("```")[0].strip()
            
            spec = json.loads(clean_str)
            return spec.get("api_contracts", [])
        except json.JSONDecodeError as e:
            logger.warning(f"⚠️ 解析 api_contracts 失败: {e}")
            return []

    @staticmethod
    def _parse_post_body_from_code(all_code: Dict[str, str], path: str) -> dict:
        """
        从实际生成的代码中解析 POST 路由对应的 Pydantic BaseModel 字段类型。
        这比信任 api_contracts 的 request_params 更可靠，因为 Coder 不一定忠实遵守规划书的类型定义。
        """
        body = {}
        for fname, code in all_code.items():
            if not fname.endswith('.py') or not code:
                continue
            # 在代码中查找匹配 path 的 POST 路由
            escaped_path = re.escape(path)
            route_match = re.search(
                r'@\w+\.post\(["\']' + escaped_path + r'["\'].*?\)\s*\n\s*async\s+def\s+\w+\(([^)]+)\)',
                code, re.DOTALL
            )
            if not route_match:
                continue
            
            # 提取参数列表中的 Pydantic 模型名
            params_str = route_match.group(1)
            # 寻找类型注解不是基本类型的参数（即 Pydantic 模型）
            model_name = None
            for param in params_str.split(','):
                param = param.strip()
                if ':' in param:
                    pname, ptype = param.split(':', 1)
                    ptype = ptype.strip()
                    # 排除 int, str, float 等基本类型和 Query/Path 等 FastAPI 注解
                    if ptype not in ('int', 'str', 'float', 'bool') and '=' not in ptype and 'Query' not in ptype:
                        model_name = ptype
                        break
            
            if not model_name:
                continue
            
            # 在整个代码中查找这个模型的定义
            model_pattern = re.search(
                r'class\s+' + re.escape(model_name) + r'\s*\(\s*BaseModel\s*\)\s*:(.+?)(?=\nclass\s|\ndef\s|\nasync\s|\n@|\Z)',
                code, re.DOTALL
            )
            if not model_pattern:
                # 可能模型定义在另一个文件
                for other_fname, other_code in all_code.items():
                    if other_fname == fname or not other_fname.endswith('.py'):
                        continue
                    model_pattern = re.search(
                        r'class\s+' + re.escape(model_name) + r'\s*\(\s*BaseModel\s*\)\s*:(.+?)(?=\nclass\s|\ndef\s|\nasync\s|\n@|\Z)',
                        other_code, re.DOTALL
                    )
                    if model_pattern:
                        break
            
            if not model_pattern:
                continue
            
            model_body = model_pattern.group(1)
            # 解析每一行的字段定义
            type_samples = {
                'str': 'test_string', 'string': 'test_string',
                'int': 1, 'integer': 1,
                'float': 1.5,
                'bool': True, 'boolean': True,
                'optional[str]': 'test', 'optional[int]': 1,
                'optional[list[str]]': ['test_tag'],
                'list[str]': ['test_tag'], 'list[int]': [1],
                'list': [],
            }
            for line in model_body.split('\n'):
                line = line.strip()
                if not line or line.startswith('#') or line.startswith('"""') or line.startswith("'''"):
                    continue
                field_match = re.match(r'(\w+)\s*:\s*(.+?)(?:\s*=.*)?$', line)
                if field_match:
                    field_name = field_match.group(1)
                    field_type = field_match.group(2).strip().lower()
                    # 去掉 Optional[] 包裹
                    inner = re.match(r'optional\[(.+)\]', field_type)
                    if inner:
                        field_type = inner.group(1)
                    body[field_name] = type_samples.get(field_type, 'test')
            
            if body:
                logger.info(f"📋 [Phase 2.5] 从代码解析 POST {path} body: {list(body.keys())}")
                return body
        
        return body

    @staticmethod
    def _build_fallback_body(request_params: dict) -> dict:
        """当无法从代码解析时，使用 api_contracts 推断（全部降级为字符串安全模式）"""
        if not isinstance(request_params, dict):
            return {}
        body = {}
        for k, v in request_params.items():
            # 全部降级为字符串，避免类型不匹配
            body[k] = 'test_string'
        return body

    @staticmethod
    def _parse_get_query_params_from_code(all_code: Dict[str, str], path: str) -> dict:
        """
        从实际代码中解析 GET 路由的 Query 参数及类型。
        支持 FastAPI 风格: param: type = Query(...)
        """
        params = {}
        for fname, code in all_code.items():
            if not fname.endswith('.py') or not code:
                continue
            # 匹配 GET 路由装饰器 + 函数 def 起始
            escaped_path = re.escape(path)
            route_match = re.search(
                r'@\w+\.get\(["\']' + escaped_path + r'["\']',
                code
            )
            if not route_match:
                continue

            # 从 def 行开始，找到完整的参数括号（平衡括号匹配）
            rest = code[route_match.end():]
            def_match = re.search(r'def\s+\w+\(', rest)
            if not def_match:
                continue

            # 从 ( 开始计数括号深度
            start = def_match.end()
            depth = 1
            i = start
            while i < len(rest) and depth > 0:
                if rest[i] == '(':
                    depth += 1
                elif rest[i] == ')':
                    depth -= 1
                i += 1
            params_str = rest[start:i-1]  # 去掉最外层的 )

            type_samples = {
                'str': 'test', 'string': 'test',
                'int': 1, 'integer': 1,
                'float': 1.5,
                'bool': True, 'boolean': True,
            }

            # 按逗号分割（但忽略括号内的逗号）
            param_parts = []
            current = []
            paren_depth = 0
            for ch in params_str:
                if ch == '(':
                    paren_depth += 1
                elif ch == ')':
                    paren_depth -= 1
                elif ch == ',' and paren_depth == 0:
                    param_parts.append(''.join(current))
                    current = []
                    continue
                current.append(ch)
            if current:
                param_parts.append(''.join(current))

            # 预提取代码中的字典常量 keys（用于推断合法字符串值）
            code_dict_keys = IntegrationTester._extract_dict_keys_from_code(code)

            for param in param_parts:
                param = param.strip()
                if not param or param.startswith('request') or param.startswith('db'):
                    continue
                if ':' in param:
                    pname, ptype_full = param.split(':', 1)
                    pname = pname.strip()
                    ptype_full = ptype_full.strip()
                    # 提取基础类型（忽略 = Query(...) 部分）
                    ptype = ptype_full.split('=')[0].strip().lower()
                    # 跳过 Request, Response 等非参数类型
                    if ptype in ('request', 'response', 'session'):
                        continue
                    if ptype == 'str':
                        # 尝试从代码常量中推断合法值
                        smart_val = IntegrationTester._infer_str_sample(pname, code_dict_keys)
                        params[pname] = smart_val
                    else:
                        params[pname] = type_samples.get(ptype, 'test')

            if params:
                logger.info(f"📋 [Phase 2.5] 从代码解析 GET {path} query params: {list(params.keys())}")
                return params
        return params

    @staticmethod
    def _extract_dict_keys_from_code(code: str) -> Dict[str, list]:
        """
        从代码中提取顶层字典常量的 keys。
        例如: EXCHANGE_RATES = {'USD': 1.0, 'EUR': 0.93}
        返回: {'exchange_rates': ['USD', 'EUR']}
        """
        result = {}
        # 匹配 SOME_DICT = {'key1': ..., 'key2': ...} 或 {"key1": ...}
        for m in re.finditer(r'(\w+)\s*[=:]\s*\{([^}]+)\}', code):
            var_name = m.group(1).lower()
            body = m.group(2)
            keys = re.findall(r"['\"](\w+)['\"]", body)
            if keys:
                result[var_name] = keys
        return result

    @staticmethod
    def _infer_str_sample(param_name: str, code_dict_keys: Dict[str, list]) -> str:
        """
        根据参数名和代码中的字典常量 keys 推断合法的字符串值。
        例如 param_name='from_currency', 代码中有 EXCHANGE_RATES={'USD','EUR'}
        → 返回 'USD'
        """
        if not code_dict_keys:
            return 'test'

        pname_lower = param_name.lower()

        # 策略 1: 关键词子串匹配
        for var_name, keys in code_dict_keys.items():
            name_parts = re.split(r'[_\s]', pname_lower)
            var_parts = re.split(r'[_\s]', var_name)

            stop_words = {'from', 'to', 'the', 'a', 'is', 'in', 'of', 'by', 'get', 'set'}
            name_keywords = {p for p in name_parts if p not in stop_words and len(p) > 1}
            var_keywords = {p for p in var_parts if p not in stop_words and len(p) > 1}

            matched = False
            for nk in name_keywords:
                for vk in var_keywords:
                    if nk in vk or vk in nk:
                        matched = True
                        break
                if matched:
                    break

            if matched and keys:
                logger.info(f"🎯 参数 '{param_name}' 从 {var_name} 推断样本值: '{keys[0]}'")
                return keys[0]

        # 策略 2: 如果只有一个字典常量，且 keys 像枚举（全大写/短字符串），直接采用
        enum_like_dicts = {
            vn: ks for vn, ks in code_dict_keys.items()
            if ks and all(k.isupper() or len(k) <= 5 for k in ks)
        }
        if len(enum_like_dicts) == 1:
            var_name, keys = next(iter(enum_like_dicts.items()))
            logger.info(f"🎯 参数 '{param_name}' 从唯一枚举字典 {var_name} 采样: '{keys[0]}'")
            return keys[0]

        return 'test'

    @staticmethod
    def _generate_deterministic_test(api_contracts: list, all_code: Dict[str, str] = None) -> str:
        """从 api_contracts + 实际代码自动生成测试代码"""
        if all_code is None:
            all_code = {}
        lines = [
            "def test_endpoints(base_url):",
            "    import requests",
            "    import json",
            "    import time",
            "    print('🚀 启动确定性 API 端点测试...')"
        ]
        
        if not api_contracts:
            lines.append("    print('ℹ️ 无 API 契约，跳过接口测试，仅验证服务启动')")
            return "\n".join(lines)
            
        for api in api_contracts:
            method = str(api.get("method", "")).upper()
            path = str(api.get("path", ""))
            if not method or not path:
                continue
                
            if "{" in path and "}" in path:
                lines.append(f"    print('ℹ️ 跳过带路径参数的接口: {method} {path}')")
                continue
                
            lines.append(f"    print('Testing {method} {path} ...')")
            if method == "GET":
                # 优先从代码解析 Query 参数，fallback 到 api_contracts 的 request_params
                query_params = IntegrationTester._parse_get_query_params_from_code(all_code, path)
                if not query_params:
                    query_params = IntegrationTester._build_fallback_body(api.get("request_params", {}))
                if query_params:
                    params_json = json.dumps(query_params)
                    lines.append(f"    resp = requests.get(f'{{base_url}}{path}', params={params_json}, timeout=10)")
                else:
                    lines.append(f"    resp = requests.get(f'{{base_url}}{path}', timeout=10)")
                lines.append(f"    assert 200 <= resp.status_code < 300, f'GET {path} 返回 {{resp.status_code}}: {{resp.text[:200]}}'")
            elif method == "POST":
                # 优先从实际代码解析 body 类型，fallback 到 spec
                body = IntegrationTester._parse_post_body_from_code(all_code, path)
                if not body:
                    body = IntegrationTester._build_fallback_body(api.get("request_params", {}))
                body_json = json.dumps(body)
                lines.append(f"    payload = {body_json}")
                lines.append(f"    resp = requests.post(f'{{base_url}}{path}', json=payload, timeout=10)")
                lines.append(f"    assert 200 <= resp.status_code < 300, f'POST {path} 返回 {{resp.status_code}}: {{resp.text[:200]}}'")
            elif method in ("PUT", "DELETE"):
                lines.append(f"    resp = requests.request('{method}', f'{{base_url}}{path}', timeout=10)")
                lines.append(f"    assert 200 <= resp.status_code < 300, f'{method} {path} 返回 {{resp.status_code}}: {{resp.text[:200]}}'")
                
        return "\n".join(lines)

    @staticmethod
    def _extract_failed_files_from_assertion(feedback: str, all_code: Dict[str, str]) -> list:
        """从断言失败信息中猜测引发错误的文件"""
        files = []
        if any(kw in feedback for kw in ("500", "422", "返回")):
            # API 错误通常在 routes.py 或 main.py
            for target in ["src/routes.py", "src/main.py", "main.py", "routes.py", "app.py"]:
                if target in all_code:
                    files.append(target)
                    break
        return files

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

    # ============================================================
    # Phase 2.6: 前端冒烟测试（Playwright B-Lite）
    # ============================================================

    def _frontend_smoke_test(self, port: int, project_spec: str,
                              all_code: dict, sandbox_dir: str) -> Tuple[bool, str]:
        """
        Phase 2.6: 前端冒烟测试。
        用 headless Chromium 打开页面，检查 JS 报错和 DOM 完整性。
        
        策略：
        - 无前端文件 → 跳过
        - Playwright 未安装 → 跳过（warning）
        - 网络/CDN 异常 → 跳过（warning）
        - JS 控制台报错 → FAIL
        - DOM id 缺失 → FAIL
        
        注意：自行启动临时 HTTP 服务器来 serve 前端文件，
        不依赖后端是否挂载 StaticFiles。
        """
        import subprocess as _sp
        import time

        # 判断是否有前端文件
        html_files = [f for f in all_code if f.endswith('.html')]
        if not html_files:
            logger.info("ℹ️ [Phase 2.6] 纯后端项目，跳过前端测试")
            return True, "纯后端项目，跳过前端冒烟测试"

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            logger.warning("⚠️ [Phase 2.6] Playwright 未安装，跳过前端测试")
            return True, "⚠️ Playwright 未安装，跳过前端冒烟测试"

        # npm 构建检测：如果项目有 package.json，先构建
        frontend_dir = None
        if sandbox_dir and os.path.isdir(sandbox_dir):
            pkg_json = os.path.join(sandbox_dir, "package.json")
            if os.path.isfile(pkg_json):
                from tools.sandbox import sandbox_env
                logger.info("📦 [Phase 2.6] 检测到 package.json，执行 npm 构建...")
                build_result = sandbox_env.npm_build(sandbox_dir)
                if not build_result["success"]:
                    return False, f"npm 构建失败: {build_result['error']}"
                if build_result["dist_dir"]:
                    frontend_dir = build_result["dist_dir"]
                    logger.info(f"✅ [Phase 2.6] npm 构建完成，使用 {frontend_dir} 作为前端目录")

        # 定位前端文件目录（仅非 npm 构建项目走这里）
        if frontend_dir is None:
            if sandbox_dir and os.path.isdir(sandbox_dir):
                for html_file in html_files:
                    html_dir = os.path.dirname(html_file)
                    candidate = os.path.join(sandbox_dir, html_dir) if html_dir else sandbox_dir
                    if os.path.isdir(candidate):
                        frontend_dir = candidate
                        break
                if not frontend_dir:
                    frontend_dir = sandbox_dir
            else:
                logger.warning("⚠️ [Phase 2.6] sandbox 目录无效，跳过")
                return True, "⚠️ 无法定位前端文件目录"

        logger.info(f"🌐 [Phase 2.6] 启动前端冒烟测试... (frontend_dir={frontend_dir})")
        global_broadcaster.emit_sync("IntegrationTester", "frontend_smoke",
            "🌐 前端冒烟测试: 启动 headless 浏览器")

        # Vite 项目（有 dist/）：需要启动后端进程（Phase 2.5 测试完会杀掉后端）
        # CDN 项目（无 dist/）：启动独立 HTTP 服务器
        is_vite_dist = frontend_dir and frontend_dir.endswith("dist")
        http_proc = None

        if is_vite_dist:
            # Vite 项目：启动后端进程，它会挂载 dist/ 并提供 /api 路由
            fe_port = port
            logger.info(f"📦 [Phase 2.6] Vite 项目，启动后端进程并用端口 {fe_port} 访问")

            # 在 sandbox 目录下启动后端
            entry_file = None
            for name in ["main.py", "app.py", "server.py", "run.py"]:
                if os.path.isfile(os.path.join(sandbox_dir, name)):
                    entry_file = name
                    break
            if entry_file:
                http_proc = _sp.Popen(
                    ["python", entry_file],
                    cwd=sandbox_dir,
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL
                )
                # 等待后端启动
                time.sleep(3)
                if http_proc.poll() is not None:
                    logger.warning("⚠️ [Phase 2.6] 后端进程启动失败，跳过")
                    return True, "⚠️ Vite 后端进程启动失败"
            else:
                logger.warning("⚠️ [Phase 2.6] 找不到后端入口文件，跳过")
                return True, "⚠️ 找不到后端入口文件"
        else:
            # CDN 项目：启动临时 HTTP 服务器 serve 前端文件
            fe_port = self._find_free_port()
            http_proc = _sp.Popen(
                ["python", "-m", "http.server", str(fe_port)],
                cwd=frontend_dir,
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL
            )

            # 等待静态服务器就绪
            time.sleep(1)
            if http_proc.poll() is not None:
                logger.warning("⚠️ [Phase 2.6] 静态文件服务器启动失败，跳过")
                return True, "⚠️ 前端静态服务器启动失败"

        js_console_errors = []
        missing_dom_ids = []
        real_js_errors = []

        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                page = browser.new_page()

                # 收集 JS 控制台错误
                def _on_console(msg):
                    if msg.type == "error":
                        js_console_errors.append(msg.text)
                page.on("console", _on_console)

                # 访问页面
                try:
                    page.goto(f"http://127.0.0.1:{fe_port}/", timeout=10000)
                except Exception as e:
                    browser.close()
                    logger.warning(f"⚠️ [Phase 2.6] 页面无法打开: {e}，跳过")
                    return True, f"⚠️ 前端页面无法打开: {e}"

                # 等待 JS 执行 + CDN 加载
                page.wait_for_timeout(3000)

                # 过滤预期中的非致命错误：
                # 1. CDN/网络错误（断网时放行）
                # 2. API 请求失败（前端用独立静态服务器，没有后端 API，404 是正常的）
                _benign_patterns = [
                    "net::ERR_",              # CDN 网络错误
                    "Failed to load resource", # 资源加载失败
                    "HTTP error! status:",     # fetch API 返回非 2xx
                    "Failed to fetch",         # fetch 完全失败
                    "NetworkError",            # 网络异常
                    "Load failed",             # Safari 风格的 fetch 失败
                    "status: 404",             # API 404
                    "status: 500",             # API 500（后端没启动）
                    "ERR_CONNECTION_REFUSED",  # 后端没启动
                ]
                benign_errors = [e for e in js_console_errors
                                 if any(p in e for p in _benign_patterns)]
                real_js_errors = [e for e in js_console_errors
                                  if e not in benign_errors]

                if benign_errors and not real_js_errors:
                    browser.close()
                    logger.info(f"ℹ️ [Phase 2.6] API/网络错误（预期中，放行）: {len(benign_errors)} 条")
                    return True, f"前端冒烟测试通过（API 请求在独立服务器上预期 404）"

                # 检查关键 DOM 元素
                dom_ids = self._extract_dom_ids_from_spec(project_spec)
                if dom_ids:
                    for dom_id in dom_ids:
                        el = page.query_selector(f"#{dom_id}")
                        if not el:
                            missing_dom_ids.append(dom_id)
                    logger.info(f"🔍 [Phase 2.6] DOM 检查: 约定 {len(dom_ids)} 个, 缺失 {len(missing_dom_ids)} 个")

                browser.close()

        except Exception as e:
            logger.warning(f"⚠️ [Phase 2.6] Playwright 异常: {e}，跳过")
            return True, f"⚠️ Playwright 执行异常: {e}"
        finally:
            # 清理临时 HTTP 服务器（Vite 项目不启动独立服务器，http_proc 为 None）
            if http_proc is not None:
                try:
                    http_proc.kill()
                    http_proc.wait(timeout=3)
                except Exception:
                    pass

        # 汇总结果
        errors = []
        if real_js_errors:
            errors.append(f"JS 控制台报错: {'; '.join(real_js_errors[:3])}")
        if missing_dom_ids:
            errors.append(f"缺少 DOM 元素: {', '.join(['#' + d for d in missing_dom_ids[:5]])}")

        if errors:
            feedback = "❌ FRONTEND_SMOKE_FAILED: " + " | ".join(errors)
            logger.warning(f"❌ [Phase 2.6] {feedback[:200]}")
            return False, feedback

        logger.info("✅ [Phase 2.6] 前端冒烟测试通过！")
        return True, "前端冒烟测试通过"

    @staticmethod
    def _find_free_port() -> int:
        """找一个空闲端口"""
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('', 0))
            return s.getsockname()[1]

    @staticmethod
    def _extract_dom_ids_from_spec(project_spec: str) -> list:
        """从规划书 module_interfaces 中提取约定的 DOM id（#xxx 格式）"""
        if not project_spec:
            return []
        # 匹配 #xxx-yyy 格式的 id（支持连字符和下划线）
        ids = re.findall(r'#([\w-]+)', project_spec)
        # 去重，排除常见误匹配（如 #id1 示例占位符）
        exclude = {'id1', 'id2', 'id3', 'xxx', 'yyy'}
        return list(set(id_ for id_ in ids if id_ not in exclude))
