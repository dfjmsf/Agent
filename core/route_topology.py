import ast
import re
from typing import Any, Dict, List


HTTP_METHOD_ATTRS = {"get", "post", "put", "delete", "patch"}
APP_MODULE_SUFFIXES = ("app.py", "main.py", "__init__.py")
NON_ENDPOINT_HELPERS = {
    "register_blueprints",
    "register_routes",
    "init_routes",
    "init_blueprints",
    "create_app",
    "init_app",
    "token_required",
    "login_required",
    "wrapper",
}


def normalize_route_path(path: str) -> str:
    text = str(path or "").strip()
    if not text:
        return ""
    if not text.startswith("/"):
        text = "/" + text
    text = re.sub(r"/+", "/", text)
    if text != "/" and text.endswith("/"):
        text = text[:-1]
    return text


def join_route_path(prefix: str, local_path: str) -> str:
    prefix_norm = normalize_route_path(prefix)
    local_norm = normalize_route_path(local_path)
    if not local_norm:
        return prefix_norm or ""
    if local_norm == "/":
        return prefix_norm or "/"
    if not prefix_norm or prefix_norm == "/":
        return local_norm
    return normalize_route_path(prefix_norm + local_norm)


def extract_module_interface_handlers(module_interfaces: Dict[str, Any], target_file: str) -> List[str]:
    blob = module_interfaces.get(target_file, "")
    if not isinstance(blob, str):
        return []
    handlers = re.findall(r"\bdef\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", blob)
    return list(dict.fromkeys(handlers))


def extract_module_interface_symbols(module_interfaces: Dict[str, Any], target_file: str) -> List[str]:
    """提取 module_interfaces 中指定文件暴露的顶层符号。"""
    if not isinstance(module_interfaces, dict):
        return []

    blob = _lookup_module_interface_blob(module_interfaces, target_file)
    if not isinstance(blob, str) or not blob.strip():
        return []

    symbols: List[str] = []
    for pattern in (
        r"\b(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)\b",
        r"\b([A-Z_][A-Z_0-9]*)\s*=",
    ):
        for match in re.finditer(pattern, blob):
            name = match.group(1)
            if _is_pydantic_inner_config_symbol(name, target_file, blob):
                continue
            if name not in symbols:
                symbols.append(name)
    return symbols


def extract_route_contract_handlers_for_target(project_spec: Dict[str, Any], target_file: str) -> List[str]:
    """从 route_contracts/effective_route_manifest 提取指定路由文件的 handler 硬契约。"""
    if not isinstance(project_spec, dict):
        return []

    handlers: List[str] = []
    for field in ("route_contracts", "effective_route_manifest"):
        for contract in project_spec.get(field, []) or []:
            if not isinstance(contract, dict):
                continue
            module = (
                contract.get("module")
                or contract.get("target_file")
                or contract.get("file")
            )
            if module and not _module_path_matches(module, target_file):
                continue
            if not module and not _looks_like_route_module(target_file):
                continue

            handler = contract.get("handler") or contract.get("function")
            if not handler or is_non_endpoint_helper(handler):
                continue
            if handler not in handlers:
                handlers.append(handler)
    return handlers


def extract_expected_symbols_for_target(
    project_spec: Dict[str, Any],
    target_file: str,
    module_interfaces: Dict[str, Any] | None = None,
) -> List[str]:
    """
    返回 Reviewer 与 Coder 共用的符号契约。

    路由文件优先使用 route_contracts/effective_route_manifest 的 handler；
    只有没有路由契约时才回退 module_interfaces，避免多源命名互相打架。
    """
    route_handlers = extract_route_contract_handlers_for_target(project_spec, target_file)
    if route_handlers:
        return route_handlers

    interfaces = module_interfaces
    if interfaces is None and isinstance(project_spec, dict):
        interfaces = project_spec.get("module_interfaces", {}) or {}
    return extract_module_interface_symbols(interfaces or {}, target_file)


def extract_contract_handlers(project_spec: Dict[str, Any]) -> Dict[str, List[str]]:
    handlers_by_module: Dict[str, List[str]] = {}
    for contract in project_spec.get("route_contracts", []) or []:
        if not isinstance(contract, dict):
            continue
        module = contract.get("module")
        handler = contract.get("handler")
        if not module or not handler or is_non_endpoint_helper(handler):
            continue
        handlers_by_module.setdefault(module, [])
        if handler not in handlers_by_module[module]:
            handlers_by_module[module].append(handler)
    return handlers_by_module


def _lookup_module_interface_blob(module_interfaces: Dict[str, Any], target_file: str) -> Any:
    normalized_target = _normalize_module_path(target_file)
    basename = normalized_target.rsplit("/", 1)[-1]
    stem = basename.rsplit(".", 1)[0] if "." in basename else basename

    for key in (target_file, normalized_target, basename, stem):
        if key in module_interfaces:
            return module_interfaces.get(key)

    for key, value in module_interfaces.items():
        if _module_path_matches(key, target_file):
            return value
    return ""


def _normalize_module_path(path: Any) -> str:
    text = str(path or "").replace("\\", "/").strip().lstrip("./")
    return re.sub(r"/+", "/", text)


def _module_path_matches(module: Any, target_file: str) -> bool:
    module_norm = _normalize_module_path(module)
    target_norm = _normalize_module_path(target_file)
    if not module_norm or not target_norm:
        return False
    if module_norm == target_norm:
        return True
    return module_norm.endswith("/" + target_norm) or target_norm.endswith("/" + module_norm)


def _looks_like_route_module(target_file: str) -> bool:
    normalized = _normalize_module_path(target_file).lower()
    basename = normalized.rsplit("/", 1)[-1]
    return basename in {"routes.py", "views.py"} or "/routes/" in normalized


def _is_pydantic_inner_config_symbol(name: str, target_file: str, blob: str) -> bool:
    if name != "Config":
        return False

    normalized = _normalize_module_path(target_file).lower()
    basename = normalized.rsplit("/", 1)[-1]
    schema_like_file = basename in {"schemas.py", "schema.py"} or normalized.endswith((
        "/schemas.py",
        "/schema.py",
    ))
    if not schema_like_file:
        return False

    text = str(blob or "")
    pydantic_signal = (
        "BaseModel" in text
        or "pydantic" in text.lower()
        or "orm_mode" in text
        or "from_attributes" in text
    )
    return pydantic_signal


def extract_blueprint_variables_from_code(code_content: str) -> List[str]:
    tree = _safe_parse(code_content)
    if not tree:
        return []

    variables: List[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        func = node.value.func
        if not isinstance(func, ast.Name) or func.id != "Blueprint":
            continue
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id not in variables:
                variables.append(target.id)
    return variables


def extract_blueprint_registrations_from_code(code_content: str) -> List[Dict[str, str]]:
    tree = _safe_parse(code_content)
    if not tree:
        return []

    registrations: List[Dict[str, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "register_blueprint":
            continue

        blueprint = ""
        if node.args and isinstance(node.args[0], ast.Name):
            blueprint = node.args[0].id
        if not blueprint:
            continue

        url_prefix = ""
        for keyword in node.keywords or []:
            if keyword.arg == "url_prefix":
                url_prefix = _extract_string(keyword.value)
                break

        registrations.append({
            "blueprint": blueprint,
            "url_prefix": normalize_route_path(url_prefix) if url_prefix else "",
        })
    return registrations


def extract_route_bindings_from_code(code_content: str) -> List[Dict[str, Any]]:
    tree = _safe_parse(code_content)
    if not tree:
        return []

    bindings: List[Dict[str, Any]] = []

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                func = decorator.func
                if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
                    continue
                if func.attr not in HTTP_METHOD_ATTRS and func.attr != "route":
                    continue

                local_path = _extract_route_rule(decorator)
                if not local_path:
                    continue

                methods = _extract_route_methods(decorator, func.attr)
                bindings.append({
                    "blueprint": func.value.id,
                    "handler": node.name,
                    "local_path": normalize_route_path(local_path),
                    "methods": methods,
                    "source": "decorator",
                })

        if isinstance(node, ast.Call):
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr != "add_url_rule":
                continue
            if not isinstance(func.value, ast.Name):
                continue

            local_path = _extract_route_rule(node)
            if not local_path:
                continue

            handler = _extract_add_url_rule_handler(node)
            bindings.append({
                "blueprint": func.value.id,
                "handler": handler,
                "local_path": normalize_route_path(local_path),
                "methods": _extract_route_methods(node, "route"),
                "source": "add_url_rule",
            })

    return bindings


def extract_top_level_function_names(code_content: str) -> List[str]:
    tree = _safe_parse(code_content)
    if not tree:
        return []
    return [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]


def extract_requested_paths(feedback: str) -> List[str]:
    requested: List[str] = []
    seen = set()

    patterns = [
        r"https?://[^/\s]+(?P<path>/[^\s，。]*)",
        r"(?:GET|POST|PUT|DELETE|PATCH)\s+(?P<path>/[^\s，。]*)",
    ]
    for pattern in patterns:
        for match in re.finditer(pattern, feedback or "", re.IGNORECASE):
            path = normalize_route_path(match.group("path"))
            if path and path not in seen:
                requested.append(path)
                seen.add(path)
    return requested


def analyze_route_topology(
    all_code: Dict[str, str],
    project_spec: Dict[str, Any] | None = None,
    feedback: str = "",
) -> Dict[str, Any]:
    project_spec = project_spec or {}
    module_interfaces = project_spec.get("module_interfaces", {}) or {}
    expected_handlers_by_module = {
        module: extract_module_interface_handlers(module_interfaces, module)
        for module in module_interfaces.keys()
    }
    contract_handlers_by_module = extract_contract_handlers(project_spec)
    route_module_contracts = project_spec.get("route_module_contracts", {}) or {}

    registrations: List[Dict[str, str]] = []
    app_modules: List[str] = []
    for module, code in all_code.items():
        module_regs = extract_blueprint_registrations_from_code(code)
        if not module_regs:
            continue
        app_modules.append(module)
        for item in module_regs:
            registrations.append({
                "module": module,
                "blueprint": item["blueprint"],
                "url_prefix": item["url_prefix"],
            })

    prefix_by_blueprint = {
        item["blueprint"]: item["url_prefix"]
        for item in registrations
        if item.get("blueprint")
    }

    double_issue_map: Dict[tuple, Dict[str, Any]] = {}
    unregistered_handlers: List[Dict[str, Any]] = []
    repair_scope = set()
    effective_routes = set()

    for module, code in all_code.items():
        bindings = extract_route_bindings_from_code(code)
        blueprint_vars = set(extract_blueprint_variables_from_code(code))
        module_contract = route_module_contracts.get(module, {}) if isinstance(route_module_contracts, dict) else {}
        helper_functions = {
            name for name in (module_contract.get("helper_functions", []) or [])
            if isinstance(name, str)
        }
        interface_handlers = [
            name for name in (expected_handlers_by_module.get(module) or [])
            if not is_non_endpoint_helper(name) and name not in helper_functions
        ]
        contract_handlers = [
            name for name in (contract_handlers_by_module.get(module) or [])
            if not is_non_endpoint_helper(name) and name not in helper_functions
        ]
        expected_handlers = sorted(dict.fromkeys(interface_handlers + contract_handlers))
        if not expected_handlers:
            expected_handlers = [
                name for name in extract_top_level_function_names(code)
                if looks_like_endpoint_function(name) and name not in helper_functions
            ]

        if blueprint_vars:
            registered_handlers = {
                binding["handler"]
                for binding in bindings
                if binding.get("handler")
            }
            missing_handlers = sorted(
                handler
                for handler in expected_handlers
                if handler and handler not in registered_handlers
            )
            if missing_handlers:
                unregistered_handlers.append({
                    "module": module,
                    "blueprints": sorted(blueprint_vars),
                    "handlers": missing_handlers,
                })
                repair_scope.add(module)

        for binding in bindings:
            blueprint = binding.get("blueprint", "")
            local_path = normalize_route_path(binding.get("local_path", ""))
            url_prefix = prefix_by_blueprint.get(blueprint, "")

            if url_prefix and local_path and local_path != url_prefix and (
                local_path == url_prefix or local_path.startswith(url_prefix + "/")
            ):
                key = (module, blueprint, url_prefix)
                issue = double_issue_map.setdefault(key, {
                    "module": module,
                    "blueprint": blueprint,
                    "url_prefix": url_prefix,
                    "local_paths": [],
                    "handlers": [],
                })
                if local_path not in issue["local_paths"]:
                    issue["local_paths"].append(local_path)
                if binding.get("handler") and binding["handler"] not in issue["handlers"]:
                    issue["handlers"].append(binding["handler"])
                repair_scope.add(module)
                if registrations:
                    repair_scope.update(item["module"] for item in registrations)

            if blueprint == "app":
                if local_path:
                    effective_routes.add(local_path)
                continue

            if blueprint in prefix_by_blueprint and local_path:
                effective_routes.add(join_route_path(url_prefix, local_path))

    expected_routes = set()
    for api in project_spec.get("api_contracts", []) or []:
        if isinstance(api, dict) and api.get("path"):
            expected_routes.add(normalize_route_path(api.get("path", "")))
    for page in project_spec.get("page_routes", []) or []:
        if isinstance(page, dict) and page.get("path"):
            expected_routes.add(normalize_route_path(page.get("path", "")))
    for path in extract_requested_paths(feedback):
        expected_routes.add(path)

    missing_effective_routes = sorted(
        path for path in expected_routes
        if path and path not in effective_routes
    )
    if missing_effective_routes and registrations:
        repair_scope.update(item["module"] for item in registrations)

    double_prefixed_blueprints = list(double_issue_map.values())

    if not double_prefixed_blueprints and not unregistered_handlers and not missing_effective_routes:
        return {}

    return {
        "error_type": "ROUTE_TOPOLOGY_MISMATCH",
        "double_prefixed_blueprints": double_prefixed_blueprints,
        "unregistered_handlers": unregistered_handlers,
        "missing_effective_routes": missing_effective_routes,
        "repair_scope": sorted(repair_scope),
        "app_modules": app_modules,
        "actual_effective_routes": sorted(effective_routes),
    }


def is_non_endpoint_helper(name: str) -> bool:
    normalized = str(name or "").strip()
    if not normalized:
        return True
    if normalized in NON_ENDPOINT_HELPERS:
        return True
    if re.match(r"^(?:init|register)_[A-Za-z0-9_]*(?:routes?|blueprints?)$", normalized):
        return True
    return False


def looks_like_endpoint_function(name: str) -> bool:
    normalized = str(name or "").strip()
    if not normalized or is_non_endpoint_helper(normalized):
        return False
    if normalized.endswith("_route"):
        return True
    if normalized in {"index", "login", "logout", "register", "dashboard"}:
        return True
    return normalized.startswith((
        "get_",
        "create_",
        "update_",
        "delete_",
        "add_",
        "edit_",
        "upload_",
        "list_",
        "mark_",
        "search_",
        "export_",
        "import_",
        "toggle_",
        "reset_",
        "view_",
        "show_",
    ))


def _safe_parse(code_content: str) -> ast.AST | None:
    try:
        return ast.parse(code_content or "")
    except SyntaxError:
        return None


def _extract_string(node: ast.AST) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return ""


def _extract_route_rule(call_node: ast.Call) -> str:
    if call_node.args:
        value = _extract_string(call_node.args[0])
        if value:
            return value
    for keyword in call_node.keywords or []:
        if keyword.arg in {"path", "rule"}:
            value = _extract_string(keyword.value)
            if value:
                return value
    return ""


def _extract_route_methods(call_node: ast.Call, attr_name: str) -> List[str]:
    if attr_name in HTTP_METHOD_ATTRS:
        return [attr_name.upper()]

    for keyword in call_node.keywords or []:
        if keyword.arg != "methods" or not isinstance(keyword.value, (ast.List, ast.Tuple)):
            continue
        methods = []
        for item in keyword.value.elts:
            value = _extract_string(item).upper()
            if value:
                methods.append(value)
        return sorted(set(methods))
    return []


def _extract_add_url_rule_handler(call_node: ast.Call) -> str:
    for keyword in call_node.keywords or []:
        if keyword.arg == "view_func" and isinstance(keyword.value, ast.Name):
            return keyword.value.id

    if len(call_node.args) >= 3 and isinstance(call_node.args[2], ast.Name):
        return call_node.args[2].id
    if len(call_node.args) >= 2 and isinstance(call_node.args[1], ast.Name):
        return call_node.args[1].id
    return ""
