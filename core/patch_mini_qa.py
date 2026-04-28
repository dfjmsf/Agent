import json
import logging
import os
import re
import socket
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("PatchMiniQA")


FRONTEND_SOURCE_EXTS = (".html", ".htm", ".js", ".jsx", ".ts", ".tsx", ".vue", ".css")


def build_patch_mini_qa_plan(
    user_requirement: str,
    tech_lead_diagnosis: Optional[dict],
    project_dir: str,
) -> List[Dict[str, str]]:
    """生成 Patch 后最小 QA 计划。优先使用 TechLead 显式 qa_plan，缺省时只做低风险启发式。"""
    explicit = _normalize_qa_plan((tech_lead_diagnosis or {}).get("qa_plan"))
    if explicit:
        return explicit

    text = f"{user_requirement or ''}\n{(tech_lead_diagnosis or {}).get('root_cause', '')}\n{(tech_lead_diagnosis or {}).get('fix_instruction', '')}"
    lowered = text.lower()
    create_intent = any(token in lowered for token in (
        "新建", "新增", "创建", "create", "add",
    ))
    click_intent = any(token in lowered for token in (
        "按钮", "入口", "点击", "无反应", "死按钮", "click", "button",
    ))
    if not (create_intent or click_intent):
        return []

    selectors = _collect_project_selectors(project_dir)
    if "#create-btn" in selectors and "#editor-panel" in selectors:
        return [{
            "action": "click",
            "selector": "#create-btn",
            "assert": "visible",
            "target": "#editor-panel",
        }]
    if "#empty-create-btn" in selectors and "#editor-panel" in selectors:
        return [{
            "action": "click",
            "selector": "#empty-create-btn",
            "assert": "visible",
            "target": "#editor-panel",
        }]
    return []


def choose_patch_mini_qa_repair_target(
    project_dir: str,
    qa_plan: List[Dict[str, str]],
    changed_files: Optional[List[str]] = None,
) -> str:
    """根据失败断言选择下一轮最小修复目标。优先修 JS，其次 CSS，最后 HTML。"""
    candidates = _list_source_files(project_dir)
    changed = {str(item).replace("\\", "/") for item in (changed_files or [])}
    selector_tokens = []
    for item in qa_plan or []:
        for key in ("selector", "target"):
            token = _selector_token(item.get(key, ""))
            if token:
                selector_tokens.append(token)

    def score(path: str) -> int:
        ext = os.path.splitext(path)[1].lower()
        value = 0
        if path in changed:
            value += 20
        if ext in (".js", ".jsx", ".ts", ".tsx", ".vue"):
            value += 90
        elif ext == ".css":
            value += 70
        elif ext in (".html", ".htm"):
            value += 30
        try:
            content = _read_text(os.path.join(project_dir, path))
        except Exception:
            content = ""
        for token in selector_tokens:
            if token and token in content:
                value += 15
        return value

    if not candidates:
        return ""
    ranked = sorted(candidates, key=lambda p: (score(p), -len(p)), reverse=True)
    return ranked[0] if ranked and score(ranked[0]) > 0 else ""


def run_patch_mini_qa(
    project_dir: str,
    qa_plan: List[Dict[str, str]],
    project_id: str = "",
    timeout_seconds: int = 30,
) -> Dict[str, Any]:
    """执行浏览器级最小 QA。当前只支持 click -> visible。"""
    qa_plan = _normalize_qa_plan(qa_plan)
    if not qa_plan:
        return {"passed": True, "skipped": True, "feedback": "无 Patch Mini QA 计划"}

    server = _start_local_server(project_dir, project_id, timeout_seconds=timeout_seconds)
    if not server.get("ok"):
        return {
            "passed": False,
            "env_failed": True,
            "feedback": server.get("feedback", "Patch Mini QA 环境启动失败"),
            "qa_plan": qa_plan,
        }

    proc = server.get("proc")
    try:
        return _run_browser_assertions(server["url"], qa_plan)
    finally:
        _terminate_process_tree(proc)


def _normalize_qa_plan(raw: Any) -> List[Dict[str, str]]:
    if not raw:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []

    normalized = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action", "")).strip().lower()
        selector = str(item.get("selector", "")).strip()
        assertion = str(item.get("assert", item.get("assertion", ""))).strip().lower()
        target = str(item.get("target", "")).strip()
        if action != "click" or assertion != "visible" or not selector or not target:
            continue
        normalized.append({
            "action": action,
            "selector": selector,
            "assert": assertion,
            "target": target,
        })
    return normalized


def _run_browser_assertions(url: str, qa_plan: List[Dict[str, str]]) -> Dict[str, Any]:
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return {
            "passed": False,
            "env_failed": True,
            "feedback": f"Patch Mini QA 无法导入 Playwright: {exc}",
            "qa_plan": qa_plan,
        }

    console_errors: List[str] = []
    page_errors: List[str] = []
    failures: List[str] = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
            page.on("pageerror", lambda exc: page_errors.append(str(exc)))
            page.goto(url, wait_until="domcontentloaded", timeout=10000)

            for item in qa_plan:
                selector = item["selector"]
                target = item["target"]
                try:
                    page.wait_for_selector(selector, state="visible", timeout=3000)
                    page.click(selector, timeout=3000)
                    page.wait_for_selector(target, state="visible", timeout=2000)
                except PlaywrightTimeoutError:
                    detail = _describe_dom_visibility(page, selector, target)
                    failures.append(
                        f"点击 {selector} 后 {target} 未变为可见。{detail}"
                    )
                except PlaywrightError as exc:
                    failures.append(f"执行 {selector} -> {target} 时浏览器操作失败: {exc}")

            browser.close()
    except Exception as exc:
        return {
            "passed": False,
            "env_failed": True,
            "feedback": f"Patch Mini QA 浏览器环境失败: {exc}",
            "qa_plan": qa_plan,
        }

    if failures:
        evidence = []
        if console_errors:
            evidence.append("console_errors=" + " | ".join(console_errors[:3]))
        if page_errors:
            evidence.append("page_errors=" + " | ".join(page_errors[:3]))
        suffix = ("\n" + "\n".join(evidence)) if evidence else ""
        return {
            "passed": False,
            "feedback": "[PATCH_MINI_QA_FAILED] " + "；".join(failures) + suffix,
            "qa_plan": qa_plan,
        }
    return {
        "passed": True,
        "feedback": "Patch Mini QA 通过",
        "qa_plan": qa_plan,
    }


def _describe_dom_visibility(page, selector: str, target: str) -> str:
    script = """
    ([selector, target]) => {
      function info(sel) {
        const el = document.querySelector(sel);
        if (!el) return `${sel}: not_found`;
        const cs = window.getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return `${sel}: display=${cs.display}, visibility=${cs.visibility}, opacity=${cs.opacity}, width=${rect.width}, height=${rect.height}, class=${el.className || ''}, inline=${el.getAttribute('style') || ''}`;
      }
      return `${info(selector)}; ${info(target)}`;
    }
    """
    try:
        return str(page.evaluate(script, [selector, target]))
    except Exception:
        return ""


def _start_local_server(project_dir: str, project_id: str, timeout_seconds: int) -> Dict[str, Any]:
    port = _find_free_port()
    static_index = _find_static_index(project_dir)
    entry = _detect_python_web_entry(project_dir)
    python_cmd = _resolve_python(project_id)

    if entry:
        cmd = _build_python_web_command(entry, port, python_cmd)
    elif static_index:
        cmd = [python_cmd, "-m", "http.server", str(port), "--bind", "127.0.0.1"]
    else:
        return {"ok": False, "feedback": "Patch Mini QA 未找到可启动入口或静态首页"}

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=project_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_server_env(port),
        )
    except Exception as exc:
        return {"ok": False, "feedback": f"Patch Mini QA 服务启动失败: {exc}"}

    deadline = time.time() + min(max(timeout_seconds, 5), 30)
    while time.time() < deadline:
        if proc.poll() is not None:
            stderr = _read_proc_stream(proc.stderr, 1200)
            return {
                "ok": False,
                "feedback": f"Patch Mini QA 服务立即退出: rc={proc.returncode}; stderr={stderr}",
            }
        if _is_port_open(port):
            path = "/" if entry else "/" + os.path.basename(static_index)
            return {
                "ok": True,
                "url": f"http://127.0.0.1:{port}{path}",
                "proc": proc,
            }
        time.sleep(0.2)

    stderr = _read_proc_stream(proc.stderr, 1200)
    _terminate_process_tree(proc)
    return {
        "ok": False,
        "feedback": f"Patch Mini QA 服务未在 {timeout_seconds}s 内就绪; stderr={stderr}",
    }


def _detect_python_web_entry(project_dir: str) -> str:
    for name in ("main.py", "app.py", "server.py", "run.py"):
        path = os.path.join(project_dir, name)
        if not os.path.isfile(path):
            continue
        content = _read_text(path)
        if "Flask(" in content or "FastAPI(" in content or "uvicorn" in content:
            return name
    return ""


def _build_python_web_command(entry: str, port: int, python_cmd: str) -> List[str]:
    module = os.path.splitext(entry)[0]
    content = (
        "import os\n"
        f"os.environ['PORT'] = '{port}'\n"
        f"import {module} as _entry\n"
        "app = getattr(_entry, 'app', None)\n"
        "if app is None:\n"
        "    raise SystemExit('entry module has no app object')\n"
        "if hasattr(app, 'run'):\n"
        f"    app.run(host='127.0.0.1', port={port}, debug=False, use_reloader=False)\n"
        "else:\n"
        "    import uvicorn\n"
        f"    uvicorn.run(app, host='127.0.0.1', port={port}, log_level='warning')\n"
    )
    return [python_cmd, "-c", content]


def _resolve_python(project_id: str) -> str:
    try:
        from tools.sandbox import sandbox_env
        python_path = sandbox_env.venv_manager.get_or_create_venv(project_id)
        if python_path:
            return python_path
    except Exception:
        pass
    return sys.executable or "python"


def _server_env(port: int) -> dict:
    env = os.environ.copy()
    env["PORT"] = str(port)
    env["FLASK_ENV"] = "testing"
    env["FLASK_DEBUG"] = "0"
    return env


def _find_static_index(project_dir: str) -> str:
    for rel in ("index.html", "templates/index.html", "public/index.html"):
        path = os.path.join(project_dir, rel)
        if os.path.isfile(path):
            return path
    return ""


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _is_port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _terminate_process_tree(proc) -> None:
    if not proc or proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _read_proc_stream(stream, limit: int) -> str:
    try:
        return stream.read().decode("utf-8", errors="replace")[:limit]
    except Exception:
        return ""


def _collect_project_selectors(project_dir: str) -> set:
    selectors = set()
    for rel in _list_source_files(project_dir):
        content = _read_text(os.path.join(project_dir, rel))
        for match in re.finditer(r'id=["\']([^"\']+)["\']', content):
            selectors.add("#" + match.group(1))
        for match in re.finditer(r'getElementById\(["\']([^"\']+)["\']\)', content):
            selectors.add("#" + match.group(1))
    return selectors


def _list_source_files(project_dir: str) -> List[str]:
    results: List[str] = []
    ignore = {".git", ".astrea", "__pycache__", "node_modules", ".venv", "venv"}
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in ignore]
        for name in files:
            if not name.lower().endswith(FRONTEND_SOURCE_EXTS):
                continue
            rel = os.path.relpath(os.path.join(root, name), project_dir).replace("\\", "/")
            results.append(rel)
    return results


def _selector_token(selector: str) -> str:
    selector = str(selector or "").strip()
    if selector.startswith("#") or selector.startswith("."):
        return selector[1:]
    return selector


def _read_text(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()
    except UnicodeDecodeError:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
