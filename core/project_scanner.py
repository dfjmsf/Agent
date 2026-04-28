"""
ProjectScanner — 老项目上下文统一采集入口

职责：
- 扫描已有项目的技术栈、路由、模型、入口文件与导入信息
- 优先消费 Blackboard 快照，快照缺失时再退化到 Observer 全量扫描
- 供 PM / Manager / Engine 统一复用，避免多处重复扫描
"""
import ast
import os
import re
from typing import Any, Dict, List, Optional

from core.blackboard import BlackboardState
from core.project_observer import ProjectObserver


_IGNORE_DIRS = {
    ".git", ".sandbox", ".astrea", "__pycache__", "node_modules", ".venv", "venv", ".idea",
}
_ENTRYPOINT_CANDIDATES = ("app.py", "main.py", "server.py", "manage.py", "run.py")


def _normalize_snapshot(blackboard_state: Any) -> Dict[str, Any]:
    if blackboard_state is None:
        return {}
    if isinstance(blackboard_state, dict):
        return dict(blackboard_state)
    if hasattr(blackboard_state, "model_dump"):
        return blackboard_state.model_dump()
    return {}


def _load_snapshot_from_disk(project_dir: str) -> Dict[str, Any]:
    state = BlackboardState.load_from_disk(project_dir)
    return state.model_dump() if state else {}


def _list_project_files(project_dir: str) -> List[str]:
    files: List[str] = []
    for root, dirs, filenames in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS and not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            rel = os.path.relpath(os.path.join(root, name), project_dir).replace("\\", "/")
            files.append(rel)
    return sorted(files)


def _choose_entrypoint(file_tree: List[str]) -> str:
    lower_map = {path.lower(): path for path in file_tree}
    for candidate in _ENTRYPOINT_CANDIDATES:
        if candidate in lower_map:
            return lower_map[candidate]
    for path in file_tree:
        if path.endswith(".py"):
            return path
    return ""


def _format_import_alias(name: str, alias: Optional[str]) -> str:
    return f"{name} as {alias}" if alias and alias != name else name


def _parse_top_level_imports(abs_path: str) -> List[str]:
    if not abs_path or not os.path.isfile(abs_path):
        return []

    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
    except Exception:
        return []

    imports: List[str] = []
    try:
        tree = ast.parse(content)
        for node in tree.body:
            if isinstance(node, ast.Import):
                names = ", ".join(_format_import_alias(alias.name, alias.asname) for alias in node.names)
                imports.append(f"import {names}")
            elif isinstance(node, ast.ImportFrom):
                module = "." * int(getattr(node, "level", 0) or 0) + (node.module or "")
                names = ", ".join(_format_import_alias(alias.name, alias.asname) for alias in node.names)
                imports.append(f"from {module} import {names}")
        return imports
    except SyntaxError:
        for line in content.splitlines():
            stripped = line.strip()
            if re.match(r"^(from\s+\S+\s+import\s+.+|import\s+.+)$", stripped):
                imports.append(stripped)
        return imports


def _flatten_snapshot_items(snapshot_map: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    if not isinstance(snapshot_map, dict):
        return items

    for file_path, entries in snapshot_map.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            item = dict(entry)
            item["file"] = item.get("file") or file_path
            items.append(item)
    return items


def _scan_routes_and_models_from_observer(project_dir: str, file_tree: List[str]) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    from tools.observer import Observer

    observer = Observer(project_dir)
    routes: List[Dict[str, Any]] = []
    models: List[Dict[str, Any]] = []

    for path in file_tree:
        if not path.endswith((".py", ".js", ".jsx", ".ts", ".tsx", ".vue")):
            continue

        try:
            for route in observer.extract_routes(path) or []:
                item = dict(route)
                item["file"] = item.get("file") or path
                routes.append(item)
        except Exception:
            pass

        if not path.endswith(".py"):
            continue

        try:
            for model in observer.extract_schema(path) or []:
                item = dict(model)
                item["file"] = item.get("file") or path
                models.append(item)
        except Exception:
            pass

    return routes, models


def _read_project_text(project_dir: str, file_tree: List[str], limit: int = 4000) -> str:
    parts: List[str] = []
    for path in file_tree:
        if not path.endswith(".py"):
            continue
        abs_path = os.path.join(project_dir, path)
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                parts.append(f.read(limit))
        except Exception:
            continue
    return "\n".join(parts)


def _infer_package_layout(file_tree: List[str]) -> str:
    if any(path.startswith("src/") for path in file_tree):
        return "package_src"
    package_dirs = {
        os.path.dirname(path)
        for path in file_tree
        if path.endswith("/__init__.py") or path == "__init__.py"
    }
    return "package_src" if package_dirs else "flat_modules"


def _infer_import_style(entrypoint_imports: List[str], package_layout: str) -> str:
    if any("from src." in line or line.startswith("from src import") for line in entrypoint_imports):
        return "package_import"
    if any(re.search(r"^from\s+[A-Za-z0-9_]+\.[A-Za-z0-9_\.]+\s+import", line) for line in entrypoint_imports):
        return "package_import" if package_layout == "package_src" else "sibling_import"
    return "sibling_import"


def _infer_architecture_contract(
    project_dir: str,
    file_tree: List[str],
    tech_stack: List[str],
    entrypoint_file: str,
    entrypoint_imports: List[str],
    existing_routes: List[Dict[str, Any]],
) -> Dict[str, str]:
    text_blob = _read_project_text(project_dir, file_tree)
    lowered = text_blob.lower()
    tech_set = {item.lower() for item in tech_stack}
    package_layout = _infer_package_layout(file_tree)

    if "fastapi" in tech_set or "from fastapi" in lowered:
        backend_framework = "fastapi"
    elif "flask" in tech_set or "from flask" in lowered or "import flask" in lowered:
        backend_framework = "flask"
    elif "django" in tech_set or "from django" in lowered:
        backend_framework = "django"
    else:
        backend_framework = "python"

    if "flask_sqlalchemy" in lowered or "db.model" in lowered or "sqlalchemy(" in lowered:
        orm_mode = "flask_sqlalchemy"
    elif "sessionmaker" in lowered or "declarative_base" in lowered or "create_engine" in lowered:
        orm_mode = "sqlalchemy_session"
    elif "sqlite3" in lowered:
        orm_mode = "sqlite_native"
    else:
        orm_mode = "unknown"

    if "apirouter" in lowered:
        router_mode = "fastapi_apirouter"
    elif "blueprint" in lowered or any("blueprint" in str(route.get("function", "")).lower() for route in existing_routes):
        router_mode = "flask_blueprint"
    elif any(path.endswith("urls.py") for path in file_tree):
        router_mode = "django_urls"
    else:
        router_mode = "unknown"

    entrypoint_abs = os.path.join(project_dir, entrypoint_file) if entrypoint_file else ""
    entrypoint_text = ""
    if entrypoint_abs and os.path.isfile(entrypoint_abs):
        try:
            with open(entrypoint_abs, "r", encoding="utf-8", errors="replace") as f:
                entrypoint_text = f.read(4000).lower()
        except Exception:
            entrypoint_text = ""

    if backend_framework == "flask" and "create_app(" in entrypoint_text:
        entrypoint_mode = "flask_app_factory"
    elif backend_framework == "fastapi" and ("uvicorn.run" in entrypoint_text or "fastapi(" in entrypoint_text):
        entrypoint_mode = "uvicorn_app"
    else:
        entrypoint_mode = "unknown"

    auth_mode = "none"
    if "flask_login" in lowered or "loginmanager" in lowered:
        auth_mode = "flask_login_session"
    elif "jwt" in lowered or "oauth2passwordbearer" in lowered or "bearer" in lowered:
        auth_mode = "jwt_header"

    return {
        "backend_framework": backend_framework,
        "orm_mode": orm_mode,
        "auth_mode": auth_mode,
        "router_mode": router_mode,
        "entrypoint_mode": entrypoint_mode,
        "entrypoint_file": entrypoint_file,
        "package_layout": package_layout,
        "import_style": _infer_import_style(entrypoint_imports, package_layout),
    }


def scan_existing_project(project_dir: str, blackboard_state: dict | None = None) -> dict:
    """统一采集老项目上下文，返回 existing_project_context。"""
    project_dir = os.path.abspath(project_dir)
    snapshot = _normalize_snapshot(blackboard_state) or _load_snapshot_from_disk(project_dir)
    file_tree = _list_project_files(project_dir)
    entrypoint_file = _choose_entrypoint(file_tree)
    entrypoint_imports = _parse_top_level_imports(
        os.path.join(project_dir, entrypoint_file) if entrypoint_file else ""
    )

    existing_routes = _flatten_snapshot_items(snapshot.get("global_routes"))
    existing_models = _flatten_snapshot_items(snapshot.get("global_schema"))
    if not existing_routes and not existing_models:
        existing_routes, existing_models = _scan_routes_and_models_from_observer(project_dir, file_tree)

    tech_stack = ProjectObserver.infer_tech_stack(project_dir)
    architecture_contract = _infer_architecture_contract(
        project_dir=project_dir,
        file_tree=file_tree,
        tech_stack=tech_stack,
        entrypoint_file=entrypoint_file,
        entrypoint_imports=entrypoint_imports,
        existing_routes=existing_routes,
    )

    existing_routes = sorted(
        existing_routes,
        key=lambda item: (
            str(item.get("path", "")),
            str(item.get("method", "")),
            str(item.get("file", "")),
        ),
    )
    existing_models = sorted(
        existing_models,
        key=lambda item: (
            str(item.get("name", "")),
            str(item.get("file", "")),
        ),
    )

    return {
        "tech_stack": tech_stack,
        "architecture_contract": architecture_contract,
        "existing_routes": existing_routes,
        "existing_models": existing_models,
        "file_tree": file_tree,
        "entrypoint_imports": entrypoint_imports,
    }
