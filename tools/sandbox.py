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
import re
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
        # PowerSandbox: 后台进程注册表 {project_id: [{"pid": int, "port": int, "proc": Popen, "cmd": str}]}
        self._background_processes: Dict[str, list] = {}
    
    def warm_up(self, project_id: str, tech_stacks: List[str]):
        """
        异步预热：创建 venv + 预装 tech_stack 依赖。
        Manager 规划书完成后由后台线程调用。
        """
        event = threading.Event()
        self._warmup_events[project_id] = event
        
        # 规划书 tech_stack → pip 包名 映射（规划书名称 ≠ import 名 ≠ pip 包名）
        # 设计原则：每个框架带上其最常见的伴生依赖，"一装到位"
        TECH_STACK_TO_PACKAGES = {
            # ── Web 框架 ──
            "fastapi": ["fastapi", "uvicorn", "httpx", "python-multipart"],
            "flask": ["flask", "flask-cors"],
            "flask-cors": ["flask-cors"],
            "flask-sqlalchemy": ["flask-sqlalchemy", "flask"],
            "flask-login": ["flask-login", "flask"],
            "flask-restful": ["flask-restful", "flask"],
            "flask-socketio": ["flask-socketio", "flask"],
            "flask-migrate": ["flask-migrate", "flask", "alembic"],
            "flask-wtf": ["flask-wtf", "flask"],
            "django": ["django"],
            "django-rest-framework": ["djangorestframework", "django"],
            "djangorestframework": ["djangorestframework", "django"],
            "drf": ["djangorestframework", "django"],
            "tornado": ["tornado"],
            "sanic": ["sanic"],
            "starlette": ["starlette", "uvicorn"],
            "aiohttp": ["aiohttp"],
            "bottle": ["bottle"],
            "express": [],  # Node.js，不走 pip

            # ── 数据库 & ORM ──
            "sqlalchemy": ["sqlalchemy"],
            "alembic": ["alembic", "sqlalchemy"],
            "sqlite": [], "sqlite3": [],  # 标准库
            "postgresql": ["psycopg2-binary"],
            "postgres": ["psycopg2-binary"],
            "psycopg2": ["psycopg2-binary"],
            "mysql": ["pymysql"],
            "mariadb": ["pymysql"],
            "mongodb": ["pymongo"],
            "pymongo": ["pymongo"],
            "motor": ["motor", "pymongo"],         # 异步 MongoDB
            "redis": ["redis"],
            "peewee": ["peewee"],
            "tortoise-orm": ["tortoise-orm"],
            "prisma": [],  # Node.js ORM

            # ── HTTP & 网络 ──
            "requests": ["requests"],
            "httpx": ["httpx"],
            "urllib3": ["urllib3"],
            "websocket": ["websockets"],
            "websockets": ["websockets"],
            "socket.io": ["python-socketio"],
            "grpc": ["grpcio", "protobuf"],
            "graphql": ["graphene"],

            # ── 认证 & 安全 ──
            "jwt": ["PyJWT"],
            "pyjwt": ["PyJWT"],
            "python-jose": ["python-jose"],
            "passlib": ["passlib", "bcrypt"],
            "bcrypt": ["bcrypt"],
            "cryptography": ["cryptography"],
            "pycryptodome": ["pycryptodome"],
            "oauth": ["authlib"],

            # ── 数据科学 & ML ──
            "pandas": ["pandas"],
            "numpy": ["numpy"],
            "scipy": ["scipy"],
            "matplotlib": ["matplotlib"],
            "seaborn": ["seaborn", "matplotlib"],
            "plotly": ["plotly"],
            "scikit-learn": ["scikit-learn"],
            "sklearn": ["scikit-learn"],
            "tensorflow": ["tensorflow"],
            "pytorch": ["torch"],
            "torch": ["torch"],
            "keras": ["keras"],
            "xgboost": ["xgboost"],
            "lightgbm": ["lightgbm"],
            "opencv": ["opencv-python"],
            "cv2": ["opencv-python"],
            "pillow": ["Pillow"],
            "pil": ["Pillow"],

            # ── 数据处理 & 序列化 ──
            "pydantic": ["pydantic"],
            "marshmallow": ["marshmallow"],
            "pyyaml": ["PyYAML"],
            "toml": ["toml"],
            "beautifulsoup": ["beautifulsoup4"],
            "bs4": ["beautifulsoup4"],
            "lxml": ["lxml"],
            "scrapy": ["scrapy"],
            "openpyxl": ["openpyxl"],
            "xlrd": ["xlrd"],
            "python-docx": ["python-docx"],
            "python-pptx": ["python-pptx"],
            "reportlab": ["reportlab"],
            "pypdf": ["pypdf"],

            # ── 异步 & 任务队列 ──
            "celery": ["celery", "redis"],
            "rq": ["rq", "redis"],
            "dramatiq": ["dramatiq"],
            "apscheduler": ["apscheduler"],

            # ── 测试 & 开发 ──
            "pytest": ["pytest"],
            "unittest": [],  # 标准库
            "selenium": ["selenium"],
            "playwright": ["playwright"],
            "coverage": ["coverage"],
            "black": ["black"],
            "flake8": ["flake8"],

            # ── 模板 & 前端集成 ──
            "jinja2": ["jinja2"],
            "jinja": ["jinja2"],  # Manager 可能写 Jinja 而不是 Jinja2
            "mako": ["mako"],
            "markdown": ["markdown"],
            "marked": [],  # JS 库，不走 pip

            # ── CLI & TUI ──
            "click": ["click"],
            "typer": ["typer"],
            "rich": ["rich"],
            "tqdm": ["tqdm"],
            "colorama": ["colorama"],
            "tabulate": ["tabulate"],

            # ── 环境 & 配置 ──
            "dotenv": ["python-dotenv"],
            "python-dotenv": ["python-dotenv"],
            "decouple": ["python-decouple"],

            # ── 日志 & 监控 ──
            "loguru": ["loguru"],
            "sentry": ["sentry-sdk"],

            # ── 图像 & 多媒体 ──
            "ffmpeg": ["ffmpeg-python"],
            "pydub": ["pydub"],

            # ── 空映射（标准库或非 pip）──
            "cors": [],  # 通常随框架安装
            "rest": [],  # 概念名
            "api": [],   # 概念名
        }
        # 跳过的纯标识名（语言、前端技术等，不是 pip 包）
        SKIP_NAMES = {
            # 编程语言
            'python', 'python3', 'python 3', 'java', 'go', 'golang', 'rust', 'c', 'c++', 'ruby',
            # 前端语言 & 标记
            'html', 'html5', 'css', 'css3', 'javascript', 'js', 'es6', 'es2015', 'vanilla',
            'typescript', 'ts', 'jsx', 'tsx',
            # 数据格式
            'sql', 'sqlite', 'sqlite3', 'json', 'yaml', 'xml', 'csv', 'toml', 'graphql',
            # 前端框架（JS 生态，不走 pip）
            'react', 'vue', 'angular', 'svelte', 'next', 'nextjs', 'nuxt', 'nuxtjs',
            'node', 'nodejs', 'npm', 'yarn', 'pnpm', 'bun', 'deno',
            'webpack', 'vite', 'rollup', 'esbuild', 'parcel',
            # 前端 CSS 框架
            'tailwind', 'tailwindcss', 'bootstrap', 'sass', 'scss', 'less',
            'material', 'antd', 'chakra',
            # 通用概念
            'restful', 'rest', 'api', 'microservice', 'serverless', 'docker', 'kubernetes',
            'git', 'github', 'ci', 'cd',
        }
        
        try:
            logger.info(f"🔥 Sandbox 预热启动: {project_id}, tech_stacks={tech_stacks}")
            self.venv_manager.get_or_create_venv(project_id)
            
            packages_to_install = set()
            for tech in tech_stacks:
                # 标准化：拆分复合名称（如 "HTML/CSS/JavaScript (Vanilla)"）
                # 去除括号内容，按 / 和空格拆分
                cleaned = re.sub(r'\([^)]*\)', '', tech)  # 去掉 (Vanilla) 等
                parts = re.split(r'[/,\s]+', cleaned)
                
                for part in parts:
                    key = part.lower().strip()
                    # 去除版本号后缀：python 3 → python, flask 2.0 → flask
                    key = re.sub(r'\s*[\d.]+$', '', key).strip()
                    if not key:
                        continue
                    if key in SKIP_NAMES:
                        continue
                    if key in TECH_STACK_TO_PACKAGES:
                        packages_to_install.update(TECH_STACK_TO_PACKAGES[key])
                    else:
                        pkg = IMPORT_TO_PACKAGE.get(key, key)
                        if pkg.lower() not in STDLIB_MODULES:
                            packages_to_install.add(pkg)
            
            if packages_to_install:
                logger.info(f"📦 预热安装: {packages_to_install}")
            for pkg in packages_to_install:
                self.venv_manager.install_package(project_id, pkg)
            
            logger.info(f"✅ Sandbox 预热完成: {project_id}")

            # 检测 Node.js 环境并记录（仅日志，不阻塞）
            node_info = self._detect_node()
            if node_info["node"]:
                logger.info(f"📦 Node.js 可用: node={node_info['node']}, npm={node_info['npm']}")
            else:
                logger.info("ℹ️ Node.js 未检测到（npm 构建项目将不可用）")
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
    
    @staticmethod
    def _read_file_safe(path: str) -> str:
        """安全读取临时输出文件"""
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception:
            return ""
    
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
    
    def _auto_install_deps(self, code_string: str, project_id: str, sandbox_dir: str = None):
        """
        自动扫描 import 并安装缺失的第三方包。
        同时扫描项目文件的 import（因为被测代码可能依赖项目内的其他文件）。

        v1.3: 优先从 sandbox_dir（VfsUtils .sandbox 目录）读取项目文件。
        """
        all_imports = self._extract_imports(code_string)
        
        # 扫描项目文件，同时收集项目内模块名
        vfs_modules = set()
        try:
            project_files = {}
            if sandbox_dir and os.path.isdir(sandbox_dir):
                # v1.3: 从 VfsUtils sandbox 目录读取
                for root, _, files in os.walk(sandbox_dir):
                    for fname in files:
                        if fname.endswith('.py'):
                            fpath = os.path.join(root, fname)
                            rel = os.path.relpath(fpath, sandbox_dir).replace('\\', '/')
                            try:
                                with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                                    project_files[rel] = f.read()
                            except Exception:
                                pass
            else:
                # sandbox_dir 未提供，跳过项目文件扫描
                pass

            for file_path, content in project_files.items():
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
    
    # ============================================================
    # Node.js / npm 构建支持
    # ============================================================

    def _detect_node(self) -> dict:
        """检测 Node.js 和 npm 是否可用，返回版本信息"""
        result = {"node": None, "npm": None}
        _shell = (os.name == 'nt')  # Windows 上 npm 是 .cmd，需要 shell=True
        try:
            node_out = subprocess.run(
                ["node", "--version"],
                capture_output=True, text=True, timeout=5, shell=_shell
            )
            if node_out.returncode == 0:
                result["node"] = node_out.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        try:
            npm_out = subprocess.run(
                ["npm", "--version"],
                capture_output=True, text=True, timeout=5, shell=_shell
            )
            if npm_out.returncode == 0:
                result["npm"] = npm_out.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass
        return result

    def npm_build(self, sandbox_dir: str, timeout: int = 120) -> dict:
        """
        在项目目录中执行 npm install + npm run build。

        触发条件：sandbox_dir 中存在 package.json
        产出：dist/ 或 build/ 目录

        Args:
            sandbox_dir: 项目文件所在目录（含 package.json）
            timeout: npm 命令超时（秒）

        Returns:
            {"success": bool, "dist_dir": str|None, "error": str}
        """
        pkg_json = os.path.join(sandbox_dir, "package.json")
        if not os.path.isfile(pkg_json):
            return {"success": True, "dist_dir": None, "error": ""}

        node_info = self._detect_node()
        if not node_info["npm"]:
            logger.error("❌ npm 未安装，无法构建前端项目")
            return {
                "success": False, "dist_dir": None,
                "error": "npm 未安装。请安装 Node.js: https://nodejs.org"
            }

        logger.info(f"📦 [npm] 开始构建... (dir={sandbox_dir})")
        _shell = (os.name == 'nt')  # Windows 上 npm 是 .cmd

        # npm install
        try:
            install_result = subprocess.run(
                ["npm", "install", "--legacy-peer-deps"],
                cwd=sandbox_dir, capture_output=True, text=True,
                timeout=timeout, encoding="utf-8", errors="replace",
                shell=_shell
            )
            if install_result.returncode != 0:
                err = install_result.stderr[:500] if install_result.stderr else "未知错误"
                logger.error(f"❌ npm install 失败: {err}")
                return {"success": False, "dist_dir": None,
                        "error": f"npm install 失败: {err}"}
            logger.info("✅ npm install 完成")
        except subprocess.TimeoutExpired:
            logger.error(f"❌ npm install 超时 (>{timeout}s)")
            return {"success": False, "dist_dir": None,
                    "error": f"npm install 超时 (>{timeout}s)"}
        except Exception as e:
            return {"success": False, "dist_dir": None,
                    "error": f"npm install 异常: {e}"}

        # npm run build
        try:
            build_result = subprocess.run(
                ["npm", "run", "build"],
                cwd=sandbox_dir, capture_output=True, text=True,
                timeout=timeout, encoding="utf-8", errors="replace",
                shell=_shell
            )
            if build_result.returncode != 0:
                err = build_result.stderr[:500] if build_result.stderr else "未知错误"
                logger.error(f"❌ npm run build 失败: {err}")
                return {"success": False, "dist_dir": None,
                        "error": f"npm run build 失败: {err}"}
            logger.info("✅ npm run build 完成")
        except subprocess.TimeoutExpired:
            logger.error(f"❌ npm run build 超时 (>{timeout}s)")
            return {"success": False, "dist_dir": None,
                    "error": f"npm run build 超时 (>{timeout}s)"}
        except Exception as e:
            return {"success": False, "dist_dir": None,
                    "error": f"npm run build 异常: {e}"}

        # 查找构建产物目录
        for candidate in ["dist", "build", "out", ".next"]:
            dist_path = os.path.join(sandbox_dir, candidate)
            if os.path.isdir(dist_path):
                logger.info(f"✅ [npm] 构建产物: {dist_path}")
                return {"success": True, "dist_dir": dist_path, "error": ""}

        return {"success": False, "dist_dir": None,
                "error": "构建成功但未找到 dist/build 产物目录"}

    # ============================================================
    # PowerSandbox: 后台进程 + 端口管理
    # ============================================================

    def alloc_port(self, project_id: str) -> int:
        """
        为项目分配一个可用端口（范围 5000-5999）。
        基于 project_id 哈希分配基址，避免跨项目冲突。
        """
        import socket
        base = 5000 + (hash(project_id) % 500)
        for offset in range(100):
            port = base + offset
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(0.5)
                    if s.connect_ex(('localhost', port)) != 0:
                        logger.info(f"🛋️ [PowerSandbox] 分配端口: {port}")
                        return port
            except Exception:
                continue
        raise RuntimeError(f"无可用端口 (base={base})")

    def wait_port_ready(self, port: int, timeout: float = 15) -> bool:
        """轮询检测端口是否就绪"""
        import socket
        start = time.time()
        while time.time() - start < timeout:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(1)
                    if s.connect_ex(('localhost', port)) == 0:
                        logger.info(f"✅ [PowerSandbox] 端口 {port} 已就绪 ({time.time()-start:.1f}s)")
                        return True
            except Exception:
                pass
            time.sleep(0.5)
        logger.warning(f"⚠️ [PowerSandbox] 端口 {port} 超时未就绪 ({timeout}s)")
        return False

    def start_background(self, cmd: list, project_id: str,
                         port: int = None, cwd: str = None,
                         wait_ready: bool = True, timeout: float = 15) -> dict:
        """
        后台启动一个服务进程。

        Args:
            cmd: 命令列表，如 [python_path, "main.py"]
            project_id: 项目 ID
            port: 服务监听端口（可选，用于就绪检测）
            cwd: 工作目录
            wait_ready: 是否等待端口就绪
            timeout: 等待超时

        Returns:
            {"pid": int, "port": int, "success": bool}
        """
        # 环境变量清洗
        env = {k: v for k, v in os.environ.items() if k not in SENSITIVE_ENV_KEYS}
        env["PYTHONIOENCODING"] = "utf-8"

        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )

            # 注册到进程池
            entry = {"pid": proc.pid, "port": port, "proc": proc, "cmd": " ".join(cmd)}
            if project_id not in self._background_processes:
                self._background_processes[project_id] = []
            self._background_processes[project_id].append(entry)

            logger.info(f"🚀 [PowerSandbox] 后台启动: PID={proc.pid}, cmd={' '.join(cmd)}")

            # 等待端口就绪
            if port and wait_ready:
                ready = self.wait_port_ready(port, timeout)
                if not ready:
                    # 检查进程是否已崩溃
                    if proc.poll() is not None:
                        _, stderr = proc.communicate(timeout=3)
                        logger.error(f"❌ [PowerSandbox] 服务启动崩溃 (exit={proc.returncode}): {stderr[:500]}")
                    return {"pid": proc.pid, "port": port, "success": False, "error": "端口未就绪"}

            return {"pid": proc.pid, "port": port, "success": True}

        except Exception as e:
            logger.error(f"❌ [PowerSandbox] 后台启动失败: {e}")
            return {"pid": -1, "port": port, "success": False, "error": str(e)}

    def stop_background(self, pid: int):
        """终止指定后台进程"""
        self._kill_process_tree(pid)
        # 从注册表中移除
        for project_id, entries in self._background_processes.items():
            self._background_processes[project_id] = [e for e in entries if e["pid"] != pid]
        logger.info(f"🔪 [PowerSandbox] 已停止后台进程 PID={pid}")

    def cleanup_all(self, project_id: str = None):
        """清理项目的所有后台进程（或全部）"""
        if project_id:
            entries = self._background_processes.pop(project_id, [])
            for entry in entries:
                try:
                    self._kill_process_tree(entry["pid"])
                except Exception:
                    pass
            if entries:
                logger.info(f"🧹 [PowerSandbox] 已清理 {len(entries)} 个后台进程 (project={project_id})")
        else:
            total = 0
            for pid_list in self._background_processes.values():
                for entry in pid_list:
                    try:
                        self._kill_process_tree(entry["pid"])
                    except Exception:
                        pass
                    total += 1
            self._background_processes.clear()
            if total:
                logger.info(f"🧹 [PowerSandbox] 已清理全部 {total} 个后台进程")

    def get_background_info(self, project_id: str) -> list:
        """获取项目的后台进程信息"""
        return [
            {"pid": e["pid"], "port": e["port"], "cmd": e["cmd"]}
            for e in self._background_processes.get(project_id, [])
        ]

    # ============================================================
    # 核心执行
    # ============================================================

    def execute_code(self, code_string: str, project_id: str, stdin_data: str = None,
                     sandbox_dir: str = None, timeout: int = None) -> Dict[str, Any]:
        """
        核心执行方法：
        1. 等待预热完成（如有）
        2. 获取项目 sandbox venv
        3. 自动安装依赖
        4. 在阅后即焚临时目录中执行

        v1.3: sandbox_dir 由 Engine 传入（VfsUtils .sandbox 目录），
        优先从该目录复制项目文件，不再依赖 StateManager VFS。
        """
        # 等待预热完成（如果 Manager 已触发异步预热）
        self.wait_warmup(project_id)
        
        # 1. 获取 sandbox venv 的 python 路径
        sandbox_python = self.venv_manager.get_or_create_venv(project_id)
        
        # 2. 自动扫描并安装缺失依赖
        self._auto_install_deps(code_string, project_id, sandbox_dir=sandbox_dir)
        
        # 3. 阅后即焚执行
        try:
            with tempfile.TemporaryDirectory() as temp_dir:
                # v1.3: 从 VfsUtils sandbox 目录复制项目文件
                if sandbox_dir and os.path.isdir(sandbox_dir):
                    import shutil
                    for item in os.listdir(sandbox_dir):
                        src = os.path.join(sandbox_dir, item)
                        dst = os.path.join(temp_dir, item)
                        if os.path.isdir(src):
                            shutil.copytree(src, dst, dirs_exist_ok=True)
                        else:
                            shutil.copy2(src, dst)
                else:
                    # sandbox_dir 未提供，无项目文件可复制
                    logger.warning("⚠️ sandbox_dir 未提供，测试脚本将在空目录中执行")
                
                # 生成运行脚本
                task_id = uuid.uuid4().hex[:8]
                script_name = f"_run_task_{task_id}.py"
                script_path = os.path.join(temp_dir, script_name)
                
                with open(script_path, "w", encoding="utf-8") as f:
                    f.write(code_string)
                logger.info(f"⏳ 使用 sandbox venv 执行: {script_name} (Timeout: {timeout or EXECUTION_TIMEOUT}s)")
                
                # stdin 处理
                stdin_pipe = subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL
                
                # 环境变量清洗：删除敏感信息
                env = {k: v for k, v in os.environ.items() if k not in SENSITIVE_ENV_KEYS}
                env["PYTHONIOENCODING"] = "utf-8"
                
                # 使用 Popen 替代 run，以便在 Windows 上正确杀死进程树
                # 背景：Flask 等框架的 reloader 会 spawn 子进程，
                # subprocess.run(timeout=N) 只杀父进程，子进程持有管道导致 communicate() 永远挂死
                creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0
                
                # 使用临时文件替代 PIPE 捕获输出
                # 原因：communicate() 等待 PIPE 关闭，子进程（如 uvicorn worker）
                # 继承 PIPE 句柄后会阻塞 communicate() 直到超时。
                # wait() 只等进程退出，不受子进程句柄影响。
                stdout_path = os.path.join(temp_dir, "_stdout.txt")
                stderr_path = os.path.join(temp_dir, "_stderr.txt")
                
                stdout_file = open(stdout_path, "w", encoding="utf-8", errors="replace")
                stderr_file = open(stderr_path, "w", encoding="utf-8", errors="replace")
                
                try:
                    process = subprocess.Popen(
                        [sandbox_python, script_path],
                        cwd=temp_dir,
                        stdout=stdout_file,
                        stderr=stderr_file,
                        stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
                        env=env,
                        creationflags=creationflags,
                    )
                    
                    # 如果有 stdin 数据则写入
                    if stdin_data is not None:
                        try:
                            process.stdin.write(stdin_data.encode("utf-8"))
                            process.stdin.close()
                        except Exception:
                            pass
                    
                    effective_timeout = timeout or EXECUTION_TIMEOUT
                    try:
                        process.wait(timeout=effective_timeout)
                    except subprocess.TimeoutExpired:
                        logger.error(f"⚠️ 执行超时 (>{effective_timeout}s)，正在强制终止进程树...")
                        self._kill_process_tree(process.pid)
                        try:
                            process.wait(timeout=5)
                        except Exception:
                            pass
                        
                        if os.name == 'nt':
                            time.sleep(0.3)
                        
                        # 关闭文件后读取
                        stdout_file.close()
                        stderr_file.close()
                        stdout_str = self._read_file_safe(stdout_path)
                        stderr_str = self._read_file_safe(stderr_path)
                        
                        return {
                            "success": False,
                            "stdout": self._truncate_output(stdout_str),
                            "stderr": f"Execution timed out after {effective_timeout} seconds. (可能存在死循环或阻塞式服务器，进程树已被强制终止)\n{self._truncate_output(stderr_str)}",
                            "returncode": -1
                        }
                finally:
                    # 确保文件句柄关闭
                    if not stdout_file.closed:
                        stdout_file.close()
                    if not stderr_file.closed:
                        stderr_file.close()
                
                # 正常完成，读取输出
                stdout_str = self._read_file_safe(stdout_path)
                stderr_str = self._read_file_safe(stderr_path)
                
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
