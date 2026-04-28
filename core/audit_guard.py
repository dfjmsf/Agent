import logging
import os
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger("AuditGuard")

_VALID_SEVERITIES = {"high", "medium", "low", "info"}
_VALID_CATEGORIES = {"安全", "性能", "质量", "架构", "逻辑", "运行时"}
_NEGATIVE_KEYWORDS = {
    "缺少", "缺失", "没有", "不存在", "未设置", "未调用", "未初始化",
    "missing", "not found", "no such", "without",
}
_IDENTIFIER_STOPWORDS = {
    "main", "index", "high", "medium", "low", "route", "routes", "router",
    "view", "views", "template", "templates", "config", "module", "import",
    "error", "errors", "page", "action", "form", "table", "such", "not",
    "found", "missing", "app", "server", "entry", "point",
}
_CODE_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_\.]{1,}")
_ROUTE_LITERAL_RE = re.compile(r"/[A-Za-z0-9_\-/{}/:<>]*[A-Za-z0-9_}>]")
_ROUTE_DECL_RE = re.compile(r"route\s*\(\s*['\"]([^'\"]+)['\"]")
_ENTRYPOINT_NAMES = {
    "app.py", "main.py", "server.py", "manage.py", "wsgi.py", "asgi.py",
    "index.js", "index.ts", "main.js", "main.ts", "main.jsx", "main.tsx",
}
_ROUTE_FILE_HINTS = ("route", "router", "routes", "view", "views", "api", "controller")


@dataclass(frozen=True)
class FindingClaim:
    claim_type: str
    negative_claim: bool
    required_scope_role: Optional[str]
    verifier: Optional[str]


def validate_audit_findings(
    project_dir: str,
    findings: Iterable[dict],
    allowed_files: Optional[Sequence[str]] = None,
) -> Tuple[List[dict], List[dict]]:
    normalized_allowed = _normalize_paths(allowed_files)
    scope_profile = _build_scope_profile(normalized_allowed)
    validated: List[dict] = []
    dropped: List[dict] = []
    dedupe_keys = set()

    for raw_finding in findings or []:
        finding = _normalize_finding(raw_finding)
        file_path = finding["file"]
        if not file_path:
            dropped.append({"reason": "missing_file", "finding": raw_finding})
            continue
        if normalized_allowed and file_path not in normalized_allowed:
            dropped.append({"reason": "out_of_scope", "finding": raw_finding})
            continue

        abs_path = os.path.join(project_dir, file_path)
        if not os.path.isfile(abs_path):
            dropped.append({"reason": "file_not_found", "finding": raw_finding})
            continue

        lines = _read_lines(abs_path)
        if not lines:
            dropped.append({"reason": "empty_file", "finding": raw_finding})
            continue

        line_no = finding["line"]
        if line_no <= 0 or line_no > len(lines):
            dropped.append({"reason": "invalid_line", "finding": raw_finding})
            continue

        start_line = max(1, line_no - 1)
        end_line = min(len(lines), line_no + 1)
        context_lines = lines[start_line - 1:end_line]
        context_text = "\n".join(context_lines)
        source_text = "\n".join(lines)

        claim = _classify_claim(finding)
        finding["claim_type"] = claim.claim_type

        if claim.required_scope_role and not _scope_has_role(scope_profile, claim.required_scope_role):
            dropped.append({"reason": "scope_insufficient", "finding": raw_finding})
            continue

        if claim.claim_type == "generic" and finding["severity"] == "high" and not finding["evidence_text"]:
            dropped.append({"reason": "high_severity_without_strong_evidence", "finding": raw_finding})
            continue

        if not _evidence_gate_passes(finding, source_text, context_text):
            dropped.append({"reason": "evidence_mismatch", "finding": raw_finding})
            continue

        if claim.verifier:
            verdict = _run_deterministic_verifier(
                project_dir=project_dir,
                finding=finding,
                claim=claim,
                scope_profile=scope_profile,
                allowed_files=normalized_allowed,
                context_text=context_text,
                source_text=source_text,
            )
            if verdict == "contradicted":
                dropped.append({"reason": "contradicted_by_verifier", "finding": raw_finding})
                continue
            if verdict == "unverified":
                dropped.append({"reason": "unverified_negative_claim", "finding": raw_finding})
                continue

        dedupe_key = _build_dedupe_key(finding)
        if dedupe_key in dedupe_keys:
            dropped.append({"reason": "duplicate", "finding": raw_finding})
            continue
        dedupe_keys.add(dedupe_key)

        evidence_excerpt = "\n".join(
            f"{idx}: {lines[idx - 1].rstrip()}"
            for idx in range(start_line, end_line + 1)
        )
        finding["evidence_start_line"] = start_line
        finding["evidence_end_line"] = end_line
        finding["evidence_excerpt"] = evidence_excerpt
        validated.append(finding)

    if dropped:
        logger.warning("audit guard dropped %s findings", len(dropped))

    return validated, dropped


def render_audit_report_markdown(
    findings: Sequence[dict],
    user_request: str,
    scope_text: str,
) -> str:
    severity_order = {"high": 0, "medium": 1, "low": 2, "info": 3}
    ordered = sorted(
        findings,
        key=lambda item: (
            severity_order.get(item.get("severity"), 9),
            str(item.get("file", "")),
            int(item.get("line", 0) or 0),
            str(item.get("issue", "")),
        ),
    )
    high_count = sum(1 for item in ordered if item.get("severity") == "high")
    medium_count = sum(1 for item in ordered if item.get("severity") == "medium")
    rating = _health_rating(high_count, medium_count, len(ordered))

    lines: List[str] = [
        f"# 定向审查报告：{user_request}",
        "",
        f"**概述**：本次为定向审查，共确认 {len(ordered)} 个问题，健康度评级为 **{rating}**。",
        "",
        "**审查范围**",
        scope_text,
        "",
    ]

    grouped = {
        "high": [item for item in ordered if item.get("severity") == "high"],
        "medium": [item for item in ordered if item.get("severity") == "medium"],
        "low": [item for item in ordered if item.get("severity") == "low"],
        "info": [item for item in ordered if item.get("severity") == "info"],
    }

    section_titles = [
        ("high", "高危问题"),
        ("medium", "中危问题"),
        ("low", "低危问题"),
        ("info", "提示"),
    ]
    for severity, title in section_titles:
        items = grouped[severity]
        if not items:
            continue
        lines.extend([f"## {title}", ""])
        for index, item in enumerate(items, 1):
            lines.append(f"{index}. **{item['file']}:{item['line']}** [{item['category']}] {item['issue']}")
            if item.get("suggestion"):
                lines.append(f"   修复建议：{item['suggestion']}")
            if item.get("claim_type"):
                lines.append(f"   结论类型：{item['claim_type']}")
            if item.get("confidence") is not None:
                lines.append(f"   置信度：{item['confidence']:.2f}")
            if item.get("evidence_excerpt"):
                lines.append("   证据：")
                lines.append("```text")
                lines.append(item["evidence_excerpt"])
                lines.append("```")
            lines.append("")

    if not ordered:
        lines.extend(["## 结论", "", "未确认到可落盘的问题。"])

    return "\n".join(lines).strip() + "\n"


def _normalize_finding(raw_finding: dict) -> dict:
    finding = dict(raw_finding or {})
    file_path = str(finding.get("file", "")).replace("\\", "/").strip("/")
    severity = str(finding.get("severity", "info") or "info").lower()
    if severity not in _VALID_SEVERITIES:
        severity = "info"
    category = str(finding.get("category", "质量") or "质量")
    if category not in _VALID_CATEGORIES:
        category = "质量"

    try:
        line_no = int(finding.get("line", 0) or 0)
    except (TypeError, ValueError):
        line_no = 0

    confidence = finding.get("confidence")
    try:
        confidence = float(confidence) if confidence is not None else None
    except (TypeError, ValueError):
        confidence = None

    return {
        "file": file_path,
        "line": line_no,
        "severity": severity,
        "category": category,
        "issue": str(finding.get("issue", "") or "").strip(),
        "suggestion": str(finding.get("suggestion", "") or "").strip(),
        "confidence": confidence,
        "evidence_text": str(
            finding.get("evidence_text")
            or finding.get("evidence_excerpt")
            or ""
        ).strip(),
    }


def _classify_claim(finding: dict) -> FindingClaim:
    text = " ".join(part for part in [finding.get("issue", ""), finding.get("suggestion", "")] if part).lower()
    negative_claim = any(keyword in text for keyword in _NEGATIVE_KEYWORDS)
    route_hints = _extract_route_hints(finding)
    file_path = str(finding.get("file", "")).lower()

    if "row_factory" in text and negative_claim:
        return FindingClaim("row_factory_missing", True, None, "row_data")
    if "sqlite3.row" in text or "row 对象" in text or "row对象" in text or "row object" in text:
        if "template" in text or "模板" in text or "attribute" in text or ".attribute" in text or "属性" in text:
            return FindingClaim("row_object_template_compatibility", negative_claim, None, "row_data")
        return FindingClaim("row_factory_missing", negative_claim, None, "row_data")
    if "tuple" in text:
        return FindingClaim("tuple_vs_dict_conversion", negative_claim, None, "row_data")
    if (
        route_hints
        and (
            negative_claim
            or "route" in text
            or "路由" in text
            or "endpoint" in text
            or "handler" in text
            or ("action" in text and file_path.endswith((".html", ".htm", ".jinja2", ".j2")))
        )
    ):
        return FindingClaim("route_missing", True, "route", "route")
    if ("init_db" in text or "初始化" in text or "no such table" in text or "启动" in text) and negative_claim:
        return FindingClaim("lifecycle_missing", True, "entrypoint", "symbol")
    if ("导入" in text or "import" in text or "模块" in text or "module" in text) and negative_claim:
        return FindingClaim("import_missing", True, None, "import")
    if ("符号" in text or "函数" in text or "方法" in text or "字段" in text or "导出" in text) and negative_claim:
        return FindingClaim("symbol_missing", True, None, "symbol")
    if ("配置" in text or "config" in text or "env" in text) and negative_claim:
        return FindingClaim("config_missing", True, None, "config")
    if "重定向" in text or "redirect loop" in text or "循环重定向" in text:
        return FindingClaim("redirect_logic", False, None, "redirect")
    return FindingClaim("generic", negative_claim, None, None)


def _run_deterministic_verifier(
    project_dir: str,
    finding: dict,
    claim: FindingClaim,
    scope_profile: dict,
    allowed_files: Sequence[str],
    context_text: str,
    source_text: str,
) -> str:
    if claim.verifier == "row_data":
        return _verify_row_data_claim(project_dir, finding, claim, allowed_files or [finding["file"]])

    if claim.verifier == "route":
        routes = _collect_routes(project_dir, allowed_files)
        route_hints = _extract_route_hints(finding)
        if not route_hints:
            return "unverified"
        for hint in route_hints:
            if any(_route_matches_hint(route, hint) for route in routes):
                return "contradicted"
        return "verified" if claim.negative_claim else "unknown"

    if claim.verifier == "import":
        identifiers = _extract_identifiers(" ".join([finding.get("issue", ""), finding.get("suggestion", "")]))
        if _context_shows_import(context_text.lower(), identifiers):
            return "contradicted"
        if _scope_contains_named_file(allowed_files, identifiers, project_dir):
            return "contradicted"
        return "verified" if claim.negative_claim else "unknown"

    if claim.verifier == "symbol":
        identifiers = _extract_identifiers(" ".join([finding.get("issue", ""), finding.get("suggestion", "")]))
        if not identifiers:
            return "unverified"
        if _scope_contains_call_or_name(project_dir, allowed_files or [finding["file"]], identifiers):
            return "contradicted"
        return "verified" if claim.negative_claim else "unknown"

    if claim.verifier == "config":
        identifiers = _extract_identifiers(" ".join([finding.get("issue", ""), finding.get("suggestion", "")]))
        if not identifiers:
            return "unverified"
        if _scope_contains_name(project_dir, allowed_files or [finding["file"]], identifiers):
            return "contradicted"
        return "verified" if claim.negative_claim else "unknown"

    if claim.verifier == "redirect":
        lowered_context = context_text.lower()
        if "url_for('main.index')" in lowered_context or 'url_for("main.index")' in lowered_context:
            return "contradicted"
        return "unknown"

    return "unknown"


def _verify_row_data_claim(
    project_dir: str,
    finding: dict,
    claim: FindingClaim,
    files: Sequence[str],
) -> str:
    sources = _read_scope_sources(project_dir, files)
    if not sources:
        return "unverified"

    has_row_factory = _scope_has_row_factory(sources)
    has_dict_conversion = _scope_has_dict_row_conversion(sources)
    has_template_dot_access = _scope_has_template_dot_access(sources)

    if claim.claim_type == "row_factory_missing":
        if has_row_factory:
            return "contradicted"
        return "verified" if claim.negative_claim else "unknown"

    if claim.claim_type == "tuple_vs_dict_conversion":
        if has_dict_conversion:
            return "contradicted"
        if has_row_factory:
            return "unverified"
        return "verified" if claim.negative_claim else "unverified"

    if claim.claim_type == "row_object_template_compatibility":
        if has_dict_conversion:
            return "contradicted"
        if has_row_factory and has_template_dot_access:
            return "unverified"
        return "unverified"

    return "unknown"


def _read_scope_sources(project_dir: str, files: Sequence[str]) -> dict:
    sources = {}
    for rel_path in files or []:
        normalized = str(rel_path).replace("\\", "/").strip("/")
        abs_path = os.path.join(project_dir, normalized)
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as handle:
                sources[normalized] = handle.read()
        except OSError:
            continue
    return sources


def _scope_has_row_factory(sources: dict) -> bool:
    for content in sources.values():
        lowered = content.lower()
        if "row_factory" in lowered and "sqlite3.row" in lowered:
            return True
    return False


def _scope_has_dict_row_conversion(sources: dict) -> bool:
    patterns = [
        r"\bdict\s*\(\s*row\s*\)",
        r"\bdict\s*\(\s*[a-zA-Z_][a-zA-Z0-9_]*\s*\)",
    ]
    for rel_path, content in sources.items():
        if not rel_path.endswith(".py"):
            continue
        for pattern in patterns:
            if re.search(pattern, content):
                return True
    return False


def _scope_has_template_dot_access(sources: dict) -> bool:
    template_suffixes = (".html", ".htm", ".jinja2", ".j2")
    for rel_path, content in sources.items():
        if not rel_path.endswith(template_suffixes):
            continue
        if re.search(r"\{\{[^}]+?\.[A-Za-z_][A-Za-z0-9_]*", content):
            return True
    return False


def _evidence_gate_passes(finding: dict, source_text: str, context_text: str) -> bool:
    evidence_text = finding.get("evidence_text", "")
    if evidence_text:
        return evidence_text in source_text

    claim = _classify_claim(finding)
    if claim.claim_type == "generic" and finding.get("severity") == "high":
        return False
    if claim.negative_claim and claim.verifier is None:
        return False
    return bool(context_text.strip())


def _build_dedupe_key(finding: dict) -> tuple:
    return (
        finding.get("file", ""),
        int(finding.get("line", 0) or 0),
        finding.get("claim_type", ""),
        _normalize_text_key(finding.get("issue", "")),
        _normalize_text_key(finding.get("suggestion", "")),
    )


def _normalize_text_key(text: str) -> str:
    return re.sub(r"[\W_]+", "", str(text or "").lower())


def _normalize_paths(paths: Sequence[str] | None) -> List[str]:
    return [
        str(path).replace("\\", "/").strip("/")
        for path in (paths or [])
        if str(path).strip()
    ]


def _build_scope_profile(allowed_files: Sequence[str]) -> dict:
    profile = {"has_entrypoint": False, "has_route": False}
    for rel_path in allowed_files or []:
        filename = os.path.basename(rel_path).lower()
        lowered = rel_path.lower()
        if filename in _ENTRYPOINT_NAMES or lowered.endswith("/app.py") or lowered.endswith("/server.py"):
            profile["has_entrypoint"] = True
        if any(hint in lowered for hint in _ROUTE_FILE_HINTS):
            profile["has_route"] = True
    return profile


def _scope_has_role(scope_profile: dict, role: str) -> bool:
    if role == "entrypoint":
        return bool(scope_profile.get("has_entrypoint"))
    if role == "route":
        return bool(scope_profile.get("has_route"))
    return True


def _read_lines(abs_path: str) -> List[str]:
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read().splitlines()
    except OSError:
        return []


def _collect_routes(project_dir: str, allowed_files: Sequence[str]) -> List[str]:
    routes: List[str] = []
    for rel_path in allowed_files or []:
        if not rel_path.endswith((".py", ".js", ".ts", ".jsx", ".tsx")):
            continue
        abs_path = os.path.join(project_dir, rel_path)
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read()
        except OSError:
            continue
        routes.extend(_ROUTE_DECL_RE.findall(content))
        routes.extend(re.findall(r"(?:app|router|bp)\.(?:get|post|put|patch|delete)\s*\(\s*['\"]([^'\"]+)['\"]", content))
    return list(dict.fromkeys(route for route in routes if route))


def _extract_route_hints(finding: dict) -> List[str]:
    text = " ".join(part for part in [finding.get("issue", ""), finding.get("suggestion", ""), finding.get("evidence_text", "")] if part)
    hints = _ROUTE_LITERAL_RE.findall(text)
    normalized = []
    for hint in hints:
        base = _normalize_route_hint(hint)
        if base and base not in normalized:
            normalized.append(base)
    return normalized


def _normalize_route_hint(route: str) -> str:
    cleaned = route.strip().rstrip(".,);:!?")
    cleaned = re.sub(r"\{[^}]+\}", "", cleaned)
    cleaned = re.sub(r"<[^>]+>", "", cleaned)
    cleaned = cleaned.rstrip("/")
    return cleaned or "/"


def _route_matches_hint(route: str, hint: str) -> bool:
    normalized_route = _normalize_route_hint(route)
    return normalized_route == hint or normalized_route.startswith(hint + "/") or hint.startswith(normalized_route + "/")


def _extract_identifiers(text: str) -> List[str]:
    identifiers = []
    for token in _CODE_IDENTIFIER_RE.findall(text or ""):
        lowered = token.lower().strip(".")
        if len(lowered) < 2:
            continue
        if lowered in {"main.index"} or lowered in _IDENTIFIER_STOPWORDS:
            continue
        identifiers.append(lowered)
    return list(dict.fromkeys(identifiers))


def _context_shows_import(context_text: str, identifiers: Sequence[str]) -> bool:
    for identifier in identifiers:
        module_name = identifier.replace(".py", "")
        module_expr = module_name.replace(".", " ")
        if (
            f"import {module_name}" in context_text
            or f"from {module_name} import" in context_text
            or f"import {module_expr}" in context_text
            or f"from {module_expr} import" in context_text
        ):
            return True
    return False


def _scope_contains_named_file(project_dir_files: Sequence[str], identifiers: Sequence[str], project_dir: str) -> bool:
    normalized = set(project_dir_files or [])
    for identifier in identifiers:
        filename = identifier.replace(".", "/")
        candidates = {f"{filename}.py", f"{identifier}.py"}
        if any(candidate in normalized for candidate in candidates):
            return True
    return False


def _scope_contains_call_or_name(project_dir: str, files: Sequence[str], identifiers: Sequence[str]) -> bool:
    for rel_path in files or []:
        abs_path = os.path.join(project_dir, rel_path)
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read().lower()
        except OSError:
            continue
        for identifier in identifiers:
            name = identifier.split(".")[-1]
            if f"{name}(" in content or re.search(rf"\b{name}\b", content):
                return True
    return False


def _scope_contains_name(project_dir: str, files: Sequence[str], identifiers: Sequence[str]) -> bool:
    for rel_path in files or []:
        abs_path = os.path.join(project_dir, rel_path)
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as handle:
                content = handle.read().lower()
        except OSError:
            continue
        for identifier in identifiers:
            name = identifier.split(".")[-1]
            if re.search(rf"\b{name}\b", content):
                return True
    return False


def _health_rating(high_count: int, medium_count: int, total_count: int) -> str:
    if high_count >= 2:
        return "D"
    if high_count == 1 or medium_count >= 4:
        return "C"
    if medium_count >= 1:
        return "B"
    if total_count == 0:
        return "A"
    return "B"
