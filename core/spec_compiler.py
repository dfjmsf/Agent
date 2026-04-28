"""
Spec Compiler

将 Manager 产出的原始 project_spec 编译为执行期可消费的闭环契约：
- route_contracts
- blueprint_mounts
- effective_route_manifest
- app_registration_contracts
- compiler_metadata
"""
import copy
import re
from typing import Any, Dict, List, Tuple


def compile_spec(spec: dict) -> dict:
    """兼容旧 spec 的确定性编译入口。"""
    if not isinstance(spec, dict):
        return spec

    compiled = copy.deepcopy(spec)
    module_interfaces = compiled.get("module_interfaces", {}) or {}
    default_route_module = _infer_default_route_module(module_interfaces)
    route_module_contracts = _merge_route_module_contracts(
        _normalize_route_module_contracts(compiled.get("route_module_contracts") or {}),
        _infer_route_module_contracts(module_interfaces, compiled.get("api_contracts", []) or []),
    )

    interface_routes = _extract_interface_routes(module_interfaces)
    route_contracts = _build_route_contracts(
        compiled,
        interface_routes,
        default_route_module,
        route_module_contracts,
    )
    route_contracts = _hydrate_route_contracts(
        route_contracts,
        interface_routes,
        default_route_module,
        route_module_contracts,
    )
    architecture_contract, architecture_signals = _compile_architecture_contract(
        compiled,
        module_interfaces,
        route_module_contracts,
        route_contracts,
    )

    explicit_mounts = _normalize_blueprint_mounts(compiled.get("blueprint_mounts", []) or [])
    inferred_mounts, mount_errors = _infer_blueprint_mounts(route_contracts)
    blueprint_mounts, merge_errors = _merge_blueprint_mounts(explicit_mounts, inferred_mounts)

    route_errors = _collect_route_closure_errors(route_contracts, blueprint_mounts)
    app_registration_contracts, app_errors = _build_app_registration_contracts(
        module_interfaces, blueprint_mounts
    )
    effective_route_manifest = _build_effective_route_manifest(route_contracts, blueprint_mounts)

    compiled["route_contracts"] = route_contracts
    compiled["blueprint_mounts"] = blueprint_mounts
    compiled["app_registration_contracts"] = app_registration_contracts
    compiled["effective_route_manifest"] = effective_route_manifest
    compiled["route_module_contracts"] = route_module_contracts
    compiled["route_module_modes"] = {
        module: item.get("mode", "unknown")
        for module, item in route_module_contracts.items()
    }
    compiled["architecture_contract"] = architecture_contract
    compiled["compiler_metadata"] = {
        "legacy_route_inference": not bool(spec.get("route_contracts")),
        "legacy_mount_inference": not bool(spec.get("blueprint_mounts")),
        "interface_route_count": len(interface_routes),
        "route_contract_count": len(route_contracts),
        "blueprint_mount_count": len(blueprint_mounts),
        "route_module_contract_count": len(route_module_contracts),
        "architecture_contract_inferred": not bool(spec.get("architecture_contract")),
        "architecture_signals": architecture_signals,
        "contract_closure_errors": mount_errors + merge_errors + route_errors + app_errors,
    }
    # v4.4: ORM 模式一致性修正 — 防止 module_interfaces 与 orm_mode 矛盾
    _fix_orm_mode_consistency(compiled)

    return compiled


def _build_route_contracts(
    spec: dict,
    interface_routes: List[Dict[str, Any]],
    default_route_module: str | None = None,
    route_module_contracts: Dict[str, Dict[str, Any]] | None = None,
) -> List[Dict[str, Any]]:
    explicit = spec.get("route_contracts", []) or []
    if explicit:
        contracts = [_normalize_route_contract(item) for item in explicit if isinstance(item, dict)]
        return [item for item in contracts if item]

    contracts: List[Dict[str, Any]] = []
    handler_index = {item.get("handler"): item for item in interface_routes if item.get("handler")}

    for route in spec.get("page_routes", []) or []:
        if not isinstance(route, dict):
            continue
        methods = _normalize_methods(route.get("method") or route.get("methods"))
        handler = (
            route.get("function")
            or route.get("handler")
            or _infer_handler_name(route.get("path", ""), methods, "page")
        )
        seeded = handler_index.get(handler, {})
        contracts.append({
            "surface": "page",
            "handler": handler,
            "module": (
                seeded.get("module")
                or _infer_route_module_for_effective_path(route.get("path", ""), route_module_contracts)
                or default_route_module
            ),
            "blueprint": seeded.get("blueprint"),
            "local_path": seeded.get("local_path"),
            "effective_path": route.get("path", ""),
            "methods": methods,
            "source_type": "page_routes",
        })

    for api in spec.get("api_contracts", []) or []:
        if not isinstance(api, dict):
            continue
        method = _normalize_methods(api.get("method") or api.get("methods"))
        effective_path = api.get("path", "")
        seeded = _match_interface_route(interface_routes, effective_path, method)
        contracts.append({
            "surface": "api",
            "handler": (
                api.get("function")
                or seeded.get("handler")
                or _infer_handler_name(effective_path, method, "api")
            ),
            "module": (
                seeded.get("module")
                or _infer_route_module_for_effective_path(effective_path, route_module_contracts)
                or default_route_module
            ),
            "blueprint": seeded.get("blueprint"),
            "local_path": seeded.get("local_path"),
            "effective_path": effective_path,
            "methods": method,
            "source_type": "api_contracts",
        })

    return [_normalize_route_contract(item) for item in contracts if item]


def _hydrate_route_contracts(
    route_contracts: List[Dict[str, Any]],
    interface_routes: List[Dict[str, Any]],
    default_route_module: str | None = None,
    route_module_contracts: Dict[str, Dict[str, Any]] | None = None,
) -> List[Dict[str, Any]]:
    handler_index = {item.get("handler"): item for item in interface_routes if item.get("handler")}
    hydrated: List[Dict[str, Any]] = []

    for contract in route_contracts:
        merged = dict(contract)
        handler = merged.get("handler")
        seeded = handler_index.get(handler, {})
        if not merged.get("handler"):
            merged["handler"] = _infer_handler_name(
                merged.get("effective_path", ""),
                merged.get("methods", []),
                merged.get("surface", "page"),
            )
            handler = merged.get("handler")
            seeded = handler_index.get(handler, {})
        if not merged.get("module"):
            merged["module"] = (
                seeded.get("module")
                or _infer_route_module_for_effective_path(merged.get("effective_path", ""), route_module_contracts)
                or default_route_module
            )
        if not merged.get("blueprint"):
            merged["blueprint"] = seeded.get("blueprint")
        if not merged.get("local_path"):
            merged["local_path"] = seeded.get("local_path")
        if not merged.get("methods"):
            merged["methods"] = seeded.get("methods", [])
        if not merged.get("handler"):
            alt = _match_interface_route(
                interface_routes,
                merged.get("effective_path", ""),
                merged.get("methods", []),
            )
            if alt:
                merged["handler"] = alt.get("handler")
                merged["module"] = merged.get("module") or alt.get("module")
                merged["blueprint"] = merged.get("blueprint") or alt.get("blueprint")
                merged["local_path"] = merged.get("local_path") or alt.get("local_path")
        hydrated.append(_normalize_route_contract(merged))

    return hydrated


def _infer_route_module_for_effective_path(
    effective_path: str,
    route_module_contracts: Dict[str, Dict[str, Any]] | None,
) -> str | None:
    if not route_module_contracts:
        return None

    normalized_path = _canonicalize_path(effective_path)
    best_match: tuple[int, str] | None = None

    for module_name, contract in route_module_contracts.items():
        if not isinstance(contract, dict):
            continue
        prefix = _normalize_prefix(contract.get("url_prefix_hint", ""))
        if not prefix:
            continue
        normalized_prefix = _canonicalize_path(prefix)
        if normalized_path != normalized_prefix and not normalized_path.startswith(normalized_prefix + "/"):
            continue
        score = len(_split_tokens(normalized_prefix))
        candidate = (score, module_name)
        if best_match is None or candidate[0] > best_match[0]:
            best_match = candidate

    return best_match[1] if best_match else None


def _infer_default_route_module(module_interfaces: Dict[str, Any]) -> str | None:
    candidates = []
    for module_name in module_interfaces.keys():
        normalized = str(module_name).replace("\\", "/").lower()
        if normalized.endswith("routes.py") or "/routes/" in normalized:
            candidates.append(module_name)

    if "routes.py" in module_interfaces:
        return "routes.py"
    if len(candidates) == 1:
        return candidates[0]
    root_level = [item for item in candidates if "/" not in str(item).replace("\\", "/")]
    if len(root_level) == 1:
        return root_level[0]
    return None


def _compile_architecture_contract(
    spec: dict,
    module_interfaces: Dict[str, Any],
    route_module_contracts: Dict[str, Dict[str, Any]],
    route_contracts: List[Dict[str, Any]],
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    explicit = _normalize_architecture_contract(spec.get("architecture_contract") or {})
    inferred, signals = _infer_architecture_contract(
        spec,
        module_interfaces,
        route_module_contracts,
        route_contracts,
    )
    return _merge_architecture_contract(explicit, inferred), signals


def _normalize_architecture_contract(raw_contract: Any) -> Dict[str, str]:
    if not isinstance(raw_contract, dict):
        return {}

    allowed = {
        "backend_framework": {"fastapi", "flask", "mixed", "unknown"},
        "orm_mode": {"sqlalchemy_session", "flask_sqlalchemy", "mixed", "unknown"},
        "auth_mode": {"jwt_header", "flask_login_session", "none", "mixed", "unknown"},
        "router_mode": {"fastapi_apirouter", "flask_blueprint", "mixed", "unknown"},
        "entrypoint_mode": {"uvicorn_app", "flask_app_factory", "mixed", "unknown"},
        "package_layout": {"flat_modules", "package_src", "unknown"},
        "import_style": {"sibling_import", "package_import", "mixed", "unknown"},
    }

    normalized: Dict[str, str] = {}
    for key, allowed_values in allowed.items():
        value = str(raw_contract.get(key) or "").strip().lower()
        normalized[key] = value if value in allowed_values else "unknown"
    return normalized


def _merge_architecture_contract(
    explicit: Dict[str, str],
    inferred: Dict[str, str],
) -> Dict[str, str]:
    merged: Dict[str, str] = {}
    for key in (
        "backend_framework",
        "orm_mode",
        "auth_mode",
        "router_mode",
        "entrypoint_mode",
        "package_layout",
        "import_style",
    ):
        explicit_value = str(explicit.get(key) or "").strip().lower()
        merged[key] = (
            explicit_value
            if explicit_value and explicit_value != "unknown"
            else inferred.get(key, "unknown")
        )
    return merged


def _infer_architecture_contract(
    spec: dict,
    module_interfaces: Dict[str, Any],
    route_module_contracts: Dict[str, Dict[str, Any]],
    route_contracts: List[Dict[str, Any]],
) -> Tuple[Dict[str, str], Dict[str, Any]]:
    tech_stack = [str(item or "").strip().lower() for item in (spec.get("tech_stack", []) or [])]
    interface_text = "\n".join(
        str(blob) for blob in module_interfaces.values() if isinstance(blob, str)
    ).lower()
    module_names = [str(name or "") for name in module_interfaces.keys()]
    local_module_names = {
        _module_stem(name) for name in module_names
        if _module_stem(name)
    }

    fastapi_signal = _contains_any(tech_stack, ("fastapi",)) or _contains_any_text(
        interface_text,
        ("from fastapi", "fastapi(", "apirouter", "include_router", "uvicorn"),
    )
    flask_signal = _contains_any(tech_stack, ("flask",)) or _contains_any_text(
        interface_text,
        ("from flask", "flask(", "blueprint(", "register_blueprint", "flask_login", "flask_sqlalchemy"),
    )

    flask_sqlalchemy_signal = _contains_any_text(
        interface_text,
        ("from flask_sqlalchemy", "flask_sqlalchemy", "db.model", "db.session", "sqlalchemy()", "migrate("),
    )
    sqlalchemy_session_signal = _contains_any_text(
        interface_text,
        ("declarative_base", "sessionlocal", "sessionmaker", "create_engine(", "depends(get_db)", "get_db", "mapped_column"),
    )

    jwt_signal = _contains_any_text(
        interface_text,
        ("jwt", "oauth2passwordbearer", "bearer", "authorization", "create_access_token", "token_required"),
    )
    flask_login_signal = _contains_any_text(
        interface_text,
        ("flask_login", "loginmanager", "login_user", "logout_user", "current_user", "@login_required"),
    )

    fastapi_router_signal = _contains_any_text(
        interface_text,
        ("apirouter", "@router.", "@api_router.", "include_router"),
    )
    flask_blueprint_signal = any(
        bool(item.get("blueprint")) for item in route_contracts
    ) or any(
        bool((contract or {}).get("blueprints"))
        for contract in route_module_contracts.values()
    ) or _contains_any_text(interface_text, ("blueprint(", ".route(", "register_blueprint"))

    fastapi_entry_signal = _contains_any_text(
        interface_text,
        ("fastapi(", "uvicorn.run", "include_router", "app = fastapi"),
    )
    flask_entry_signal = _contains_any_text(
        interface_text,
        ("create_app(", "flask(__name__", "app = flask", "register_blueprint"),
    )

    has_init_module = any(str(name).replace("\\", "/").endswith("__init__.py") for name in module_names)
    package_import_signal, sibling_import_signal = _detect_import_style_signals(
        interface_text,
        local_module_names,
    )

    backend_framework = _resolve_dual_signal(fastapi_signal, flask_signal, "fastapi", "flask")
    router_mode = _resolve_dual_signal(
        fastapi_router_signal,
        flask_blueprint_signal,
        "fastapi_apirouter",
        "flask_blueprint",
    )
    entrypoint_mode = _resolve_dual_signal(
        fastapi_entry_signal,
        flask_entry_signal,
        "uvicorn_app",
        "flask_app_factory",
    )
    orm_mode = _resolve_dual_signal(
        sqlalchemy_session_signal,
        flask_sqlalchemy_signal,
        "sqlalchemy_session",
        "flask_sqlalchemy",
    )
    auth_mode = _resolve_auth_mode(jwt_signal, flask_login_signal)

    has_sqlalchemy_stack = any(
        token in tech for tech in tech_stack for token in ("sqlalchemy", "sqlite", "postgres", "mysql")
    ) or bool(spec.get("data_models"))
    if orm_mode == "unknown" and has_sqlalchemy_stack:
        orm_mode = "sqlalchemy_session"

    if backend_framework == "fastapi":
        if router_mode == "unknown":
            router_mode = "fastapi_apirouter"
        if entrypoint_mode == "unknown":
            entrypoint_mode = "uvicorn_app"
    elif backend_framework == "flask":
        if router_mode == "unknown":
            router_mode = "flask_blueprint"
        if entrypoint_mode == "unknown":
            entrypoint_mode = "flask_app_factory"

    package_layout = "package_src" if has_init_module else ("flat_modules" if module_names else "unknown")
    import_style = "unknown"
    if package_import_signal and sibling_import_signal:
        import_style = "mixed"
    elif package_import_signal:
        import_style = "package_import"
    elif sibling_import_signal:
        import_style = "sibling_import"
    elif package_layout == "package_src":
        import_style = "package_import"
    elif package_layout == "flat_modules":
        import_style = "sibling_import"

    contract = _normalize_architecture_contract({
        "backend_framework": backend_framework,
        "orm_mode": orm_mode,
        "auth_mode": auth_mode,
        "router_mode": router_mode,
        "entrypoint_mode": entrypoint_mode,
        "package_layout": package_layout,
        "import_style": import_style,
    })
    signals = {
        "fastapi_signal": fastapi_signal,
        "flask_signal": flask_signal,
        "sqlalchemy_session_signal": sqlalchemy_session_signal,
        "flask_sqlalchemy_signal": flask_sqlalchemy_signal,
        "jwt_signal": jwt_signal,
        "flask_login_signal": flask_login_signal,
        "fastapi_router_signal": fastapi_router_signal,
        "flask_blueprint_signal": flask_blueprint_signal,
        "fastapi_entry_signal": fastapi_entry_signal,
        "flask_entry_signal": flask_entry_signal,
        "package_import_signal": package_import_signal,
        "sibling_import_signal": sibling_import_signal,
        "local_module_names": sorted(local_module_names),
    }
    return contract, signals


def _resolve_auth_mode(jwt_signal: bool, flask_login_signal: bool) -> str:
    if jwt_signal and flask_login_signal:
        return "mixed"
    if jwt_signal:
        return "jwt_header"
    if flask_login_signal:
        return "flask_login_session"
    return "none"


def _resolve_dual_signal(
    left_signal: bool,
    right_signal: bool,
    left_value: str,
    right_value: str,
) -> str:
    if left_signal and right_signal:
        return "mixed"
    if left_signal:
        return left_value
    if right_signal:
        return right_value
    return "unknown"


def _detect_import_style_signals(
    interface_text: str,
    local_module_names: set[str],
) -> Tuple[bool, bool]:
    package_import_signal = bool(re.search(r"\bfrom\s+\.[A-Za-z_][A-Za-z0-9_\.]*\s+import\b", interface_text))
    package_import_signal = package_import_signal or bool(
        re.search(r"\bfrom\s+(?:src|backend\.src)\.[A-Za-z_][A-Za-z0-9_\.]*\s+import\b", interface_text)
    )

    sibling_import_signal = False
    for module_name in sorted(local_module_names):
        if re.search(rf"\bfrom\s+{re.escape(module_name)}\s+import\b", interface_text):
            sibling_import_signal = True
            break
        if re.search(rf"\bimport\s+{re.escape(module_name)}\b", interface_text):
            sibling_import_signal = True
            break

    return package_import_signal, sibling_import_signal


def _contains_any_text(text: str, tokens: tuple[str, ...]) -> bool:
    lowered = str(text or "").lower()
    return any(token in lowered for token in tokens)


def _contains_any(values: List[str], tokens: tuple[str, ...]) -> bool:
    return any(any(token in value for token in tokens) for value in values)


def _module_stem(module_name: str) -> str:
    basename = str(module_name or "").replace("\\", "/").split("/")[-1]
    if not basename.endswith(".py"):
        return ""
    stem = basename[:-3]
    return stem if stem and stem != "__init__" else ""


def _infer_handler_name(effective_path: str, methods: List[str], surface: str) -> str | None:
    normalized_surface = str(surface or "").lower()
    normalized_path = _canonicalize_path(effective_path)
    all_tokens = _split_tokens(normalized_path)
    if all_tokens and all_tokens[0] == "api":
        all_tokens = all_tokens[1:]
    named_tokens = [token for token in all_tokens if not (token.startswith("{") and token.endswith("}"))]
    method_set = set(_normalize_methods(methods))

    if not named_tokens:
        return "index" if normalized_surface == "page" else None

    last = named_tokens[-1]
    if last in {"login", "logout", "register"}:
        return last
    if last == "dashboard":
        return "dashboard" if normalized_surface == "page" else "get_dashboard"
    if last == "status" and method_set.intersection({"PATCH", "PUT"}):
        return "update_task_status"
    if last == "comments":
        if "POST" in method_set:
            return "add_comment"
        if "GET" in method_set:
            return "get_task_comments"
    if last == "attachments":
        if "POST" in method_set:
            return "upload_attachment"
        if "GET" in method_set:
            return "get_task_attachments"
    if last == "notifications":
        if "GET" in method_set:
            return "get_notifications"
        if "POST" in method_set:
            return "create_notification"
    if last == "members":
        if "POST" in method_set:
            return "add_project_member"
        if "DELETE" in method_set:
            return "remove_project_member"
    if len(named_tokens) >= 2 and named_tokens[-2:] == ["projects", "tasks"]:
        if "GET" in method_set:
            return "get_project_tasks"
        if "POST" in method_set:
            return "create_task"

    is_detail = bool(all_tokens and all_tokens[-1].startswith("{") and all_tokens[-1].endswith("}"))
    resource = named_tokens[-1]
    singular = _singularize(resource)

    if normalized_surface == "page":
        if last == "create":
            parent = named_tokens[-2] if len(named_tokens) >= 2 else "item"
            return f"{_singularize(parent)}_create"
        if is_detail:
            return f"{singular}_detail"
        if "GET" in method_set or not method_set:
            if singular in {"login", "register", "dashboard", "index"}:
                return singular
            return f"{singular}_list"
        return singular

    if "GET" in method_set and not is_detail:
        return f"get_{resource}"
    if "POST" in method_set and not is_detail:
        return f"create_{singular}"
    if "GET" in method_set and is_detail:
        return f"get_{singular}"
    if method_set.intersection({"PATCH", "PUT"}):
        return f"update_{singular}"
    if "DELETE" in method_set:
        return f"delete_{singular}"
    return None


def _singularize(resource: str) -> str:
    text = str(resource or "").strip().lower()
    if not text:
        return text
    if text.endswith("ies") and len(text) > 3:
        return text[:-3] + "y"
    if text.endswith("s") and not text.endswith("ss"):
        return text[:-1]
    return text


def _extract_interface_routes(module_interfaces: Dict[str, Any]) -> List[Dict[str, Any]]:
    routes: List[Dict[str, Any]] = []
    pattern = re.compile(
        r"@(?P<blueprint>\w+)\.route\(\s*(?P<quote>['\"])(?P<path>.*?)(?P=quote)"
        r"(?P<extra>[^)]*)\)\s*def\s+(?P<handler>\w+)\(",
        re.DOTALL,
    )

    for module_name, blob in module_interfaces.items():
        if not isinstance(blob, str):
            continue
        for match in pattern.finditer(blob):
            extra = match.group("extra") or ""
            methods_match = re.search(r"methods\s*=\s*\[(?P<body>[^\]]+)\]", extra)
            methods = _normalize_methods(methods_match.group("body") if methods_match else None)
            routes.append({
                "module": module_name,
                "blueprint": match.group("blueprint"),
                "local_path": match.group("path"),
                "handler": match.group("handler"),
                "methods": methods,
            })

    return routes


def _normalize_route_module_contracts(raw_contracts: Any) -> Dict[str, Dict[str, Any]]:
    if not isinstance(raw_contracts, dict):
        return {}

    normalized: Dict[str, Dict[str, Any]] = {}
    for module, contract in raw_contracts.items():
        if not isinstance(contract, dict):
            continue
        normalized[str(module)] = {
            "mode": str(contract.get("mode") or "unknown"),
            "blueprints": sorted(dict.fromkeys(contract.get("blueprints", []) or [])),
            "helper_functions": sorted(dict.fromkeys(contract.get("helper_functions", []) or [])),
            "url_prefix_hint": _normalize_prefix(contract.get("url_prefix_hint", "")),
        }
    return normalized


def _merge_route_module_contracts(
    explicit: Dict[str, Dict[str, Any]],
    inferred: Dict[str, Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    merged = copy.deepcopy(inferred)
    for module, contract in explicit.items():
        current = merged.get(module, {})
        explicit_mode = str(contract.get("mode") or "").strip().lower()
        merged[module] = {
            "mode": (
                explicit_mode
                if explicit_mode and explicit_mode != "unknown"
                else current.get("mode") or "unknown"
            ),
            "blueprints": sorted(dict.fromkeys(
                (contract.get("blueprints", []) or []) + (current.get("blueprints", []) or [])
            )),
            "helper_functions": sorted(dict.fromkeys(
                (contract.get("helper_functions", []) or []) + (current.get("helper_functions", []) or [])
            )),
            "url_prefix_hint": _normalize_prefix(
                contract.get("url_prefix_hint", "") or current.get("url_prefix_hint", "")
            ),
        }
    return merged


def _infer_route_module_contracts(
    module_interfaces: Dict[str, Any],
    api_contracts: List[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    contracts: Dict[str, Dict[str, Any]] = {}
    for module_name, blob in module_interfaces.items():
        if not isinstance(blob, str):
            continue
        normalized = str(module_name).replace("\\", "/").lower()
        if not (normalized.endswith("routes.py") or "/routes/" in normalized):
            continue

        blueprints = _extract_blueprint_names_from_interface(blob)
        helper_functions = _extract_route_helper_names(blob)
        mode = _infer_route_module_mode(blob, helper_functions)
        contracts[module_name] = {
            "mode": mode,
            "blueprints": blueprints,
            "helper_functions": helper_functions,
            "url_prefix_hint": _infer_url_prefix_hint(module_name, blueprints, api_contracts),
        }
    return contracts


def _extract_blueprint_names_from_interface(blob: str) -> List[str]:
    results = []
    for pattern in (
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*Blueprint\s*\(",
        r"\b([A-Za-z_][A-Za-z0-9_]*)\s*:\s*Blueprint\b",
    ):
        for item in re.findall(pattern, blob or ""):
            if item not in results:
                results.append(item)
    return results


def _extract_route_helper_names(blob: str) -> List[str]:
    helpers = []
    for item in re.findall(r"\bdef\s+((?:init|register)_[A-Za-z0-9_]*(?:routes?|blueprints?))\s*\(", blob or ""):
        if item not in helpers:
            helpers.append(item)
    return helpers


def _infer_route_module_mode(blob: str, helper_functions: List[str]) -> str:
    text = blob or ""
    has_route_decorators = bool(re.search(r"@\w+\.(?:route|get|post|put|delete|patch)\s*\(", text))
    has_add_url_rule = "add_url_rule" in text
    has_blueprint = "Blueprint" in text
    has_helper = bool(helper_functions)

    if has_helper and (has_route_decorators or has_add_url_rule):
        return "mixed"
    if has_helper:
        return "init_function"
    if has_blueprint or has_route_decorators or has_add_url_rule:
        return "direct_blueprint"
    return "unknown"


def _infer_url_prefix_hint(
    module_name: str,
    blueprints: List[str],
    api_contracts: List[Dict[str, Any]],
) -> str:
    candidates = _module_resource_candidates(module_name, blueprints)
    api_prefixes = []
    for item in api_contracts or []:
        if not isinstance(item, dict):
            continue
        path = _canonicalize_path(item.get("path", ""))
        if not path.startswith("/api/"):
            continue
        tokens = _split_tokens(path)
        if len(tokens) < 2:
            continue
        api_prefixes.append("/api/" + tokens[1])

    for candidate in candidates:
        normalized = _normalize_segment(candidate)
        plural = _pluralize_segment(normalized)
        for prefix in api_prefixes:
            leaf = prefix.rsplit("/", 1)[-1]
            if leaf in {normalized, plural}:
                return prefix

    for candidate in candidates:
        normalized = _normalize_segment(candidate)
        if normalized in {"auth", "dashboard", "audit"}:
            return f"/api/{normalized}"
        return f"/api/{_pluralize_segment(normalized)}"

    return ""


def _module_resource_candidates(module_name: str, blueprints: List[str]) -> List[str]:
    candidates = []
    basename = str(module_name).replace("\\", "/").split("/")[-1]
    stem = basename[:-3] if basename.endswith(".py") else basename
    for suffix in ("_routes", "routes"):
        if stem.endswith(suffix):
            stem = stem[:-len(suffix)]
            break
    stem = stem.strip("_")
    if stem and stem not in candidates:
        candidates.append(stem)
    for blueprint in blueprints:
        name = str(blueprint or "")
        if name.endswith("_bp"):
            name = name[:-3]
        if name and name not in candidates:
            candidates.append(name)
    return candidates


def _normalize_segment(text: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", str(text or "").strip().lower())


def _pluralize_segment(text: str) -> str:
    normalized = _normalize_segment(text)
    irregular = {
        "auth": "auth",
        "dashboard": "dashboard",
        "audit": "audit",
        "attachment": "attachments",
        "comment": "comments",
        "member": "members",
        "notification": "notifications",
        "project": "projects",
        "tag": "tags",
        "task": "tasks",
    }
    if normalized in irregular:
        return irregular[normalized]
    if normalized.endswith("y") and len(normalized) > 1:
        return normalized[:-1] + "ies"
    if normalized.endswith("s"):
        return normalized
    return normalized + "s"


def _normalize_route_contract(item: Dict[str, Any]) -> Dict[str, Any]:
    normalized = dict(item)
    normalized["surface"] = (normalized.get("surface") or "").lower() or "page"
    normalized["handler"] = normalized.get("handler")
    normalized["module"] = normalized.get("module")
    normalized["blueprint"] = normalized.get("blueprint")
    normalized["local_path"] = _normalize_path(normalized.get("local_path", "")) if normalized.get("local_path") is not None else None
    normalized["effective_path"] = _normalize_path(normalized.get("effective_path", "")) if normalized.get("effective_path") else ""
    normalized["methods"] = _normalize_methods(normalized.get("methods"))
    normalized["source_type"] = normalized.get("source_type") or "route_contracts"
    return normalized


def _normalize_blueprint_mounts(mounts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for mount in mounts:
        if not isinstance(mount, dict):
            continue
        blueprint = mount.get("blueprint")
        if not blueprint:
            continue
        normalized.append({
            "blueprint": blueprint,
            "module": mount.get("module"),
            "url_prefix": _normalize_prefix(mount.get("url_prefix", "")),
        })
    return normalized


def _infer_blueprint_mounts(route_contracts: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    prefixes_by_blueprint: Dict[str, set[str]] = {}
    modules_by_blueprint: Dict[str, str] = {}
    errors: List[str] = []

    for contract in route_contracts:
        blueprint = contract.get("blueprint")
        local_path = contract.get("local_path")
        effective_path = contract.get("effective_path")
        if not blueprint or local_path is None or not effective_path:
            continue

        prefix = _derive_prefix(effective_path, local_path)
        if prefix is None:
            errors.append(
                f"无法为 blueprint '{blueprint}' 推导挂载前缀："
                f"effective_path='{effective_path}' 与 local_path='{local_path}' 不闭合"
            )
            continue

        prefixes_by_blueprint.setdefault(blueprint, set()).add(prefix)
        modules_by_blueprint.setdefault(blueprint, contract.get("module"))

    mounts: List[Dict[str, Any]] = []
    for blueprint, prefixes in prefixes_by_blueprint.items():
        if len(prefixes) > 1:
            errors.append(
                f"blueprint '{blueprint}' 推导出多个互相冲突的 url_prefix: {sorted(prefixes)}"
            )
            continue
        mounts.append({
            "blueprint": blueprint,
            "module": modules_by_blueprint.get(blueprint),
            "url_prefix": next(iter(prefixes)),
        })

    return mounts, errors


def _merge_blueprint_mounts(
    explicit_mounts: List[Dict[str, Any]],
    inferred_mounts: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    merged = {item["blueprint"]: dict(item) for item in inferred_mounts}
    errors: List[str] = []

    for mount in explicit_mounts:
        blueprint = mount["blueprint"]
        if blueprint in merged:
            inferred_prefix = _normalize_prefix(merged[blueprint].get("url_prefix", ""))
            explicit_prefix = _normalize_prefix(mount.get("url_prefix", ""))
            if inferred_prefix != explicit_prefix:
                errors.append(
                    f"blueprint '{blueprint}' 的显式 url_prefix='{explicit_prefix}' "
                    f"与从路由契约推导出的 '{inferred_prefix}' 冲突"
                )
            merged[blueprint].update({k: v for k, v in mount.items() if v is not None})
        else:
            merged[blueprint] = dict(mount)

    return list(merged.values()), errors


def _collect_route_closure_errors(
    route_contracts: List[Dict[str, Any]],
    blueprint_mounts: List[Dict[str, Any]],
) -> List[str]:
    errors: List[str] = []
    mounts = {item.get("blueprint"): item for item in blueprint_mounts if item.get("blueprint")}

    for contract in route_contracts:
        blueprint = contract.get("blueprint")
        local_path = contract.get("local_path")
        effective_path = contract.get("effective_path")
        if not blueprint:
            continue
        mount = mounts.get(blueprint)
        if not mount:
            errors.append(
                f"route_contract '{contract.get('handler') or effective_path}' 引用了未挂载的 blueprint '{blueprint}'"
            )
            continue
        if local_path is None:
            errors.append(
                f"route_contract '{contract.get('handler') or effective_path}' 声明了 blueprint '{blueprint}'，但缺少 local_path"
            )
            continue
        derived = _join_paths(mount.get("url_prefix", ""), local_path)
        if effective_path and _normalize_path(derived) != _normalize_path(effective_path):
            errors.append(
                f"route_contract '{contract.get('handler') or effective_path}' 不闭合："
                f"url_prefix='{mount.get('url_prefix', '')}' + local_path='{local_path}' "
                f"推导为 '{derived}'，但 effective_path 是 '{effective_path}'"
            )

    return errors


def _build_app_registration_contracts(
    module_interfaces: Dict[str, Any],
    blueprint_mounts: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    app_candidates = [
        module_name for module_name in module_interfaces.keys()
        if str(module_name).replace("\\", "/").endswith(("app.py", "main.py", "__init__.py"))
    ]
    errors: List[str] = []
    app_module = app_candidates[0] if app_candidates else None

    if blueprint_mounts and not app_module:
        errors.append("存在 blueprint_mounts，但 module_interfaces 中未找到 app.py/main.py/__init__.py，无法闭环 app 注册契约")

    registrations = []
    for mount in blueprint_mounts:
        registrations.append({
            "blueprint": mount.get("blueprint"),
            "app_module": app_module,
            "url_prefix": _normalize_prefix(mount.get("url_prefix", "")),
        })

    return registrations, errors


def _build_effective_route_manifest(
    route_contracts: List[Dict[str, Any]],
    blueprint_mounts: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    mounts = {item.get("blueprint"): item for item in blueprint_mounts if item.get("blueprint")}
    manifest: List[Dict[str, Any]] = []

    for contract in route_contracts:
        item = dict(contract)
        blueprint = item.get("blueprint")
        mount = mounts.get(blueprint, {})
        item["resolved_url_prefix"] = mount.get("url_prefix", "")
        if not item.get("effective_path") and item.get("local_path") is not None:
            item["effective_path"] = _join_paths(item["resolved_url_prefix"], item["local_path"])
        manifest.append(item)

    return manifest


def _match_interface_route(
    interface_routes: List[Dict[str, Any]],
    effective_path: str,
    methods: List[str],
) -> Dict[str, Any]:
    normalized_target = _normalize_path(effective_path)
    method_set = set(_normalize_methods(methods))
    for route in interface_routes:
        if _normalize_path(route.get("local_path", "")) != normalized_target:
            continue
        route_methods = set(_normalize_methods(route.get("methods")))
        if not method_set or not route_methods or route_methods == method_set:
            return route
    return {}


def _normalize_methods(methods: Any) -> List[str]:
    if methods is None:
        return []
    if isinstance(methods, str):
        if "," in methods or "'" in methods or '"' in methods:
            items = re.findall(r"[A-Za-z]+", methods)
        else:
            items = [methods]
    elif isinstance(methods, list):
        items = methods
    else:
        items = [str(methods)]

    normalized = []
    for item in items:
        method = str(item).strip().strip("'\"").upper()
        if method:
            normalized.append(method)
    return sorted(set(normalized))


def _derive_prefix(effective_path: str, local_path: str) -> str | None:
    normalized_effective = _canonicalize_path(effective_path)
    normalized_local = _canonicalize_path(local_path)

    if normalized_local == "/":
        return _normalize_prefix(normalized_effective if normalized_effective != "/" else "")

    effective_tokens = _split_tokens(normalized_effective)
    local_tokens = _split_tokens(normalized_local)
    if len(local_tokens) > len(effective_tokens):
        return None
    if effective_tokens[-len(local_tokens):] != local_tokens:
        return None

    prefix_tokens = effective_tokens[:-len(local_tokens)]
    return _normalize_prefix("/" + "/".join(prefix_tokens) if prefix_tokens else "")


def _join_paths(prefix: str, local_path: str) -> str:
    prefix_norm = _normalize_prefix(prefix)
    local_norm = _normalize_path(local_path)
    if local_norm == "/":
        return prefix_norm or "/"
    if not prefix_norm:
        return local_norm
    if prefix_norm == "/":
        return local_norm
    if local_norm.startswith("/"):
        return _normalize_path(prefix_norm + local_norm)
    return _normalize_path(prefix_norm + "/" + local_norm)


def _normalize_prefix(path: str) -> str:
    normalized = _normalize_path(path or "")
    if normalized in ("", "/"):
        return ""
    return normalized


def _normalize_path(path: str) -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    if not text.startswith("/"):
        text = "/" + text
    text = re.sub(r"/+", "/", text)
    if text != "/" and text.endswith("/"):
        text = text[:-1]
    return text


def _canonicalize_path(path: str) -> str:
    normalized = _normalize_path(path)
    normalized = re.sub(r"<(?:[^:>]+:)?([^>]+)>", r"{\1}", normalized)
    return normalized


def _split_tokens(path: str) -> List[str]:
    normalized = _normalize_path(path)
    if normalized in ("", "/"):
        return []
    return [token for token in normalized.strip("/").split("/") if token]


def _fix_orm_mode_consistency(compiled: dict) -> None:
    """
    v4.4: 当 orm_mode == flask_sqlalchemy 时，修正 module_interfaces 中
    与原生 SQLAlchemy 语义矛盾的签名描述。

    核心矛盾：
    - Manager LLM 在 orm_mode=flask_sqlalchemy 下仍可能生成
      `get_db() -> sqlite3.Connection` 或 `create_engine` 等签名
    - L0.2 强制 Coder 实现这些函数 → Coder 用 create_engine → L0.3A 违规

    修正策略（确定性，零 LLM 成本）：
    1. 将 `get_db() -> sqlite3.Connection` 替换为 Flask-SQLAlchemy 兼容描述
    2. 移除 module_interfaces 中的 `create_engine` / `sessionmaker` 引用
    """
    contract = compiled.get("architecture_contract", {}) or {}
    orm_mode = str(contract.get("orm_mode") or "").strip().lower()
    if orm_mode != "flask_sqlalchemy":
        return

    module_interfaces = compiled.get("module_interfaces", {}) or {}
    updated = False

    for module_name, interface_blob in module_interfaces.items():
        if not isinstance(interface_blob, str):
            continue
        if not module_name.lower().endswith((".py",)):
            continue

        original = interface_blob

        # 替换 get_db() -> sqlite3.Connection 为 Flask-SQLAlchemy 兼容描述
        if "sqlite3.Connection" in interface_blob:
            interface_blob = interface_blob.replace(
                "sqlite3.Connection", "db.session"
            )
            updated = True

        # 移除对 create_engine / sessionmaker / declarative_base 的引用
        for token in ("create_engine", "sessionmaker", "declarative_base", "SessionLocal"):
            if token in interface_blob:
                interface_blob = interface_blob.replace(token, "")
                updated = True

        if interface_blob != original:
            module_interfaces[module_name] = interface_blob

    if updated:
        compiled["module_interfaces"] = module_interfaces

