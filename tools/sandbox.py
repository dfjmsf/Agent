"""
Sandbox — 阅后即焚沙盒 + 项目级 Venv 环境管理

核心升级：
- 每个项目独立 sandbox venv（解释器隔离 + 依赖隔离）
- 自动 import 扫描 + 静态映射表装包（零 LLM 消耗）
- 环境变量清洗（删除 API Key 等敏感信息）
- LRU 淘汰 + 锁定/收藏机制
- Windows 安全删除（重试 + 标记待清理）
"""
import os
import sys
import ast
import uuid
import time
import shutil
import subprocess
import logging
import tempfile
import threading
from typing import Dict, Any, Optional, Set, List

from core.state_manager import global_state_manager
from tools.package_map import IMPORT_TO_PACKAGE, STDLIB_MODULES, COMMON_PROJECT_MODULES

logger = logging.getLogger("Sandbox")

MAX_OUTPUT_LENGTH = 1500
EXECUTION_TIMEOUT = 60
PIP_INSTALL_TIMEOUT = 120

# sandbox venv 管理配置
SANDBOXES_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "sandboxes"))
MAX_SANDBOX_COUNT = 3       # LRU 池大小（不含锁定的）
MAX_PINNED_COUNT = 5        # 最多锁定数

# 环境变量黑名单
SENSITIVE_ENV_KEYS = {
    "QWEN_API_KEY", "DATABASE_URL", "QWEN_BASE_URL",
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "AWS_SECRET_ACCESS_KEY", "GOOGLE_API_KEY",
}


class SandboxVenvManager:
    """
    项目级 Sandbox Venv 生命周期管理器
    
    - 每个项目独立 venv：sandboxes/{project_id}/
    - LRU 淘汰超过 K 个的非锁定 venv
    - 锁定的 venv 不受 K 限制
    """
    
    def __init__(self):
        os.makedirs(SANDBOXES_DIR, exist_ok=True)
        # 缓存已安装过的包，避免重复 pip install
        self._installed_cache: Dict[str, Set[str]] = {}
    
    def get_or_create_venv(self, project_id: str) -> str:
        """
        获取或创建项目的 sandbox venv，返回 python 可执行文件路径。
        """
        venv_dir = os.path.join(SANDBOXES_DIR, project_id)
        python_path = self._get_python_path(venv_dir)
        
        if os.path.isfile(python_path):
            # 更新 last_used 时间戳
            self._touch_last_used(venv_dir)
            return python_path
        
        # 需要创建新 venv → 先检查是否需要淘汰
        self._evict_lru_if_needed()
        
        # 创建 venv
        logger.info(f"🔧 正在为项目 {project_id} 创建 sandbox venv...")
        try:
            subprocess.run(
                [sys.executable, "-m", "venv", venv_dir],
                check=True,
                capture_output=True,
                timeout=30,
            )
            self._touch_last_used(venv_dir)
            logger.info(f"✅ Sandbox venv 创建成功: {venv_dir}")
        except Exception as e:
            logger.error(f"❌ 创建 sandbox venv 失败: {e}")
            # fallback: 返回当前 python 解释器
            return sys.executable
        
        return python_path
    
    def get_pip_path(self, project_id: str) -> str:
        """获取项目 sandbox venv 的 pip 路径"""
        venv_dir = os.path.join(SANDBOXES_DIR, project_id)
        if os.name == 'nt':
            return os.path.join(venv_dir, "Scripts", "pip.exe")
        return os.path.join(venv_dir, "bin", "pip")
    
    def install_package(self, project_id: str, package_name: str) -> bool:
        """在项目 sandbox venv 中安装指定包"""
        # 检查缓存
        if project_id in self._installed_cache:
            if package_name in self._installed_cache[project_id]:
                return True
        
        pip_path = self.get_pip_path(project_id)
        if not os.path.isfile(pip_path):
            logger.warning(f"⚠️ pip 不存在: {pip_path}")
            return False
        
        logger.info(f"📦 正在安装: {package_name} → sandbox/{project_id}")
        try:
            result = subprocess.run(
                [pip_path, "install", "--quiet", package_name],
                capture_output=True,
                text=True,
                timeout=PIP_INSTALL_TIMEOUT,
            )
            if result.returncode == 0:
                logger.info(f"✅ 安装成功: {package_name}")
                # 加入缓存
                if project_id not in self._installed_cache:
                    self._installed_cache[project_id] = set()
                self._installed_cache[project_id].add(package_name)
                return True
            else:
                logger.warning(f"⚠️ pip install 失败: {result.stderr[:300]}")
                return False
        except subprocess.TimeoutExpired:
            logger.error(f"⚠️ pip install 超时: {package_name}")
            return False
        except Exception as e:
            logger.error(f"⚠️ pip install 异常: {e}")
            return False
    
    def pin_sandbox(self, project_id: str) -> bool:
        """锁定沙盒（不受 LRU 淘汰）"""
        venv_dir = os.path.join(SANDBOXES_DIR, project_id)
        if not os.path.isdir(venv_dir):
            return False
        
        # 检查锁定上限
        pinned_count = len(self._list_pinned())
        if pinned_count >= MAX_PINNED_COUNT:
            logger.warning(f"⚠️ 已达锁定上限 ({MAX_PINNED_COUNT})")
            return False
        
        marker = os.path.join(venv_dir, ".pinned")
        with open(marker, "w") as f:
            f.write(str(time.time()))
        return True
    
    def unpin_sandbox(self, project_id: str) -> bool:
        """解锁沙盒"""
        marker = os.path.join(SANDBOXES_DIR, project_id, ".pinned")
        if os.path.exists(marker):
            os.remove(marker)
            return True
        return False
    
    def is_pinned(self, project_id: str) -> bool:
        """检查是否锁定"""
        return os.path.exists(os.path.join(SANDBOXES_DIR, project_id, ".pinned"))
    
    def cleanup_stale(self):
        """启动时清理残留的 .cleanup 标记目录"""
        if not os.path.isdir(SANDBOXES_DIR):
            return
        
        for name in os.listdir(SANDBOXES_DIR):
            full_path = os.path.join(SANDBOXES_DIR, name)
            cleanup_marker = full_path + ".cleanup"
            
            if os.path.exists(cleanup_marker):
                logger.info(f"🧹 启动清理残留 sandbox: {name}")
                self._safe_rmtree(full_path)
                try:
                    os.remove(cleanup_marker)
                except Exception:
                    pass
    
    # ==================== 内部方法 ====================
    
    def _get_python_path(self, venv_dir: str) -> str:
        """获取 venv 的 python 可执行文件路径"""
        if os.name == 'nt':
            return os.path.join(venv_dir, "Scripts", "python.exe")
        return os.path.join(venv_dir, "bin", "python")
    
    def _touch_last_used(self, venv_dir: str):
        """更新最后使用时间"""
        marker = os.path.join(venv_dir, ".last_used")
        with open(marker, "w") as f:
            f.write(str(time.time()))
    
    def _get_last_used(self, venv_dir: str) -> float:
        """获取最后使用时间"""
        marker = os.path.join(venv_dir, ".last_used")
        if os.path.exists(marker):
            try:
                with open(marker, "r") as f:
                    return float(f.read().strip())
            except Exception:
                pass
        return 0.0
    
    def _list_pinned(self) -> List[str]:
        """列出所有锁定的 sandbox"""
        pinned = []
        if not os.path.isdir(SANDBOXES_DIR):
            return pinned
        for name in os.listdir(SANDBOXES_DIR):
            full_path = os.path.join(SANDBOXES_DIR, name)
            if os.path.isdir(full_path) and os.path.exists(os.path.join(full_path, ".pinned")):
                pinned.append(name)
        return pinned
    
    def _evict_lru_if_needed(self):
        """如果非锁定 sandbox 数量超过 K，淘汰最旧的"""
        if not os.path.isdir(SANDBOXES_DIR):
            return
        
        # 收集所有 sandbox 信息
        sandboxes = []
        for name in os.listdir(SANDBOXES_DIR):
            full_path = os.path.join(SANDBOXES_DIR, name)
            if not os.path.isdir(full_path):
                continue
            pinned = os.path.exists(os.path.join(full_path, ".pinned"))
            last_used = self._get_last_used(full_path)
            sandboxes.append({"name": name, "path": full_path, "pinned": pinned, "last_used": last_used})
        
        # 过滤出可淘汰的（非锁定）
        evictable = [s for s in sandboxes if not s["pinned"]]
        
        while len(evictable) >= MAX_SANDBOX_COUNT:
            # 淘汰最旧的
            oldest = min(evictable, key=lambda s: s["last_used"])
            logger.info(f"🗑️ LRU 淘汰 sandbox: {oldest['name']}")
            self._safe_rmtree(oldest["path"])
            evictable.remove(oldest)
            # 清除缓存
            self._installed_cache.pop(oldest["name"], None)
    
    def _safe_rmtree(self, path: str, max_retries: int = 3):
        """Windows 安全的 rmtree，带重试 + 标记待清理"""
        for attempt in range(max_retries):
            try:
                def on_error(func, fpath, exc_info):
                    try:
                        os.chmod(fpath, 0o777)
                        func(fpath)
                    except Exception:
                        pass
                
                shutil.rmtree(path, onerror=on_error)
                logger.info(f"✅ 已删除: {path}")
                return
                
            except PermissionError:
                if attempt < max_retries - 1:
                    delay = (attempt + 1) * 1.0
                    logger.warning(f"⚠️ 删除失败 (尝试 {attempt+1}/{max_retries})，等待 {delay}s...")
                    time.sleep(delay)
                else:
                    logger.error(f"❌ 无法删除，标记待清理: {path}")
                    try:
                        with open(path + ".cleanup", "w") as f:
                            f.write(str(time.time()))
                    except Exception:
                        pass


class PythonSandbox:
    """
    阅后即焚沙盒 + 项目级 Venv 环境管理
    
    核心执行逻辑：
    1. 获取/创建项目的 sandbox venv
    2. 自动扫描 import → 安装缺失依赖
    3. VFS 同步到临时目录
    4. 用 sandbox venv 的 python 执行
    5. 临时目录自动清除
    """
    
    def __init__(self):
        self.venv_manager = SandboxVenvManager()
        self._warmup_events: Dict[str, threading.Event] = {}
    
    def warm_up(self, project_id: str, tech_stacks: List[str]):
        """
        异步预热：创建 venv + 预装 tech_stack 依赖。
        Manager 规划书完成后由后台线程调用。
        """
        event = threading.Event()
        self._warmup_events[project_id] = event
        
        # 规划书 tech_stack → pip 包名 映射（规划书名称 ≠ import 名 ≠ pip 包名）
        TECH_STACK_TO_PACKAGES = {
            "fastapi": ["fastapi", "uvicorn"],
            "flask": ["flask"],
            "django": ["django"],
            "express": [],  # Node.js，不走 pip
            "sqlalchemy": ["sqlalchemy"],
            "sqlite": [], "sqlite3": [],  # 标准库
            "postgresql": ["psycopg2-binary"],
            "mysql": ["pymysql"],
            "mongodb": ["pymongo"],
            "redis": ["redis"],
            "pydantic": ["pydantic"],
            "requests": ["requests"],
            "httpx": ["httpx"],
            "beautifulsoup": ["beautifulsoup4"],
            "selenium": ["selenium"],
            "pandas": ["pandas"],
            "numpy": ["numpy"],
            "matplotlib": ["matplotlib"],
            "pillow": ["Pillow"],
            "pytest": ["pytest"],
            "jinja2": ["jinja2"],
            "celery": ["celery"],
            "websocket": ["websockets"],
            "cors": [],  # 通常随 fastapi 安装
        }
        # 跳过的纯标识名（语言、前端技术等，不是 pip 包）
        SKIP_NAMES = {'python', 'python3', 'html', 'css', 'javascript', 'js',
                      'typescript', 'ts', 'sql', 'json', 'yaml', 'xml',
                      'react', 'vue', 'angular', 'node', 'nodejs', 'npm'}
        
        try:
            logger.info(f"🔥 Sandbox 预热启动: {project_id}, tech_stacks={tech_stacks}")
            self.venv_manager.get_or_create_venv(project_id)
            
            packages_to_install = set()
            for tech in tech_stacks:
                key = tech.lower().strip()
                if key in SKIP_NAMES:
                    continue
                if key in TECH_STACK_TO_PACKAGES:
                    packages_to_install.update(TECH_STACK_TO_PACKAGES[key])
                else:
                    # fallback: 试试 IMPORT_TO_PACKAGE，再 fallback 到原名
                    pkg = IMPORT_TO_PACKAGE.get(key, key)
                    if pkg.lower() not in STDLIB_MODULES:
                        packages_to_install.add(pkg)
            
            if packages_to_install:
                logger.info(f"📦 预热安装: {packages_to_install}")
            for pkg in packages_to_install:
                self.venv_manager.install_package(project_id, pkg)
            
            logger.info(f"✅ Sandbox 预热完成: {project_id}")
        except Exception as e:
            logger.warning(f"⚠️ Sandbox 预热异常: {e}")
        finally:
            event.set()  # 无论成功失败都标记完成
    
    def wait_warmup(self, project_id: str, timeout: float = 120):
        """等待预热完成（Reviewer 调用 execute_code 前自动调用）"""
        event = self._warmup_events.get(project_id)
        if event and not event.is_set():
            logger.info(f"⏳ 等待 Sandbox 预热完成: {project_id}")
            event.wait(timeout=timeout)
            logger.info(f"✅ Sandbox 预热等待结束: {project_id}")
    
    def _truncate_output(self, text: str) -> str:
        """输出硬性截断，保留最后 N 个字符"""
        if not text:
            return ""
        if len(text) <= MAX_OUTPUT_LENGTH:
            return text
        return f"\n...[Output Truncated (输出过长已截断)]...\n{text[-MAX_OUTPUT_LENGTH:]}"
    
    def _kill_process_tree(self, pid: int):
        """
        强制终止进程及其所有子进程。
        Windows: taskkill /T /F /PID（杀整棵进程树）
        Linux/Mac: os.killpg（杀进程组）
        """
        try:
            if os.name == 'nt':
                # /T = 终止子进程树  /F = 强制终止
                subprocess.run(
                    ['taskkill', '/T', '/F', '/PID', str(pid)],
                    capture_output=True,
                    timeout=10,
                )
                logger.info(f"🔪 已强制终止进程树 (PID: {pid})")
            else:
                import signal
                os.killpg(os.getpgid(pid), signal.SIGKILL)
                logger.info(f"🔪 已发送 SIGKILL 到进程组 (PID: {pid})")
        except Exception as e:
            logger.warning(f"⚠️ 进程树终止异常: {e}")
    
    def _extract_imports(self, code_string: str) -> Set[str]:
        """
        用 AST 解析提取代码中的所有顶级 import 模块名。
        只提取顶级包名（如 from flask.cli import ... → flask）
        """
        imports = set()
        try:
            tree = ast.parse(code_string)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top_level = alias.name.split(".")[0]
                        imports.add(top_level)
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        top_level = node.module.split(".")[0]
                        imports.add(top_level)
        except SyntaxError:
            # 代码有语法错误，跳过 import 扫描
            pass
        return imports
    
    def _auto_install_deps(self, code_string: str, project_id: str):
        """
        自动扫描 import 并安装缺失的第三方包。
        同时扫描 VFS 中其他文件的 import（因为被测代码可能依赖项目内的其他文件，
        而那些文件可能 import 了第三方库）。
        """
        all_imports = self._extract_imports(code_string)
        
        # 也扫描 VFS 中的项目文件，同时收集项目内模块名
        vfs_modules = set()
        try:
            vfs = global_state_manager.get_vfs(project_id)
            vfs_dict = vfs.get_all_vfs()
            for file_path, content in vfs_dict.items():
                file_imports = self._extract_imports(content)
                all_imports.update(file_imports)
                # 收集项目内模块名：main.py → main, backend/main.py → backend, main
                parts = file_path.replace("\\", "/").split("/")
                for part in parts:
                    name_no_ext = os.path.splitext(part)[0]
                    if name_no_ext:
                        vfs_modules.add(name_no_ext)
        except Exception:
            pass
        
        # 过滤：排除标准库 + 排除项目内模块名 + 排除常见项目名
        third_party = set()
        for imp in all_imports:
            if imp in STDLIB_MODULES:
                continue
            if imp in vfs_modules:
                continue
            if imp in COMMON_PROJECT_MODULES:
                continue
            third_party.add(imp)
        
        if not third_party:
            return
        
        # 过滤掉已缓存的包，减少日志噪音
        cached = self.venv_manager._installed_cache.get(project_id, set())
        new_deps = {imp for imp in third_party if IMPORT_TO_PACKAGE.get(imp, imp) not in cached}
        if new_deps:
            logger.info(f"📦 检测到第三方依赖: {new_deps}")
        
        for imp in third_party:
            # 查映射表，获取实际 pip 包名
            package_name = IMPORT_TO_PACKAGE.get(imp, imp)
            self.venv_manager.install_package(project_id, package_name)
    
    def execute_code(self, code_string: str, project_id: str, stdin_data: str = None) -> Dict[str, Any]:
        """
        核心执行方法：
        1. 等待预热完成（如有）
        2. 获取项目 sandbox venv
        3. 自动安装依赖
        4. 在阅后即焚临时目录中执行
        """
        # 等待预热完成（如果 Manager 已触发异步预热）
        self.wait_warmup(project_id)
        
        vfs = global_state_manager.get_vfs(project_id)
        
        # 1. 获取 sandbox venv 的 python 路径
        sandbox_python = self.venv_manager.get_or_create_venv(project_id)
        
        # 2. 自动扫描并安装缺失依赖
        self._auto_install_deps(code_string, project_id)
        
        # 3. 阅后即焚执行
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                # 挂载 VFS 文件到临时目录
                vfs.sync_to_sandbox(temp_dir)
                
                # 生成运行脚本
                task_id = uuid.uuid4().hex[:8]
                script_name = f"_run_task_{task_id}.py"
                script_path = os.path.join(temp_dir, script_name)
                
                with open(script_path, "w", encoding="utf-8") as f:
                    f.write(code_string)
                logger.info(f"⏳ 使用 sandbox venv 执行: {script_name} (Timeout: {EXECUTION_TIMEOUT}s)")
                
                # stdin 处理
                stdin_pipe = subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL
                
                # 环境变量清洗：删除敏感信息
                env = {k: v for k, v in os.environ.items() if k not in SENSITIVE_ENV_KEYS}
                env["PYTHONIOENCODING"] = "utf-8"
                
                # 使用 Popen 替代 run，以便在 Windows 上正确杀死进程树
                # 背景：Flask 等框架的 reloader 会 spawn 子进程，
                # subprocess.run(timeout=N) 只杀父进程，子进程持有管道导致 communicate() 永远挂死
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
                
                process = subprocess.Popen(
                    [sandbox_python, script_path],
                    cwd=temp_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=stdin_pipe,
                    env=env,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    creationflags=creationflags,
                )
                
                try:
                    # 如果有 stdin 数据则传入
                    stdout_str, stderr_str = process.communicate(
                        input=stdin_data,
                        timeout=EXECUTION_TIMEOUT
                    )
                except subprocess.TimeoutExpired:
                    logger.error(f"⚠️ 执行超时 (>{EXECUTION_TIMEOUT}s)，正在强制终止进程树...")
                    # Windows: 用 taskkill /T /F 杀整棵进程树（包括 Flask reloader 等子进程）
                    self._kill_process_tree(process.pid)
                    
                    # 读取已有的输出（短超时防止再次挂死）
                    try:
                        stdout_str, stderr_str = process.communicate(timeout=5)
                    except Exception:
                        stdout_str, stderr_str = "", ""
                    
                    # Windows: 等待文件句柄释放
                    if os.name == 'nt':
                        time.sleep(0.3)
                    
                    return {
                        "success": False,
                        "stdout": self._truncate_output(stdout_str),
                        "stderr": f"Execution timed out after {EXECUTION_TIMEOUT} seconds. (可能存在死循环或阻塞式服务器，进程树已被强制终止)\n{self._truncate_output(stderr_str)}",
                        "returncode": -1
                    }
                
                stdout_truncated = self._truncate_output(stdout_str or "")
                stderr_truncated = self._truncate_output(stderr_str or "")
                
                success = (process.returncode == 0)
                
                if success:
                    logger.info("✅ 执行成功 (Return: 0)")
                else:
                    logger.warning(f"❌ 执行失败 (Return: {process.returncode})")
                
                # Windows: 等待文件句柄释放
                if os.name == 'nt':
                    time.sleep(0.3)
                
                return {
                    "success": success,
                    "stdout": stdout_truncated,
                    "stderr": stderr_truncated,
                    "returncode": process.returncode
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
