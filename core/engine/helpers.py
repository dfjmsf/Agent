"""
engine/helpers.py — AstreaEngine 通用辅助工具

从 engine.py 迁出的纯工具方法，通过 engine 实例代理访问状态。
"""
import os
import re
import json
import time
import threading
import logging
from typing import Optional

from core.blackboard import ProjectStatus
from core.database import (
    rename_project_events, rename_project_meta, update_project_status,
)
from core.ws_broadcaster import global_broadcaster

logger = logging.getLogger("AstreaEngine")

# 路径基准：Agent/ 根目录（core/engine/ 上溯两级）
_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_PROJECTS_DIR = os.path.join(_ROOT_DIR, "projects")


# ============================================================
# 目录解析
# ============================================================

def resolve_artifact_dir(engine, fallback_dir: str = None) -> str:
    """解析用于保存本地黑板快照的目录。"""
    candidate = engine.blackboard.state.out_dir or fallback_dir
    if candidate:
        return os.path.abspath(candidate)
    project_id = engine.blackboard.state.project_id or engine.project_id
    return os.path.join(_PROJECTS_DIR, project_id)


def resolve_output_dir(engine, out_dir: str = None) -> str:
    """计算项目输出目录 + 动态重命名"""
    project_name = engine.blackboard.state.project_name or "Unnamed"

    # 动态重命名逻辑
    if "新建项目" in engine.project_id or "new_project" in engine.project_id or "default_project" == engine.project_id:
        parts = engine.project_id.split("_", 2)
        timestamp = f"{parts[0]}_{parts[1]}" if len(parts) >= 2 else time.strftime("%Y%m%d_%H%M%S")
        # 目录名仅允许 ASCII（中文项目名仅存入 project_meta 表）
        safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', project_name)
        safe_name = re.sub(r'_+', '_', safe_name).strip('_') or "Unnamed"
        new_id = f"{timestamp}_{safe_name}"

        old_dir = os.path.join(_PROJECTS_DIR, engine.project_id)
        new_dir = os.path.join(_PROJECTS_DIR, new_id)

        if os.path.exists(old_dir) and old_dir != new_dir:
            try:
                os.rename(old_dir, new_dir)
                old_id = engine.project_id
                # 更新 project_id
                engine.blackboard.state.project_id = new_id
                rename_project_events(old_id, new_id)
                rename_project_meta(old_id, new_id, safe_name)
                # 清理旧 Checkpoint（否则旧 "新建项目" key 永不删除）
                from core.database import delete_checkpoint
                delete_checkpoint(old_id)
                global_broadcaster.emit_sync("System", "project_renamed",
                    f"项目已重命名: {safe_name}",
                    {"old_id": old_id, "new_id": new_id})
            except Exception as e:
                logger.warning(f"⚠️ 立即重命名失败，登记延迟重试: {e}")
                engine._pending_project_rename = (engine.project_id, new_id, safe_name)
                rename_project_meta(engine.project_id, engine.project_id, safe_name)

    if out_dir:
        return os.path.abspath(out_dir)

    return os.path.join(_PROJECTS_DIR, engine.blackboard.state.project_id)


# ============================================================
# 持久化
# ============================================================

def persist_blackboard_artifacts(engine, project_dir: str, failed: bool = False):
    """将当前黑板状态落盘；失败时额外保留一份失败态快照。"""
    if not project_dir:
        return
    try:
        engine.blackboard.state.save_to_disk(project_dir)
        if failed:
            engine.blackboard.state.save_to_disk(project_dir, "blackboard_state.failed.json")
    except Exception as e:
        logger.warning(f"⚠️ Blackboard 持久化失败: {e}")


def record_planning_failure(engine, reason: str, error_message: str,
                            out_dir: str = None,
                            extra_context: Optional[dict] = None):
    """规划阶段失败统一收口，禁止空 spec 继续下沉到执行链。"""
    artifact_dir = resolve_artifact_dir(engine, out_dir)
    engine.blackboard.set_project_status(ProjectStatus.FAILED)
    engine.blackboard.record_failure_context(reason, error_message)
    if extra_context:
        engine.blackboard.state.failure_context.update(extra_context)
    engine.blackboard._touch()
    persist_blackboard_artifacts(engine, artifact_dir, failed=True)
    update_project_status(engine.project_id, "planning_blocked")
    global_broadcaster.emit_sync(
        "System", "error",
        f"规划阶段已阻断: {error_message}",
    )


# ============================================================
# 项目重命名
# ============================================================

def finalize_project_rename(engine):
    """在主流程尾部再次尝试项目目录重命名，降低 Windows 目录锁导致的失败率。"""
    if not engine._pending_project_rename:
        return

    old_id, new_id, safe_name = engine._pending_project_rename
    old_dir = os.path.join(_PROJECTS_DIR, old_id)
    new_dir = os.path.join(_PROJECTS_DIR, new_id)

    if not os.path.exists(old_dir) or old_dir == new_dir:
        engine._pending_project_rename = None
        return

    deadline = time.time() + 3.0
    last_error = None
    while time.time() < deadline:
        try:
            os.rename(old_dir, new_dir)
            engine.blackboard.state.project_id = new_id
            engine.blackboard.state.out_dir = new_dir
            rename_project_events(old_id, new_id)
            rename_project_meta(old_id, new_id, safe_name)
            global_broadcaster.emit_sync(
                "System", "project_renamed",
                f"项目已重命名: {safe_name}",
                {"old_id": old_id, "new_id": new_id},
            )
            engine._pending_project_rename = None
            return
        except Exception as e:
            last_error = e
            time.sleep(0.2)

    logger.warning(f"⚠️ 延迟重命名仍失败，保留原目录 ID: {last_error}")
    rename_project_meta(old_id, old_id, safe_name)
    engine._pending_project_rename = None


# ============================================================
# Sandbox 预热
# ============================================================

def warmup_sandbox(engine, project_spec: dict = None):
    """预热 Sandbox（安装依赖）"""
    spec = project_spec or engine.blackboard.state.project_spec
    tech_stacks = spec.get("tech_stack", []) if spec else []
    if tech_stacks:
        from tools.sandbox import sandbox_env
        pid = engine.blackboard.state.project_id
        def _bg():
            sandbox_env.warm_up(pid, tech_stacks)
        threading.Thread(target=_bg, daemon=True).start()


# ============================================================
# Tech Stack 推断
# ============================================================

def infer_tech_stack(engine_or_none, project_dir: str) -> list:
    """
    从已有项目文件推断 tech_stack（用于 Patch Mode 加载 Playbook）。
    规则：按文件扩展名、关键 import 语句和配置文件嗅探。
    注意：此方法不依赖 engine 实例，第一参数保留是为了统一签名。
    """
    stack = set()
    if not os.path.isdir(project_dir):
        return []

    for root, dirs, files in os.walk(project_dir):
        # 跳过隐藏目录和缓存
        dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__' and d != 'node_modules']
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            fpath = os.path.join(root, fname)

            if ext == '.py':
                stack.add("Python")
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        head = f.read(2000)
                    # 后端框架嗅探
                    if "from flask" in head or "import flask" in head:
                        stack.add("Flask")
                    if "from django" in head or "import django" in head:
                        stack.add("Django")
                    if "from fastapi" in head or "import fastapi" in head:
                        stack.add("FastAPI")
                    # 数据库嗅探
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

            # 配置文件嗅探
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
# Patch 受影响端点推断
# ============================================================

def infer_focus_endpoints(plan: dict, project_dir: str) -> list | None:
    """
    v4.4: 从 Patch 修改的文件反向推断受影响的 HTTP 端点。

    策略:
    - 模板文件 (.html): grep 所有 .py 找 render_template('name') → 提取 @route
    - Python 文件 (.py): 直接提取 @app.route / @bp.route 装饰器
    - 静态资源 (.css/.js): 降级为首页冒烟 (GET /)
    - 无法推断时返回 None → 走全量测试
    """
    import re as _re

    target_files = [
        t.get("target_file", "") for t in plan.get("tasks", [])
        if t.get("target_file")
    ]
    if not target_files:
        return None

    # 收集所有 .py 文件内容（用于 grep）
    py_files = {}
    if os.path.isdir(project_dir):
        ignore = {'.sandbox', '.git', '__pycache__', '.venv', 'node_modules', '.astrea'}
        for root, dirs, files in os.walk(project_dir):
            dirs[:] = [d for d in dirs if d not in ignore]
            for f in files:
                if f.endswith('.py'):
                    fpath = os.path.join(root, f)
                    rel = os.path.relpath(fpath, project_dir).replace('\\', '/')
                    try:
                        with open(fpath, 'r', encoding='utf-8') as fh:
                            py_files[rel] = fh.read()
                    except Exception:
                        pass

    focus = set()
    # 匹配 @app.route / @bp.route / @blueprint.route 等
    route_pattern = _re.compile(
        r"@\w+\.route\(\s*['\"]([^'\"]+)['\"]"
        r"(?:.*?methods\s*=\s*\[([^\]]+)\])?"
    , _re.DOTALL)

    # 动态检测端口
    port = 5001
    for content in py_files.values():
        m = _re.search(r'port\s*=\s*(\d{4,5})', content, _re.IGNORECASE)
        if m:
            port = int(m.group(1))
            break

    for target in target_files:
        basename = os.path.basename(target)
        ext = os.path.splitext(target)[1].lower()

        if ext in ('.html', '.htm'):
            # 模板文件：grep render_template('basename') 找路由
            for py_rel, py_content in py_files.items():
                if f"'{basename}'" in py_content or f'"{basename}"' in py_content:
                    lines = py_content.split('\n')
                    last_route = None
                    last_methods = ['GET']
                    for line in lines:
                        rm = route_pattern.search(line)
                        if rm:
                            last_route = rm.group(1)
                            methods_str = rm.group(2)
                            if methods_str:
                                last_methods = [
                                    m.strip().strip("'\"")
                                    for m in methods_str.split(',')
                                ]
                            else:
                                last_methods = ['GET']
                        if (f"render_template('{basename}'" in line or
                                f'render_template("{basename}"' in line):
                            if last_route:
                                route_url = _re.sub(r'<\w+:\w+>', '1', last_route)
                                route_url = _re.sub(r'<\w+>', '1', route_url)
                                for method in last_methods:
                                    focus.add(f"{method.upper()} http://127.0.0.1:{port}{route_url}")

        elif ext == '.py':
            content = py_files.get(target, '')
            if content:
                for rm in route_pattern.finditer(content):
                    route_path = rm.group(1)
                    methods_str = rm.group(2)
                    if methods_str:
                        methods = [m.strip().strip("'\"") for m in methods_str.split(',')]
                    else:
                        methods = ['GET']
                    route_url = _re.sub(r'<\w+:\w+>', '1', route_path)
                    route_url = _re.sub(r'<\w+>', '1', route_url)
                    for method in methods:
                        focus.add(f"{method.upper()} http://127.0.0.1:{port}{route_url}")

        elif ext in ('.css', '.js', '.scss', '.less'):
            focus.add(f"GET http://127.0.0.1:{port}/")

    if focus:
        result = sorted(focus)
        logger.info(f"🎯 [Patch Mode] 推断受影响端点: {result}")
        return result

    logger.info("🎯 [Patch Mode] 无法推断受影响端点，走全量测试")
    return None
