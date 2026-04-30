"""
ProjectObserver — 项目情报局

从 engine.py 拆解而来 (Engine A-1 Phase B)。
职责：
- 三级精准依赖解析（传递闭包 + AST import + 兜底扫描）
- 路由文件发现与路由清单构建
- 技术栈推断（Patch Mode 用）
- Coder 上下文一站式组装（build_task_meta）
- 复杂文件识别与预估

设计原则：
- 只读取，不修改任何文件或状态
- 通过 Blackboard 和 VFS 接口获取项目信息
"""
import os
import json
import logging
from typing import Optional, Dict, List

from core.blackboard import Blackboard, TaskItem

logger = logging.getLogger("ProjectObserver")

# 单一职责阈值
_SRP_ENDPOINT_THRESHOLD = 5
_SRP_MODEL_THRESHOLD = 3


def build_route_module_contract_hint(project_spec: dict, target_file: str) -> str:
    if not isinstance(project_spec, dict):
        return ""

    contracts = project_spec.get("route_module_contracts", {}) or {}
    contract = contracts.get(target_file)
    if not isinstance(contract, dict):
        return ""

    mode = str(contract.get("mode") or "unknown").strip().lower()
    blueprints = [item for item in (contract.get("blueprints", []) or []) if item]
    helpers = [item for item in (contract.get("helper_functions", []) or []) if item]
    url_prefix_hint = str(contract.get("url_prefix_hint") or "").strip()

    lines = ["--- [路由模块实现契约] ---", f"模块: {target_file}", f"实现范式: {mode}"]
    if blueprints:
        lines.append(f"blueprint: {', '.join(blueprints)}")
    if helpers:
        lines.append(f"helper 函数: {', '.join(helpers)}")

    if mode == "init_function":
        lines.append("`init_*_routes` / `register_*_routes` 是挂载 helper，不是 HTTP 端点。")
        lines.append("不要给 helper 自身添加 `@bp.route(...)`，真实端点函数应单独注册。")
    elif mode == "direct_blueprint":
        lines.append("直接导出 blueprint 与顶层 handler，不要把所有路由包进 init helper。")

    if url_prefix_hint:
        lines.append(
            f"app 侧预期 url_prefix: `{url_prefix_hint}`。"
            "本文件中的 `@bp.route(...)` 只能写局部相对路径，禁止重复写完整 `/api/...` 前缀。"
        )

    return "\n".join(lines) + "\n"


def build_architecture_contract_hint(project_spec: dict, target_file: str) -> str:
    if not isinstance(project_spec, dict):
        return ""
    if not str(target_file or "").lower().endswith(".py"):
        return ""

    contract = project_spec.get("architecture_contract", {}) or {}
    if not isinstance(contract, dict) or not contract:
        return ""

    backend_framework = str(contract.get("backend_framework") or "unknown").strip().lower()
    orm_mode = str(contract.get("orm_mode") or "unknown").strip().lower()
    auth_mode = str(contract.get("auth_mode") or "unknown").strip().lower()
    router_mode = str(contract.get("router_mode") or "unknown").strip().lower()
    entrypoint_mode = str(contract.get("entrypoint_mode") or "unknown").strip().lower()
    package_layout = str(contract.get("package_layout") or "unknown").strip().lower()
    import_style = str(contract.get("import_style") or "unknown").strip().lower()

    lines = [
        "--- [后端单一架构契约] ---",
        f"backend_framework: {backend_framework}",
        f"orm_mode: {orm_mode}",
        f"auth_mode: {auth_mode}",
        f"router_mode: {router_mode}",
        f"entrypoint_mode: {entrypoint_mode}",
        f"package_layout: {package_layout}",
        f"import_style: {import_style}",
    ]

    if backend_framework == "fastapi":
        lines.append("禁止生成 Flask / Blueprint / Flask-Login / Flask-SQLAlchemy 语义。")
    elif backend_framework == "flask":
        lines.append("禁止生成 FastAPI / APIRouter / Depends(get_db) 语义。")

    if orm_mode == "sqlalchemy_session":
        lines.append("数据库层必须使用 create_engine/sessionmaker/get_db 或 Session，禁止 db.Model / db.session。")
    elif orm_mode == "flask_sqlalchemy":
        lines.append("数据库层必须使用 Flask-SQLAlchemy，禁止 SessionLocal/declarative_base/get_db。")

    if auth_mode == "jwt_header":
        lines.append("认证层必须使用 JWT header / bearer token，禁止 Flask-Login session。")
    elif auth_mode == "flask_login_session":
        lines.append("认证层必须使用 Flask-Login session，禁止 OAuth2/JWT bearer 流程。")

    if package_layout == "flat_modules" and import_style == "sibling_import":
        lines.append("本项目是扁平模块布局，禁止 `from src.xxx import ...` 或相对包导入。")

    return "\n".join(lines) + "\n"


def identify_complex_files(project_spec: dict, tasks: List[dict]) -> Dict[str, str]:
    """
    根据 project_spec 的 api_contracts / data_models 数量，
    识别结构复杂度高的后端文件。

    Returns:
        {target_file: reason} — 被标记为复杂的文件及原因。
        前端文件永远不标记（前端靠拆组件解决）。
    """
    if not project_spec:
        return {}

    complex_files: Dict[str, str] = {}

    # 前端文件后缀（永不标记）
    frontend_exts = {'.html', '.css', '.js', '.jsx', '.ts', '.tsx', '.vue', '.svelte'}

    # 收集所有 target_file
    task_files = {t.get("target_file", "") for t in tasks}

    # 1. 统计每个文件关联的 API 端点数
    api_contracts = project_spec.get("api_contracts", [])
    if api_contracts:
        route_files = [f for f in task_files
                       if any(kw in f.lower() for kw in ('route', 'api', 'app', 'view', 'endpoint'))
                       and not any(f.endswith(ext) for ext in frontend_exts)]

        if route_files:
            endpoints_per_file = len(api_contracts) / len(route_files)
            if endpoints_per_file >= _SRP_ENDPOINT_THRESHOLD:
                for f in route_files:
                    complex_files[f] = f"API端点密度高({endpoints_per_file:.0f}个端点)"

    # 2. 统计数据模型数
    data_models = project_spec.get("data_models", [])
    if data_models:
        model_files = [f for f in task_files
                       if any(kw in f.lower() for kw in ('model', 'schema', 'entity', 'db'))
                       and not any(f.endswith(ext) for ext in frontend_exts)]

        if model_files:
            models_per_file = len(data_models) / len(model_files)
            if models_per_file >= _SRP_MODEL_THRESHOLD:
                for f in model_files:
                    reason = f"数据模型密度高({models_per_file:.0f}个模型)"
                    if f in complex_files:
                        complex_files[f] += f" + {reason}"
                    else:
                        complex_files[f] = reason

    if complex_files:
        logger.info(f"🔍 复杂文件识别: {complex_files}")

    return complex_files


class ProjectObserver:
    """项目情报局 — 为 Coder/Reviewer 提供精确的上下文快照"""

    def __init__(self, blackboard: Blackboard, vfs):
        self.blackboard = blackboard
        self.vfs = vfs

    # ============================================================
    # 三级精准依赖解析
    # ============================================================

    def resolve_smart_deps(self, task: TaskItem, existing_code: str = "") -> list:
        """
        三级精准依赖解析（零 LLM 成本）：
        L1: 传递闭包 — 递归展开 task.dependencies
        L2: AST import — 解析 existing_code 的 import 语句
        L3: 兜底全量 — 真理区所有源码文件（上限 3 个）
        """
        target = task.target_file
        truth_dir = self.vfs.truth_dir if self.vfs else None

        # L1: 传递闭包
        dep_files = self._resolve_transitive_deps(task)

        # L2: AST import 分析（修复模式有 existing_code）
        if existing_code and truth_dir:
            import_deps = self._resolve_imports(existing_code, truth_dir, target)
            dep_files = list(set(dep_files + import_deps))

        # L3: 兜底
        if not dep_files and truth_dir:
            dep_files = self._get_all_truth_files(truth_dir, exclude=target)

        return dep_files

    def _resolve_transitive_deps(self, task: TaskItem) -> list:
        """L1: 递归展开 task.dependencies → 传递闭包所有上游文件"""
        id_to_task = {t.task_id: t for t in self.blackboard.state.tasks}
        visited = set()

        def _walk(deps):
            for dep_id in deps:
                if dep_id in visited:
                    continue
                visited.add(dep_id)
                dep_task = id_to_task.get(dep_id)
                if dep_task:
                    _walk(dep_task.dependencies)

        _walk(task.dependencies)
        return [id_to_task[d].target_file for d in visited if d in id_to_task]

    @staticmethod
    def _resolve_imports(code: str, truth_dir: str, exclude: str = "") -> list:
        """L2: 从代码的 import 语句精准定位依赖文件"""
        import ast as ast_module
        try:
            tree = ast_module.parse(code)
        except SyntaxError:
            return []

        needed = []
        for node in ast_module.walk(tree):
            module = None
            if isinstance(node, ast_module.ImportFrom) and node.module:
                module = node.module
            elif isinstance(node, ast_module.Import):
                for alias in node.names:
                    module = alias.name

            if not module:
                continue

            candidates = [
                module.replace(".", "/") + ".py",
                "src/" + module.replace(".", "/") + ".py",
            ]
            for c in candidates:
                if c != exclude and os.path.isfile(os.path.join(truth_dir, c)):
                    needed.append(c)
                    break

        return needed

    @staticmethod
    def _get_all_truth_files(truth_dir: str, exclude: str = "") -> list:
        """L3: 兜底 — 获取真理区所有源码文件（上限 3 个）"""
        SKIP = {'.git', '__pycache__', 'node_modules', '.venv'}
        EXTS = {'.py', '.js', '.jsx', '.ts', '.tsx', '.html', '.css'}
        result = []
        for root, dirs, files in os.walk(truth_dir):
            dirs[:] = [d for d in dirs if d not in SKIP]
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in EXTS:
                    rel = os.path.relpath(os.path.join(root, f), truth_dir).replace("\\", "/")
                    if rel != exclude:
                        result.append(rel)
        return result[:3]

    # ============================================================
    # 路由发现与清单构建
    # ============================================================

    def find_routes_file(self) -> Optional[str]:
        """动态发现路由文件：从当前任务列表中找含路由定义的 .py 文件"""
        from tools.observer import Observer
        obs = Observer(self.vfs.truth_dir if self.vfs else ".")

        ROUTE_BASENAMES = {'routes.py', 'views.py', 'urls.py', 'main.py', 'app.py'}
        for task in self.blackboard.state.tasks:
            basename = os.path.basename(task.target_file).lower()
            if basename in ROUTE_BASENAMES:
                routes = obs.extract_routes(task.target_file)
                if routes:
                    return task.target_file
        return None

    def build_route_manifest(self) -> str:
        """构建可用路由清单文本（供 Coder prompt 消费）。
        优先级：page_routes 契约 > global_routes > 真理区扫描。
        纯前端项目 → 返回空字符串 → 不产生约束。"""
        all_routes = []
        # 来源 0: page_routes 契约（最权威，来自项目规划书）
        try:
            spec = json.loads(self.blackboard.state.spec_text or "{}")
            for r in spec.get("page_routes", []):
                entry = f"{r.get('method','?')} {r.get('path','?')} → {r.get('function','?')}"
                # P1-d: 展示 endpoint（契约中可能有，也可能没有）
                ep = r.get('endpoint', r.get('function', '?'))
                entry += f" (endpoint: {ep})"
                if r.get("renders"):
                    entry += f" → renders {r['renders']}"
                all_routes.append(entry)
        except Exception:
            pass
        if all_routes:
            return "\n".join(all_routes)
        # 来源 1: global_routes（已提交到真理区的路由）
        for file_path, routes in self.blackboard.state.global_routes.items():
            for r in routes:
                ep = r.get('endpoint', r.get('function', '?'))
                entry = f"{r['method']} {r['path']} → {r.get('function', '?')} (endpoint: {ep})"
                if entry not in all_routes:
                    all_routes.append(entry)
        # 来源 2: 真理区扫描
        if self.vfs and not all_routes:
            from tools.observer import Observer
            obs = Observer(self.vfs.truth_dir)
            for f in self.vfs.list_truth_files():
                if f.endswith('.py'):
                    routes = obs.extract_routes(f)
                    for r in routes:
                        ep = r.get('endpoint', r.get('function', '?'))
                        entry = f"{r['method']} {r['path']} → {r.get('function', '?')} (endpoint: {ep})"
                        if entry not in all_routes:
                            all_routes.append(entry)
        if not all_routes:
            return ""
        return "\n".join(all_routes)

    # ============================================================
    # 技术栈推断
    # ============================================================

    @staticmethod
    def infer_tech_stack(project_dir: str) -> list:
        """
        从已有项目文件推断 tech_stack（用于 Patch Mode 加载 Playbook）。
        规则：按文件扩展名、关键 import 语句和配置文件嗅探。
        """
        stack = set()
        if not os.path.isdir(project_dir):
            return []

        for root, dirs, files in os.walk(project_dir):
            dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__' and d != 'node_modules']
            for fname in files:
                ext = os.path.splitext(fname)[1].lower()
                fpath = os.path.join(root, fname)

                if ext == '.py':
                    stack.add("Python")
                    try:
                        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                            head = f.read(2000)
                        if "from flask" in head or "import flask" in head:
                            stack.add("Flask")
                        if "from django" in head or "import django" in head:
                            stack.add("Django")
                        if "from fastapi" in head or "import fastapi" in head:
                            stack.add("FastAPI")
                        if "import sqlite3" in head:
                            stack.add("SQLite")
                        if "from sqlalchemy" in head or "import sqlalchemy" in head:
                            stack.add("SQLAlchemy")
                    except Exception:
                        pass
                elif ext == '.html':
                    stack.add("HTML")
                elif ext == '.css':
                    stack.add("CSS")
                elif ext in ('.js', '.jsx'):
                    stack.add("JavaScript")
                elif ext in ('.ts', '.tsx'):
                    stack.add("TypeScript")
                elif ext == '.vue':
                    stack.add("Vue3")

                fname_lower = fname.lower()
                if fname_lower == 'package.json':
                    try:
                        with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                            pkg = json.loads(f.read())
                        all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                        if "react" in all_deps:
                            stack.add("React")
                        if "vue" in all_deps:
                            stack.add("Vue3")
                        if "next" in all_deps:
                            stack.add("Next.js")
                    except Exception:
                        pass
                elif fname_lower in ('tailwind.config.js', 'tailwind.config.ts', 'tailwind.config.mjs'):
                    stack.add("Tailwind")
                elif fname_lower == 'next.config.js' or fname_lower == 'next.config.mjs':
                    stack.add("Next.js")

        return sorted(stack)

    # ============================================================
    # 复杂度预估
    # ============================================================

    @staticmethod
    def estimate_file_count(project_spec: dict) -> int:
        """从 project_spec 预估项目文件数。"""
        if not project_spec:
            return 0

        mi_count = len(project_spec.get("module_interfaces", {}) or {})
        api_count = len(project_spec.get("api_contracts", []) or [])
        model_count = len(project_spec.get("data_models", []) or [])
        page_count = _count_unique_page_routes(project_spec)
        template_count = _count_non_layout_templates(project_spec)
        has_frontend, has_backend = _detect_stack_shape(project_spec)

        backend_floor = 0
        if has_backend or api_count or model_count:
            backend_floor = model_count + ((api_count + 1) // 2)
            if api_count >= 8:
                backend_floor = max(backend_floor, 6)
            if api_count >= 12:
                backend_floor = max(backend_floor, 8)

        frontend_floor = 0
        if has_frontend or page_count or template_count:
            frontend_floor = max(page_count, template_count, 2 if has_frontend else 0)

        architecture_floor = 2 if has_frontend and has_backend else 0

        inferred_floor = backend_floor + frontend_floor + architecture_floor
        if api_count or model_count or page_count or template_count:
            inferred_floor = max(inferred_floor, 6)

        return max(mi_count, inferred_floor)

    @staticmethod
    def build_complex_files_hint(project_spec: dict) -> str:
        """生成复杂文件提示文本，注入到 Manager prompt 中。"""
        if not project_spec:
            return ""
        apis = project_spec.get("api_contracts", [])
        models = project_spec.get("data_models", [])

        hints = []
        if len(apis) >= 5:
            hints.append(f"⚠️ 项目有 {len(apis)} 个 API 端点，路由文件可能结构复杂，建议使用 sub_tasks 骨架先行")
        if len(models) >= 3:
            hints.append(f"⚠️ 项目有 {len(models)} 个数据模型，models 文件可能结构复杂，建议使用 sub_tasks 骨架先行")

        if hints:
            return "\n【⚠️ 复杂度预警（Engine 静态分析）】\n" + "\n".join(hints)
        return ""

    # ============================================================
    # P0-2: 已完成任务上下文聚合
    # ============================================================

    def _build_completed_tasks_context(self, current_task: TaskItem) -> str:
        """
        从 Blackboard 的 completed_tasks 账本聚合已完成任务的语义摘要。

        设计原则：
        - 排除当前 task_id（避免自引用）
        - 截断至最近 15 条（防止 context 膨胀，~300 tokens）
        - 单行格式 "✅ file.py — description"（token 效率优先）
        """
        records = self.blackboard.state.completed_tasks
        if not records:
            return ""

        # 过滤当前任务 + 截断
        filtered = [
            r for r in records
            if r.task_id != current_task.task_id
        ]
        if not filtered:
            return ""

        # 最近 15 条（按完成时间倒序取，再正序输出）
        recent = filtered[-15:]

        lines = []
        for r in recent:
            lines.append(f"  ✅ {r.target_file} — {r.description}")

        return (
            "【📋 已完成任务账本（兄弟任务的架构决策已落地，你必须与它们对齐）】\n"
            + "\n".join(lines)
        )

    # ============================================================
    # 一站式上下文组装（核心方法）
    # ============================================================

    def build_task_meta(self, task: TaskItem, existing_code: str,
                        feedback: str = None) -> dict:
        """
        为 Coder 一站式组装 task_meta 上下文。
        整合：Observer 扫描 + Playbook 加载 + AST 显微镜 + 路由清单 + 用户潜规则。
        """
        from tools.observer import Observer
        from core.playbook_loader import PlaybookLoader

        # (1) Observer 预取：项目文件树 + 精准依赖骨架
        observer_tree = ""
        observer_context = ""
        try:
            obs = Observer(self.vfs.truth_dir if self.vfs else ".")
            observer_tree = obs.get_tree()

            # 精准依赖分析
            dep_files = self.resolve_smart_deps(task, existing_code)
            context_parts = []
            if dep_files:
                for dep_path in dep_files:
                    skeleton = obs.get_skeleton(dep_path)
                    if skeleton and "Error" not in skeleton:
                        context_parts.append(f"--- [依赖文件骨架: {dep_path}] ---\n{skeleton}\n")
                    else:
                        content = obs.read_file(dep_path)
                        if content and "Error" not in content:
                            preview = content[:800] + "\n...[省略]" if len(content) > 800 else content
                            context_parts.append(f"--- [依赖文件: {dep_path}] ---\n{preview}\n")
                logger.info(f"📐 精准依赖注入: {dep_files}")

            # 前端文件：动态注入路由上下文
            FRONTEND_EXTS = {'.html', '.htm', '.vue', '.svelte', '.jsx', '.tsx'}
            target_ext = os.path.splitext(task.target_file)[1].lower()
            if target_ext in FRONTEND_EXTS:
                routes_file = self.find_routes_file()
                if routes_file and routes_file not in (dep_files or []):
                    skeleton = obs.get_skeleton(routes_file)
                    if skeleton and "Error" not in skeleton:
                        context_parts.append(f"--- [路由文件骨架: {routes_file}] ---\n{skeleton}\n")
                        logger.info(f"🛤️ 前端路由注入: {routes_file}")

            observer_context = "".join(context_parts)
        except Exception as e:
            logger.warning(f"⚠️ Observer 预取异常: {e}")

        # (2) Playbook 加载
        _pb_loader = PlaybookLoader()
        _tech_stack = (self.blackboard.state.project_spec or {}).get("tech_stack", [])
        _arch_contract = (self.blackboard.state.project_spec or {}).get("architecture_contract")
        playbook_content = _pb_loader.load_for_coder(
            _tech_stack, task.target_file,
            architecture_contract=_arch_contract,
        )

        # (3) 用户项目潜规则嗅探
        user_rules_block = ""
        if self.vfs:
            for rule_name in (".astrea.md", ".cursorrules", "CLAUDE.md"):
                rule_path = os.path.join(self.vfs.truth_dir, rule_name)
                if os.path.isfile(rule_path):
                    try:
                        with open(rule_path, "r", encoding="utf-8") as f:
                            rules_content = f.read().strip()
                        if rules_content:
                            user_rules_block = (
                                "\n═══════════════════════════════════════════\n"
                                "【P0.5 — 用户的项目专属潜规则（User Project Rules）】\n"
                                "═══════════════════════════════════════════\n\n"
                                "[重要指令]: 以下是主人为本项目订制的特例规则，"
                                "优先级凌驾于所有 Playbook 最佳实践之上！\n"
                                "如果在技术实现时遇到冲突，请完全服从本规则。\n\n"
                                f"{rules_content}\n\n"
                            )
                            logger.info(f"📜 P0.5 潜规则加载: {rule_name} ({len(rules_content)} chars)")
                            break
                    except Exception as e:
                        logger.warning(f"⚠️ P0.5 潜规则读取失败: {e}")

        # (4) 构建 task_meta 基础结构
        tasks_dict = [
            {"task_id": t.task_id, "target_file": t.target_file, "description": t.description}
            for t in self.blackboard.state.tasks
        ]
        # (4.1) 聚合已完成任务上下文 — P0-2 核心锚点
        completed_context = self._build_completed_tasks_context(task)

        task_meta = {
            "project_spec": self.blackboard.state.spec_text,
            "dependencies": task.dependencies,
            "all_tasks": tasks_dict,
            "observer_tree": observer_tree,
            "observer_context": observer_context,
            "existing_code": existing_code,
            "task_type": task.task_type,
            "draft_action": task.draft_action,
            "write_targets": task.write_targets,
            "force_modify": task.task_type == "weld" or task.draft_action == "modify",
            "playbook": playbook_content,
            "global_snapshot": self.blackboard.get_global_snapshot_text(),
            "retry_count": task.retry_count,
            "user_rules_block": user_rules_block,
            "completed_context": completed_context,
        }
        is_fill_task = bool(task.sub_tasks and task.current_sub_task_index >= 1)
        feedback_text = feedback or ""
        fill_file_level_feedback = is_fill_task and (
            "[L0.0" in feedback_text
            or "骨架残留" in feedback_text
            or "[L0.C1" in feedback_text
            or "路由未注册" in feedback_text
        )
        task_meta["fill_file_level_feedback"] = fill_file_level_feedback

        # (5) AST 显微镜 — 大文件修改时注入精准切片
        architecture_hint = build_architecture_contract_hint(
            self.blackboard.state.project_spec or {},
            task.target_file,
        )
        if architecture_hint:
            task_meta["observer_context"] += "\n\n" + architecture_hint

        route_module_hint = build_route_module_contract_hint(
            self.blackboard.state.project_spec or {},
            task.target_file,
        )
        if route_module_hint:
            task_meta["observer_context"] += "\n\n" + route_module_hint

        if existing_code and len(existing_code.splitlines()) > 30:
            # v4.3: Continue 模式跳过 AST 切片 — 修复描述混合多端点，关键词匹配会选错函数
            # Coder 看到全文件 + LLM 诊断指令效果更好
            is_continue_fix = task.task_id.startswith("continue_")
            target_ext = os.path.splitext(task.target_file)[1].lower()
            force_modify = bool(task_meta.get("force_modify"))
            sfc_structure_exts = {'.vue', '.html', '.htm', '.css', '.scss', '.less'}
            skip_frontend_structure_slice = force_modify and target_ext in sfc_structure_exts
            if is_continue_fix:
                logger.info(f"⏭️ [Continue] 跳过 AST 切片，Coder 将看到全文件 + LLM 诊断指令")
            elif fill_file_level_feedback:
                logger.info(f"⏭️ [Fill] {task.target_file} 命中文件级填充反馈，跳过 AST 切片")
            elif skip_frontend_structure_slice:
                logger.info(
                    f"⏭️ [Patch] {task.target_file} 是前端结构类局部修复，跳过 AST 切片，强制 Editor 行号编辑"
                )
            elif task.tech_lead_invoked:
                # TechLead 定向修复跳过 AST 切片
                # 原因：TechLead 修复指令通常涉及多个函数（如 viewSnippet + editSnippet），
                # AST 切片只能选一个且关键词匹配易选错。
                # Coder 需要看到全文件 + TechLead 精确指令，用 Editor 差量编辑多个函数。
                logger.info(
                    f"⏭️ [TechLead] {task.target_file} 是 TechLead 定向修复任务，跳过 AST 切片，强制 Editor 全文件编辑"
                )
            else:
                # v4.4: Scope Expansion 检测 — 任务需要新增函数时跳过 AST 切片
                # 根因：AST 切片假设"修改已有函数"，将 Coder 锁入单函数视窗。
                # 当任务描述要求新增 get_expense_by_id / update_expense 等不存在的函数时，
                # Coder 被困在 get_all_expenses 切片里无法创建新定义 → Reviewer 驳回 → 熔断。
                # 解法：在切片前用符号表对比检测，命中则给 Coder 全文件上下文 + Editor 差量编辑。
                _skip_for_scope_expansion = False
                try:
                    from tools.ast_microscope import ASTMicroscope, detect_lang
                    lang = detect_lang(task.target_file)
                    if lang != "unknown":
                        scope = ASTMicroscope()
                        slice_query = task.description
                        if task.tech_lead_feedback:
                            slice_query += "\n" + task.tech_lead_feedback
                        if scope.requires_scope_expansion(existing_code, slice_query, lang):
                            _skip_for_scope_expansion = True
                            logger.info(
                                f"⏭️ [Scope Expansion] {task.target_file} 任务需要新增符号，"
                                f"跳过 AST 切片，强制 Editor 全文件编辑"
                            )
                except Exception as e:
                    logger.warning(f"⚠️ Scope Expansion 检测异常: {e}")

                if not _skip_for_scope_expansion:
                    try:
                        from tools.ast_microscope import ASTMicroscope, detect_lang
                        lang = detect_lang(task.target_file)
                        if lang != "unknown":
                            scope = ASTMicroscope()
                            # 合并 description + tech_lead_feedback 用于 AST 切片匹配
                            # Mini QA 修复任务的 description 是短描述，
                            # TechLead 指定的精确函数名在 tech_lead_feedback 中
                            slice_query = task.description
                            if task.tech_lead_feedback:
                                slice_query += "\n" + task.tech_lead_feedback
                            ast_slice = scope.find_relevant_slice(
                                existing_code, slice_query, lang, context_lines=10
                            )
                            if ast_slice:
                                task_meta["ast_slice"] = ast_slice
                                task_meta["ast_full_code"] = existing_code
                                logger.info(
                                    f"🔬 AST 显微镜切片: {ast_slice['name']} "
                                    f"L{ast_slice['start_line']}-{ast_slice['end_line']} "
                                    f"({len(ast_slice['code'])} chars)"
                                )
                    except Exception as e:
                        logger.warning(f"⚠️ AST 显微镜切片失败: {e}")

        # (6) 前端路由清单注入
        FRONTEND_EXTS = {'.html', '.htm', '.vue', '.svelte', '.jsx', '.tsx'}
        target_ext = os.path.splitext(task.target_file)[1].lower()
        if target_ext in FRONTEND_EXTS:
            try:
                route_manifest = self.build_route_manifest()
                if route_manifest:
                    task_meta["observer_context"] += (
                        "\n\n--- [⚠️ 可用路由清单（禁止使用清单外的 URL）] ---\n"
                        + route_manifest + "\n"
                    )
                    logger.info(f"🛤️ 路由清单注入: {len(route_manifest.splitlines())} 条路由")
            except Exception as e:
                logger.warning(f"⚠️ 路由清单构建异常: {e}")
            
            _gs = task_meta.get("global_snapshot", "")
            if _gs and "to_dict" in _gs:
                task_meta["observer_context"] += (
                    "\n\n⚠️ 【Jinja/模板变量铁律】模板中引用的所有数据对象字段，"
                    "必须且只能来自上述 global_snapshot 中的 `to_dict() 可用字段` 列表。"
                    "如果在其中找不到想要使用的字段，说明后端并未返回该字段，"
                    "请坚决放弃使用它，而不是自己去硬造！\n"
                )

        # (7) Fill 模式骨架注入
        if task.sub_tasks and task.current_sub_task_index >= 1:
            if task.retry_count >= 2 and not fill_file_level_feedback:
                logger.info(f"🔓 [{task.task_id}] 重试 {task.retry_count} 次，解除骨架约束（退出 Fill 模式）")
                self.blackboard.unlock_fill_mode(task.task_id)
            else:
                skeleton_code = ""
                if self.vfs:
                    skeleton_code = self.vfs.read_truth(task.target_file) or ""
                if skeleton_code:
                    task_meta["skeleton_code"] = skeleton_code
                    task_meta["is_fill_mode"] = True
                    if fill_file_level_feedback:
                        task_meta["force_full_fill"] = True
                    logger.info(f"🔧 [{task.task_id}] Fill 模式: 注入骨架 {len(skeleton_code)} chars")

        return task_meta


def _count_unique_page_routes(project_spec: dict) -> int:
    routes = project_spec.get("page_routes", []) or []
    unique_paths = {
        route.get("path", "").strip().lower().rstrip("/")
        for route in routes
        if route.get("path")
    }
    return len(unique_paths)


def _count_non_layout_templates(project_spec: dict) -> int:
    contracts = project_spec.get("template_contracts", {}) or {}
    count = 0
    for contract in contracts.values():
        if not isinstance(contract, dict):
            continue
        if contract.get("type") == "layout":
            continue
        count += 1
    return count


def _detect_stack_shape(project_spec: dict) -> tuple[bool, bool]:
    tech_stack = [str(item).lower() for item in (project_spec.get("tech_stack", []) or [])]
    interface_keys = [str(key).lower() for key in (project_spec.get("module_interfaces", {}) or {}).keys()]

    frontend_tokens = ("react", "vue", "next", "vite", "svelte", "angular")
    backend_tokens = ("flask", "fastapi", "django", "sqlalchemy", "sqlite", "postgres", "mysql", "api")

    has_frontend = (
        any(token in tech for tech in tech_stack for token in frontend_tokens)
        or any(
            key.startswith("src/")
            or key.endswith((".jsx", ".tsx", ".vue", ".svelte"))
            for key in interface_keys
        )
    )
    has_backend = (
        any(token in tech for tech in tech_stack for token in backend_tokens)
        or len(project_spec.get("api_contracts", []) or []) > 0
        or len(project_spec.get("data_models", []) or []) > 0
    )
    return has_frontend, has_backend
