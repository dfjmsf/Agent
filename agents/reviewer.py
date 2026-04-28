"""
Reviewer Agent v3 — L0 静态检查 + L1 合约审计

v2 → v3 变更：
- 删除: LLM 生成测试脚本 + 沙盒执行 + 三层自愈（移交 IntegrationTester）
- 新增: L0 确定性静态检查（语法+结构+导入，0 LLM 消耗）
- 新增: L1 轻量 LLM 审查（只读合约审计，~800 tokens）
"""
import os
import re
import ast
import json
import logging
from typing import Tuple

from core.llm_client import default_llm
from core.prompt import Prompts
from core.route_topology import (
    extract_contract_handlers,
    extract_blueprint_registrations_from_code,
    extract_blueprint_variables_from_code,
    extract_expected_symbols_for_target,
    extract_module_interface_handlers,
    extract_route_bindings_from_code,
    extract_top_level_function_names,
    is_non_endpoint_helper,
    looks_like_endpoint_function,
)
from tools.sandbox import sandbox_env
from core.ws_broadcaster import global_broadcaster
from core.database import get_recent_events, recall_reviewer_experience

logger = logging.getLogger("ReviewerAgent")


class ReviewerAgent:
    """
    审查 Agent (Reviewer v3 - Lite)
    L0: 静态检查（语法 + 结构 + 导入）— 0 LLM 消耗
    L1: 合约审计（LLM 只读审查）— ~800 tokens
    """
    def __init__(self, project_id: str = "default_project"):
        self.model = os.getenv("MODEL_REVIEWER", "qwen3-max")
        _et, _re = default_llm.parse_thinking_config(os.getenv("THINKING_REVIEWER", "false"))
        self.enable_thinking = _et
        self._reasoning_effort = _re
        self.project_id = project_id

    # ============================================================
    # L0: 静态检查（确定性，0 LLM 消耗）
    # ============================================================

    def _l0_static_check(self, target_file: str, code_content: str,
                         sandbox_dir: str, expected_symbols: list,
                         project_spec: dict = None,
                         allow_skeleton_placeholders: bool = False,
                         is_continue_fix: bool = False) -> Tuple[bool, str]:
        """
        L0 静态检查（HARD/SOFT 分级）：
          HARD: 失败直接阻断（纯 AST 单文件，确定性 ~100%）
          SOFT: 失败记录 warning 但不阻断（跨文件启发式，可能有假阳性）

        Returns: (passed, error_msg)
        """
        ext = os.path.splitext(target_file)[1].lower()
        is_python = ext == '.py'
        is_js = ext == '.js'
        is_vue = ext == '.vue'
        soft_warnings = []  # 收集 SOFT 级别的 warning

        # ═══ HARD 检查（失败 = 直接阻断）═══

        # --- [HARD] L0.0 骨架残留检测 ---
        if is_python and not allow_skeleton_placeholders:
            try:
                tree_pre = ast.parse(code_content)
                stub_funcs = []
                for node in ast.walk(tree_pre):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        body = node.body
                        if (body and isinstance(body[0], ast.Expr)
                                and isinstance(body[0].value, ast.Constant)
                                and isinstance(body[0].value.value, str)):
                            body = body[1:]
                        if len(body) == 1:
                            stmt = body[0]
                            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant):
                                if stmt.value.value is ...:
                                    stub_funcs.append(node.name)
                if stub_funcs:
                    error = (f"[L0.0 骨架残留] {target_file}: 以下函数仍是 `...` 占位未实现: "
                             f"{', '.join(stub_funcs)}。请将所有 `...` 替换为完整的业务实现代码。")
                    logger.warning(f"❌ {error}")
                    global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
                    return False, error
            except SyntaxError:
                pass

        # --- [HARD] L0.1 语法检查 ---
        tree = None
        if is_python:
            try:
                tree = ast.parse(code_content)
                logger.info(f"✅ [L0.1] 语法检查通过: {target_file}")
            except SyntaxError as e:
                error = f"[L0.1 语法错误] {target_file} 第 {e.lineno} 行: {e.msg}"
                logger.warning(f"❌ {error}")
                global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
                return False, error
        elif is_js:
            l01_pass, l01_error = self._l0_js_syntax_check(target_file, sandbox_dir)
            if not l01_pass:
                return False, l01_error
            logger.info(f"✅ [L0.1] JS 语法检查通过: {target_file}")
        else:
            if not code_content or not code_content.strip():
                error = f"[L0.1] {target_file} 内容为空"
                logger.warning(f"❌ {error}")
                return False, error
            logger.info(f"✅ [L0.1] 非 Python 文件，内容非空: {target_file}")

        # --- [HARD] L0.VUE 单文件组件结构检查 ---
        if is_vue:
            vue_pass, vue_error = self._l0_vue_sfc_check(target_file, code_content)
            if not vue_pass:
                return False, vue_error

        # --- [HARD] L0.2 结构检查 ---
        if is_python and expected_symbols and tree:
            defined = self._extract_defined_symbols(tree)
            missing = [s for s in expected_symbols if s not in defined]
            if missing:
                error = f"[L0.2 结构缺失] {target_file} 缺少规划书中定义的: {', '.join(missing)}"
                logger.warning(f"❌ {error}")
                global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
                return False, error
            logger.info(f"✅ [L0.2] 结构检查通过: {len(expected_symbols)} 个符号全部存在")

        if is_python and os.path.basename(target_file) == 'models.py':
            l02_orm_pass, l02_orm_error = self._l0_sqlalchemy_association_check(
                target_file, code_content, expected_symbols
            )
            if not l02_orm_pass:
                return False, l02_orm_error

        # --- [HARD] L0.3 导入检查 ---
        if is_python:
            l03_pass, l03_error = self._l0_import_check(target_file, sandbox_dir)
            if not l03_pass:
                return False, l03_error
            logger.info(f"✅ [L0.3] 导入检查通过: {target_file}")

        if is_python:
            l03a_pass, l03a_error = self._l0_architecture_contract_check(
                target_file, code_content, project_spec
            )
            if not l03a_pass:
                return False, l03a_error
            logger.info(f"✅ [L0.3A] 架构一致性检查通过: {target_file}")

        # --- [HARD] L0.4 FastAPI POST/PUT 参数检查 ---
        if is_python and tree:
            l04_pass, l04_error = self._l0_fastapi_param_check(tree, target_file, code_content)
            if not l04_pass:
                return False, l04_error

        # --- [HARD] L0.5 路由装饰器检查 ---
        if is_python and tree:
            l05_pass, l05_error = self._l0_route_decorator_check(tree, target_file, code_content)
            if not l05_pass:
                return False, l05_error

        # --- [HARD] L0.7 Flask template_folder 检查 ---
        if sandbox_dir and target_file.endswith('.py'):
            l07_pass, l07_error = self._l0_template_folder_check(
                target_file, code_content, sandbox_dir)
            if not l07_pass:
                return False, l07_error

        # --- [HARD] L0.10 种子数据检查 ---
        if is_python and tree:
            l10_pass, l10_error = self._l0_seed_data_check(target_file, code_content)
            if not l10_pass:
                return False, l10_error

        # --- [HARD] L0.12 models.py tuple 返回检测 ---
        if is_python and os.path.basename(target_file) == 'models.py':
            l12_pass, l12_error = self._l0_tuple_return_check(target_file, code_content)
            if not l12_pass:
                return False, l12_error

        # --- [SOFT/HARD] L0.15 命名空间覆盖检测 ---
        # v4.1: 首次生成 → SOFT（与 L0.2 结构完整性存在乒乓矛盾）
        # v4.3: Continue 修复模式 → HARD（修复阶段不触发 L0.2，可安全阻断）
        if is_python and tree:
            l15_pass, l15_error = self._l0_name_shadow_check(target_file, tree)
            if not l15_pass:
                if is_continue_fix:
                    # Continue 模式：命名冲突是已确认的致命 bug，HARD 阻断
                    logger.warning(f"❌ [HARD/Continue] {l15_error}")
                    global_broadcaster.emit_sync("Reviewer", "l0_fail", l15_error)
                    return False, l15_error
                else:
                    # 首次生成：保持 SOFT（避免与 L0.2 乒乓）
                    logger.warning(f"⚠️ [SOFT] {l15_error}")
                    soft_warnings.append(l15_error)

        # ═══ SOFT 检查（失败 = 记录 warning，不阻断）═══

        # --- [SOFT] L0.6 跨文件字段一致性检查 ---
        if sandbox_dir:
            l06_pass, l06_error = self._l0_cross_file_check(
                target_file, code_content, sandbox_dir)
            if not l06_pass:
                logger.warning(f"⚠️ [SOFT] {l06_error}")
                soft_warnings.append(l06_error)

        # --- [SOFT] L0.9 前后端 API 契约检查 ---
        if sandbox_dir:
            l09_pass, l09_error = self._l0_fetch_api_contract_check(
                target_file, code_content, sandbox_dir)
            if not l09_pass:
                logger.warning(f"⚠️ [SOFT] {l09_error}")
                soft_warnings.append(l09_error)

        # --- [SOFT] L0.11 前端 JS/TS AST 深度语义检查 ---
        FRONTEND_EXTS = {'.js', '.jsx', '.ts', '.tsx', '.vue'}
        if sandbox_dir and ext in FRONTEND_EXTS:
            l11_pass, l11_error = self._l0_js_ast_semantic_check(
                target_file, code_content, sandbox_dir)
            if not l11_pass:
                logger.warning(f"⚠️ [SOFT] {l11_error}")
                soft_warnings.append(l11_error)

        # --- [SOFT] L0.14 跨文件函数调用签名一致性检查 ---
        if is_python and sandbox_dir and tree:
            l14_pass, l14_error = self._l0_call_signature_check(
                target_file, code_content, sandbox_dir, tree)
            if not l14_pass:
                logger.warning(f"⚠️ [SOFT] {l14_error}")
                soft_warnings.append(l14_error)

        # 将 SOFT warnings 广播（供 Coder 下轮参考）
        if soft_warnings:
            warning_text = " | ".join(soft_warnings)
            logger.info(f"📋 [L0 SOFT] {target_file}: {len(soft_warnings)} 个 SOFT 警告（不阻断）")
            global_broadcaster.emit_sync("Reviewer", "l0_soft_warning",
                f"⚠️ L0 SOFT 警告 ({len(soft_warnings)}): {warning_text[:200]}")

        return True, ""

    def _l0_vue_sfc_check(self, target_file: str, code_content: str) -> Tuple[bool, str]:
        """Vue SFC 的确定性结构闸门，阻止只有 style 的坏文件通过。"""
        has_template = bool(re.search(r"<template(?:\s|>)", code_content, re.IGNORECASE))
        has_script = bool(re.search(r"<script(?:\s|>)", code_content, re.IGNORECASE))
        has_style = bool(re.search(r"<style(?:\s|>)", code_content, re.IGNORECASE))

        if not has_template and not has_script:
            detail = "当前文件只包含 <style> 块" if has_style else "未找到 <template>/<script> 块"
            error = (
                f"[L0.VUE SFC结构缺失] {target_file}: {detail}。"
                "Vue 单文件组件必须至少包含 <template> 或 <script>；"
                "请补回组件模板或 <script setup>，不要只提交样式块。"
            )
            logger.warning(f"❌ {error}")
            global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
            return False, error

        for tag in ("template", "script", "style"):
            open_count = len(re.findall(rf"<{tag}(?:\s[^>]*)?>", code_content, re.IGNORECASE))
            close_count = len(re.findall(rf"</{tag}>", code_content, re.IGNORECASE))
            if open_count != close_count:
                error = (
                    f"[L0.VUE 标签不闭合] {target_file}: <{tag}> 开启 {open_count} 次，"
                    f"关闭 {close_count} 次。请修正 Vue SFC 顶层块。"
                )
                logger.warning(f"❌ {error}")
                global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
                return False, error

        style_blocks = re.finditer(
            r"<style(?:\s[^>]*)?>(.*?)</style>",
            code_content,
            re.IGNORECASE | re.DOTALL,
        )
        for index, match in enumerate(style_blocks, start=1):
            css = re.sub(r"/\*.*?\*/", "", match.group(1), flags=re.DOTALL)
            balance = 0
            for char in css:
                if char == "{":
                    balance += 1
                elif char == "}":
                    balance -= 1
                    if balance < 0:
                        error = (
                            f"[L0.VUE CSS花括号错误] {target_file}: 第 {index} 个 <style> "
                            "存在多余的 `}`，会触发 Vite/PostCSS 解析失败。"
                        )
                        logger.warning(f"❌ {error}")
                        global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
                        return False, error
            if balance != 0:
                error = (
                    f"[L0.VUE CSS花括号错误] {target_file}: 第 {index} 个 <style> "
                    "存在未闭合的 `{`，会触发 Vite/PostCSS 解析失败。"
                )
                logger.warning(f"❌ {error}")
                global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
                return False, error

        logger.info(f"✅ [L0.VUE] SFC 结构检查通过: {target_file}")
        return True, ""

    def evaluate_skeleton(self, target_file: str, code_content: str = None,
                          sandbox_dir: str = None,
                          module_interfaces: dict = None,
                          project_spec: dict = None) -> Tuple[bool, str]:
        """
        对 skeleton 进行轻量 L0 验收。
        目标是阻止坏骨架进入真理区，不做 L1 合同审计，也允许函数体保留 ... 占位。
        """
        if not code_content:
            return False, "没有找到 skeleton 代码内容"

        logger.info(f"🧱 Reviewer 正在验收 skeleton: {target_file}")
        global_broadcaster.emit_sync(
            "Reviewer", "skeleton_review_start",
            f"开始验收 skeleton {target_file}", {"target": target_file}
        )

        expected_symbols = self._extract_expected_symbols(target_file, module_interfaces, project_spec)
        l0_pass, l0_error = self._l0_static_check(
            target_file,
            code_content,
            sandbox_dir,
            expected_symbols,
            project_spec=project_spec,
            allow_skeleton_placeholders=True,
        )

        if not l0_pass:
            feedback = f"[SKELETON_REJECTED] {l0_error}"
            logger.warning(f"❌ Reviewer skeleton 驳回: {feedback[:200]}")
            global_broadcaster.emit_sync(
                "Reviewer", "skeleton_review_fail",
                "Skeleton L0 验收未通过", {"feedback": feedback}
            )
            return False, feedback

        global_broadcaster.emit_sync(
            "Reviewer", "skeleton_review_pass",
            "✅ Skeleton L0 验收通过", {"feedback": "Skeleton L0 通过"}
        )
        logger.info(f"✅ Reviewer skeleton 验收通过: {target_file}")
        return True, "Skeleton L0 通过"

    def _l0_js_syntax_check(self, target_file: str, sandbox_dir: str) -> Tuple[bool, str]:
        """L0.1 JS: 用 Node.js --check 验证 JS 语法"""
        import subprocess as _sp

        js_path = os.path.join(sandbox_dir, target_file)
        if not os.path.isfile(js_path):
            logger.warning(f"⚠️ [L0.1 JS] 文件不存在: {js_path}，跳过")
            return True, ""

        try:
            result = _sp.run(
                ["node", "--check", js_path],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return True, ""

            # 提取错误信息
            err = result.stderr.strip()[:500] if result.stderr else "未知语法错误"
            error = f"[L0.1 JS 语法错误] {target_file}: {err}"
            logger.warning(f"❌ {error}")
            global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
            return False, error

        except FileNotFoundError:
            logger.warning("⚠️ [L0.1 JS] Node.js 不可用，跳过 JS 语法检查")
            return True, ""
        except Exception as e:
            logger.warning(f"⚠️ [L0.1 JS] 检查异常: {e}，跳过")
            return True, ""

    @staticmethod
    def _l0_fastapi_param_check(tree: ast.AST, target_file: str,
                                 code_content: str) -> Tuple[bool, str]:
        """
        L0.4: 检测 FastAPI POST/PUT 路由是否使用了裸参数（会导致 422）。
        
        FastAPI 对 POST/PUT 的 str/int 参数默认解析为 query parameter，
        但前端通常发 JSON body → 导致 422 Unprocessable Entity。
        
        正确做法：用 Pydantic BaseModel 接收 JSON body。
        """
        def _extract_route_meta(decorator: ast.AST) -> Tuple[str, set]:
            if not isinstance(decorator, ast.Call):
                return "", set()

            func = decorator.func
            if not isinstance(func, ast.Attribute):
                return "", set()

            method = str(func.attr).lower()
            if method not in ("post", "put"):
                return "", set()

            route_path = ""
            if decorator.args:
                first_arg = decorator.args[0]
                if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                    route_path = first_arg.value

            if not route_path:
                for keyword in decorator.keywords or []:
                    if keyword.arg == "path":
                        value = keyword.value
                        if isinstance(value, ast.Constant) and isinstance(value.value, str):
                            route_path = value.value
                            break

            path_params = set(re.findall(r"{([^{}:]+)}", route_path))
            path_params.update(re.findall(r"<(?:[^:>]+:)?([^>]+)>", route_path))
            return method.upper(), {item.strip() for item in path_params if item.strip()}

        def _iter_function_params(node: ast.AST):
            args = getattr(node, "args", None)
            if not args:
                return

            positional = list(getattr(args, "posonlyargs", [])) + list(args.args)
            positional_defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
            for arg, default in zip(positional, positional_defaults):
                yield arg.arg, arg.annotation, default

            for arg, default in zip(args.kwonlyargs, args.kw_defaults):
                yield arg.arg, arg.annotation, default

        def _has_explicit_fastapi_marker(default_node: ast.AST) -> bool:
            if default_node is None:
                return False
            default_text = ast.unparse(default_node)
            return any(
                marker in default_text
                for marker in ("Body(", "Depends(", "Form(", "File(", "Query(", "Path(", "Header(", "Cookie(", "Security(")
            )

        def _is_bare_fastapi_type(annotation: ast.AST) -> bool:
            if annotation is None:
                return True

            text = ast.unparse(annotation).replace("typing.", "").replace(" ", "")
            lowered = text.lower()

            if lowered.startswith("annotated["):
                return False

            bare_types = {
                "str", "int", "float", "bool", "dict", "list", "bytes",
                "optional[str]", "optional[int]", "optional[float]", "optional[bool]",
                "optional[dict]", "optional[list]", "optional[bytes]",
                "str|none", "int|none", "float|none", "bool|none",
                "dict|none", "list|none", "bytes|none",
                "list[str]", "list[int]", "list[float]", "list[bool]",
                "dict[str,any]", "dict[str,str]", "dict[str,int]",
            }
            return lowered in bare_types

        violations = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            for decorator in node.decorator_list:
                method, path_params = _extract_route_meta(decorator)
                if not method:
                    continue

                bare_params = []
                for param_name, annotation, default in _iter_function_params(node):
                    if param_name in ('self', 'request', 'req', 'db', 'session'):
                        continue
                    if param_name in path_params:
                        continue
                    if _has_explicit_fastapi_marker(default):
                        continue
                    if _is_bare_fastapi_type(annotation):
                        bare_params.append(
                            f"{param_name}: {ast.unparse(annotation)}" if annotation is not None else param_name
                        )

                if bare_params:
                    violations.append(
                        f"{method} 路由函数参数 [{', '.join(bare_params)}] "
                        f"使用了裸类型，FastAPI 会解析为 query parameter 导致前端 JSON 请求 422"
                    )

        if violations:
            fix_hint = ("修复方法：用 Pydantic BaseModel 类接收 JSON body，"
                       "例如 class CreateRequest(BaseModel): title: str; content: str")
            error = (f"[L0.4 FastAPI 参数错误] {target_file}: "
                    f"{'; '.join(violations)}。{fix_hint}")
            logger.warning(f"❌ {error}")
            global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
            return False, error

        return True, ""

    @staticmethod
    def _l0_route_decorator_check(tree: ast.AST, target_file: str,
                                   code_content: str) -> Tuple[bool, str]:
        """
        L0.5: 检测 routes.py 是否有 APIRouter 但无路由装饰器。

        常见 Bug: Coder 创建 `router = APIRouter()`，但函数没有 @router.get/@router.post
        装饰器，导致路由为空 → 所有请求返回 405 Method Not Allowed。
        """
        basename = os.path.basename(target_file).lower()

        # 仅对路由相关文件生效
        if basename not in ('routes.py', 'router.py', 'api.py'):
            return True, ""

        # 检查是否有 APIRouter() 或 FastAPI() 赋值给 router 变量
        has_api_router = False
        has_fastapi_as_router = False
        router_var_name = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and isinstance(node.value, ast.Call):
                        func = node.value.func
                        func_name = ""
                        if isinstance(func, ast.Name):
                            func_name = func.id
                        elif isinstance(func, ast.Attribute):
                            func_name = func.attr
                        if func_name == "APIRouter":
                            has_api_router = True
                            router_var_name = target.id
                        elif func_name == "FastAPI" and target.id in ("router", "app_router"):
                            has_fastapi_as_router = True
                            router_var_name = target.id

        # 致命错误：router = FastAPI()
        if has_fastapi_as_router:
            error = (f"[L0.5 路由致命错误] {target_file}: "
                     f"`{router_var_name} = FastAPI()` 应改为 "
                     f"`{router_var_name} = APIRouter()`。"
                     f"FastAPI() 是应用实例，不能作为 router 使用，"
                     f"app.include_router() 无法注册其路由，导致所有 API 返回 405。")
            logger.warning(f"❌ {error}")
            global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
            return False, error

        if not has_api_router:
            return True, ""  # 不是路由文件，跳过

        # 检查是否有 @router.xxx 装饰器
        has_route_decorator = False
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for deco in node.decorator_list:
                    # @router.get(...) / @router.post(...)
                    if isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute):
                        if isinstance(deco.func.value, ast.Name):
                            if deco.func.value.id == router_var_name:
                                if deco.func.attr in ('get', 'post', 'put', 'delete', 'patch'):
                                    has_route_decorator = True
                                    break
                    # @router.get (无括号，不太常见)
                    elif isinstance(deco, ast.Attribute):
                        if isinstance(deco.value, ast.Name) and deco.value.id == router_var_name:
                            if deco.attr in ('get', 'post', 'put', 'delete', 'patch'):
                                has_route_decorator = True
                                break
                if has_route_decorator:
                    break

        if not has_route_decorator:
            error = (f"[L0.5 路由装饰器缺失] {target_file}: "
                     f"发现 `{router_var_name} = APIRouter()` 但没有任何 "
                     f"`@{router_var_name}.get/post/put/delete` 装饰器。"
                     f"所有函数都未注册为 HTTP 端点，将导致 405 Method Not Allowed。"
                     f"修复方法：在每个处理 HTTP 请求的函数上添加 @{router_var_name}.post('/api/xxx') 等装饰器。")
            logger.warning(f"❌ {error}")
            global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
            return False, error

        return True, ""

    # ============================================================
    # L0.6: 跨文件字段一致性检查（确定性，0 LLM 消耗）
    # ============================================================

    def _l0_cross_file_check(self, target_file: str, code_content: str,
                              sandbox_dir: str) -> Tuple[bool, str]:
        """
        L0.6: 跨文件字段一致性检查。
        检查 1: HTML Jinja 模板变量 vs models.py to_dict() 返回字段
        检查 2: routes.py request.form keys vs HTML name 属性
        检查 3: render_template() 参数名 vs 模板顶层变量名
        """
        basename = os.path.basename(target_file).lower()

        # HTML 文件 → 检查 Jinja 字段一致性 + 反向表单字段检查
        if target_file.endswith('.html'):
            # L0.6-A: Jinja 字段
            jinja_pass, jinja_error = self._l0_jinja_field_check(
                target_file, code_content, sandbox_dir)
            if not jinja_pass:
                return False, jinja_error

            # L0.6-C: render_template 参数名 vs 模板顶层变量名
            rt_pass, rt_error = self._l0_render_template_var_check(
                target_file, code_content, sandbox_dir)
            if not rt_pass:
                return False, rt_error

            # L0.6-B 反向检查已禁用：
            # 审查 HTML 时反向检查 routes.py 的 form keys 会产生误报，
            # 因为 routes.py 中其他路由的 form keys（如 add_category 的 'name'）
            # 可能对应尚未创建的 HTML 文件（如 categories.html）。
            # L0.6-B 正向检查（直接审查 routes.py 时）已经足够覆盖这一场景。

            # L0.7: 路由 URL 一致性检查（动态：无路由文件时自动跳过）
            route_pass, route_error = self._l0_route_url_check(
                target_file, code_content, sandbox_dir)
            if not route_pass:
                return False, route_error

            # L0.8: form action 路径 vs 路由注册路径一致性检查
            action_pass, action_error = self._l0_form_action_check(
                target_file, code_content, sandbox_dir)
            if not action_pass:
                return False, action_error

            # L0.9: 模板继承一致性检查
            inherit_pass, inherit_error = self._l0_template_inherit_check(
                target_file, code_content, sandbox_dir)
            if not inherit_pass:
                return False, inherit_error

            # L0.13: url_for endpoint 一致性检查
            ep_pass, ep_error = self._l0_url_for_endpoint_check(
                target_file, code_content, sandbox_dir)
            if not ep_pass:
                return False, ep_error

            return True, ""

        # routes.py / views.py → 检查 form field 一致性
        if basename in ('routes.py', 'views.py'):
            return self._l0_form_field_check(target_file, code_content, sandbox_dir)

        return True, ""

    def _l0_jinja_field_check(self, target_file: str, html_content: str,
                               sandbox_dir: str) -> Tuple[bool, str]:
        """
        L0.6-A: 检查 Jinja 模板变量是否匹配 models.py to_dict() 返回字段。
        拦截典型 bug: {{ expense.category.name }} 而 to_dict() 返回 category_name
        """
        # 1. 找 models.py
        models_path = self._find_file_in_sandbox(sandbox_dir, 'models.py')
        if not models_path:
            return True, ""  # 没有 models.py，跳过

        try:
            with open(models_path, 'r', encoding='utf-8') as f:
                models_content = f.read()
        except Exception:
            return True, ""

        # 2. 检查是否是 Flask SSR（需要有 render_template）
        routes_content = ""  # 提升到方法作用域，供三跳链路使用
        routes_path = self._find_file_in_sandbox(sandbox_dir, 'routes.py')
        if routes_path:
            try:
                with open(routes_path, 'r', encoding='utf-8') as f:
                    routes_content = f.read()
                if 'render_template' not in routes_content:
                    return True, ""  # 不是 SSR，跳过
            except Exception:
                pass

        # 3. 提取 to_dict() 返回的 key（按模型类分组）
        class_keys = self._extract_to_dict_keys(models_content)

        # 3b. 若 models.py 用了 SELECT * 但没有 CREATE TABLE，
        #     去 database.py / db.py / app.py 找 CREATE TABLE 补充字段
        has_select_star = 'SELECT *' in models_content or 'SELECT * ' in models_content
        has_table_keys = '_table' in class_keys
        if has_select_star and not has_table_keys:
            for db_file in ('database.py', 'db.py', 'app.py'):
                db_path = self._find_file_in_sandbox(sandbox_dir, db_file)
                if db_path:
                    try:
                        with open(db_path, 'r', encoding='utf-8') as f:
                            db_content = f.read()
                        extra_keys = self._extract_to_dict_keys(db_content)
                        if '_table' in extra_keys:
                            class_keys['_table'] = extra_keys['_table']
                            logger.info(f"✅ [L0.6-A] 从 {db_file} 补充 CREATE TABLE 列: {sorted(extra_keys['_table'])}")
                            break
                    except Exception:
                        pass

        if not class_keys:
            return True, ""  # 没有 to_dict / SQL 列，跳过

        # 合并所有 key 作为 fallback（无法匹配具体类时使用）
        # 优先使用 _table 列名（CREATE TABLE 是最完整的字段集）
        all_keys = set()
        if '_table' in class_keys:
            all_keys = class_keys['_table'].copy()
        else:
            for ks in class_keys.values():
                all_keys |= ks

        # 3c. 三跳追踪链：template for-var → routes.py render_template arg → models.py SQL 函数字段
        #     解决 fallback _table 列名过于宽泛导致的误放行（如 stat.amount 通过但实际应是 stat.total）
        #     链路: {% for stat in category_stats %} → render_template(category_stats=get_category_stats()) → _sql_get_category_stats={category, total}
        for_loop_map = {}  # {loop_var: collection_var}  e.g. {"stat": "category_stats"}
        for fl_m in re.finditer(r'\{%[-\s]*for\s+(\w+)\s+in\s+(\w+)', html_content):
            for_loop_map[fl_m.group(1)] = fl_m.group(2)

        rt_var_func_map = {}  # {template_var: func_name}  e.g. {"category_stats": "get_category_stats"}
        if routes_content:
            template_basename = os.path.basename(target_file)
            # 找到包含 render_template('index.html', ...) 的函数体
            for rt_m in re.finditer(
                r'render_template\s*\(\s*["\']' + re.escape(template_basename) + r'["\']\s*,(.*?)\)',
                routes_content, re.DOTALL
            ):
                args_str = rt_m.group(1)
                # 模式 A：直接函数调用 → key=func(...)
                for kv_m in re.finditer(r'(\w+)\s*=\s*(\w+)\s*\(', args_str):
                    rt_var_func_map[kv_m.group(1)] = kv_m.group(2)
                # 模式 B：局部变量赋值 → key=local_var
                # 需要追溯 local_var = func(...) 的赋值
                for kv_m in re.finditer(r'(\w+)\s*=\s*(\w+)\s*(?:,|\s*$)', args_str):
                    tmpl_key = kv_m.group(1)
                    local_var = kv_m.group(2)
                    if tmpl_key in rt_var_func_map:
                        continue  # 已被模式 A 命中
                    # 在同一个路由函数体内查找 local_var = func(...)
                    assign_pat = re.compile(
                        re.escape(local_var) + r'\s*=\s*(\w+)\s*\(',
                        re.MULTILINE
                    )
                    assign_m = assign_pat.search(routes_content)
                    if assign_m:
                        rt_var_func_map[tmpl_key] = assign_m.group(1)

        if for_loop_map or rt_var_func_map:
            logger.info(f"🔗 [L0.6-A] 三跳链路: for_loop={for_loop_map}, rt_var_func={rt_var_func_map}")

        # 4. 提取 Jinja {{ }} 块中的字段访问
        jinja_blocks = re.findall(r'\{\{(.*?)\}\}', html_content, re.DOTALL)

        # 跳过的变量名（Jinja 内置 / 非数据变量 / WTForms 表单对象）
        skip_vars = {'loop', 'range', 'request', 'config', 'session',
                     'g', 'url_for', 'get_flashed_messages', 'true', 'false',
                     'none', 'self', 'caller', 'joiner',
                     'form', 'csrf', 'pagination', 'paginate',  # WTForms / 分页对象
                     }

        mismatches = []
        for block in jinja_blocks:
            # 先剥离字符串字面量（防止 'style.css' 等文件名误报）
            cleaned_block = re.sub(r'["\'][^"\']*["\']', '', block)

            # 提取所有 xxx.yyy 或 xxx.yyy.zzz 模式
            for m in re.finditer(r'(\w+)\.(\w+(?:\.\w+)*)', cleaned_block):
                var_name = m.group(1)
                field_path = m.group(2)  # 可能是 "amount" 或 "category.name"

                # 跳过 Jinja 内置变量和过滤器
                if var_name.lower() in skip_vars:
                    continue

                # 按变量名匹配模型类（expense→Expense, category→Category）
                matched_keys = None
                for cls_name, ks in class_keys.items():
                    if cls_name.lower() == var_name.lower():
                        matched_keys = ks
                        break

                # 三跳链路精确匹配：for-loop var → collection → SQL function fields
                # 优先级高于 _table fallback，解决统计查询字段误放行
                if matched_keys is None and var_name in for_loop_map:
                    collection_var = for_loop_map[var_name]
                    func_name = rt_var_func_map.get(collection_var)
                    if func_name:
                        sql_key = f"_sql_{func_name}"
                        if sql_key in class_keys:
                            matched_keys = class_keys[sql_key]
                            logger.debug(f"🔗 [L0.6-A] 三跳命中: {var_name} → {collection_var} → {func_name} → {sorted(matched_keys)}")

                # fallback: 使用合并 key（仅在三跳链路也未命中时）
                if matched_keys is None:
                    matched_keys = all_keys
                    # P1-b: 三跳链路降级时追加 WARNING（便于诊断误放行）
                    if var_name in for_loop_map:
                        logger.warning(
                            f"⚠️ [L0.6-A] 三跳链路未命中，降级到 _table 兜底: "
                            f"{var_name} → {for_loop_map[var_name]} → ???"
                        )

                if '.' in field_path:
                    # 嵌套访问：expense.category.name
                    first_level = field_path.split('.')[0]
                    if first_level not in matched_keys:
                        # 看看是否有展平后的字段名（category.name → category_name）
                        flat_name = field_path.replace('.', '_')
                        suggestion = f"'{flat_name}'" if flat_name in matched_keys else ""
                        hint = f"。应使用 {suggestion}" if suggestion else ""
                        mismatches.append(
                            f"`{{{{ {var_name}.{field_path} }}}}` — "
                            f"'{first_level}' 不是 to_dict() 的返回字段{hint}。"
                            f"可用字段: {sorted(matched_keys)}"
                        )
                else:
                    # 单层访问：expense.amount
                    if field_path not in matched_keys:
                        mismatches.append(
                            f"`{{{{ {var_name}.{field_path} }}}}` — "
                            f"'{field_path}' 不在 to_dict() 返回字段中。"
                            f"可用字段: {sorted(matched_keys)}"
                        )

        if mismatches:
            # 确定冲突源文件（Jinja 字段不匹配时，有罪方可能是 models.py 的 to_dict）
            cross_file_tag = ""
            for root, dirs, files in os.walk(sandbox_dir):
                dirs[:] = [d for d in dirs if d not in ('__pycache__', 'venv', '.venv', 'node_modules', '.sandbox')]
                for f in files:
                    if f == 'models.py':
                        cross_file_tag = f"[CROSS_FILE:{os.path.relpath(os.path.join(root, f), sandbox_dir)}] "
                        break

            error = (
                f"{cross_file_tag}[L0.6 Jinja 字段不匹配] {target_file}: "
                f"模板引用了 models.py to_dict() 中不存在的字段：\n"
                + "\n".join(f"  - {m}" for m in mismatches[:5])
                + "\n修复方法：将模板变量改为 to_dict() 实际返回的字段名。"
            )
            logger.warning(f"❌ {error}")
            global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
            return False, error

        logger.info(f"✅ [L0.6-A] Jinja 字段校验通过: {target_file}")
        return True, ""

    def _l0_form_field_check(self, target_file: str, routes_content: str,
                              sandbox_dir: str) -> Tuple[bool, str]:
        """
        L0.6-B: 检查 routes.py 的 request.form keys 是否与 HTML name 属性一致。
        拦截典型 bug: request.form['category_id'] 但 HTML 中 name="category"
        """
        # 1. 提取 request.form keys
        form_keys = set()
        # request.form['xxx'] / request.form["xxx"]
        for m in re.finditer(r"request\.form\[(['\"])(\w+)\1\]", routes_content):
            form_keys.add(m.group(2))
        # request.form.get('xxx') / request.form.get("xxx")
        for m in re.finditer(r"request\.form\.get\((['\"])(\w+)\1", routes_content):
            form_keys.add(m.group(2))

        if not form_keys:
            return True, ""  # 没有 request.form，跳过（可能是 REST API）

        # 2. 从沙盒中所有 HTML 文件提取 name 属性
        html_names = set()
        for root, dirs, files in os.walk(sandbox_dir):
            dirs[:] = [d for d in dirs if d not in
                       ('__pycache__', 'venv', '.venv', 'node_modules', '.sandbox')]
            for f in files:
                if f.endswith('.html'):
                    try:
                        with open(os.path.join(root, f), 'r', encoding='utf-8') as fh:
                            content = fh.read()
                        for m in re.finditer(r'<(?:input|select|textarea|button)\b[^>]*\bname\s*=\s*["\']([\w-]+)["\']', content, re.IGNORECASE):
                            html_names.add(m.group(1))
                    except Exception:
                        pass

        if not html_names:
            return True, ""  # 没找到 HTML name，跳过

        # 3. 检查 routes.py 的 form keys 是否在 HTML name 中
        missing_in_html = form_keys - html_names
        if missing_in_html:
            # 冲突源文件标记：错误在 routes.py（form key 与 HTML 不匹配）
            # target_file 就是 routes.py 自身路径（可能是直接审查，也可能是反向检查）
            cross_file_tag = f"[CROSS_FILE:{target_file}] "

            error = (
                f"{cross_file_tag}[L0.6 表单字段不匹配] {target_file}: "
                f"request.form 引用了 HTML 中不存在的字段名: {sorted(missing_in_html)}。"
                f"HTML 中实际的 name 属性: {sorted(html_names)}。"
                f"修复方法：确保 request.form['xxx'] 的 key 与 HTML <input name='xxx'> 完全一致。"
            )
            logger.warning(f"❌ {error}")
            global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
            return False, error

        logger.info(f"✅ [L0.6-B] 表单字段校验通过: {target_file}")
        return True, ""

    def _l0_render_template_var_check(self, target_file: str, html_content: str,
                                       sandbox_dir: str) -> Tuple[bool, str]:
        """
        L0.6-C: 检查 render_template() 传入的顶层变量名是否与模板中使用的顶层变量名一致。
        拦截典型 bug: render_template('index.html', summary=data) 但模板中
        {% for item in summaries %} — summaries 不存在，导致空循环或 UndefinedError。
        """
        # 1. 确定当前 HTML 文件名
        html_basename = os.path.basename(target_file)

        # 2. 在沙盒中找所有 Python 路由文件提取 render_template 调用
        route_files = ['routes.py', 'views.py', 'app.py']
        render_kwargs = set()  # render_template 传入的关键字参数名

        for rf in route_files:
            rf_path = self._find_file_in_sandbox(sandbox_dir, rf)
            if not rf_path:
                continue
            try:
                with open(rf_path, 'r', encoding='utf-8') as f:
                    routes_content = f.read()
            except Exception:
                continue

            # 匹配 render_template('index.html', key1=..., key2=...)
            # 支持单引号和双引号
            for m in re.finditer(
                r"render_template\s*\(\s*['\"]" + re.escape(html_basename) + r"['\"]\s*,([^)]+)\)",
                routes_content
            ):
                args_str = m.group(1)
                # 提取所有 keyword=value 中的 keyword
                for kw in re.finditer(r'(\w+)\s*=', args_str):
                    render_kwargs.add(kw.group(1))

        if not render_kwargs:
            return True, ""  # 没找到 render_template 调用，跳过

        # 3. 提取模板中使用的顶层变量名
        template_vars = set()

        # 3a. {% for xxx in YYY %} → YYY 是顶层变量
        for m in re.finditer(r'\{%\s*for\s+\w+\s+in\s+(\w+)', html_content):
            template_vars.add(m.group(1))

        # 3b. {% if YYY %} / {% if YYY|length %} → YYY 是顶层变量
        for m in re.finditer(r'\{%\s*(?:if|elif)\s+(\w+)', html_content):
            template_vars.add(m.group(1))

        # 3c. {{ YYY }} / {{ YYY|filter }} → 单独的顶层变量（不含点号的）
        for m in re.finditer(r'\{\{\s*(\w+)(?:\s*[|}]|\s*\}\})', html_content):
            template_vars.add(m.group(1))

        # 过滤掉 Jinja 内置变量、关键字和 for 循环局部变量
        jinja_builtins = {
            # Jinja2 内置变量/函数
            'loop', 'range', 'request', 'config', 'session', 'g',
            'url_for', 'get_flashed_messages', 'true', 'false', 'none',
            'self', 'caller', 'joiner', 'namespace',
            # Jinja2 逻辑关键字（{% if not X %}, {% if X and Y %} 等）
            'not', 'and', 'or', 'is', 'in', 'if', 'else', 'elif',
            'for', 'endfor', 'endif', 'set', 'block', 'extends',
            'include', 'import', 'macro', 'defined', 'undefined',
        }
        # 提取 for 循环的迭代变量（局部变量，不算顶层）
        loop_vars = set()
        for m in re.finditer(r'\{%\s*for\s+(\w+)\s+in\s+', html_content):
            loop_vars.add(m.group(1))

        template_vars -= jinja_builtins
        template_vars -= loop_vars

        # 过滤 {% set xxx = ... %} 定义的局部变量
        set_vars = set()
        for m in re.finditer(r'\{%[-\s]*set\s+(\w+)\s*=', html_content):
            set_vars.add(m.group(1))
        template_vars -= set_vars

        if not template_vars:
            return True, ""  # 模板中没有可检查的顶层变量

        # 4. 检查：模板使用的顶层变量是否都在 render_template 参数中
        missing = template_vars - render_kwargs
        if missing:
            # 进一步过滤：只报告那些看起来像数据变量的（排除 CSS class 等）
            # 如果 missing 中有变量同时出现在 for/if 块中，才认为是真正的数据变量
            confirmed_missing = set()
            for var in missing:
                # 确认该变量确实在 for/if 上下文中被引用（不是随机的单词）
                if re.search(r'\{%\s*(?:for|if|elif)\s+.*?\b' + re.escape(var) + r'\b', html_content):
                    confirmed_missing.add(var)

            if confirmed_missing:
                routes_tag = ""
                for rf in route_files:
                    rf_path = self._find_file_in_sandbox(sandbox_dir, rf)
                    if rf_path:
                        routes_tag = f"[CROSS_FILE:{os.path.relpath(rf_path, sandbox_dir)}] "
                        break

                # 智能提示：检测是否是集合字段的误用（如 month vs monthly_summary）
                field_hints = []
                for var in sorted(confirmed_missing):
                    for kwarg in render_kwargs:
                        # 如果缺失变量名是某个已传入集合名的子串（如 month ⊂ monthly_summary）
                        if var in kwarg and var != kwarg:
                            field_hints.append(
                                f"'{var}' 看起来是 '{kwarg}' 集合中的字段名。"
                                f"禁止直接用 '{{% if {var} %}}' 或 '{{% for x in {var} %}}'！"
                                f"应改为 '{{% for entry in {kwarg} %}}' + 'entry.{var}' 点号访问。"
                            )
                            break

                hint_block = ""
                if field_hints:
                    hint_block = "\n💡 常见错误提示：\n" + "\n".join(f"  - {h}" for h in field_hints)

                error = (
                    f"{routes_tag}[L0.6-C render_template 变量缺失] {target_file}: "
                    f"模板引用了 render_template() 未传入的顶层变量: {sorted(confirmed_missing)}。"
                    f"render_template('{html_basename}', ...) 实际传入的变量名: {sorted(render_kwargs)}。"
                    f"修复方法：模板中 '{{% for x in YYY %}}' / '{{% if YYY %}}' 的 YYY 只能使用 render_template 传入的变量名。"
                    f"如果要访问集合内部字段，必须用 '{{% for entry in collection %}}' + 'entry.field' 点号语法，"
                    f"禁止将集合字段名（如 month、category）直接当作顶层变量使用！"
                    f"{hint_block}"
                )
                logger.warning(f"❌ {error}")
                global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
                return False, error

        logger.info(f"✅ [L0.6-C] render_template 变量校验通过: {target_file}")
        return True, ""

    @staticmethod
    def _extract_to_dict_keys(models_content: str) -> dict:
        """
        从 models.py 提取 to_dict() 返回的字典 key，按模型类分组。
        返回: {"ClassName": {key1, key2, ...}, ...}
        支持两种模式：
        1. ORM to_dict() 方法中的 return {"key": ...}
        2. 原生 SQL SELECT 列名（含 AS 别名）→ 归入 "_sql" 虚拟类
        """
        class_keys = {}  # {class_name: set()}

        # 模式 1: 按 class 分组提取 to_dict() keys
        # 先找所有 class 定义及其范围
        class_blocks = re.finditer(
            r'class\s+(\w+).*?(?=\nclass\s|\Z)',
            models_content, re.DOTALL
        )
        for cls_match in class_blocks:
            cls_name = cls_match.group(1)
            cls_body = cls_match.group(0)

            # 在这个 class 内找 to_dict
            to_dict_match = re.search(
                r'def\s+to_dict\s*\(self\).*?(?=\n    def\s|\nclass\s|\Z)',
                cls_body, re.DOTALL
            )
            if to_dict_match:
                keys = set()
                for m in re.finditer(r'["\']([\w]+)["\']\s*:', to_dict_match.group(0)):
                    keys.add(m.group(1))
                if keys:
                    class_keys[cls_name] = keys
        # 模式 2: 原生 SQL — 提取 SELECT 列名和 AS 别名（按函数分组）
        sql_keywords = {
            'SELECT', 'FROM', 'WHERE', 'GROUP', 'BY', 'ORDER', 'DESC', 'ASC',
            'DISTINCT', 'NULL', 'NOT', 'AND', 'OR', 'INTEGER', 'TEXT', 'REAL',
            'PRIMARY', 'KEY', 'AUTOINCREMENT', 'DEFAULT', 'CURRENT_TIMESTAMP',
            'IF', 'EXISTS', 'TABLE', 'CREATE', 'INSERT', 'UPDATE', 'DELETE',
            'INTO', 'VALUES', 'SET', 'HAVING', 'LIMIT', 'OFFSET', 'JOIN',
            'LEFT', 'RIGHT', 'INNER', 'ON', 'AS', 'LIKE', 'IN', 'BETWEEN',
        }
        func_blocks = re.finditer(
            r'def\s+(\w+)\s*\([^)]*\)\s*.*?(?=\ndef\s|\Z)',
            models_content, re.DOTALL
        )
        for func_match in func_blocks:
            func_name = func_match.group(1)
            func_body = func_match.group(0)

            select_blocks = re.finditer(
                r'SELECT\s+(.*?)\s+FROM\s',
                func_body, re.DOTALL | re.IGNORECASE
            )
            for sel in select_blocks:
                select_clause = sel.group(1)
                keys = set()

                columns = re.split(r',\s*(?![^(]*\))', select_clause)
                for col in columns:
                    col = col.strip()
                    if not col or col == '*':
                        continue

                    as_match = re.search(r'\bAS\s+(\w+)\s*$', col, re.IGNORECASE)
                    if as_match:
                        keys.add(as_match.group(1))
                    else:
                        ident_match = re.search(r'(\w+)\s*$', col)
                        if ident_match:
                            name = ident_match.group(1)
                            if name.upper() not in sql_keywords:
                                keys.add(name)

                if keys:
                    group_name = f"_sql_{func_name}"
                    class_keys[group_name] = keys

        # 模式 3: CREATE TABLE 列名 → 作为兜底字段集
        table_keys = set()
        create_blocks = re.finditer(
            r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\)',
            models_content, re.DOTALL | re.IGNORECASE
        )
        for tbl_match in create_blocks:
            columns_def = tbl_match.group(2)
            for col_def in columns_def.split(','):
                col_def = col_def.strip()
                if not col_def:
                    continue
                first_word = col_def.split()[0] if col_def.split() else ''
                constraint_kw = {'PRIMARY', 'FOREIGN', 'UNIQUE', 'CHECK', 'CONSTRAINT'}
                if first_word.upper() in constraint_kw:
                    continue
                if first_word and first_word.upper() not in sql_keywords:
                    table_keys.add(first_word)

        if table_keys:
            class_keys["_table"] = table_keys

        return class_keys


    @staticmethod
    def _find_file_in_sandbox(sandbox_dir: str, filename: str):
        """在沙盒目录中递归查找指定文件名，返回完整路径或 None"""
        for root, dirs, files in os.walk(sandbox_dir):
            dirs[:] = [d for d in dirs if d not in
                       ('__pycache__', 'venv', '.venv', 'node_modules', '.sandbox')]
            if filename in files:
                return os.path.join(root, filename)
        return None

    def _l0_template_inherit_check(self, target_file: str, html_content: str,
                                    sandbox_dir: str) -> Tuple[bool, str]:
        """
        L0.9: 模板继承一致性检查。
        如果沙盒中存在 base.html（且定义了 {% block %}），
        其他 HTML 文件必须使用 {% extends "base.html" %}。
        """
        import re as _re

        # 跳过 base.html 自身
        basename = os.path.basename(target_file).lower()
        if basename in ('base.html', 'layout.html', 'base_layout.html'):
            return True, ""

        # 在沙盒中查找 base 模板
        base_path = self._find_file_in_sandbox(sandbox_dir, 'base.html')
        if not base_path:
            base_path = self._find_file_in_sandbox(sandbox_dir, 'layout.html')
        if not base_path:
            return True, ""  # 没 base 模板 → 不约束

        # 验证 base 模板确实定义了 {% block %}
        try:
            with open(base_path, 'r', encoding='utf-8') as f:
                base_content = f.read()
            if '{% block ' not in base_content and '{%block ' not in base_content:
                return True, ""  # base.html 没有 block 定义 → 不是布局模板
        except Exception:
            return True, ""

        # 检查当前文件是否有 {% extends %}
        extends_match = _re.search(r'\{%[-\s]*extends\s+["\']([^"\']+)["\']', html_content)
        if extends_match:
            # v4.1: 反向校验 — extends 的目标文件必须存在
            parent_template = extends_match.group(1)
            parent_path = self._find_file_in_sandbox(sandbox_dir, parent_template)
            if not parent_path:
                # 在 templates 子目录中也查找
                parent_path = self._find_file_in_sandbox(sandbox_dir, f"templates/{parent_template}")
            if not parent_path:
                error = (
                    f"[L0.9 模板继承目标缺失] {target_file}: "
                    f"使用了 {{% extends \"{parent_template}\" %}}，"
                    f"但 {parent_template} 在项目中不存在。"
                    f"修复方法：创建 {parent_template} 布局模板（含 {{% block content %}}），"
                    f"或将当前文件改为独立页面（移除 extends，自己包含完整 HTML 结构）。"
                )
                return False, error
            return True, ""  # 有继承且目标存在 → 通过

        # 没有继承 → 报错
        base_rel = os.path.relpath(base_path, sandbox_dir).replace('\\', '/')
        error = (
            f"[L0.9 模板继承缺失] {target_file}: "
            f"沙盒中存在布局模板 {base_rel}（含 {{% block %}} 定义），"
            f"但当前文件未使用 {{% extends \"{os.path.basename(base_path)}\" %}}。"
            f"修复方法：在文件开头添加 {{% extends \"{os.path.basename(base_path)}\" %}}，"
            f"将页面内容放入 {{% block content %}} 中。"
        )
        return False, error

    def _l0_route_url_check(self, target_file: str, html_content: str,
                             sandbox_dir: str) -> Tuple[bool, str]:
        """
        L0.7: 检查 HTML 中的 href/action/fetch URL 是否与后端路由匹配。
        动态扫描沙盒中所有 .py 文件提取路由。
        纯前端项目（无路由定义）→ 自动跳过，不产生约束。
        """
        import re as _re

        # 1. 动态发现沙盒中所有已注册路由
        registered_routes = set()
        route_source_file = None
        try:
            from tools.observer import Observer
            obs = Observer(sandbox_dir)
            for root, dirs, files in os.walk(sandbox_dir):
                dirs[:] = [d for d in dirs if d not in
                           {'.git', '__pycache__', 'node_modules', '.sandbox', 'venv', '.venv'}]
                for f in files:
                    if f.endswith('.py'):
                        rel = os.path.relpath(os.path.join(root, f), sandbox_dir).replace('\\', '/')
                        routes = obs.extract_routes(rel)
                        for r in routes:
                            registered_routes.add(r['path'])
                            if not route_source_file:
                                route_source_file = rel
        except Exception:
            return True, ""  # Observer 异常时不阻塞

        # 纯前端项目：没发现任何路由 → 跳过检查
        if not registered_routes:
            return True, ""

        registered_routes.add('/')  # 根路由总是合法

        # 2. 提取 HTML 中所有引用的内部 URL
        html_urls = set()
        # href="/xxx" 和 action="/xxx"（排除锚点、外链、static）
        for m in _re.finditer(r'(?:href|action)\s*=\s*["\'](/[^"\'#]*)["\']', html_content):
            url = m.group(1).split('?')[0]
            if not url.startswith('/static'):
                html_urls.add(url)
        # fetch("/xxx")
        for m in _re.finditer(r'fetch\s*\(\s*[`"\'](/[^`"\']*)[`"\']', html_content):
            html_urls.add(m.group(1).split('?')[0])

        if not html_urls:
            return True, ""

        # 3. 对比：移除 Jinja 模板参数后匹配
        def normalize(url):
            """将 Jinja 变量替换为通配段，如 /todos/{{ todo.id }}/delete → /todos/DYNVAR/delete"""
            cleaned = _re.sub(r'\{\{[^}]*\}\}', 'DYNVAR', url)
            cleaned = _re.sub(r'\{%[^%]*%\}', '', cleaned)
            return cleaned.rstrip('/') or '/'

        phantom_urls = set()
        for url in html_urls:
            norm_url = normalize(url)
            matched = False
            for route in registered_routes:
                # Flask <int:id> / FastAPI {id} → 通配
                route_pattern = _re.sub(r'<[^>]+>', '[^/]+', route)
                route_pattern = _re.sub(r'\{[^}]+\}', '[^/]+', route_pattern)
                if _re.fullmatch(route_pattern, norm_url):
                    matched = True
                    break
                # 前缀匹配：/category 可以匹配 /category/<int:id>
                # 取原始路由（非正则）的前缀段做比较
                route_segments = route.rstrip('/').split('/')
                if len(route_segments) > 1:
                    # 去掉最后一段（参数段）→ 得到路由前缀
                    route_prefix = '/'.join(route_segments[:-1])
                    if route_prefix and norm_url == route_prefix:
                        matched = True
                        break
                # 反向前缀匹配：DYNVAR 替换后的 /category/DYNVAR
                # 也匹配 /category/<int:id>
                if 'DYNVAR' in norm_url:
                    dynvar_pattern = norm_url.replace('DYNVAR', '[^/]+')
                    if _re.fullmatch(dynvar_pattern, route.rstrip('/')):
                        matched = True
                        break
            if not matched:
                phantom_urls.add(url)

        if phantom_urls:
            error = (
                f"[CROSS_FILE:{route_source_file}] [L0.7 幻影路由] {target_file}: "
                f"HTML 引用了后端不存在的路由 URL: {sorted(phantom_urls)}。"
                f"已注册的路由: {sorted(registered_routes)}。"
                f"修复方法：删除引用不存在路由的链接/按钮，只使用已注册路由的 URL。"
            )
            return False, error

        return True, ""

    def _l0_form_action_check(self, target_file: str, html_content: str,
                               sandbox_dir: str) -> Tuple[bool, str]:
        """
        L0.8: 检查 HTML <form action="/xxx"> 的路径是否与 routes.py 注册的路径一致。
        拦截典型 bug: action="/add_expense" 但路由注册的是 "/add"
        """
        import re as _re

        # 1. 提取 HTML 中所有 form action 路径（排除 url_for 和 #）
        form_actions = _re.findall(
            r'<form[^>]+action\s*=\s*["\']([^"\'#]+)["\']',
            html_content, _re.IGNORECASE
        )
        # 过滤掉 Jinja url_for 表达式（如 {{ url_for('xxx') }}）和空路径
        static_actions = []
        for action in form_actions:
            action = action.strip()
            if not action or '{{' in action or '{%' in action:
                continue  # Jinja 动态路径，跳过（这是正确做法）
            if action.startswith('/'):
                static_actions.append(action)

        if not static_actions:
            return True, ""  # 没有硬编码的 form action（或全用 url_for），跳过

        # 2. 从 routes.py / app.py 收集所有注册路径
        registered_routes = set()
        for fname in ('routes.py', 'views.py', 'app.py'):
            fpath = self._find_file_in_sandbox(sandbox_dir, fname)
            if not fpath:
                continue
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    py_content = f.read()
            except Exception:
                continue

            # @app.route('/path', ...) 或 @bp.route('/path', ...)
            for m in _re.finditer(r'@\w+\.(?:route|post|get|put|delete)\s*\(\s*["\']([^"\']+)["\']', py_content):
                registered_routes.add(m.group(1))

            # app.add_url_rule('/path', ...)
            for m in _re.finditer(r'add_url_rule\s*\(\s*["\']([^"\']+)["\']', py_content):
                registered_routes.add(m.group(1))

        if not registered_routes:
            return True, ""  # 没有路由信息，跳过

        # 3. 比对
        mismatched = []
        for action in static_actions:
            if action not in registered_routes:
                mismatched.append(action)

        if mismatched:
            error = (
                f"[L0.8 form action 路径不匹配] {target_file}: "
                f"HTML 中 <form action> 的路径 {mismatched} "
                f"不在已注册的路由中。已注册路由: {sorted(registered_routes)}。"
                f"修复方法：将 action 改为已注册的路径，"
                f"或使用 action=\"{{{{ url_for('endpoint_name') }}}}\" 动态生成。"
            )
            logger.warning(f"❌ {error}")
            global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
            return False, error

        return True, ""

    def _l0_template_folder_check(self, target_file: str, code_content: str,
                                   sandbox_dir: str) -> Tuple[bool, str]:
        """
        L0.7: 检查 Flask(__name__) 是否缺少 template_folder 参数。
        当 py 文件在 src/ 而 templates/ 在项目根目录时，必须配置 template_folder。
        """
        # 只检查包含 Flask(__name__) 的文件
        if 'Flask(__name__)' not in code_content:
            return True, ""

        # 检查是否已经配置了 template_folder
        if 'template_folder' in code_content:
            return True, ""

        # 检查项目结构：py 文件在子目录，templates/ 在其他位置
        py_dir = os.path.dirname(os.path.join(sandbox_dir, target_file))
        templates_in_py_dir = os.path.isdir(os.path.join(py_dir, 'templates'))

        if templates_in_py_dir:
            return True, ""  # templates/ 是 py 文件的兄弟目录，默认配置能找到

        # 检查项目中是否存在 templates/ 目录
        has_templates = False
        for root, dirs, files in os.walk(sandbox_dir):
            dirs[:] = [d for d in dirs if d not in
                       ('__pycache__', 'venv', '.venv', 'node_modules', '.sandbox')]
            if 'templates' in dirs:
                has_templates = True
                break

        if has_templates:
            error = (
                f"[L0.7 template_folder 缺失] {target_file}: "
                f"Flask(__name__) 缺少 template_folder 参数。"
                f"当前文件在 {os.path.dirname(target_file) or '.'}/，"
                f"但 templates/ 目录不在同级。"
                f"必须改为 Flask(__name__, template_folder='../templates')，"
                f"否则会 TemplateNotFound 错误！"
            )
            logger.warning(f"❌ {error}")
            global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
            return False, error

        return True, ""

    @staticmethod
    def _l0_seed_data_check(target_file: str, code_content: str) -> Tuple[bool, str]:
        """
        L0.10: 种子数据检查。

        场景：models.py 的 init_db() 创建了 categories 等表但未插入种子数据
        → 前端下拉框为空 → 用户无法正常使用。

        检查逻辑：
        1. 在 init_db 函数体中查找 CREATE TABLE
        2. 如果表名包含 categor/type/tag/status 等分类关键词
        3. 检查同函数体内是否有 INSERT 语句
        """
        # 仅检查可能包含 init_db 的文件
        if 'init_db' not in code_content:
            return True, ""

        # 提取 init_db 函数体（简化：从 def init_db 到下一个 def 或文件末）
        init_match = re.search(r'def\s+init_db\s*\([^)]*\)\s*:', code_content)
        if not init_match:
            return True, ""

        func_start = init_match.end()
        next_def = re.search(r'\ndef\s+\w+\s*\(', code_content[func_start:])
        func_body = code_content[func_start:func_start + next_def.start()] if next_def else code_content[func_start:]

        # 查找 CREATE TABLE 语句中的分类相关表名
        SEED_KEYWORDS = {'categor', 'type', 'tag', 'status', 'role', 'level', 'priority'}
        create_tables = re.findall(
            r'CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["\']?(\w+)["\']?',
            func_body, re.IGNORECASE
        )

        needs_seed = []
        for table in create_tables:
            table_lower = table.lower()
            for kw in SEED_KEYWORDS:
                if kw in table_lower:
                    needs_seed.append(table)
                    break

        if not needs_seed:
            return True, ""  # 没有分类相关表，跳过

        # 检查是否有 INSERT 语句
        has_insert = bool(re.search(r'INSERT\s+(?:OR\s+\w+\s+)?INTO', func_body, re.IGNORECASE))

        if not has_insert:
            error = (
                f"[L0.10 种子数据缺失] {target_file}: "
                f"init_db() 创建了 {', '.join(needs_seed)} 表但未插入种子数据。"
                f"前端下拉框/选择器将为空！"
                f"请在 init_db() 中使用 INSERT OR IGNORE 插入默认分类数据"
                f"（如：餐饮、交通、购物、娱乐等）。"
            )
            logger.warning(f"❌ {error}")
            global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
            return False, error

        return True, ""

    def _l0_js_ast_semantic_check(self, target_file: str, code_content: str, sandbox_dir: str) -> Tuple[bool, str]:
        """
        L0.11: 跨文件 JS/TS 语义检查 (Tree-sitter)
        检查 1: JS 调用的所有 API URL 是否在后端路由表中。
        检查 2: JS 读取的所有属性变量（如 data.userName）是否严重偏离后端 models.py/Schema 的字段名。
        """
        FRONTEND_EXTS = {'.js', '.jsx', '.ts', '.tsx', '.vue'}
        ext = os.path.splitext(target_file)[1].lower()
        if ext not in FRONTEND_EXTS:
            return True, ""

        try:
            from core.js_ast_parser import JSAstParser
            parser = JSAstParser()
            semantic = parser.extract_semantic_info(code_content, ext)
        except Exception as e:
            logger.warning(f"JS AST 解析跳过: {e}")
            return True, ""

        api_urls = semantic.get("api_urls", set())
        props = semantic.get("property_access", set())

        if not api_urls and not props:
            return True, ""

        # --- 检查 1: API 路由幻影检查 ---
        if api_urls:
            registered_routes = set()
            try:
                from tools.observer import Observer
                obs = Observer(sandbox_dir)
                for root, dirs, files in os.walk(sandbox_dir):
                    dirs[:] = [d for d in dirs if d not in {'.git', '__pycache__', 'node_modules', '.sandbox', 'venv', '.venv'}]
                    for f in files:
                        if f.endswith('.py'):
                            rel = os.path.relpath(os.path.join(root, f), sandbox_dir).replace('\\', '/')
                            for r in obs.extract_routes(rel):
                                registered_routes.add(r['path'])
            except Exception:
                pass
            
            if registered_routes:
                phantom_urls = set()
                for url in api_urls:
                    if not url.startswith('/'):
                        continue # 我们只查全路径
                    matched = False
                    url_normalized = url.rstrip('/')
                    for route in registered_routes:
                        route_pattern = re.sub(r'<[^>]+>', '[^/]+', route)
                        route_pattern = re.sub(r'\{[^}]+\}', '[^/]+', route_pattern)
                        if re.fullmatch(route_pattern, url) or re.fullmatch(route_pattern, url_normalized):
                            matched = True
                            break
                        route_segments = route.rstrip('/').split('/')
                        if len(route_segments) > 1:
                            route_prefix = '/'.join(route_segments[:-1])
                            if route_prefix and (url == route_prefix or url_normalized == route_prefix):
                                matched = True
                                break
                    if not matched:
                        phantom_urls.add(url)
                
                if phantom_urls:
                    error = (f"[L0.11 JS API 不存在] {target_file}: "
                             f"前端请求了不存在的后端路由: {sorted(phantom_urls)}。"
                             f"后端已注册路由: {sorted(registered_routes)}。"
                             f"请修正 fetch/axios 参数以对接真实路由。")
                    logger.warning(f"❌ {error}")
                    global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
                    return False, error

        # --- 检查 2: JSON 字段悬空提取检查 (防御性提示为主) ---
        if props:
            # 读取后端 models.py 或 schemas.py
            class_keys = {}
            for fname in ('models.py', 'schemas.py', 'routes.py'):
                fpath = self._find_file_in_sandbox(sandbox_dir, fname)
                if fpath:
                    try:
                        with open(fpath, 'r', encoding='utf-8') as f:
                            ks = self._extract_to_dict_keys(f.read())
                            class_keys.update(ks)
                    except Exception:
                        pass
            
            all_be_keys = set()
            for ks in class_keys.values():
                all_be_keys |= ks
                
            if all_be_keys:
                # 排除通用字和常见前端自带字段
                commons = {'data', 'length', 'map', 'filter', 'push', 'json', 'log', 'target', 'value', 'status', 'error', 'message', 'msg'}
                suspicious = []
                for p in props:
                    if len(p) > 2 and p not in commons and p not in all_be_keys:
                        # 检测后端有没有对应的蛇形命名，比如 userName vs user_name
                        snake_v = re.sub(r'(?<!^)(?=[A-Z])', '_', p).lower()
                        if snake_v in all_be_keys and snake_v != p:
                            suspicious.append(f"使用了 '{p}'，但后端可能返回的是 '{snake_v}'")
                
                if suspicious:
                    error = (f"[L0.11 JS 字段驼峰错位] {target_file}: "
                             f"我们在前端提取到了极有可能的字段名错误：\n" + 
                             "\n".join([f" - {x}" for x in suspicious]) + 
                             "\n请立即确认 Python 接口返回 JSON 是驼峰还是蛇形，并对齐前端代码。")
                    logger.warning(f"❌ {error}")
                    global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
                    return False, error

        return True, ""

    def _l0_fetch_api_contract_check(self, target_file: str, code_content: str,
                                      sandbox_dir: str) -> Tuple[bool, str]:
        """
        L0.9 前后端 API 契约检查。

        场景：前端用 fetch/axios 发 DELETE/PUT/POST/PATCH 请求，但后端对应路由
        用 redirect() 响应 → 浏览器 302 跟随变 GET → 405 Method Not Allowed。

        检查逻辑：
        1. 仅在前端文件（HTML/JS/JSX/TSX/Vue）中扫描 fetch() 调用
        2. 提取 URL 和 HTTP method
        3. 在后端 Python 文件中查找对应路由
        4. 检查路由函数体是否包含 redirect() 而非 jsonify/return JSON
        """
        FRONTEND_EXTS = {'.html', '.htm', '.js', '.jsx', '.tsx', '.vue', '.svelte'}
        ext = os.path.splitext(target_file)[1].lower()

        if ext not in FRONTEND_EXTS:
            return True, ""  # 非前端文件，跳过

        # 1. 提取 fetch() 调用中的 URL 和 method
        fetch_calls = self._extract_fetch_calls(code_content)
        if not fetch_calls:
            return True, ""  # 没有 fetch 调用

        # 2. 收集后端 Python 文件
        backend_routes = self._collect_backend_routes(sandbox_dir)
        if not backend_routes:
            return True, ""  # 没有后端文件，跳过

        # 3. 逐个检查：非 GET 的 fetch 请求 + 对应路由返回 redirect
        violations = []
        for fc in fetch_calls:
            url = fc["url"]
            method = fc["method"].upper()

            if method == "GET":
                continue  # GET 请求 redirect 通常无害

            # 在后端路由中查找匹配
            for route in backend_routes:
                if self._url_matches_route(url, route["path"]):
                    if method in route.get("methods", []):
                        # 检查函数体是否有 redirect
                        if route.get("has_redirect") and not route.get("has_jsonify"):
                            violations.append(
                                f"前端 fetch('{url}', method='{method}') → "
                                f"后端 {route['file']}:{route['function']}() 使用了 redirect()。"
                                f"前端 fetch 的非 GET 请求收到 302 会变成 GET，导致 405 错误。"
                                f"请改为返回 jsonify 响应。"
                            )

        if violations:
            error = f"[L0.9 API 契约违规] {target_file}: " + " | ".join(violations)
            logger.warning(f"❌ {error}")
            global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
            return False, error

        return True, ""

    @staticmethod
    def _extract_fetch_calls(code: str) -> list:
        """从前端代码中提取 fetch() 调用的 URL 和 HTTP method"""
        results = []

        # 模式 1: fetch('/url', { method: 'DELETE' })
        pattern1 = re.compile(
            r"""fetch\s*\(\s*['"`]([^'"`]+)['"`]\s*,\s*\{[^}]*method\s*:\s*['"`](\w+)['"`]""",
            re.IGNORECASE | re.DOTALL
        )
        for m in pattern1.finditer(code):
            results.append({"url": m.group(1), "method": m.group(2)})

        # 模式 2: fetch('/url') — 默认 GET
        pattern2 = re.compile(
            r"""fetch\s*\(\s*['"`]([^'"`]+)['"`]\s*\)""",
            re.IGNORECASE
        )
        for m in pattern2.finditer(code):
            url = m.group(1)
            # 排除已经在模式1中捕获的
            if not any(r["url"] == url for r in results):
                results.append({"url": url, "method": "GET"})

        # 模式 3: axios.delete('/url'), axios.post('/url')
        pattern3 = re.compile(
            r"""axios\s*\.\s*(get|post|put|delete|patch)\s*\(\s*['"`]([^'"`]+)['"`]""",
            re.IGNORECASE
        )
        for m in pattern3.finditer(code):
            results.append({"url": m.group(2), "method": m.group(1)})

        return results

    def _collect_backend_routes(self, sandbox_dir: str) -> list:
        """扫描 sandbox 中所有 Python 文件，提取 Flask/FastAPI 路由信息"""
        routes = []
        for root, dirs, files in os.walk(sandbox_dir):
            dirs[:] = [d for d in dirs if d not in {'__pycache__', '.venv', 'node_modules', '.git'}]
            for f in files:
                if not f.endswith('.py'):
                    continue
                fpath = os.path.join(root, f)
                try:
                    with open(fpath, 'r', encoding='utf-8') as fp:
                        py_code = fp.read()
                    rel_path = os.path.relpath(fpath, sandbox_dir).replace('\\', '/')
                    routes.extend(self._parse_routes_from_python(py_code, rel_path))
                except Exception:
                    continue
        return routes

    @staticmethod
    def _parse_routes_from_python(py_code: str, file_path: str) -> list:
        """从 Python 代码中解析路由装饰器，提取路径、方法、是否有 redirect/jsonify"""
        routes = []

        # Flask: @app.route('/path', methods=['DELETE'])
        flask_pattern = re.compile(
            r"""@\w+\.route\s*\(\s*['"]([^'"]+)['"]\s*(?:,\s*methods\s*=\s*\[([^\]]*)\])?\s*\)"""
            r"""\s*def\s+(\w+)\s*\(""",
            re.DOTALL
        )

        for m in flask_pattern.finditer(py_code):
            path = m.group(1)
            methods_str = m.group(2) or "'GET'"
            func_name = m.group(3)
            methods = re.findall(r"['\"](\w+)['\"]", methods_str)
            methods = [m_val.upper() for m_val in methods]

            # 提取函数体（简化：取装饰器后到下一个 def/class 或文件末）
            func_start = m.end()
            next_def = re.search(r'\ndef\s|\nclass\s', py_code[func_start:])
            func_body = py_code[func_start:func_start + next_def.start()] if next_def else py_code[func_start:]

            routes.append({
                "path": path,
                "methods": methods,
                "function": func_name,
                "file": file_path,
                "has_redirect": "redirect(" in func_body,
                "has_jsonify": "jsonify(" in func_body or "json.dumps(" in func_body or "JSONResponse(" in func_body,
            })

        # FastAPI: @app.delete('/path')
        fastapi_pattern = re.compile(
            r"""@\w+\.(get|post|put|delete|patch)\s*\(\s*['"]([^'"]+)['"]\s*\)"""
            r"""\s*(?:async\s+)?def\s+(\w+)\s*\(""",
            re.DOTALL
        )
        for m in fastapi_pattern.finditer(py_code):
            method = m.group(1).upper()
            path = m.group(2)
            func_name = m.group(3)

            func_start = m.end()
            next_def = re.search(r'\ndef\s|\nclass\s|\nasync\s+def\s', py_code[func_start:])
            func_body = py_code[func_start:func_start + next_def.start()] if next_def else py_code[func_start:]

            routes.append({
                "path": path,
                "methods": [method],
                "function": func_name,
                "file": file_path,
                "has_redirect": "redirect(" in func_body or "RedirectResponse(" in func_body,
                "has_jsonify": "jsonify(" in func_body or "JSONResponse(" in func_body or "json.dumps(" in func_body or "return {" in func_body,
            })

        return routes

    @staticmethod
    def _url_matches_route(fetch_url: str, route_path: str) -> bool:
        """简单的 URL vs 路由路径匹配（支持 Flask/FastAPI 的 <param> 和 {param} 语法）"""
        # 将路由路径中的参数占位符替换为通配
        route_regex = re.sub(r'<[^>]+>|\{[^}]+\}', r'[^/]+', route_path)
        route_regex = '^' + route_regex + '$'
        # 去掉 fetch URL 中的模板字符串变量
        clean_url = re.sub(r'\$\{[^}]+\}', 'PLACEHOLDER', fetch_url)
        try:
            return bool(re.match(route_regex, clean_url))
        except re.error:
            return fetch_url == route_path

    def _l0_import_check(self, target_file: str, sandbox_dir: str) -> Tuple[bool, str]:

        """L0.3: 在沙盒中尝试 import 模块，超时 10 秒"""
        # 从文件路径提取模块名
        module_name = os.path.splitext(os.path.basename(target_file))[0]

        # sys.path 需要同时包含项目根目录和文件所在目录
        # 这样 from src.models import X（根目录相对）和 from models import X（同目录相对）都能工作
        dir_part = os.path.dirname(target_file).replace("\\", "/")
        path_setup = "import sys\nsys.path.insert(0, '.')"
        if dir_part:
            path_setup += f"\nsys.path.insert(0, '{dir_part}')"

        test_code = f"""
{path_setup}
try:
    import {module_name}
    print("✅ IMPORT_OK")
except Exception as e:
    print(f"❌ IMPORT_FAIL: {{e}}")
    import sys; sys.exit(1)
"""
        # 使用较短超时（10s 而非默认 60s）
        try:
            result = sandbox_env.execute_code(
                test_code, self.project_id, sandbox_dir=sandbox_dir, timeout=10)

            stdout = result.get("stdout", "")
            stderr = result.get("stderr", "")

            if "IMPORT_OK" in stdout:
                return True, ""

            # 超时（可能是 uvicorn.run 泄露到顶层等情况）→ 跳过，交给 L2
            if result.get("returncode") == -1 and "timed out" in stderr.lower():
                logger.warning("⚠️ [L0.3] import 超时（可能有阻塞代码），跳过导入检查")
                return True, ""

            # 错误信息可能在 stdout（IMPORT_FAIL）或 stderr
            fail_detail = ""
            if "IMPORT_FAIL:" in stdout:
                fail_detail = stdout.split("IMPORT_FAIL:")[1].strip()[:500]
            elif stderr:
                fail_detail = stderr[:500]

            failure_type, missing_symbol, import_module = self._parse_import_failure_detail(fail_detail)
            local_module_path = self._resolve_local_module_path(
                target_file, sandbox_dir, import_module
            ) if import_module else ""

            if failure_type == "cannot_import_name" and local_module_path:
                cross_file_prefix = (
                    f"[CROSS_FILE:{local_module_path}] "
                    f"[IMPORTER_FILE:{target_file}] "
                    f"[MISSING_SYMBOL:{missing_symbol}] "
                )
                error = (
                    cross_file_prefix
                    + f"[L0.3 本地导入缺符号] {target_file}: "
                    f"从本地模块 {import_module} ({local_module_path}) "
                    f"导入缺失符号 `{missing_symbol}`。原始错误: {fail_detail}"
                )
                logger.warning(f"❌ {error}")
                global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
                return False, error
            if failure_type == "cannot_import_name":
                logger.warning(f"⚠️ [L0.3] 第三方导入缺符号（保守跳过）: {fail_detail[:200]}")
                return True, ""

            # 已知良性错误：L0.3 的单文件 import 无法模拟完整包环境，跳过交给 L2
            benign_patterns = [
                "relative import",        # from .models import X（包内相对导入）
                "No module named",         # 兄弟模块还没写好 / 第三方未装
                "Invalid args for response field",  # FastAPI Pydantic 模型校验
                "is not a valid Pydantic field",    # FastAPI 响应模型类型错误
                "ValidationError",                   # Pydantic 校验错误
                "value is not a valid",              # Pydantic 字段校验
            ]
            if any(p in fail_detail for p in benign_patterns):
                logger.warning(f"⚠️ [L0.3] 良性导入错误（跳过）: {fail_detail[:200]}")
                return True, ""

            error = f"[L0.3 导入失败] {target_file}: {fail_detail}"
            logger.warning(f"❌ {error}")
            global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
            return False, error

        except Exception as e:
            logger.warning(f"⚠️ [L0.3] 导入检查异常: {e}，跳过")
            return True, ""

    @staticmethod
    def _parse_import_failure_detail(fail_detail: str) -> Tuple[str, str, str]:
        """解析 import 失败信息，提取错误类型、缺失符号和来源模块。"""
        cannot_import = re.search(
            r"cannot import name ['\"]?([^'\"]+)['\"]? from ['\"]?([^'\"]+)['\"]?",
            fail_detail,
        )
        if cannot_import:
            return (
                "cannot_import_name",
                cannot_import.group(1).strip(),
                cannot_import.group(2).strip(),
            )

        no_module = re.search(
            r"No module named ['\"]?([^'\"]+)['\"]?",
            fail_detail,
        )
        if no_module:
            return ("no_module_named", "", no_module.group(1).strip())

        return ("", "", "")

    @staticmethod
    def _resolve_local_module_path(target_file: str, sandbox_dir: str, module_name: str) -> str:
        """判断导入失败是否来自项目内本地模块，并返回匹配到的相对路径。"""
        if not sandbox_dir or not module_name:
            return ""

        module_name = module_name.strip().lstrip(".")
        if not module_name:
            return ""

        target_dir = os.path.dirname(target_file).replace("\\", "/").strip("/")
        module_rel = module_name.replace(".", "/").strip("/")

        candidates = [
            f"{module_rel}.py",
            f"{module_rel}/__init__.py",
        ]
        if target_dir:
            candidates.extend([
                f"{target_dir}/{module_rel}.py",
                f"{target_dir}/{module_rel}/__init__.py",
            ])

        seen = set()
        for candidate in candidates:
            normalized = os.path.normpath(candidate).replace("\\", "/")
            if normalized in seen or normalized.startswith("../"):
                continue
            seen.add(normalized)
            abs_path = os.path.join(sandbox_dir, *normalized.split("/"))
            if os.path.isfile(abs_path):
                return normalized

        return ""

    @staticmethod
    def _extract_defined_symbols(tree: ast.AST) -> set:
        """从 AST 中提取所有顶层定义的函数名、类名、变量名"""
        defined = set()
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(node.name)
            elif isinstance(node, ast.ClassDef):
                defined.add(node.name)
                # 也提取类的方法
                for item in ast.iter_child_nodes(node):
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        defined.add(f"{node.name}.{item.name}")
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        defined.add(target.id)
        return defined

    @staticmethod
    def _extract_expected_symbols(target_file: str,
                                  module_interfaces: dict,
                                  project_spec: dict = None) -> list:
        """提取当前文件的验收符号；路由文件优先使用 route_contracts。"""
        symbols = extract_expected_symbols_for_target(
            project_spec or {},
            target_file,
            module_interfaces or {},
        )
        if symbols:
            return symbols

        if not module_interfaces:
            return []

        basename = os.path.basename(target_file)
        iface_str = module_interfaces.get(basename, "")
        if not iface_str:
            # 尝试不带扩展名
            iface_str = module_interfaces.get(os.path.splitext(basename)[0], "")
        if not iface_str:
            return []

        # 从 "def create_app(), USD_TO_CNY = 7.2, class Config" 等格式中提取符号
        symbols = []
        # 匹配 def xxx 或 class xxx
        for match in re.finditer(r'(?:def|class)\s+(\w+)', str(iface_str)):
            symbols.append(match.group(1))
        # 匹配 VAR = ... 格式的常量
        for match in re.finditer(r'(\b[A-Z_][A-Z_0-9]*)\s*=', str(iface_str)):
            symbols.append(match.group(1))
        return symbols

    @staticmethod
    def _l0_architecture_contract_check(target_file: str, code_content: str,
                                        project_spec: dict) -> Tuple[bool, str]:
        """L0.3A: 检查单文件是否偏离已编译的 architecture_contract。"""
        if not target_file.endswith('.py') or not isinstance(project_spec, dict):
            return True, ""

        contract = project_spec.get("architecture_contract", {}) or {}
        if not isinstance(contract, dict) or not contract:
            return True, ""

        signals = ((project_spec.get("compiler_metadata") or {}).get("architecture_signals") or {})
        local_module_names = [
            str(item).strip()
            for item in (signals.get("local_module_names") or [])
            if str(item).strip()
        ]
        code = str(code_content or "")
        lowered = code.lower()

        backend_framework = str(contract.get("backend_framework") or "unknown").lower()
        orm_mode = str(contract.get("orm_mode") or "unknown").lower()
        auth_mode = str(contract.get("auth_mode") or "unknown").lower()
        router_mode = str(contract.get("router_mode") or "unknown").lower()
        package_layout = str(contract.get("package_layout") or "unknown").lower()
        import_style = str(contract.get("import_style") or "unknown").lower()

        violations = []

        def _contains_any(tokens: tuple[str, ...]) -> bool:
            return any(token in lowered for token in tokens)

        if backend_framework == "fastapi" and _contains_any((
            "from flask import",
            "import flask",
            "blueprint(",
            "register_blueprint",
            "from flask_login",
            "loginmanager",
            "from flask_sqlalchemy",
            "flask_sqlalchemy",
        )):
            violations.append("当前文件出现了 Flask / Blueprint / Flask-Login / Flask-SQLAlchemy 语义，但项目架构要求 FastAPI")

        if backend_framework == "flask" and _contains_any((
            "from fastapi import",
            "import fastapi",
            "fastapi(",
            "apirouter",
            "include_router",
            "depends(",
        )):
            violations.append("当前文件出现了 FastAPI / APIRouter / Depends 语义，但项目架构要求 Flask")

        if orm_mode == "sqlalchemy_session" and _contains_any((
            "from flask_sqlalchemy",
            "flask_sqlalchemy",
            "db = sqlalchemy(",
            "db.model",
            "db.session",
        )):
            violations.append(
                "当前文件使用了 Flask-SQLAlchemy 语义（db.Model/db.session），"
                "但项目架构要求 sqlalchemy session。"
                "【修复方法】使用 `from sqlalchemy import create_engine; from sqlalchemy.orm import sessionmaker, declarative_base` + "
                "`Base = declarative_base()` + `engine = create_engine(...)` + `SessionLocal = sessionmaker(bind=engine)`，"
                "禁止使用 flask_sqlalchemy"
            )

        if orm_mode == "flask_sqlalchemy" and _contains_any((
            "declarative_base",
            "sessionmaker",
            "sessionlocal",
            "create_engine(",
        )):
            violations.append(
                "当前文件使用了 sqlalchemy session 语义（declarative_base/sessionmaker/create_engine），"
                "但项目架构要求 Flask-SQLAlchemy。"
                "【修复方法】使用 `from flask_sqlalchemy import SQLAlchemy; db = SQLAlchemy()` + "
                "`db.Model` 基类 + `db.Column(...)` 定义字段，"
                "禁止使用 declarative_base / sessionmaker / create_engine"
            )

        if auth_mode == "jwt_header" and _contains_any((
            "from flask_login",
            "loginmanager",
            "login_user",
            "logout_user",
            "current_user",
            "@login_required",
        )):
            violations.append("当前文件使用了 Flask-Login session 语义，但项目架构要求 JWT header 认证")

        if auth_mode == "flask_login_session" and _contains_any((
            "oauth2passwordbearer",
            "create_access_token",
            "from jose import jwt",
            "import jwt",
            "bearer ",
        )):
            violations.append("当前文件使用了 JWT / OAuth2 bearer 语义，但项目架构要求 Flask-Login session")

        if router_mode == "fastapi_apirouter" and _contains_any((
            "blueprint(",
            "register_blueprint",
            ".route(",
        )):
            violations.append("当前文件使用了 Blueprint 路由语义，但项目架构要求 APIRouter")

        if router_mode == "flask_blueprint" and _contains_any((
            "apirouter",
            "include_router",
            "@router.",
            "@api_router.",
            "@app.get(",
            "@app.post(",
            "@app.put(",
            "@app.delete(",
            "@app.patch(",
        )):
            violations.append("当前文件使用了 FastAPI 路由语义，但项目架构要求 Flask Blueprint")

        if package_layout == "flat_modules" and import_style == "sibling_import":
            if re.search(r"\bfrom\s+(?:src|backend\.src)\.[A-Za-z_][A-Za-z0-9_\.]*\s+import\b", code):
                violations.append("当前文件使用了 `from src...` 包式导入，但项目是 flat_modules + sibling_import")
            if re.search(r"\bfrom\s+\.[A-Za-z_][A-Za-z0-9_\.]*\s+import\b", code):
                violations.append("当前文件使用了相对包导入，但项目是 flat_modules + sibling_import")

        if package_layout == "package_src" and import_style == "package_import":
            for module_name in local_module_names:
                if re.search(rf"\bfrom\s+{re.escape(module_name)}\s+import\b", code):
                    violations.append(
                        f"当前文件使用了 sibling_import (`from {module_name} import ...`)，但项目要求 package_import"
                    )
                    break

        if violations:
            error = f"[L0.3A 架构契约违规] {target_file}: " + "；".join(violations)
            logger.warning(f"❌ {error}")
            global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
            return False, error

        return True, ""

    # ============================================================
    # L1: 合约审计（轻量 LLM，只读不执行）
    # ============================================================

    def _l1_contract_audit(self, target_file: str, code_content: str,
                           description: str, memory_hint: str,
                           module_interfaces: dict) -> Tuple[bool, str]:
        """
        L1 合约审计：LLM 阅读代码检查接口一致性。
        不生成测试脚本，不执行沙盒。
        ~800 tokens。
        """
        system_prompt = Prompts.REVIEWER_SYSTEM + memory_hint

        # 构建上下文
        iface_str = ""
        if module_interfaces:
            iface_str = "\n".join([f"  {k}: {v}" for k, v in module_interfaces.items()])

        user_content = (
            f"【当前要审查的文件】: {target_file}\n"
            f"【业务需求描述】: {description}\n"
            f"【Coder 提交的代码内容】:\n```\n{code_content}\n```\n\n"
        )
        if iface_str:
            user_content += f"【规划书接口契约 module_interfaces】:\n{iface_str}\n\n"

        user_content += "请检查代码是否满足上述接口契约，输出 JSON 结果。"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ]

        try:
            response_msg = default_llm.chat_completion(
                messages=messages,
                model=self.model,
                temperature=0.1,
                enable_thinking=self.enable_thinking,
                reasoning_effort=self._reasoning_effort,
            )

            raw = response_msg.content.strip()
            # 尝试解析 JSON
            return self._parse_audit_result(raw)

        except Exception as e:
            logger.warning(f"⚠️ [L1] LLM 调用异常: {e}，放行")
            return True, "[SOFT_PASS] 审查通过（LLM 异常，跳过）"

    @staticmethod
    def _parse_audit_result(raw: str) -> Tuple[bool, str]:
        """解析 L1 审计结果（JSON 格式）"""
        # 清理 markdown 包裹
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0].strip()
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0].strip()

        try:
            result = json.loads(raw)
            status = result.get("status", "").upper()
            feedback = result.get("feedback", "")
            if status == "PASS":
                return True, feedback or "合约审计通过"
            else:
                return False, f"[L1 合约审计] {feedback}"
        except (json.JSONDecodeError, TypeError):
            # LLM 输出非 JSON → 尝试从文本中识别 PASS/FAIL
            if "PASS" in raw.upper():
                return True, "合约审计通过"
            elif "FAIL" in raw.upper():
                return False, f"[L1 合约审计] {raw[:500]}"
            else:
                # 无法解析，默认放行（带 SOFT_PASS 标记）
                return True, "[SOFT_PASS] 合约审计通过（LLM 输出格式异常，已放行）"

    # ============================================================
    # 上下文构建（保留自 v2）
    # ============================================================

    def _build_review_context(self, target_file: str, module_interfaces: dict = None) -> str:
        """构建 Reviewer 的记忆上下文"""
        memory_hint = ""

        # 1. 短期记忆 → 项目文件树
        file_tree_events = get_recent_events(
            project_id=self.project_id, limit=1,
            event_types=["file_tree"], caller="Reviewer"
        )
        if file_tree_events:
            memory_hint += f"\n\n【📂 当前项目文件结构】\n{file_tree_events[0].content[:500]}"

        # 2. 跨文件接口契约
        if module_interfaces:
            iface_str = "\n".join([f"  {k}: {v}" for k, v in module_interfaces.items()])
            memory_hint += f"\n\n【🔗 跨文件接口契约】\n{iface_str}"

        # 3. Reviewer 经验召回（预防已知错误）
        try:
            test_exps = recall_reviewer_experience(
                f"{target_file}", n_results=2, caller="Reviewer"
            )
            if test_exps:
                exp_str = "\n".join([f"  {i+1}. {e[:200]}" for i, e in enumerate(test_exps)])
                memory_hint += f"\n\n【🧪 历史审查经验】\n{exp_str}"
        except Exception as e:
            logger.warning(f"⚠️ Reviewer 经验召回失败: {e}")

        return memory_hint

    # ============================================================
    # L0-Contract: 契约校验（确定性，0 LLM 消耗）
    # ============================================================

    def _l0_contract_check(self, target_file: str, code_content: str,
                           sandbox_dir: str, project_spec: dict) -> Tuple[bool, str]:
        """L0-Contract: 基于 page_routes / template_contracts 的契约校验。
        当契约存在时，以契约为权威基准校验 Coder 的代码。"""
        if not project_spec:
            return True, ""

        page_routes = project_spec.get("page_routes", [])
        effective_route_manifest = project_spec.get("effective_route_manifest", [])
        app_registration_contracts = project_spec.get("app_registration_contracts", [])
        template_contracts = project_spec.get("template_contracts", {})
        module_interfaces = project_spec.get("module_interfaces", {}) or {}

        if not page_routes and not template_contracts and not effective_route_manifest and not module_interfaces:
            return True, ""  # 无契约 → 跳过

        contract_warnings = []

        # [SOFT] C1: 后端路由函数 vs 契约路径一致性（跨文件启发式）
        if target_file.endswith('.py') and (
            page_routes or effective_route_manifest or app_registration_contracts or module_interfaces.get(target_file)
        ):
            c1_pass, c1_error = self._l0_contract_c1_route_check(
                target_file, code_content, project_spec, sandbox_dir)
            if not c1_pass:
                logger.warning(f"⚠️ [SOFT-C1] {c1_error}")
                contract_warnings.append(c1_error)

        # [SOFT] C2: HTML form action / href vs 契约路径一致性（跨文件启发式）
        if target_file.endswith('.html') and page_routes:
            c2_pass, c2_error = self._l0_contract_c2_html_url_check(
                target_file, code_content, page_routes)
            if not c2_pass:
                logger.warning(f"⚠️ [SOFT-C2] {c2_error}")
                contract_warnings.append(c2_error)

        # [SOFT] C3: template_folder 路径 vs 实际目录结构一致性（跨文件启发式）
        if target_file.endswith('.py') and sandbox_dir:
            c3_pass, c3_error = self._l0_contract_c3_template_folder_check(
                target_file, code_content, sandbox_dir)
            if not c3_pass:
                logger.warning(f"⚠️ [SOFT-C3] {c3_error}")
                contract_warnings.append(c3_error)

        if contract_warnings:
            warning_text = " | ".join(contract_warnings)
            logger.info(f"📋 [L0-Contract SOFT] {target_file}: {len(contract_warnings)} 个契约警告（不阻断）")
            global_broadcaster.emit_sync("Reviewer", "l0_contract_soft_warning",
                f"⚠️ L0-Contract SOFT 警告 ({len(contract_warnings)}): {warning_text[:200]}")

        return True, ""

    def _l0_contract_c1_route_check(self, target_file: str, code_content: str,
                                    project_spec: dict,
                                    sandbox_dir: str = "") -> Tuple[bool, str]:
        """L0.C1: 检查 Python 路由文件中注册的路径是否与 page_routes 契约一致。
        仅对包含路由注册的文件生效（routes.py / app.py 等）。"""
        import re as _re

        # 只检查包含路由注册的文件
        has_route_registration = (
            'Blueprint(' in code_content or
            'add_url_rule' in code_content or
            'register_blueprint' in code_content or
            '@app.' in code_content or
            '@router.' in code_content or
            '@bp.' in code_content or
            'route(' in code_content
        )
        if not has_route_registration:
            return True, ""

        effective_manifest = project_spec.get("effective_route_manifest", []) or []
        app_registration_contracts = project_spec.get("app_registration_contracts", []) or []

        topology_pass, topology_error = self._l0_route_topology_check(
            target_file, code_content, project_spec, sandbox_dir
        )
        if not topology_pass:
            return False, topology_error

        # 优先使用编译后的闭环契约
        if 'register_blueprint' in code_content and app_registration_contracts:
            code_registrations = set()
            for m in _re.finditer(
                r"register_blueprint\s*\(\s*(\w+)(?:\s*,\s*url_prefix\s*=\s*['\"]([^'\"]*)['\"])?",
                code_content
            ):
                blueprint = m.group(1)
                url_prefix = self._normalize_contract_path(m.group(2) or "")
                code_registrations.add((blueprint, url_prefix))

            expected_registrations = set()
            for item in app_registration_contracts:
                if not isinstance(item, dict):
                    continue
                app_module = item.get("app_module")
                if app_module and app_module != target_file:
                    continue
                expected_registrations.add((
                    item.get("blueprint"),
                    self._normalize_contract_path(item.get("url_prefix", "")),
                ))

            missing_regs = expected_registrations - code_registrations
            if missing_regs:
                error = (
                    f"[L0.C1 路由契约违规] {target_file}: "
                    f"app 注册契约要求以下 blueprint/url_prefix，但代码中未找到: {sorted(missing_regs)}。"
                    f"代码中已注册: {sorted(code_registrations)}。"
                )
                logger.warning(f"❌ {error}")
                global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
                return False, error

            if expected_registrations:
                logger.info(f"✅ [L0.C1] app 注册契约校验通过: {target_file}")
                return True, ""

        manifest_for_file = [
            item for item in effective_manifest
            if isinstance(item, dict)
            and item.get("module") == target_file
            and item.get("local_path") is not None
        ]
        if manifest_for_file:
            code_routes = set()
            for m in _re.finditer(
                r'@(?P<blueprint>\w+)\.(?:route|get|post|put|delete|patch)\s*\(\s*["\'](?P<path>/[^"\']*)["\']',
                code_content
            ):
                code_routes.add((
                    m.group("blueprint"),
                    self._normalize_contract_path(m.group("path")),
                ))

            expected_routes = {
                (
                    item.get("blueprint"),
                    self._normalize_contract_path(item.get("local_path", "")),
                )
                for item in manifest_for_file
            }
            missing_in_code = expected_routes - code_routes
            if missing_in_code:
                error = (
                    f"[L0.C1 路由契约违规] {target_file}: "
                    f"编译后契约要求以下 blueprint/local_path，但代码中未找到: {sorted(missing_in_code)}。"
                    f"代码中已注册: {sorted(code_routes)}。"
                )
                logger.warning(f"❌ {error}")
                global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
                return False, error

            logger.info(f"✅ [L0.C1] 局部路由契约校验通过: {target_file}")
            return True, ""

        page_routes = project_spec.get("page_routes", []) or []

        # 从代码中提取所有注册路径
        code_routes = set()
        # @app.route('/path') / @bp.route('/path') / @router.get('/path')
        for m in _re.finditer(r'@\w+\.(?:route|get|post|put|delete|patch)\s*\(\s*["\'](/[^"\']*)["\']', code_content):
            code_routes.add(m.group(1))
        # add_url_rule('/path', ...)
        for m in _re.finditer(r'add_url_rule\s*\(\s*["\'](/[^"\']*)["\']', code_content):
            code_routes.add(m.group(1))

        if not code_routes:
            return True, ""  # 没提取到路由路径，跳过

        # 从契约提取所有路径
        contract_routes = set()
        for r in page_routes:
            contract_routes.add(r.get('path', ''))

        # 契约中有但代码中没有的路径（Coder 遗漏注册）
        missing_in_code = contract_routes - code_routes
        if missing_in_code:
            error = (
                f"[L0.C1 路由契约违规] {target_file}: "
                f"page_routes 契约要求注册以下路径，但代码中未找到: {sorted(missing_in_code)}。"
                f"代码中已注册的路径: {sorted(code_routes)}。"
                f"修复方法：按照契约补全缺失的路由注册。"
            )
            logger.warning(f"❌ {error}")
            global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
            return False, error

        logger.info(f"✅ [L0.C1] 路由契约校验通过: {target_file}")
        return True, ""

    def _l0_route_topology_check(self, target_file: str, code_content: str,
                                 project_spec: dict, sandbox_dir: str = "") -> Tuple[bool, str]:
        """补充校验：
        1. blueprint 已挂 url_prefix 时，局部路由不得重复写前缀
        2. 规划中声明的 handler 存在但未注册为端点时，直接驳回
        """
        blueprint_vars = set(extract_blueprint_variables_from_code(code_content))
        route_bindings = extract_route_bindings_from_code(code_content)

        if not blueprint_vars and not route_bindings:
            return True, ""

        prefix_by_blueprint = {}
        for item in (project_spec.get("app_registration_contracts", []) or []):
            if not isinstance(item, dict) or not item.get("blueprint"):
                continue
            prefix_by_blueprint[item["blueprint"]] = self._normalize_contract_path(
                item.get("url_prefix", "")
            )

        if sandbox_dir:
            for app_name in ("app.py", "main.py", "__init__.py"):
                app_path = self._find_file_in_sandbox(sandbox_dir, app_name)
                if not app_path:
                    continue
                try:
                    with open(app_path, "r", encoding="utf-8") as fh:
                        app_code = fh.read()
                except Exception:
                    continue
                for item in extract_blueprint_registrations_from_code(app_code):
                    prefix_by_blueprint[item["blueprint"]] = self._normalize_contract_path(
                        item.get("url_prefix", "")
                    )

        double_prefixed = []
        for binding in route_bindings:
            blueprint = binding.get("blueprint", "")
            local_path = self._normalize_contract_path(binding.get("local_path", ""))
            url_prefix = prefix_by_blueprint.get(blueprint, "")
            if not blueprint or not local_path or not url_prefix:
                continue
            if local_path == url_prefix or local_path.startswith(url_prefix + "/"):
                double_prefixed.append(
                    f"{blueprint}:{binding.get('handler') or '<anonymous>'} -> {local_path} (url_prefix={url_prefix})"
                )

        if double_prefixed:
            error = (
                f"[L0.C1 路由拓扑错误] {target_file}: 检测到 blueprint 局部路由重复写入 app url_prefix，"
                f"会导致双重前缀和 404。问题项: {double_prefixed}。"
                f"修复方法：route 文件内只保留 blueprint 相对路径，例如把 '/api/auth/login' 改成 '/login'。"
            )
            logger.warning(f"❌ {error}")
            global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
            return False, error

        module_contracts = project_spec.get("route_module_contracts", {}) or {}
        module_contract = module_contracts.get(target_file, {}) if isinstance(module_contracts, dict) else {}

        # init_function 模式：路由通过 init_xxx_routes(app) 内部的 add_url_rule 注册，
        # handler 函数本身没有 @bp.route 装饰器，extract_route_bindings 无法可靠提取。
        # 跳过"路由未注册"检查，防止持续误报消耗重试预算。
        contract_mode = str(module_contract.get("mode") or "").strip().lower()
        if contract_mode == "init_function":
            logger.info(f"✅ [L0.C1] {target_file}: init_function 模式，跳过路由未注册检查")
            return True, ""

        helper_names = {
            name for name in (module_contract.get("helper_functions", []) or [])
            if isinstance(name, str)
        }
        expected_handlers = [
            name for name in extract_module_interface_handlers(
                project_spec.get("module_interfaces", {}) or {},
                target_file,
            )
            if name and not is_non_endpoint_helper(name) and name not in helper_names
        ]
        contract_handlers = [
            name for name in extract_contract_handlers(project_spec).get(target_file, [])
            if name and not is_non_endpoint_helper(name) and name not in helper_names
        ]
        expected_handlers = sorted(dict.fromkeys(expected_handlers + contract_handlers))
        if not expected_handlers:
            expected_handlers = [
                name for name in extract_top_level_function_names(code_content)
                if looks_like_endpoint_function(name) and name not in helper_names
            ]

        if expected_handlers:
            registered_handlers = {
                binding.get("handler")
                for binding in route_bindings
                if binding.get("handler")
            }
            missing_handlers = []
            for handler in expected_handlers:
                if not handler or handler in registered_handlers:
                    continue
                # GET/POST 拆分 或 命名空间冲突变体：
                # edit_expense → edit_expense_get / edit_expense_post
                # delete_expense → delete_expense_handler（因 from models import delete_expense 同名）
                has_variant = any(
                    f"{handler}_{suffix}" in registered_handlers
                    for suffix in ("get", "post", "page", "view", "form",
                                   "submit", "handler", "route", "action",
                                   "endpoint", "api")
                )
                if has_variant:
                    continue
                # 反向检查：registered 中是否有以 handler 为前缀的函数
                has_prefix_match = any(
                    rh.startswith(handler + "_") for rh in registered_handlers
                )
                if has_prefix_match:
                    continue
                missing_handlers.append(handler)

            if missing_handlers and blueprint_vars:
                error = (
                    f"[L0.C1 路由未注册] {target_file}: 发现已声明但未注册为 HTTP 端点的 handler: "
                    f"{missing_handlers}。"
                    f"修复方法：为这些函数补齐 @{sorted(blueprint_vars)[0]}.route(...) 或 add_url_rule(...)。"
                )
                logger.warning(f"❌ {error}")
                global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
                return False, error

        return True, ""

    @staticmethod
    def _normalize_contract_path(path: str) -> str:
        path = (path or "").strip()
        if not path or path == "/":
            return ""
        if not path.startswith("/"):
            path = "/" + path
        path = path.rstrip("/")
        return path or ""

    def _l0_contract_c2_html_url_check(self, target_file: str, html_content: str,
                                       page_routes: list) -> Tuple[bool, str]:
        """L0.C2: 检查 HTML 中 form action / href 是否全部存在于 page_routes 契约。
        当 page_routes 契约存在时，以契约为权威基准（替代动态扫描）。"""
        import re as _re

        # 从契约提取所有合法路径
        contract_paths = set()
        for r in page_routes:
            path = r.get('path', '')
            if path:
                contract_paths.add(path)
        contract_paths.add('/')  # 根路由总是合法

        # 从 HTML 中提取所有内部 URL
        html_urls = set()
        # action="/xxx" 和 href="/xxx"（排除锚点、外链、static、Jinja 动态路径）
        for m in _re.finditer(r'(?:href|action)\s*=\s*["\'](/[^"\'#]*)["\']', html_content):
            url = m.group(1).split('?')[0].rstrip('/')
            if not url:
                url = '/'
            # 跳过静态资源和 Jinja 动态路径
            if url.startswith('/static') or '{{' in m.group(0) or '{%' in m.group(0):
                continue
            html_urls.add(url)
        # fetch("/xxx")
        for m in _re.finditer(r'fetch\s*\(\s*[`"\'](/[^`"\']*)[`"\']', html_content):
            url = m.group(1).split('?')[0].rstrip('/')
            if not url:
                url = '/'
            html_urls.add(url)

        if not html_urls:
            return True, ""  # 没引用到任何内部 URL

        # 对比：HTML 中引用了契约中不存在的路径
        phantom_urls = set()
        for url in html_urls:
            # 支持路径参数匹配（如 /entries/123 匹配 /entries/<int:id>）
            matched = False
            for contract_path in contract_paths:
                # 精确匹配
                if url == contract_path:
                    matched = True
                    break
                # 路径参数通配（Flask <xxx> / FastAPI {xxx}）
                pattern = _re.sub(r'<[^>]+>', '[^/]+', contract_path)
                pattern = _re.sub(r'\{[^}]+\}', '[^/]+', pattern)
                if _re.fullmatch(pattern, url):
                    matched = True
                    break
            if not matched:
                phantom_urls.add(url)

        if phantom_urls:
            error = (
                f"[L0.C2 URL 契约违规] {target_file}: "
                f"HTML 引用的 URL {sorted(phantom_urls)} 不在 page_routes 契约中。"
                f"契约定义的合法路径: {sorted(contract_paths)}。"
                f"修复方法：将 URL 改为契约中定义的路径。"
            )
            logger.warning(f"❌ {error}")
            global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
            return False, error

        logger.info(f"✅ [L0.C2] HTML URL 契约校验通过: {target_file}")
        return True, ""

    def _l0_contract_c3_template_folder_check(self, target_file: str, code_content: str,
                                               sandbox_dir: str) -> Tuple[bool, str]:
        """L0.C3: 检查 Flask template_folder 参数指向的路径是否实际存在。
        比 L0.7 更精确：L0.7 只检查有没有 template_folder，C3 验证路径是否正确。"""
        import re as _re

        # 只检查包含 Flask( 的文件
        if 'Flask(' not in code_content:
            return True, ""

        # 提取 template_folder 参数值
        m = _re.search(r'template_folder\s*=\s*["\'](.*?)["\']', code_content)
        if not m:
            return True, ""  # 没指定 template_folder（使用默认），跳过

        tf_value = m.group(1)  # 如 '../templates'

        # 计算绝对路径：target_file 所在目录 + template_folder 的相对路径
        py_dir = os.path.dirname(os.path.join(sandbox_dir, target_file))
        resolved = os.path.normpath(os.path.join(py_dir, tf_value))

        # 检查该路径是否存在
        if os.path.isdir(resolved):
            logger.info(f"✅ [L0.C3] template_folder 路径验证通过: {tf_value} → {resolved}")
            return True, ""

        # 路径不存在 → 尝试推断正确路径
        correct_tf = None
        # 检查 templates/ 是不是在沙盒根目录
        root_templates = os.path.join(sandbox_dir, 'templates')
        if os.path.isdir(root_templates):
            correct_rel = os.path.relpath(root_templates, py_dir).replace('\\', '/')
            correct_tf = correct_rel

        suggestion = ""
        if correct_tf:
            if correct_tf == 'templates':
                suggestion = "正确值应为 template_folder='templates'，或直接删除此参数（Flask 默认就是 templates/）。"
            else:
                suggestion = f"正确值应为 template_folder='{correct_tf}'。"
        else:
            suggestion = "请确认 templates/ 目录的位置，或删除 template_folder 参数使用默认值。"

        error = (
            f"[L0.C3 模板路径错误] {target_file}: "
            f"template_folder='{tf_value}' 解析后指向 {resolved}，但该目录不存在。"
            f"{suggestion}"
        )
        logger.warning(f"❌ {error}")
        global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
        return False, error

    # ============================================================
    # 主入口
    # ============================================================

    def evaluate_draft(self, target_file: str, description: str,
                       code_content: str = None, sandbox_dir: str = None,
                       module_interfaces: dict = None,
                       project_spec: dict = None,
                       task_id: str = "") -> Tuple[bool, str]:
        """
        评估文件草稿（v3 L0+L0C+L1 管线）

        Args:
            target_file: 目标文件路径
            description: 任务描述
            code_content: Engine 传入的已缝合代码
            sandbox_dir: 沙盒工作目录
            module_interfaces: 跨文件接口契约（来自 Manager 规划书）
            project_spec: 完整项目规划书（含 page_routes / template_contracts）
            task_id: 任务 ID（用于判断 Continue 修复模式）

        返回:
            is_pass (bool): 是否审查通过
            feedback (str): 修改建议/报错 或 简短评语
        """
        if not code_content:
            return False, "没有找到该文件的代码内容（Engine 未传入 code_content）"

        is_continue_fix = str(task_id).startswith("continue_")
        logger.info(f"🛡️ Reviewer 正在审查文件: {target_file}" + (" [Continue 修复模式]" if is_continue_fix else ""))
        global_broadcaster.emit_sync("Reviewer", "review_start",
            f"开始审查目标文件: {target_file}", {"target": target_file})

        # === Step 1: L0 静态检查 (0 LLM) ===
        expected_symbols = self._extract_expected_symbols(target_file, module_interfaces, project_spec)
        l0_pass, l0_error = self._l0_static_check(
            target_file, code_content, sandbox_dir, expected_symbols,
            project_spec=project_spec, is_continue_fix=is_continue_fix)

        if not l0_pass:
            logger.warning(f"❌ Reviewer L0 驳回: {l0_error[:200]}")
            global_broadcaster.emit_sync("Reviewer", "review_fail",
                "L0 静态检查未通过", {"feedback": l0_error})
            return False, l0_error

        global_broadcaster.emit_sync("Reviewer", "l0_pass", "✅ L0 静态检查通过")

        # === Step 1.5: L0-Contract 契约校验 (0 LLM) ===
        lc_pass, lc_error = self._l0_contract_check(
            target_file, code_content, sandbox_dir, project_spec)
        if not lc_pass:
            logger.warning(f"❌ Reviewer L0-Contract 驳回: {lc_error[:200]}")
            global_broadcaster.emit_sync("Reviewer", "review_fail",
                "L0-Contract 契约校验未通过", {"feedback": lc_error})
            return False, lc_error

        # === Step 2: L1 合约审计 (~800 tokens) ===
        # 非 Python 文件跳过 L1（HTML/CSS/JS 的合约审计意义不大）
        if not target_file.endswith('.py'):
            logger.info(f"✅ [L1] 非 Python 文件，跳过合约审计: {target_file}")
            global_broadcaster.emit_sync("Reviewer", "review_pass",
                "✓ 审核通过！合并入主分支。", {"feedback": "L0+Contract 通过，非 Python 跳过 L1"})
            return True, "L0+Contract 通过（非 Python 文件，跳过 L1）"

        memory_hint = self._build_review_context(target_file, module_interfaces)
        l1_pass, l1_feedback = self._l1_contract_audit(
            target_file, code_content, description, memory_hint, module_interfaces)

        if l1_pass:
            logger.info(f"✅ Reviewer 审查通过: {l1_feedback[:100]}")
            global_broadcaster.emit_sync("Reviewer", "review_pass",
                "✓ 审核通过！合并入主分支。", {"feedback": l1_feedback})
        else:
            logger.warning(f"❌ Reviewer L1 驳回: {l1_feedback[:200]}")
            global_broadcaster.emit_sync("Reviewer", "review_fail",
                "审查未通过！", {"feedback": l1_feedback})

        return l1_pass, l1_feedback

    # ============================================================
    # L0.13: url_for endpoint 一致性检查
    # ============================================================

    def _l0_url_for_endpoint_check(self, target_file: str, html_content: str,
                                    sandbox_dir: str) -> Tuple[bool, str]:
        """
        L0.13: 检查 HTML 模板中 url_for('endpoint') 的 endpoint 名是否已注册。

        链路：
        1. 从 HTML 中提取所有 url_for('xxx') / url_for('bp.func') 的 endpoint 名
        2. 从 sandbox 中的所有 .py 文件提取 Blueprint 注册和路由函数名
        3. 组合 bp_name.func_name 格式的合法 endpoint
        4. 对比：HTML 引用了未注册的 endpoint → FAIL
        """
        if not sandbox_dir:
            return True, ""

        # 1. 从 HTML 中提取 url_for('xxx') 的 endpoint 名（支持带点号的 bp.func 格式）
        html_endpoints = set()
        for m in re.finditer(r"url_for\s*\(\s*['\"]([a-zA-Z0-9_.]+)['\"]", html_content):
            html_endpoints.add(m.group(1))

        if not html_endpoints:
            return True, ""  # 没有 url_for 调用

        # 仅跳过 Jinja 内置 endpoint（static 是 Flask 硬注册的）
        # 注意：'index' 不是内置 endpoint，在 Blueprint 场景中裸 'index' 通常是 bug
        builtin_endpoints = {'static'}

        # 2. 收集所有已注册的 endpoint
        registered_endpoints = set()
        # blueprint_name → set(func_name)  用于生成 bp.func 格式 endpoint
        blueprint_functions: dict = {}
        # blueprint 变量名 → Blueprint('name', ...) 中的 name
        blueprint_var_to_name: dict = {}

        # 2a. 动态扫描 sandbox 中所有 .py 文件
        py_files = []
        for root, dirs, files in os.walk(sandbox_dir):
            dirs[:] = [d for d in dirs if d not in {
                '__pycache__', '.git', '.venv', 'venv', 'node_modules',
                '.sandbox', '.astrea', 'dist', 'build',
            }]
            depth = root.replace(sandbox_dir, "").count(os.sep)
            if depth > 2:
                dirs.clear()
                continue
            for fname in files:
                if fname.endswith('.py'):
                    py_files.append(os.path.join(root, fname))

        for py_path in py_files:
            try:
                with open(py_path, 'r', encoding='utf-8') as f:
                    py_content = f.read()
                tree = ast.parse(py_content)
            except Exception:
                continue

            # 提取 Blueprint 变量名和注册名
            # bp = Blueprint('expense_routes', __name__)
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    for target in node.targets:
                        if isinstance(target, ast.Name) and isinstance(node.value, ast.Call):
                            func = node.value.func
                            is_blueprint = False
                            if isinstance(func, ast.Name) and func.id == 'Blueprint':
                                is_blueprint = True
                            elif isinstance(func, ast.Attribute) and func.attr == 'Blueprint':
                                is_blueprint = True
                            if is_blueprint and node.value.args:
                                first_arg = node.value.args[0]
                                if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                                    bp_var = target.id
                                    bp_name = first_arg.value
                                    blueprint_var_to_name[bp_var] = bp_name
                                    if bp_name not in blueprint_functions:
                                        blueprint_functions[bp_name] = set()

            # 提取 @bp.route / @app.route 装饰的函数
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for dec in node.decorator_list:
                        if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute):
                            if dec.func.attr in ('route', 'get', 'post', 'put', 'delete', 'patch'):
                                bp_var = dec.func.value.id if isinstance(dec.func.value, ast.Name) else ""
                                bp_name = blueprint_var_to_name.get(bp_var, "")
                                if bp_name:
                                    # Blueprint 场景：Flask 只注册 bp_name.func_name 格式
                                    # 裸 func_name 不是合法 endpoint
                                    blueprint_functions.setdefault(bp_name, set()).add(node.name)
                                    registered_endpoints.add(f"{bp_name}.{node.name}")
                                else:
                                    # 非 Blueprint（直接 @app.route）：裸函数名是合法 endpoint
                                    registered_endpoints.add(node.name)

            # 提取 add_url_rule 注册的 endpoint
            for m in re.finditer(
                r"add_url_rule\s*\(\s*['\"][^'\"]*['\"]\s*,\s*['\"](\w+)['\"]",
                py_content
            ):
                registered_endpoints.add(m.group(1))

        if not registered_endpoints:
            return True, ""  # 没找到任何注册的 endpoint，跳过

        # 3. 对比
        unregistered = html_endpoints - registered_endpoints - builtin_endpoints
        if unregistered:
            # 构造修复建议
            suggestions = []
            for ep in sorted(unregistered):
                # 尝试推荐正确的 bp.func 格式
                found = False
                for bp_name, funcs in blueprint_functions.items():
                    if ep in funcs:
                        suggestions.append(f"url_for('{ep}') → url_for('{bp_name}.{ep}')")
                        found = True
                        break
                if not found:
                    suggestions.append(f"url_for('{ep}') 未找到匹配的注册 endpoint")

            error = (
                f"[L0.13 url_for endpoint 不一致] {target_file}: "
                f"模板中 url_for() 引用了未注册的 endpoint: {sorted(unregistered)}。"
                f"已注册的 endpoint: {sorted(registered_endpoints)}。"
                f"修复建议: {'; '.join(suggestions)}。"
                f"在 Blueprint 架构中，url_for 必须使用 'blueprint_name.function_name' 格式。"
            )
            logger.warning(f"❌ {error}")
            global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
            return False, error

        logger.info(f"✅ [L0.13] url_for endpoint 校验通过: {target_file}")
        return True, ""

    @staticmethod
    def _l0_sqlalchemy_association_check(target_file: str, code_content: str,
                                          expected_symbols: list) -> Tuple[bool, str]:
        """
        L0.2-ORM: 约束 SQLAlchemy 中间表建模只能二选一：
        1. 显式 ORM 实体类，例如 class TaskTag
        2. 裸 association table，例如 task_tags = db.Table(...)

        若规划书已经明确要求 class TaskTag，则禁止同时再定义同名裸关联表。
        """
        expected_set = set(expected_symbols or [])
        if "TaskTag" not in expected_set:
            return True, ""

        has_task_tag_class = bool(re.search(r"^\s*class\s+TaskTag\b", code_content, re.MULTILINE))
        has_task_tags_table = bool(
            re.search(r"(?:\b\w+\s*=\s*)?(?:db\.)?Table\s*\(\s*['\"]task_tags['\"]", code_content)
        )

        if has_task_tags_table and not has_task_tag_class:
            error = (
                f"[L0.2 ORM 建模冲突] {target_file}: 规划书明确要求 `class TaskTag`，"
                f"当前代码却只定义了裸关联表 `task_tags = Table(...)`。"
                f"修复方法：把 TaskTag 实现为显式 ORM 模型类，不要只写裸关联表。"
            )
            logger.warning(f"❌ {error}")
            global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
            return False, error

        if has_task_tags_table and has_task_tag_class:
            error = (
                f"[L0.2 ORM 重复定义] {target_file}: 同时检测到 `class TaskTag` 和 "
                f"`task_tags = Table(...)`。这会让 SQLAlchemy 对同一张表重复注册。"
                f"修复方法：二选一；当前项目按规划书应保留 `class TaskTag`，删除裸 `task_tags` 关联表。"
            )
            logger.warning(f"❌ {error}")
            global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
            return False, error

        return True, ""

    # ============================================================
    # L0.12: models.py tuple 返回检测
    # ============================================================

    @staticmethod
    def _l0_tuple_return_check(target_file: str, code_content: str) -> Tuple[bool, str]:
        """
        L0.12: 检测 models.py 是否将 sqlite3.Row 转成 tuple 返回。

        典型 anti-pattern:
            conn.row_factory = sqlite3.Row  # 设了 row_factory
            rows = cursor.fetchall()
            return [tuple(row) for row in rows]  # ← 又手动转成 tuple！

        后果: Jinja 模板 {{ expense.amount }} 报错 'tuple has no attribute'
        正确修复方向: 删掉 tuple() 转换，直接返回 Row 或 dict(row)
        """
        import re as _re

        # 匹配危险模式: tuple(row) / list(tuple(...)) / (row[0], row[1]...)
        dangerous_patterns = [
            (_re.compile(r'return\s+\[tuple\(', _re.MULTILINE),
             "return [tuple(row) for row in rows]"),
            (_re.compile(r'return\s+tuple\(', _re.MULTILINE),
             "return tuple(row)"),
            (_re.compile(r'->\s*(?:List\[Tuple|Tuple\[)', _re.MULTILINE),
             "返回类型标注为 List[Tuple] 或 Tuple"),
        ]

        violations = []
        for pattern, desc in dangerous_patterns:
            for m in pattern.finditer(code_content):
                # 找到违规行号
                line_no = code_content[:m.start()].count('\n') + 1
                violations.append(f"L{line_no}: {desc}")

        if violations:
            error = (
                f"[L0.12 tuple 返回反模式] {target_file}: "
                f"检测到 {len(violations)} 处将 sqlite3.Row 转为 tuple 的危险代码：\n"
                + "\n".join(f"  - {v}" for v in violations[:5])
                + "\n\n【正确修复方向（必须遵守！）】\n"
                "1. 确保每个函数中 conn.row_factory = sqlite3.Row（或使用统一的 get_db()）\n"
                "2. 删掉所有 tuple(row) 转换，直接 return rows 或 return [dict(row) for row in rows]\n"
                "3. 返回类型标注改为 List[dict] 或 List[sqlite3.Row]，禁止 List[Tuple]\n"
                "4. 禁止修改模板为索引访问（如 expense[0]），那是治标不治本！"
            )
            logger.warning(f"❌ {error}")
            global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
            return False, error

        return True, ""

    @staticmethod
    def _find_file_in_sandbox(sandbox_dir: str, rel_path: str) -> str:
        """在 sandbox 目录中查找文件，返回绝对路径或空字符串。"""
        if not sandbox_dir or not rel_path:
            return ""
        # 直接拼接
        candidate = os.path.join(sandbox_dir, rel_path)
        if os.path.isfile(candidate):
            return candidate
        # 尝试只用 basename（models.py 可能直接在根目录）
        basename = os.path.basename(rel_path)
        candidate2 = os.path.join(sandbox_dir, basename)
        if os.path.isfile(candidate2):
            return candidate2
        # 递归搜索（最多 2 层）
        for root, dirs, files in os.walk(sandbox_dir):
            depth = root.replace(sandbox_dir, "").count(os.sep)
            if depth > 2:
                dirs.clear()
                continue
            if basename in files:
                return os.path.join(root, basename)
        return ""

    def _l0_call_signature_check(self, target_file: str, code_content: str,
                                  sandbox_dir: str, tree: 'ast.AST') -> 'Tuple[bool, str]':
        """
        L0.14: Cross-file function call signature consistency check.
        Catches: routes.py calls update_expense(id) but models.py defines
        def update_expense(id, amount, category, description, date) -> TypeError at runtime.
        """
        import ast as _ast

        # 1. Extract from xxx import yyy statements
        local_imports = {}
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ImportFrom):
                module = node.module or ''
                if module.startswith(('os', 'sys', 'json', 're', 'datetime',
                                      'typing', 'flask', 'fastapi', 'sqlalchemy',
                                      'pydantic', 'werkzeug', 'jinja2', 'sqlite3',
                                      'collections', 'functools', 'pathlib',
                                      'urllib', 'hashlib', 'uuid', 'math')):
                    continue
                for alias in (node.names or []):
                    name = alias.asname or alias.name
                    if name != '*':
                        local_imports[name] = module

        if not local_imports:
            return True, ''

        # 2. Parse imported module function signatures
        func_signatures = {}
        for func_name, module_name in local_imports.items():
            module_file = module_name.replace('.', '/') + '.py'
            module_path = self._find_file_in_sandbox(sandbox_dir, module_file)
            if not module_path:
                module_path = self._find_file_in_sandbox(
                    sandbox_dir, module_name.split('.')[-1] + '.py')
            if not module_path:
                continue
            try:
                with open(module_path, 'r', encoding='utf-8') as f:
                    mod_tree = _ast.parse(f.read())
            except Exception:
                continue
            for mod_node in _ast.walk(mod_tree):
                if isinstance(mod_node, _ast.FunctionDef) and mod_node.name == func_name:
                    args = mod_node.args
                    all_positional = list(getattr(args, 'posonlyargs', [])) + list(args.args)
                    param_names = [a.arg for a in all_positional]
                    if param_names and param_names[0] in ('self', 'cls'):
                        all_positional = all_positional[1:]
                    n_total = len(all_positional) + len(args.kwonlyargs)
                    n_defaults = len(args.defaults) + len(args.kw_defaults)
                    n_min = n_total - n_defaults
                    n_max = n_total
                    if args.vararg or args.kwarg:
                        n_max = 999
                    func_signatures[func_name] = (n_min, n_max)
                    break

        if not func_signatures:
            return True, ''

        # 3. Check calls in current file
        violations = []
        for node in _ast.walk(tree):
            if not isinstance(node, _ast.Call):
                continue
            call_name = ''
            if isinstance(node.func, _ast.Name):
                call_name = node.func.id
            elif isinstance(node.func, _ast.Attribute):
                continue
            if call_name not in func_signatures:
                continue
            n_min, n_max = func_signatures[call_name]
            n_actual_with_kw = len(node.args) + len(node.keywords)
            has_star_expand = any(
                isinstance(a, _ast.Starred) for a in node.args
            ) or any(kw.arg is None for kw in node.keywords)
            if has_star_expand:
                continue
            if n_actual_with_kw < n_min:
                violations.append(
                    f'`{call_name}()` 调用传了 {n_actual_with_kw} 个参数，'
                    f'但定义需要至少 {n_min} 个'
                )
            elif n_actual_with_kw > n_max:
                violations.append(
                    f'`{call_name}()` 调用传了 {n_actual_with_kw} 个参数，'
                    f'但定义最多接受 {n_max} 个'
                )

        if violations:
            cross_modules = sorted(set(local_imports.values()))
            cross_tag = f'[CROSS_FILE:{",".join(cross_modules)}] ' if cross_modules else ''
            error = (
                f'{cross_tag}[L0.14 函数调用签名不匹配] {target_file}: '
                + '; '.join(violations[:3])
                + '。修复方法：检查被调用函数的定义，确保传入参数数量正确。'
            )
            logger.warning(f'❌ {error}')
            global_broadcaster.emit_sync('Reviewer', 'l0_fail', error)
            return False, error

        return True, ''

    @staticmethod
    def _l0_name_shadow_check(target_file: str, tree: 'ast.AST') -> 'Tuple[bool, str]':
        """
        L0.15: 检测函数定义名与 from xxx import 的名字冲突。
        典型场景：routes.py 中 from models import update_expense，
        然后又 def update_expense(expense_id) → Python 覆盖，调用变递归。
        """
        import ast as _ast

        # 收集所有 from xxx import name 的 name（排除标准库）
        imported_names = set()
        for node in _ast.iter_child_nodes(tree):
            if isinstance(node, _ast.ImportFrom):
                module = node.module or ''
                if module.startswith(('os', 'sys', 'json', 're', 'datetime',
                                      'typing', 'flask', 'fastapi', 'sqlalchemy',
                                      'pydantic', 'werkzeug', 'jinja2', 'sqlite3',
                                      'collections', 'functools', 'pathlib',
                                      'urllib', 'hashlib', 'uuid', 'math',
                                      'logging', 'io', 'abc', 'enum',
                                      'dataclasses', 'contextlib')):
                    continue
                for alias in (node.names or []):
                    real_name = alias.asname or alias.name
                    if real_name != '*':
                        imported_names.add(real_name)

        if not imported_names:
            return True, ''

        # 收集所有顶层函数定义名
        shadows = []
        for node in _ast.iter_child_nodes(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                if node.name in imported_names:
                    shadows.append(node.name)

        if shadows:
            error = (
                f"[L0.15 命名空间覆盖] {target_file}: "
                f"以下函数定义名与 import 导入名冲突: {shadows}。"
                f"Python 会用后定义覆盖导入，导致调用时变递归或参数不匹配。"
                f"修复方法：将 handler 函数重命名（如加 _handler / _view 后缀），"
                f"或将 import 改为 import models 然后用 models.xxx() 调用。"
            )
            logger.warning(f'❌ {error}')
            global_broadcaster.emit_sync('Reviewer', 'l0_fail', error)
            return False, error

        return True, ''
