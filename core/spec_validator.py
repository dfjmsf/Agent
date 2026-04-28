"""
合同自检器 (Spec Validator) — Manager A-1

对 Manager 输出的 project_spec JSON 进行确定性交叉校验，
拦截 api_contracts / page_routes / module_interfaces / data_models 之间的矛盾。

设计原则：
  - 纯 Python 逻辑，零 LLM 调用
  - 只输出 Warning，不拦截流程（由调用方决定是否让 LLM 修正）
  - 每条规则独立，方便后期增删
"""
import copy
import re
import logging
from typing import Dict, List, Any
from core.spec_compiler import _join_paths, _normalize_path

logger = logging.getLogger("SpecValidator")

R2_FRAMEWORK_TYPES = {
    "SQLAlchemy", "JWTManager", "Blueprint", "FileStorage",
    "Flask", "FastAPI", "APIRouter", "BaseModel",
    "Session", "SessionLocal", "MetaData",
    "Column", "Integer", "String", "Text", "Boolean", "DateTime",
    "Float", "ForeignKey", "Response", "Request", "JSONResponse", "HTMLResponse",
}


class ValidationWarning:
    """单条校验警告"""
    __slots__ = ("rule", "severity", "message")

    def __init__(self, rule: str, severity: str, message: str):
        self.rule = rule          # 规则编号，如 "R1", "R2"
        self.severity = severity  # "warning" | "error"
        self.message = message

    def __repr__(self):
        icon = "⚠️" if self.severity == "warning" else "❌"
        return f"[{self.rule}] {icon} {self.message}"


def normalize_spec(spec: dict) -> dict:
    """
    对 project_spec 做确定性归一化，修正易导致下游 Agent 互相打架的契约表述。

    当前只处理一类高频问题：
    - models.py 暴露给 routes.py 直接 import 的 CRUD helper，
      统一按 dict / list[dict] 语义声明返回类型。
    """
    if not isinstance(spec, dict):
        return spec

    normalized = copy.deepcopy(spec)
    interfaces = normalized.get("module_interfaces")
    if not isinstance(interfaces, dict):
        return normalized

    model_names = {
        str(model.get("name", "")).strip()
        for model in (normalized.get("data_models", []) or [])
        if str(model.get("name", "")).strip()
    }
    if not model_names:
        return normalized

    for module_name, signature_blob in list(interfaces.items()):
        if not _is_models_module(module_name) or not isinstance(signature_blob, str):
            continue
        interfaces[module_name] = _normalize_models_interface_returns(signature_blob, model_names)

    return normalized


def validate_spec(spec: dict) -> List[ValidationWarning]:
    """
    对 project_spec 执行所有校验规则，返回警告列表。
    空列表 = 无问题。

    Args:
        spec: Manager 输出的 project_spec dict

    Returns:
        List[ValidationWarning]
    """
    if not isinstance(spec, dict):
        return [ValidationWarning("R0", "error", "project_spec 不是有效的 dict")]

    warnings: List[ValidationWarning] = []

    # 依次运行各规则
    warnings.extend(_r1_api_vs_page_routes(spec))
    warnings.extend(_r2_interfaces_vs_models(spec))
    warnings.extend(_r3_http_method_semantics(spec))
    warnings.extend(_r4_page_routes_completeness(spec))
    warnings.extend(_r5_template_contracts_consistency(spec))
    warnings.extend(_r6_module_interfaces_coverage(spec))
    warnings.extend(_r7_cross_module_naming_collision(spec))
    warnings.extend(_r8_scale_sufficiency(spec))
    warnings.extend(_r9_blueprint_mount_closure(spec))
    warnings.extend(_r10_effective_route_consistency(spec))
    warnings.extend(_r11_app_registration_closure(spec))
    warnings.extend(_r12_route_module_contract_closure(spec))
    warnings.extend(_r13_architecture_contract_closure(spec))

    if warnings:
        logger.warning(f"📋 [SpecValidator] 检出 {len(warnings)} 条合同警告:")
        for w in warnings:
            logger.warning(f"  {w}")
    else:
        logger.info("✅ [SpecValidator] 规划书合同自检通过，零矛盾")

    return warnings


def has_blocking_warnings(warnings: List[ValidationWarning]) -> bool:
    """是否存在必须阻断执行链的规划层错误。"""
    return any(w.severity == "error" for w in (warnings or []))


def _is_models_module(module_name: str) -> bool:
    normalized = str(module_name).replace("\\", "/").lower()
    return normalized.endswith("models.py")


def _normalize_models_interface_returns(signature_blob: str, model_names: set[str]) -> str:
    def _replace(match: re.Match) -> str:
        prefix = match.group(1)
        return_type = match.group(2)
        normalized_return = _normalize_return_annotation(return_type, model_names)
        return f"{prefix}{normalized_return}"

    return re.sub(r"(def\s+\w+\([^)]*\)\s*->\s*)([^;]+)", _replace, signature_blob)


def _normalize_return_annotation(annotation: str, model_names: set[str]) -> str:
    normalized = " ".join(str(annotation).split())

    for model_name in sorted(model_names, key=len, reverse=True):
        normalized = re.sub(rf"\b{re.escape(model_name)}\b", "dict", normalized)

    normalized = re.sub(r"\bList\s*\[\s*dict\s*\]", "list[dict]", normalized)
    normalized = re.sub(r"\blist\s*\[\s*dict\s*\]", "list[dict]", normalized)
    normalized = re.sub(r"\bOptional\s*\[\s*dict\s*\]", "dict | None", normalized)
    normalized = re.sub(r"\bOptional\s*\[\s*list\[dict\]\s*\]", "list[dict] | None", normalized)
    normalized = re.sub(r"\bUnion\s*\[\s*dict\s*,\s*None\s*\]", "dict | None", normalized)
    normalized = re.sub(r"\bUnion\s*\[\s*None\s*,\s*dict\s*\]", "dict | None", normalized)
    normalized = re.sub(r"\bUnion\s*\[\s*list\[dict\]\s*,\s*None\s*\]", "list[dict] | None", normalized)
    normalized = re.sub(r"\bUnion\s*\[\s*None\s*,\s*list\[dict\]\s*\]", "list[dict] | None", normalized)

    normalized = re.sub(r"\s+", " ", normalized).strip()
    normalized = normalized.replace("None | dict", "dict | None")
    normalized = normalized.replace("None | list[dict]", "list[dict] | None")
    return normalized


# ============================================================
# R1: api_contracts.path 不应与 page_routes.path 冲突
# ============================================================

def _r1_api_vs_page_routes(spec: dict) -> List[ValidationWarning]:
    """
    检查 api_contracts 和 page_routes 的 path 是否存在
    不合理的重叠（同一 path 同时出现在两边 → 前后端模式混淆）。
    """
    warnings = []
    api_contracts = spec.get("api_contracts", [])
    page_routes = spec.get("page_routes", [])

    if not api_contracts or not page_routes:
        return warnings

    api_paths = set()
    for api in api_contracts:
        path = api.get("path", "")
        if path:
            # 规范化：去除尾部斜杠，统一参数格式
            api_paths.add(_normalize_path(path))

    for route in page_routes:
        path = route.get("path", "")
        if path:
            norm = _normalize_path(path)
            if norm in api_paths:
                warnings.append(ValidationWarning(
                    "R1", "warning",
                    f"路径 '{path}' 同时出现在 api_contracts 和 page_routes 中 — "
                    f"可能导致前后端模式混用（API JSON 返回 vs 模板渲染冲突）"
                ))

    return warnings


# ============================================================
# R2: module_interfaces 参数名 vs data_models 字段名
# ============================================================

def _r2_interfaces_vs_models(spec: dict) -> List[ValidationWarning]:
    """
    检查 module_interfaces 中暴露的 CRUD 函数参数是否引用了
    data_models 中不存在的字段名（常见的拼写错误/命名不一致）。
    """
    warnings = []
    interfaces = spec.get("module_interfaces", {})
    data_models = spec.get("data_models", [])

    if not interfaces or not data_models:
        return warnings

    # 提取所有模型的字段名集合
    all_model_fields = set()
    model_names = set()
    for model in data_models:
        name = model.get("name", "")
        if name:
            model_names.add(name.lower())
        fields_str = model.get("fields", "")
        if isinstance(fields_str, str):
            # 解析 "id:int, name:str, amount:float" 格式
            for field_def in fields_str.split(","):
                field_name = field_def.strip().split(":")[0].strip()
                if field_name:
                    all_model_fields.add(field_name.lower())

    # 检查 module_interfaces 中的参数是否引用了模型名但拼写不一致
    for module, sig in interfaces.items():
        if not isinstance(sig, str):
            continue
        # 提取签名中可能的类型引用（如 "def add_expense(expense: Expense)")
        type_refs = re.findall(r':\s*([A-Z][A-Za-z0-9_]*)', sig)
        for ref in type_refs:
            if ref in R2_FRAMEWORK_TYPES:
                continue
            if ref.lower() not in model_names and ref not in (
                'str', 'int', 'float', 'bool', 'list', 'dict', 'None',
                'Optional', 'List', 'Dict', 'Any', 'Tuple', 'Set',
                'Response', 'Request', 'JSONResponse', 'HTMLResponse',
            ):
                # 可能是引用了不存在的模型
                warnings.append(ValidationWarning(
                    "R2", "warning",
                    f"module_interfaces['{module}'] 引用了类型 '{ref}'，"
                    f"但 data_models 中没有同名模型 (已有: {', '.join(m.get('name', '') for m in data_models)})"
                ))

    return warnings


# ============================================================
# R3: HTTP method 语义匹配
# ============================================================

_METHOD_SEMANTICS = {
    "GET": {"查询", "获取", "列表", "详情", "首页", "页面", "展示", "list", "get", "show", "index", "detail", "read"},
    "POST": {"创建", "添加", "新增", "提交", "注册", "登录", "add", "create", "submit", "register", "login"},
    "PUT": {"更新", "修改", "编辑", "update", "edit", "modify"},
    "PATCH": {"更新", "修改", "部分更新", "patch", "update"},
    "DELETE": {"删除", "移除", "remove", "delete", "destroy"},
}


def _r3_http_method_semantics(spec: dict) -> List[ValidationWarning]:
    """
    检查 api_contracts 中 HTTP method 是否与路径/描述的语义匹配。
    例如：DELETE 操作不应该用 GET method。
    """
    warnings = []
    api_contracts = spec.get("api_contracts", [])

    for api in api_contracts:
        method = (api.get("method", "") or "").upper()
        path = (api.get("path", "") or "").lower()

        if not method or not path:
            continue

        # 从路径中提取语义线索
        path_parts = path.replace("/", " ").replace("-", " ").replace("_", " ").split()

        # 检查明显的语义错误
        if method == "GET":
            delete_signals = {"delete", "remove", "destroy"}
            if any(part in delete_signals for part in path_parts):
                warnings.append(ValidationWarning(
                    "R3", "warning",
                    f"api_contracts 路径 '{api.get('path')}' 包含删除语义 "
                    f"但使用了 GET 方法 — 应改为 DELETE"
                ))
        elif method == "DELETE":
            create_signals = {"create", "add", "new", "register"}
            if any(part in create_signals for part in path_parts):
                warnings.append(ValidationWarning(
                    "R3", "warning",
                    f"api_contracts 路径 '{api.get('path')}' 包含创建语义 "
                    f"但使用了 DELETE 方法 — 应改为 POST"
                ))

    return warnings


# ============================================================
# R4: page_routes 完整性校验
# ============================================================

def _r4_page_routes_completeness(spec: dict) -> List[ValidationWarning]:
    """
    检查 page_routes 中 GET 路由是否都有 renders 和 template_vars，
    POST 路由是否都有 form_fields 和 redirects_to。
    """
    warnings = []
    page_routes = spec.get("page_routes", [])

    for route in page_routes:
        method = (route.get("method", "") or "").upper()
        path = route.get("path", "")
        func = route.get("function", "")

        if "GET" in method:
            if not route.get("renders"):
                warnings.append(ValidationWarning(
                    "R4", "warning",
                    f"page_routes GET '{path}' (function={func}) 缺少 renders 字段 — "
                    f"Coder 不知道渲染哪个模板"
                ))
            if not route.get("template_vars"):
                warnings.append(ValidationWarning(
                    "R4", "warning",
                    f"page_routes GET '{path}' (function={func}) 缺少 template_vars 字段 — "
                    f"Coder 不知道传什么变量给模板"
                ))

        if "POST" in method:
            # 跳过仅靠路径参数操作的路由（如 /delete/<id>）—— 它们不需要 form_fields
            action_only_signals = {"delete", "remove", "toggle", "activate", "deactivate", "archive"}
            path_lower = (path or "").lower()
            is_action_only = any(sig in path_lower for sig in action_only_signals)

            if not is_action_only and not route.get("form_fields") and not route.get("request_params"):
                warnings.append(ValidationWarning(
                    "R4", "warning",
                    f"page_routes POST '{path}' (function={func}) 缺少 form_fields 字段 — "
                    f"Coder 不知道接收哪些表单字段"
                ))

    return warnings


# ============================================================
# R5: template_contracts 与 page_routes 一致性
# ============================================================

def _r5_template_contracts_consistency(spec: dict) -> List[ValidationWarning]:
    """
    检查 template_contracts 引用的模板是否在 page_routes 的 renders 中被使用，
    以及 receives 是否与 template_vars 对齐。
    """
    warnings = []
    page_routes = spec.get("page_routes", [])
    template_contracts = spec.get("template_contracts", {})

    if not page_routes or not template_contracts:
        return warnings

    # 收集 page_routes 中 renders 引用的所有模板
    rendered_templates = {}
    for route in page_routes:
        renders = route.get("renders", "")
        if renders:
            template_vars = route.get("template_vars", [])
            rendered_templates[renders] = _safe_set(template_vars) if template_vars else set()

    # 检查 template_contracts 中的模板是否被引用
    for template_name, contract in template_contracts.items():
        if not isinstance(contract, dict):
            continue
        # 跳过 layout 类型（base.html 不需要被 route 直接引用）
        if contract.get("type") == "layout":
            continue
        if template_name not in rendered_templates:
            warnings.append(ValidationWarning(
                "R5", "warning",
                f"template_contracts 定义了 '{template_name}'，"
                f"但没有任何 page_routes 的 renders 引用它"
            ))
        else:
            # 检查变量对齐
            receives = _safe_set(contract.get("receives", []))
            route_vars = rendered_templates[template_name]
            if receives and route_vars:
                missing_in_template = route_vars - receives
                if missing_in_template:
                    warnings.append(ValidationWarning(
                        "R5", "warning",
                        f"模板 '{template_name}' receives 中缺少变量: {missing_in_template}，"
                        f"但 page_routes 的 template_vars 传了它们"
                    ))

    return warnings


# ============================================================
# R6: module_interfaces 覆盖度
# ============================================================

def _r6_module_interfaces_coverage(spec: dict) -> List[ValidationWarning]:
    """
    检查 page_routes / api_contracts 引用的 function 名是否
    能在 module_interfaces 中找到对应的定义。
    """
    warnings = []
    interfaces = spec.get("module_interfaces", {})
    page_routes = spec.get("page_routes", [])
    api_contracts = spec.get("api_contracts", [])

    if not interfaces:
        return warnings

    # 将所有 interfaces 的签名文本合并为一个大字符串用于模糊匹配
    all_signatures = " ".join(str(v) for v in interfaces.values()).lower()

    # 检查 page_routes 的 function 是否在 interfaces 中有定义
    for route in page_routes:
        func_name = route.get("function", "")
        if func_name and func_name.lower() not in all_signatures:
            warnings.append(ValidationWarning(
                "R6", "warning",
                f"page_routes 引用了函数 '{func_name}'，"
                f"但 module_interfaces 中未找到匹配的定义"
            ))

    return warnings


# ============================================================
# R7: 跨模块函数命名冲突
# ============================================================

def _r7_cross_module_naming_collision(spec: dict) -> List[ValidationWarning]:
    """
    检查 routes.py 和 models.py (或同类型文件) 中是否存在完全同名的函数。
    路由函数常常被错误地命名为与其调用的底层模型函数相同（如 add_expense），
    这会导致 Coder 在 routes.py 导入 models 的 add_expense 时发生本地作用域覆写冲突。
    """
    warnings = []
    interfaces = spec.get("module_interfaces", {})
    if not interfaces:
        return warnings

    # 分离 routes 层和 models 层（启发式匹配）
    route_keys = [k for k in interfaces.keys() if "route" in k.lower() or "controller" in k.lower() or "view" in k.lower() or k == "app.py"]
    model_keys = [k for k in interfaces.keys() if "model" in k.lower() or "db" in k.lower() or "database" in k.lower()]

    def _extract_func_names(sig_string: str) -> set:
        if not isinstance(sig_string, str):
            return set()
        # 匹配 "def my_func(" 或 "def my_func ("
        return set(re.findall(r'def\s+([a-zA-Z0-9_]+)\s*\(', sig_string))

    for r_key in route_keys:
        r_sigs = interfaces[r_key]
        r_funcs = _extract_func_names(r_sigs)
        
        for m_key in model_keys:
            m_sigs = interfaces[m_key]
            m_funcs = _extract_func_names(m_sigs)
            
            collisions = r_funcs.intersection(m_funcs)
            for func in collisions:
                warnings.append(ValidationWarning(
                    "R7", "warning",
                    f"存在潜在的跨模块命名冲突: 函数 '{func}' 同时定义在 '{r_key}' 和 '{m_key}' 中。 "
                    f"请将 '{r_key}' 中的函数重命名(例如改为 {func}_route) 以避免在导入时发生作用域覆盖引发崩溃。"
                ))

    return warnings


# ============================================================
# R8: project_spec 规模充分性校验
# ============================================================

def _r8_scale_sufficiency(spec: dict) -> List[ValidationWarning]:
    """
    检查 project_spec 是否明显过 sparse，避免中型项目被错误压缩成极少模块。
    这条规则不关心字段之间是否矛盾，而是检查规模信号是否合理。
    """
    warnings = []

    interfaces = spec.get("module_interfaces", {}) or {}
    mi_count = len(interfaces)
    api_count = len(spec.get("api_contracts", []) or [])
    model_count = len(spec.get("data_models", []) or [])
    page_count = _count_unique_page_routes(spec)
    template_count = _count_non_layout_templates(spec)
    has_frontend, has_backend = _detect_stack_shape(spec)

    reasons = []

    if has_frontend and has_backend and mi_count < 6:
        reasons.append(
            f"当前是前后端分离形态，但 module_interfaces 只有 {mi_count} 个，低于基础模块下限 6"
        )

    if api_count >= 8:
        expected_api_floor = max(6, (api_count + 1) // 2)
        if mi_count < expected_api_floor:
            reasons.append(
                f"api_contracts 有 {api_count} 个，但 module_interfaces 少于按 API 规模推断的下限 {expected_api_floor}"
            )

    if model_count >= 5:
        expected_model_floor = model_count + 2
        if mi_count < expected_model_floor:
            reasons.append(
                f"data_models 有 {model_count} 个，但 module_interfaces 少于按模型规模推断的下限 {expected_model_floor}"
            )

    if (page_count >= 4 or template_count >= 4) and mi_count < 6:
        reasons.append(
            f"页面/模板规模已达到 {max(page_count, template_count)}，但 module_interfaces 仍少于 6"
        )

    total_surface = api_count + model_count + page_count + template_count
    if total_surface >= 16 and mi_count < 8:
        reasons.append(
            f"接口/模型/页面总规模信号为 {total_surface}，但 module_interfaces 仍少于 8"
        )

    if reasons:
        warnings.append(ValidationWarning(
            "R8", "warning",
            "project_spec 规模疑似过 sparse："
            + "；".join(reasons)
            + "。建议补齐模块拆分，避免把中型项目压成少量超大文件。"
        ))

    return warnings


# ============================================================
# R9: blueprint_mounts 闭环
# ============================================================

def _r9_blueprint_mount_closure(spec: dict) -> List[ValidationWarning]:
    warnings = []
    route_contracts = spec.get("route_contracts", []) or []
    blueprint_mounts = spec.get("blueprint_mounts", []) or []
    mounted = {
        item.get("blueprint")
        for item in blueprint_mounts
        if isinstance(item, dict) and item.get("blueprint")
    }

    for contract in route_contracts:
        if not isinstance(contract, dict):
            continue
        blueprint = contract.get("blueprint")
        if blueprint and blueprint not in mounted:
            warnings.append(ValidationWarning(
                "R9", "error",
                f"route_contract '{contract.get('handler') or contract.get('effective_path')}' "
                f"引用了未挂载的 blueprint '{blueprint}'"
            ))

    return warnings


# ============================================================
# R10: effective_path 闭环
# ============================================================

def _r10_effective_route_consistency(spec: dict) -> List[ValidationWarning]:
    warnings = []
    route_contracts = spec.get("route_contracts", []) or []
    blueprint_mounts = spec.get("blueprint_mounts", []) or []
    mounts = {
        item.get("blueprint"): item
        for item in blueprint_mounts
        if isinstance(item, dict) and item.get("blueprint")
    }

    for contract in route_contracts:
        if not isinstance(contract, dict):
            continue
        blueprint = contract.get("blueprint")
        local_path = contract.get("local_path")
        effective_path = contract.get("effective_path")
        if not blueprint or local_path is None or not effective_path:
            continue
        mount = mounts.get(blueprint)
        if not mount:
            continue
        derived = _join_paths(mount.get("url_prefix", ""), local_path)
        if _normalize_path(derived) != _normalize_path(effective_path):
            warnings.append(ValidationWarning(
                "R10", "error",
                f"route_contract '{contract.get('handler') or effective_path}' 不闭合："
                f"url_prefix='{mount.get('url_prefix', '')}' + local_path='{local_path}' "
                f"推导为 '{derived}'，但 effective_path 是 '{effective_path}'"
            ))

    compiler_errors = (
        ((spec.get("compiler_metadata") or {}).get("contract_closure_errors")) or []
    )
    for error in compiler_errors:
        warnings.append(ValidationWarning("R10", "error", str(error)))

    return warnings


# ============================================================
# R11: app 注册闭环
# ============================================================

def _r11_app_registration_closure(spec: dict) -> List[ValidationWarning]:
    warnings = []
    blueprint_mounts = spec.get("blueprint_mounts", []) or []
    registrations = spec.get("app_registration_contracts", []) or []

    mount_map = {
        item.get("blueprint"): item
        for item in blueprint_mounts
        if isinstance(item, dict) and item.get("blueprint")
    }
    reg_map = {
        item.get("blueprint"): item
        for item in registrations
        if isinstance(item, dict) and item.get("blueprint")
    }

    for blueprint, mount in mount_map.items():
        reg = reg_map.get(blueprint)
        if not reg:
            warnings.append(ValidationWarning(
                "R11", "error",
                f"blueprint '{blueprint}' 缺少 app_registration_contract"
            ))
            continue
        if _normalize_path(reg.get("url_prefix", "")) != _normalize_path(mount.get("url_prefix", "")):
            warnings.append(ValidationWarning(
                "R11", "error",
                f"blueprint '{blueprint}' 的 app 注册前缀 "
                f"'{reg.get('url_prefix', '')}' 与挂载前缀 '{mount.get('url_prefix', '')}' 不一致"
            ))
        if not reg.get("app_module"):
            warnings.append(ValidationWarning(
                "R11", "error",
                f"blueprint '{blueprint}' 已有挂载前缀，但无法定位 app 注册入口文件"
            ))

    return warnings


# ============================================================
# R12: route module implementation mode closure
# ============================================================

def _r12_route_module_contract_closure(spec: dict) -> List[ValidationWarning]:
    warnings = []
    interfaces = spec.get("module_interfaces", {}) or {}
    route_module_contracts = spec.get("route_module_contracts", {}) or {}
    route_contracts = spec.get("route_contracts", []) or []

    route_count_by_module: Dict[str, int] = {}
    blueprint_count_by_module: Dict[str, int] = {}
    for item in route_contracts:
        if not isinstance(item, dict):
            continue
        module = item.get("module")
        if not module:
            continue
        route_count_by_module[module] = route_count_by_module.get(module, 0) + 1
        if item.get("blueprint"):
            blueprint_count_by_module[module] = blueprint_count_by_module.get(module, 0) + 1

    candidate_modules = set(route_module_contracts.keys())
    for module_name in interfaces.keys():
        normalized = str(module_name).replace("\\", "/").lower()
        if normalized.endswith("routes.py") or "/routes/" in normalized:
            candidate_modules.add(module_name)

    for module_name in sorted(candidate_modules):
        contract = route_module_contracts.get(module_name, {}) or {}
        mode = str(contract.get("mode") or "unknown").strip().lower()
        blueprints = [item for item in (contract.get("blueprints", []) or []) if item]
        helper_functions = [item for item in (contract.get("helper_functions", []) or []) if item]
        route_count = route_count_by_module.get(module_name, 0)
        blueprint_route_count = blueprint_count_by_module.get(module_name, 0)

        if mode == "mixed":
            warnings.append(ValidationWarning(
                "R12", "error",
                f"route module '{module_name}' 同时混用了 init/register helper 与直接路由声明，"
                f"必须在 `direct_blueprint` 与 `init_function` 之间二选一"
            ))
            continue

        if mode == "unknown":
            warnings.append(ValidationWarning(
                "R12", "error",
                f"route module '{module_name}' 缺少明确实现范式，无法判断应采用 "
                f"`direct_blueprint` 还是 `init_function`"
            ))
            continue

        if mode == "init_function":
            if not helper_functions:
                warnings.append(ValidationWarning(
                    "R12", "error",
                    f"route module '{module_name}' 标记为 `init_function`，"
                    f"但未声明任何 init/register helper"
                ))
            if not blueprints and blueprint_route_count == 0:
                warnings.append(ValidationWarning(
                    "R12", "error",
                    f"route module '{module_name}' 标记为 `init_function`，"
                    f"但未声明 blueprint 变量"
                ))
            if route_count == 0:
                warnings.append(ValidationWarning(
                    "R12", "error",
                    f"route module '{module_name}' 只有 init/register helper，"
                    f"但没有任何可执行的 route_contracts；规划契约不闭环"
                ))
            continue

        if mode == "direct_blueprint":
            if not blueprints and blueprint_route_count == 0:
                warnings.append(ValidationWarning(
                    "R12", "error",
                    f"route module '{module_name}' 标记为 `direct_blueprint`，"
                    f"但未声明 blueprint 变量"
                ))
            if route_count == 0:
                warnings.append(ValidationWarning(
                    "R12", "warning",
                    f"route module '{module_name}' 标记为 `direct_blueprint`，"
                    f"但 route_contracts 数量为 0；后续执行易退化为猜测式生成"
                ))

    return warnings


# ============================================================
# R13: single-stack architecture closure
# ============================================================

def _r13_architecture_contract_closure(spec: dict) -> List[ValidationWarning]:
    warnings = []
    _, has_backend = _detect_stack_shape(spec)
    if not has_backend:
        return warnings

    contract = spec.get("architecture_contract", {}) or {}
    metadata = spec.get("compiler_metadata", {}) or {}
    signals = metadata.get("architecture_signals", {}) or {}

    backend_framework = str(contract.get("backend_framework") or "unknown").lower()
    orm_mode = str(contract.get("orm_mode") or "unknown").lower()
    auth_mode = str(contract.get("auth_mode") or "unknown").lower()
    router_mode = str(contract.get("router_mode") or "unknown").lower()
    entrypoint_mode = str(contract.get("entrypoint_mode") or "unknown").lower()
    package_layout = str(contract.get("package_layout") or "unknown").lower()
    import_style = str(contract.get("import_style") or "unknown").lower()

    if backend_framework in {"unknown", "mixed"}:
        warnings.append(ValidationWarning(
            "R13", "error",
            "后端主框架未闭环：无法确定当前项目应使用 `FastAPI` 还是 `Flask`，或两者信号同时存在"
        ))

    if router_mode in {"unknown", "mixed"}:
        warnings.append(ValidationWarning(
            "R13", "error",
            "路由范式未闭环：无法确定当前项目应使用 `APIRouter` 还是 `Blueprint`，或两者信号同时存在"
        ))

    if entrypoint_mode in {"unknown", "mixed"}:
        warnings.append(ValidationWarning(
            "R13", "error",
            "入口范式未闭环：无法确定当前项目应使用 `uvicorn/FastAPI` 入口还是 `Flask app factory`"
        ))

    if _has_persistence_contract(spec) and orm_mode in {"unknown", "mixed"}:
        warnings.append(ValidationWarning(
            "R13", "error",
            "ORM 范式未闭环：检测到持久层需求，但无法确定应使用 `sqlalchemy session` 还是 `Flask-SQLAlchemy`"
        ))

    if backend_framework == "fastapi":
        if router_mode != "fastapi_apirouter":
            warnings.append(ValidationWarning(
                "R13", "error",
                f"架构契约冲突：backend_framework=fastapi，但 router_mode={router_mode}"
            ))
        if entrypoint_mode != "uvicorn_app":
            warnings.append(ValidationWarning(
                "R13", "error",
                f"架构契约冲突：backend_framework=fastapi，但 entrypoint_mode={entrypoint_mode}"
            ))
        if auth_mode == "flask_login_session":
            warnings.append(ValidationWarning(
                "R13", "error",
                "架构契约冲突：FastAPI 项目不应使用 `flask_login_session` 作为认证范式"
            ))

    if backend_framework == "flask":
        if router_mode != "flask_blueprint":
            warnings.append(ValidationWarning(
                "R13", "error",
                f"架构契约冲突：backend_framework=flask，但 router_mode={router_mode}"
            ))
        if entrypoint_mode != "flask_app_factory":
            warnings.append(ValidationWarning(
                "R13", "error",
                f"架构契约冲突：backend_framework=flask，但 entrypoint_mode={entrypoint_mode}"
            ))

    if signals.get("fastapi_signal") and signals.get("flask_signal"):
        warnings.append(ValidationWarning(
            "R13", "error",
            "检测到 `FastAPI` 与 `Flask` 语义同时存在：规划层已出现混合栈"
        ))

    if signals.get("sqlalchemy_session_signal") and signals.get("flask_sqlalchemy_signal"):
        warnings.append(ValidationWarning(
            "R13", "error",
            "检测到 `sqlalchemy session` 与 `Flask-SQLAlchemy` 语义同时存在：持久层范式混用"
        ))

    if signals.get("fastapi_signal") and signals.get("flask_sqlalchemy_signal"):
        warnings.append(ValidationWarning(
            "R13", "error",
            "检测到 `FastAPI` 与 `Flask-SQLAlchemy` 同时存在：后端运行时范式不兼容"
        ))

    if signals.get("jwt_signal") and signals.get("flask_login_signal"):
        warnings.append(ValidationWarning(
            "R13", "error",
            "检测到 `JWT header` 与 `Flask-Login session` 语义同时存在：认证范式混用"
        ))

    if import_style == "mixed" or (signals.get("package_import_signal") and signals.get("sibling_import_signal")):
        warnings.append(ValidationWarning(
            "R13", "error",
            "检测到本地模块导入风格混用：同时存在 package_import 与 sibling_import"
        ))

    if package_layout == "flat_modules" and signals.get("package_import_signal"):
        warnings.append(ValidationWarning(
            "R13", "error",
            "package_layout=flat_modules，但规划内容包含 `from src...` 或相对包导入；运行时大概率无法解析"
        ))

    return warnings


# ============================================================
# 辅助函数
# ============================================================

def _count_unique_page_routes(spec: dict) -> int:
    """按 path 粗略估算页面面数，避免同一路径的 GET/POST 重复计数。"""
    routes = spec.get("page_routes", []) or []
    unique_paths = {
        _normalize_path(route.get("path", ""))
        for route in routes
        if route.get("path")
    }
    return len(unique_paths)


def _count_non_layout_templates(spec: dict) -> int:
    """统计真正承载页面内容的模板数量，排除 layout/base 模板。"""
    contracts = spec.get("template_contracts", {}) or {}
    count = 0
    for contract in contracts.values():
        if not isinstance(contract, dict):
            continue
        if contract.get("type") == "layout":
            continue
        count += 1
    return count


def _detect_stack_shape(spec: dict) -> tuple[bool, bool]:
    """根据 tech_stack 和模块命名判断是否是前后端分离形态。"""
    tech_stack = [str(item).lower() for item in (spec.get("tech_stack", []) or [])]
    interface_keys = [str(key).lower() for key in (spec.get("module_interfaces", {}) or {}).keys()]

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
        or len(spec.get("api_contracts", []) or []) > 0
        or len(spec.get("data_models", []) or []) > 0
    )

    return has_frontend, has_backend


def _has_persistence_contract(spec: dict) -> bool:
    tech_stack = [str(item).lower() for item in (spec.get("tech_stack", []) or [])]
    if spec.get("data_models"):
        return True
    return any(
        token in tech
        for tech in tech_stack
        for token in ("sqlalchemy", "sqlite", "postgres", "mysql")
    )

def _safe_set(items) -> set:
    """将列表转为 set，兼容 LLM 返回的 dict 元素（提取 name 字段或 str 转换）。"""
    result = set()
    if not items:
        return result
    for item in items:
        if isinstance(item, str):
            result.add(item)
        elif isinstance(item, dict):
            # LLM 可能返回 {"name": "expenses", "type": "list"} 格式
            name = item.get("name") or item.get("key") or str(item)
            result.add(str(name))
        else:
            result.add(str(item))
    return result


def _normalize_path(path: str) -> str:
    """规范化路径：去尾斜杠，统一参数占位符"""
    path = path.rstrip("/")
    # 将 <id>、{id}、:id 等参数格式统一为 <param>
    path = re.sub(r'[{<:](\w+)[}>]?', '<param>', path)
    return path.lower()


def format_warnings_for_llm(warnings: List[ValidationWarning]) -> str:
    """将警告列表格式化为 LLM 可读的修正指令"""
    if not warnings:
        return ""

    lines = ["【⚠️ 规划书合同自检发现以下矛盾，请修正后重新输出完整 JSON】\n"]
    for i, w in enumerate(warnings, 1):
        lines.append(f"{i}. [{w.rule}] {w.message}")
    lines.append("\n请修正上述问题后，输出修正后的完整 project_spec JSON。")
    return "\n".join(lines)


# ============================================================
# 主入口（自测用）
# ============================================================

if __name__ == "__main__":
    # 简单自测
    test_spec = {
        "project_name": "TestApp",
        "tech_stack": ["Flask", "SQLite"],
        "api_contracts": [
            {"path": "/api/items", "method": "GET"},
            {"path": "/api/items", "method": "POST"},
            {"path": "/api/delete/<id>", "method": "GET"},  # ← R3 应该报
        ],
        "page_routes": [
            {"method": "GET", "path": "/", "function": "index", "renders": "templates/index.html", "template_vars": ["items"]},
            {"method": "POST", "path": "/add", "function": "add_item"},  # ← R4 缺 form_fields
        ],
        "template_contracts": {
            "templates/base.html": {"type": "layout", "blocks": ["title", "content"]},
            "templates/index.html": {"extends": "base.html", "receives": ["items"]},
            "templates/edit.html": {"extends": "base.html", "receives": ["item"]},  # ← R5 无人引用
        },
        "module_interfaces": {
            "models.py": "def get_all_items() -> list; def save_item(name: str, price: float) -> None",
            "routes.py": "def index() -> html; def add_item() -> redirect",
        },
        "data_models": [
            {"name": "Item", "fields": "id:int, name:str, price:float"}
        ]
    }

    results = validate_spec(test_spec)
    print(f"\n共 {len(results)} 条警告:")
    for r in results:
        print(f"  {r}")

    if results:
        print("\n===== LLM 修正指令 =====")
        print(format_warnings_for_llm(results))
