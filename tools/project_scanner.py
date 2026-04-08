"""
ProjectScanner — 已有项目逆向扫描器（Phase 1.3）

核心能力：
- 100% 确定性扫描，零 LLM 消耗
- 复用 Observer 的 get_tree / get_skeleton / extract_schema / extract_routes
- 输出 raw_scan_result dict，供 Manager LLM 合成 project_spec

扫描流程：
1. 依赖文件解析 → tech_stack
2. 全量文件骨架集 → skeletons
3. AST 路由/模型提取 → routes / models
4. 入口检测 → entry
5. 配置文件检测 → 补充 tech_stack
6. 关键文件全文 → key_files_code（入口+路由，帮 LLM 理解 key_decisions）
"""
import os
import re
import json
import logging
from typing import Dict, List, Any, Optional

from tools.observer import Observer, SKIP_DIRS, SKIP_FILES

logger = logging.getLogger("ProjectScanner")

# ============================================================
# 依赖包 → 技术标签 映射表
# ============================================================

PYTHON_PACKAGE_TO_TECH = {
    # Web 框架
    "flask": "Flask",
    "flask-cors": "Flask-CORS",
    "fastapi": "FastAPI",
    "uvicorn": "Uvicorn",
    "django": "Django",
    "starlette": "Starlette",
    # ORM / 数据库
    "sqlalchemy": "SQLAlchemy",
    "flask-sqlalchemy": "Flask-SQLAlchemy",
    "peewee": "Peewee",
    "tortoise-orm": "Tortoise-ORM",
    "pymongo": "MongoDB",
    "redis": "Redis",
    "psycopg2-binary": "PostgreSQL",
    "psycopg2": "PostgreSQL",
    # 模板
    "jinja2": "Jinja2",
    "mako": "Mako",
    # 数据验证
    "pydantic": "Pydantic",
    "marshmallow": "Marshmallow",
    # 工具
    "celery": "Celery",
    "python-dotenv": "dotenv",
    "gunicorn": "Gunicorn",
    "httpx": "HTTPX",
    "requests": "Requests",
    "beautifulsoup4": "BeautifulSoup",
    "pillow": "Pillow",
    "numpy": "NumPy",
    "pandas": "Pandas",
    "matplotlib": "Matplotlib",
}

NPM_PACKAGE_TO_TECH = {
    # 框架
    "vue": "Vue3",
    "react": "React",
    "react-dom": "React",
    "next": "Next.js",
    "svelte": "Svelte",
    "angular": "Angular",
    # 构建
    "vite": "Vite",
    "webpack": "Webpack",
    "esbuild": "esbuild",
    # 样式
    "tailwindcss": "Tailwind CSS",
    "sass": "Sass",
    "less": "Less",
    "styled-components": "Styled Components",
    # 状态管理
    "pinia": "Pinia",
    "vuex": "Vuex",
    "redux": "Redux",
    "zustand": "Zustand",
    # 路由
    "vue-router": "Vue Router",
    "react-router": "React Router",
    "react-router-dom": "React Router",
    # HTTP
    "axios": "Axios",
    # UI 库
    "element-plus": "Element Plus",
    "ant-design-vue": "Ant Design Vue",
    "antd": "Ant Design",
    "@mui/material": "Material UI",
    "chart.js": "Chart.js",
    "echarts": "ECharts",
    # 工具
    "express": "Express.js",
    "typescript": "TypeScript",
    "eslint": "ESLint",
    "prettier": "Prettier",
}

# 源文件扩展名
SOURCE_EXTENSIONS = {
    '.py', '.js', '.jsx', '.ts', '.tsx', '.vue', '.svelte',
    '.html', '.htm', '.css', '.scss', '.json', '.md',
}


class ProjectScanner:
    """
    已有项目逆向扫描器。
    100% 确定性，零 LLM 消耗。
    """

    def __init__(self, project_dir: str):
        self.project_dir = os.path.abspath(project_dir)
        if not os.path.isdir(self.project_dir):
            raise FileNotFoundError(f"项目目录不存在: {self.project_dir}")
        self.observer = Observer(self.project_dir)

    def scan(self) -> Dict[str, Any]:
        """
        执行完整扫描，返回 raw_scan_result。
        """
        logger.info(f"🔍 开始逆向扫描: {self.project_dir}")

        # 1. 收集所有源文件
        source_files = self._collect_source_files()
        logger.info(f"📂 发现 {len(source_files)} 个源文件")

        # 2. 依赖解析 → tech_stack
        tech_stack = self._detect_tech_stack()

        # 3. 目录树
        tree = self.observer.get_tree(max_depth=4)

        # 4. 入口检测
        entry = self._detect_entry(source_files)

        # 5. AST 路由提取
        routes = self._extract_all_routes(source_files)

        # 6. AST 模型提取
        models = self._extract_all_models(source_files)

        # 7. 配置文件检测 → 补充 tech_stack
        config_files = self._detect_config_files()
        tech_stack = self._enrich_tech_stack(tech_stack, config_files)

        # 8. 全量文件骨架集
        skeletons = self._extract_all_skeletons(source_files)

        # 9. 关键文件全文（入口 + 路由文件）
        key_files_code = self._extract_key_files_code(entry, routes, source_files)

        result = {
            "tech_stack": tech_stack,
            "files": source_files,
            "tree": tree,
            "skeletons": skeletons,
            "routes": routes,
            "models": models,
            "entry": entry,
            "config_files": config_files,
            "key_files_code": key_files_code,
        }

        total_routes = len(routes)
        total_models = len(models)
        total_skeletons = len(skeletons)
        total_key = len(key_files_code)
        logger.info(
            f"✅ 扫描完成: {len(source_files)} 文件, "
            f"{len(tech_stack)} 技术标签, "
            f"{total_routes} 路由, {total_models} 模型, "
            f"{total_skeletons} 骨架, {total_key} 关键文件全文"
        )
        return result

    # ============================================================
    # 内部方法
    # ============================================================

    def _collect_source_files(self) -> List[str]:
        """收集所有源文件（相对路径）"""
        files = []
        for root, dirs, filenames in os.walk(self.project_dir):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')]
            for fname in filenames:
                if fname in SKIP_FILES or fname.startswith('.'):
                    continue
                ext = os.path.splitext(fname)[1].lower()
                if ext in SOURCE_EXTENSIONS:
                    rel = os.path.relpath(os.path.join(root, fname), self.project_dir)
                    files.append(rel.replace("\\", "/"))
        return sorted(files)

    def _detect_tech_stack(self) -> List[str]:
        """从依赖文件解析技术栈"""
        tech = set()

        # requirements.txt
        req_path = os.path.join(self.project_dir, "requirements.txt")
        if os.path.isfile(req_path):
            tech.add("Python 3")
            try:
                with open(req_path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip().lower()
                        if not line or line.startswith("#") or line.startswith("-"):
                            continue
                        # 提取包名（去掉版本号）
                        pkg = re.split(r'[>=<!\[\];]', line)[0].strip()
                        if pkg in PYTHON_PACKAGE_TO_TECH:
                            tech.add(PYTHON_PACKAGE_TO_TECH[pkg])
            except Exception as e:
                logger.warning(f"⚠️ 解析 requirements.txt 失败: {e}")

        # package.json
        pkg_path = os.path.join(self.project_dir, "package.json")
        if os.path.isfile(pkg_path):
            try:
                with open(pkg_path, "r", encoding="utf-8") as f:
                    pkg_data = json.load(f)
                all_deps = {}
                all_deps.update(pkg_data.get("dependencies", {}))
                all_deps.update(pkg_data.get("devDependencies", {}))
                for dep_name in all_deps:
                    dep_lower = dep_name.lower()
                    if dep_lower in NPM_PACKAGE_TO_TECH:
                        tech.add(NPM_PACKAGE_TO_TECH[dep_lower])
            except Exception as e:
                logger.warning(f"⚠️ 解析 package.json 失败: {e}")

        # 如果没有任何依赖文件但有 .py 文件
        if not tech:
            py_files = [f for f in os.listdir(self.project_dir) if f.endswith('.py')]
            if py_files:
                tech.add("Python 3")

        return sorted(tech)

    def _detect_entry(self, source_files: List[str]) -> Dict[str, Any]:
        """检测入口文件 + 端口"""
        entry = {"file": None, "port": None, "framework": None}

        for rel_path in source_files:
            if not rel_path.endswith('.py'):
                continue
            abs_path = os.path.join(self.project_dir, rel_path)
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                    content = f.read()
            except Exception:
                continue

            # 检查 if __name__ == "__main__"
            if 'if __name__' not in content:
                continue

            # uvicorn.run / app.run 检测
            if re.search(r'uvicorn\.run', content):
                entry["file"] = rel_path
                entry["framework"] = "FastAPI"
                port_m = re.search(r'port\s*=\s*(\d+)', content)
                if port_m:
                    entry["port"] = int(port_m.group(1))
                break
            elif re.search(r'app\.run', content):
                entry["file"] = rel_path
                # 区分 Flask 和其他
                if 'Flask' in content:
                    entry["framework"] = "Flask"
                else:
                    entry["framework"] = "Python"
                port_m = re.search(r'port\s*=\s*(\d+)', content)
                if port_m:
                    entry["port"] = int(port_m.group(1))
                break

        # 如果没找到，尝试常见入口文件名
        if not entry["file"]:
            for candidate in ["app.py", "main.py", "server.py", "run.py", "manage.py",
                              "src/app.py", "src/main.py"]:
                if candidate in source_files:
                    entry["file"] = candidate
                    break

        return entry

    def _extract_all_routes(self, source_files: List[str]) -> List[Dict[str, Any]]:
        """对所有 Python 文件提取路由"""
        all_routes = []
        for rel_path in source_files:
            if not rel_path.endswith('.py'):
                continue
            routes = self.observer.extract_routes(rel_path)
            for r in routes:
                r["file"] = rel_path
            all_routes.extend(routes)
        return all_routes

    def _extract_all_models(self, source_files: List[str]) -> List[Dict[str, Any]]:
        """对所有 Python 文件提取数据模型"""
        all_models = []
        for rel_path in source_files:
            if not rel_path.endswith('.py'):
                continue
            models = self.observer.extract_schema(rel_path)
            for m in models:
                m["file"] = rel_path
            all_models.extend(models)
        return all_models

    def _detect_config_files(self) -> List[str]:
        """检测配置文件"""
        config_names = [
            "requirements.txt", "package.json", "tsconfig.json",
            "vite.config.js", "vite.config.ts",
            "tailwind.config.js", "tailwind.config.ts",
            "webpack.config.js", "postcss.config.js",
            ".env", ".env.local", ".env.production",
            "Dockerfile", "docker-compose.yml",
            "pyproject.toml", "setup.py", "setup.cfg",
            "Makefile", "Procfile",
        ]
        found = []
        for name in config_names:
            if os.path.isfile(os.path.join(self.project_dir, name)):
                found.append(name)
        return found

    def _enrich_tech_stack(self, tech: List[str], config_files: List[str]) -> List[str]:
        """根据配置文件补充技术栈"""
        tech_set = set(tech)
        config_tech_map = {
            "vite.config.js": "Vite",
            "vite.config.ts": "Vite",
            "tailwind.config.js": "Tailwind CSS",
            "tailwind.config.ts": "Tailwind CSS",
            "webpack.config.js": "Webpack",
            "tsconfig.json": "TypeScript",
            "Dockerfile": "Docker",
            "docker-compose.yml": "Docker Compose",
            "pyproject.toml": "Python 3",
        }
        for cf in config_files:
            if cf in config_tech_map:
                tech_set.add(config_tech_map[cf])
        return sorted(tech_set)

    def _extract_all_skeletons(self, source_files: List[str]) -> Dict[str, str]:
        """为所有源文件提取骨架"""
        skeletons = {}
        for rel_path in source_files:
            ext = os.path.splitext(rel_path)[1].lower()
            # 跳过 JSON/MD 等非代码文件的骨架提取
            if ext in ('.json', '.md'):
                continue
            skeleton = self.observer.get_skeleton(rel_path)
            if skeleton and not skeleton.startswith("Error:"):
                skeletons[rel_path] = skeleton
        return skeletons

    def _extract_key_files_code(self, entry: Dict, routes: List[Dict],
                                 source_files: List[str]) -> Dict[str, str]:
        """
        提取关键文件的完整代码（入口文件 + 路由文件）。
        帮助 LLM 理解 key_decisions（中间件/CORS/数据库连接方式等）。
        限制：每个文件最多 400 行，总共最多 3 个文件。
        """
        key_files = set()

        # 入口文件
        if entry.get("file"):
            key_files.add(entry["file"])

        # 路由文件（去重）
        route_files = {r["file"] for r in routes if r.get("file")}
        key_files.update(route_files)

        # 限制最多 3 个
        key_files_list = sorted(key_files)[:3]

        result = {}
        for rel_path in key_files_list:
            abs_path = os.path.join(self.project_dir, rel_path)
            if not os.path.isfile(abs_path):
                continue
            try:
                with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                    lines = f.readlines()
                # 最多 400 行
                if len(lines) > 400:
                    content = "".join(lines[:400]) + f"\n# ... (截断，共 {len(lines)} 行)"
                else:
                    content = "".join(lines)
                result[rel_path] = content
            except Exception as e:
                logger.warning(f"⚠️ 读取关键文件失败 {rel_path}: {e}")

        return result


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) < 2:
        print("用法: python project_scanner.py <项目目录>")
        sys.exit(1)

    scanner = ProjectScanner(sys.argv[1])
    result = scanner.scan()

    # 输出摘要
    print(f"\n{'='*60}")
    print(f"📊 扫描报告: {sys.argv[1]}")
    print(f"{'='*60}")
    print(f"技术栈: {', '.join(result['tech_stack'])}")
    print(f"文件数: {len(result['files'])}")
    print(f"入口: {result['entry']}")
    print(f"路由: {len(result['routes'])} 个")
    print(f"模型: {len(result['models'])} 个")
    print(f"骨架: {len(result['skeletons'])} 个")
    print(f"关键文件全文: {list(result['key_files_code'].keys())}")
    print(f"配置文件: {result['config_files']}")
    print(f"\n目录树:\n{result['tree']}")
