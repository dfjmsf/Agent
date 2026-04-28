import ast
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set

from tools.observer import Observer

_MAX_SCOPE_FILES = 8
_SOURCE_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
    ".html", ".htm", ".css", ".scss",
}
_FRONTEND_EXTS = {".html", ".htm", ".css", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte"}
_SKIP_DIRS = {
    ".git", "__pycache__", ".venv", "venv", "node_modules", ".sandbox",
    ".astrea", ".idea", ".vscode", "dist", "build",
}
_GENERIC_SCOPE_TOKENS = {
    "routes", "route", "views", "view", "templates", "template",
    "services", "service", "components", "component", "pages", "page",
    "static", "assets", "src", "api", "backend", "frontend",
    "py", "js", "jsx", "ts", "tsx", "html", "htm", "css", "scss", "vue", "svelte",
}
_ENGLISH_STOPWORDS = {
    "please", "review", "audit", "check", "scan", "module", "modules",
    "code", "project", "login", "register", "user", "auth", "session",
    "for", "the", "and", "with", "this", "that",
}
_ALIAS_GROUPS: Dict[str, Dict[str, List[str]]] = {
    "login": {
        "triggers": ["登录", "login", "signin", "auth", "session"],
        "tokens": ["login", "signin", "auth", "session"],
    },
    "register": {
        "triggers": ["注册", "register", "signup"],
        "tokens": ["register", "signup"],
    },
    "user": {
        "triggers": ["用户", "user", "profile", "account"],
        "tokens": ["user", "profile", "account"],
    },
}


@dataclass
class TargetScope:
    scope_kind: str
    anchor_routes: List[str] = field(default_factory=list)
    seed_files: List[str] = field(default_factory=list)
    candidate_files: List[str] = field(default_factory=list)
    clarify_question: str = ""

    def is_resolved(self) -> bool:
        return bool(self.candidate_files)

    def summary_text(self) -> str:
        parts: List[str] = [f"范围类型: {self.scope_kind}"]
        if self.anchor_routes:
            parts.append(f"路由锚点: {', '.join(self.anchor_routes)}")
        if self.seed_files:
            parts.append(f"种子文件: {', '.join(self.seed_files)}")
        if self.candidate_files:
            parts.append(f"候选文件: {', '.join(self.candidate_files)}")
        if self.clarify_question:
            parts.append(f"澄清: {self.clarify_question}")
        return "\n".join(parts)


@dataclass
class CrossFileSignal:
    provider_file: str
    importer_file: str
    missing_symbol: str
    feedback_tag: str
    stage: str


def resolve_target_scope(project_dir: str, user_text: str, max_candidates: int = _MAX_SCOPE_FILES) -> TargetScope:
    files = _collect_project_files(project_dir)
    routes = _collect_project_routes(project_dir, files)
    explicit_routes = _extract_explicit_routes(user_text)

    if explicit_routes:
        seed_files = _match_route_files(explicit_routes, routes)
        if not seed_files:
            route_tokens = _route_tokens(explicit_routes)
            seed_files = _match_files_by_tokens(files, route_tokens)
        if seed_files:
            candidate_files = _expand_candidate_files(
                project_dir,
                files,
                seed_files,
                _route_tokens(explicit_routes),
                max_candidates=max_candidates,
            )
            return TargetScope(
                scope_kind="route_targeted",
                anchor_routes=explicit_routes,
                seed_files=seed_files,
                candidate_files=candidate_files,
            )

    query_tokens = _extract_query_tokens(user_text)
    if not query_tokens:
        return _unresolved_scope(user_text)

    route_entries = _match_routes_by_tokens(query_tokens, routes)
    seed_files = _unique(route["file"] for route in route_entries)
    seed_files.extend(_match_files_by_tokens(files, query_tokens))
    seed_files = _unique(seed_files)

    if not seed_files:
        return _unresolved_scope(user_text)

    anchor_routes = _unique(route["path"] for route in route_entries)
    candidate_files = _expand_candidate_files(
        project_dir,
        files,
        seed_files,
        query_tokens,
        max_candidates=max_candidates,
    )
    if not candidate_files:
        return _unresolved_scope(user_text)

    return TargetScope(
        scope_kind="module_targeted",
        anchor_routes=anchor_routes,
        seed_files=seed_files,
        candidate_files=candidate_files,
    )


def build_scope_from_cross_file_signal(
    project_dir: str,
    signal: CrossFileSignal,
    max_candidates: int = _MAX_SCOPE_FILES,
) -> TargetScope:
    files = _collect_project_files(project_dir)
    seed_files = _unique([signal.provider_file, signal.importer_file])
    token_pool = _collect_scope_tokens(seed_files, signal.missing_symbol)
    candidate_files = _expand_candidate_files(
        project_dir,
        files,
        seed_files,
        token_pool,
        max_candidates=max_candidates,
    )
    return TargetScope(
        scope_kind="cross_file",
        anchor_routes=[],
        seed_files=seed_files,
        candidate_files=candidate_files,
    )


def parse_cross_file_signal(feedback: str, stage: str) -> Optional[CrossFileSignal]:
    if "[CROSS_FILE:" not in (feedback or ""):
        return None

    provider_file = _extract_tag_value(feedback, "CROSS_FILE")
    if not provider_file:
        return None

    importer_file = _extract_tag_value(feedback, "IMPORTER_FILE")
    missing_symbol = _extract_tag_value(feedback, "MISSING_SYMBOL")
    feedback_tag = ""
    tag_match = re.search(r"(\[(?:L0|L1)[^\]]+\])", feedback or "")
    if tag_match:
        feedback_tag = tag_match.group(1)

    return CrossFileSignal(
        provider_file=provider_file,
        importer_file=importer_file or "",
        missing_symbol=missing_symbol or "",
        feedback_tag=feedback_tag,
        stage=stage,
    )


def _extract_tag_value(feedback: str, tag_name: str) -> str:
    match = re.search(rf"\[{re.escape(tag_name)}:(.*?)\]", feedback or "")
    return (match.group(1).strip() if match else "")


def _collect_project_files(project_dir: str) -> List[str]:
    project_dir = os.path.abspath(project_dir)
    results: List[str] = []
    for root, dirs, files in os.walk(project_dir):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS and not d.startswith(".")]
        for file_name in files:
            if file_name.startswith("."):
                continue
            ext = os.path.splitext(file_name)[1].lower()
            if ext not in _SOURCE_EXTS:
                continue
            rel_path = os.path.relpath(os.path.join(root, file_name), project_dir).replace("\\", "/")
            results.append(rel_path)
    return sorted(results)


def _collect_project_routes(project_dir: str, files: List[str]) -> List[Dict[str, str]]:
    observer = Observer(project_dir)
    routes: List[Dict[str, str]] = []
    for rel_path in files:
        if not rel_path.endswith(".py"):
            continue
        for route in observer.extract_routes(rel_path):
            path = str(route.get("path") or "").strip()
            if not path:
                continue
            routes.append({
                "path": path,
                "file": rel_path,
            })
    return routes


def _extract_explicit_routes(user_text: str) -> List[str]:
    routes = re.findall(r"/[A-Za-z0-9_\-/{}/:]*[A-Za-z0-9_}]", user_text or "")
    results: List[str] = []
    seen: Set[str] = set()
    for route in routes:
        normalized = route.rstrip(".,);:!?").replace("\\", "/")
        if normalized and normalized not in seen:
            seen.add(normalized)
            results.append(normalized)
    return results


def _route_tokens(routes: Iterable[str]) -> List[str]:
    tokens: List[str] = []
    for route in routes:
        for part in re.split(r"[/{}:_-]+", route.lower()):
            part = part.strip()
            if len(part) >= 2 and part not in {"api", "v1", "v2"}:
                tokens.append(part)
    return _unique(tokens)


def _extract_query_tokens(user_text: str) -> List[str]:
    raw_text = user_text or ""
    lowered = raw_text.lower()
    tokens: List[str] = []

    for config in _ALIAS_GROUPS.values():
        if any(trigger.lower() in lowered for trigger in config["triggers"]):
            tokens.extend(config["tokens"])

    english = re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", lowered)
    for word in english:
        if word not in _ENGLISH_STOPWORDS:
            tokens.append(word)

    return _unique(tokens)


def _match_route_files(anchor_routes: List[str], routes: List[Dict[str, str]]) -> List[str]:
    matched: List[str] = []
    normalized_anchors = [route.lower().rstrip("/") for route in anchor_routes]
    for route in routes:
        route_path = route["path"].lower().rstrip("/")
        for anchor in normalized_anchors:
            if route_path == anchor or route_path.startswith(anchor) or anchor.startswith(route_path):
                matched.append(route["file"])
                break
    return _unique(matched)


def _match_routes_by_tokens(tokens: List[str], routes: List[Dict[str, str]]) -> List[Dict[str, str]]:
    scored: List[tuple[int, Dict[str, str]]] = []
    for route in routes:
        haystack = f"{route['path']} {route['file']}".lower()
        score = sum(1 for token in tokens if token in haystack)
        if score > 0:
            scored.append((score, route))
    scored.sort(key=lambda item: (-item[0], len(item[1]["path"]), item[1]["file"]))
    return [route for _, route in scored]


def _match_files_by_tokens(files: List[str], tokens: List[str]) -> List[str]:
    scored: List[tuple[int, str]] = []
    for rel_path in files:
        lower_path = rel_path.lower()
        score = sum(1 for token in tokens if token in lower_path)
        if score > 0:
            scored.append((score, rel_path))
    scored.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
    return _unique(path for _, path in scored)


def _expand_candidate_files(
    project_dir: str,
    all_files: List[str],
    seed_files: List[str],
    query_tokens: List[str],
    max_candidates: int,
) -> List[str]:
    ordered: List[str] = []
    file_set = set(all_files)

    for file_path in seed_files:
        _append_unique(ordered, file_path)

    index = 0
    while index < len(ordered) and len(ordered) < max_candidates:
        file_path = ordered[index]
        index += 1
        for dep in _collect_one_hop_local_imports(project_dir, file_path, file_set):
            _append_unique(ordered, dep)
            if len(ordered) >= max_candidates:
                break

    same_stem_tokens = _collect_scope_tokens(seed_files, " ".join(query_tokens))
    for related in _match_files_by_tokens(all_files, same_stem_tokens):
        if os.path.splitext(related)[1].lower() in _FRONTEND_EXTS or related.endswith(".py"):
            _append_unique(ordered, related)
        if len(ordered) >= max_candidates:
            break

    return ordered[:max_candidates]


def _collect_scope_tokens(seed_files: Iterable[str], extra_text: str = "") -> List[str]:
    tokens: List[str] = []
    for rel_path in seed_files:
        basename = os.path.splitext(os.path.basename(rel_path))[0]
        tokens.extend(
            part for part in re.split(r"[^A-Za-z0-9]+", basename.lower())
            if len(part) >= 2 and part not in _GENERIC_SCOPE_TOKENS
        )
        for part in rel_path.lower().split("/"):
            if len(part) >= 2:
                tokens.extend(
                    seg for seg in re.split(r"[^A-Za-z0-9]+", part)
                    if len(seg) >= 2 and seg not in _GENERIC_SCOPE_TOKENS
                )
    if extra_text:
        tokens.extend(
            part for part in re.split(r"[^A-Za-z0-9]+", extra_text.lower())
            if len(part) >= 2 and part not in _GENERIC_SCOPE_TOKENS
        )
    return _unique(tokens)


def _collect_one_hop_local_imports(project_dir: str, rel_path: str, project_files: Set[str]) -> List[str]:
    if not rel_path.endswith(".py"):
        return []

    abs_path = os.path.join(project_dir, rel_path)
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as handle:
            tree = ast.parse(handle.read())
    except Exception:
        return []

    found: List[str] = []
    current_dir = os.path.dirname(rel_path).replace("\\", "/")

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                resolved = _resolve_import_module(alias.name, current_dir, 0, project_files)
                if resolved:
                    found.append(resolved)
        elif isinstance(node, ast.ImportFrom):
            module_name = node.module or ""
            resolved = _resolve_import_module(module_name, current_dir, node.level, project_files)
            if resolved:
                found.append(resolved)
            for alias in node.names:
                alias_resolved = _resolve_import_alias(module_name, alias.name, current_dir, node.level, project_files)
                if alias_resolved:
                    found.append(alias_resolved)

    return _unique(found)


def _resolve_import_module(module_name: str, current_dir: str, level: int, project_files: Set[str]) -> str:
    module_path = module_name.replace(".", "/").strip("/")
    base_dir = current_dir
    if level > 0:
        segments = [segment for segment in current_dir.split("/") if segment]
        keep = max(len(segments) - (level - 1), 0)
        base_dir = "/".join(segments[:keep])
    candidates = []
    if module_path:
        if base_dir:
            candidates.append(f"{base_dir}/{module_path}.py")
            candidates.append(f"{base_dir}/{module_path}/__init__.py")
        candidates.append(f"{module_path}.py")
        candidates.append(f"{module_path}/__init__.py")
    elif base_dir:
        candidates.append(f"{base_dir}/__init__.py")

    for candidate in candidates:
        normalized = candidate.replace("\\", "/").lstrip("/")
        if normalized in project_files:
            return normalized
    return ""


def _resolve_import_alias(
    module_name: str,
    alias_name: str,
    current_dir: str,
    level: int,
    project_files: Set[str],
) -> str:
    if alias_name == "*":
        return ""

    module_path = module_name.replace(".", "/").strip("/")
    base_dir = current_dir
    if level > 0:
        segments = [segment for segment in current_dir.split("/") if segment]
        keep = max(len(segments) - (level - 1), 0)
        base_dir = "/".join(segments[:keep])

    joined = "/".join(part for part in [base_dir, module_path, alias_name] if part)
    if joined:
        for candidate in (f"{joined}.py", f"{joined}/__init__.py"):
            normalized = candidate.replace("\\", "/").lstrip("/")
            if normalized in project_files:
                return normalized
    return ""


def _unresolved_scope(user_text: str) -> TargetScope:
    return TargetScope(
        scope_kind="unresolved",
        clarify_question=(
            f"我暂时无法把“{user_text.strip() or '当前模块'}”稳定映射到明确入口。"
            "请补充路由路径（如 `/login`）、接口（如 `/api/auth/login`）或文件名。"
        ),
    )


def _append_unique(items: List[str], value: str) -> None:
    if value and value not in items:
        items.append(value)


def _unique(values: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    results: List[str] = []
    for value in values:
        normalized = str(value or "").replace("\\", "/").strip("/")
        if normalized and normalized not in seen:
            seen.add(normalized)
            results.append(normalized)
    return results
