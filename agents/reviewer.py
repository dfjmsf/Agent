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
from typing import Dict, Any, Tuple, List

from core.llm_client import default_llm
from core.prompt import Prompts
from tools.sandbox import sandbox_env
from core.ws_broadcaster import global_broadcaster
from core.database import get_recent_events, recall_reviewer_experience, infer_domain

logger = logging.getLogger("ReviewerAgent")


class ReviewerAgent:
    """
    审查 Agent (Reviewer v3 - Lite)
    L0: 静态检查（语法 + 结构 + 导入）— 0 LLM 消耗
    L1: 合约审计（LLM 只读审查）— ~800 tokens
    """
    def __init__(self, project_id: str = "default_project"):
        self.model = os.getenv("MODEL_REVIEWER", "qwen3-max")
        self.project_id = project_id

    # ============================================================
    # L0: 静态检查（确定性，0 LLM 消耗）
    # ============================================================

    def _l0_static_check(self, target_file: str, code_content: str,
                         sandbox_dir: str, expected_symbols: list) -> Tuple[bool, str]:
        """
        L0 静态检查：
          L0.1 语法检查 — ast.parse()
          L0.2 结构检查 — AST 提取符号 vs 规划书期望
          L0.3 导入检查 — 沙盒中 import（10s 超时）

        Returns: (passed, error_msg)
        """
        is_python = target_file.endswith('.py')
        is_js = target_file.endswith('.js')

        # --- L0.0 骨架残留检测（Fill 阶段失败兜底）---
        if is_python:
            # 检测函数体是否仍然是 ... 占位（骨架未被填充）
            try:
                tree_pre = ast.parse(code_content)
                stub_funcs = []
                for node in ast.walk(tree_pre):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        # 函数体只有一个 Expr(Constant(Ellipsis)) 或 Expr(Constant('...'))
                        if len(node.body) == 1:
                            stmt = node.body[0]
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
                pass  # 语法错误由 L0.1 捕获

        # --- L0.1 语法检查（仅 Python）---
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
            # JS 文件：用 Node.js 做语法检查
            l01_pass, l01_error = self._l0_js_syntax_check(target_file, sandbox_dir)
            if not l01_pass:
                return False, l01_error
            logger.info(f"✅ [L0.1] JS 语法检查通过: {target_file}")
        else:
            # 其他前端文件（HTML/CSS）：只检查内容非空
            if not code_content or not code_content.strip():
                error = f"[L0.1] {target_file} 内容为空"
                logger.warning(f"❌ {error}")
                return False, error
            logger.info(f"✅ [L0.1] 非 Python 文件，内容非空: {target_file}")

        # --- L0.2 结构检查（仅 Python + 有期望符号）---
        if is_python and expected_symbols and tree:
            defined = self._extract_defined_symbols(tree)
            missing = [s for s in expected_symbols if s not in defined]
            if missing:
                error = f"[L0.2 结构缺失] {target_file} 缺少规划书中定义的: {', '.join(missing)}"
                logger.warning(f"❌ {error}")
                global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
                return False, error
            logger.info(f"✅ [L0.2] 结构检查通过: {len(expected_symbols)} 个符号全部存在")

        # --- L0.3 导入检查（仅 Python 文件）---
        if is_python:
            l03_pass, l03_error = self._l0_import_check(target_file, sandbox_dir)
            if not l03_pass:
                return False, l03_error
            logger.info(f"✅ [L0.3] 导入检查通过: {target_file}")

        # --- L0.4 FastAPI POST/PUT 参数检查（所有 Python 文件，因为路由可能写在 main.py 中）---
        if is_python and tree:
            l04_pass, l04_error = self._l0_fastapi_param_check(tree, target_file, code_content)
            if not l04_pass:
                return False, l04_error

        # --- L0.5 路由装饰器检查（routes.py 必须有 @router.xxx 装饰器）---
        if is_python and tree:
            l05_pass, l05_error = self._l0_route_decorator_check(tree, target_file, code_content)
            if not l05_pass:
                return False, l05_error

        # --- L0.6 跨文件字段一致性检查（Flask SSR 项目 + 表单字段）---
        if sandbox_dir:
            l06_pass, l06_error = self._l0_cross_file_check(
                target_file, code_content, sandbox_dir)
            if not l06_pass:
                return False, l06_error

        # --- L0.7 Flask template_folder 检查 ---
        if sandbox_dir and target_file.endswith('.py'):
            l07_pass, l07_error = self._l0_template_folder_check(
                target_file, code_content, sandbox_dir)
            if not l07_pass:
                return False, l07_error

        # --- L0.9 前后端 API 契约检查（fetch URL vs 后端路由返回类型）---
        if sandbox_dir:
            l09_pass, l09_error = self._l0_fetch_api_contract_check(
                target_file, code_content, sandbox_dir)
            if not l09_pass:
                return False, l09_error

        # --- L0.10 种子数据检查（init_db 中 CREATE TABLE 必须配 INSERT）---
        if is_python and tree:
            l10_pass, l10_error = self._l0_seed_data_check(target_file, code_content)
            if not l10_pass:
                return False, l10_error

        return True, ""

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
        import re

        # 用正则找所有 @app.post / @router.post / @app.put / @router.put 装饰的函数
        # 匹配模式：装饰器行 + 紧跟的 async def / def 行
        pattern = re.compile(
            r'@\w+\.(post|put)\s*\([^)]*\)\s*\n'       # 装饰器行
            r'\s*(?:async\s+)?def\s+\w+\(([^)]*)\)',    # 函数签名（async def 或 def）
            re.IGNORECASE
        )

        violations = []
        for m in pattern.finditer(code_content):
            method = m.group(1).upper()  # POST / PUT
            params_str = m.group(2).strip()

            if not params_str:
                continue

            # 解析参数列表
            params = [p.strip() for p in params_str.split(',')]
            bare_params = []
            for param in params:
                # 跳过 self, request, req, db 等常见非业务参数
                param_name = param.split(':')[0].split('=')[0].strip()
                if param_name in ('self', 'request', 'req', 'db', 'session'):
                    continue
                # 跳过路径参数（在装饰器 URL 中出现的 {id} 类参数）
                if param_name in ('id', 'memo_id', 'item_id', 'user_id'):
                    continue

                # 检测裸类型注解（无 Body/Depends 等注解的基础类型）
                if ':' in param:
                    type_and_default = param.split(':', 1)[1].strip()
                    # 如果有 = Body(...) 或 = Depends(...) 注解，跳过
                    if '=' in type_and_default:
                        default_val = type_and_default.split('=', 1)[1].strip()
                        if any(kw in default_val for kw in ('Body', 'Depends', 'Form', 'File')):
                            continue
                    type_hint = type_and_default.split('=')[0].strip()
                    bare_types = ('str', 'int', 'float', 'bool', 'dict', 'list',
                                  'List', 'Dict', 'Optional[str]', 'Optional[int]',
                                  'Optional[dict]', 'Optional[list]')
                    if type_hint in bare_types:
                        bare_params.append(param.strip())
                elif '=' not in param:
                    # 无类型注解也无默认值的裸参数
                    bare_params.append(param.strip())

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
        """
        basename = os.path.basename(target_file).lower()

        # HTML 文件 → 检查 Jinja 字段一致性 + 反向表单字段检查
        if target_file.endswith('.html'):
            # L0.6-A: Jinja 字段
            jinja_pass, jinja_error = self._l0_jinja_field_check(
                target_file, code_content, sandbox_dir)
            if not jinja_pass:
                return False, jinja_error

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
        if not class_keys:
            return True, ""  # 没有 to_dict / SQL 列，跳过

        # 合并所有 key 作为 fallback（无法匹配具体类时使用）
        all_keys = set()
        for ks in class_keys.values():
            all_keys |= ks

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
                # fallback: 使用合并 key
                if matched_keys is None:
                    matched_keys = all_keys

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
        if _re.search(r'\{%[-\s]*extends\s', html_content):
            return True, ""  # 有继承 → 通过

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
                logger.warning(f"⚠️ [L0.3] import 超时（可能有阻塞代码），跳过导入检查")
                return True, ""

            # 错误信息可能在 stdout（IMPORT_FAIL）或 stderr
            fail_detail = ""
            if "IMPORT_FAIL:" in stdout:
                fail_detail = stdout.split("IMPORT_FAIL:")[1].strip()[:500]
            elif stderr:
                fail_detail = stderr[:500]

            # 已知良性错误：L0.3 的单文件 import 无法模拟完整包环境，跳过交给 L2
            benign_patterns = [
                "relative import",        # from .models import X（包内相对导入）
                "No module named",         # 兄弟模块还没写好 / 第三方未装
                "cannot import name",      # 兄弟模块接口未就绪
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
                                  module_interfaces: dict) -> list:
        """从规划书的 module_interfaces 中提取期望的函数/类名"""
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
                temperature=0.1
            )

            raw = response_msg.content.strip()
            # 尝试解析 JSON
            return self._parse_audit_result(raw)

        except Exception as e:
            logger.warning(f"⚠️ [L1] LLM 调用异常: {e}，放行")
            return True, "审查通过（LLM 异常，跳过）"

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
                # 无法解析，默认放行
                return True, f"合约审计通过（LLM 输出格式异常，已放行）"

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
        template_contracts = project_spec.get("template_contracts", {})

        if not page_routes and not template_contracts:
            return True, ""  # 无契约 → 跳过

        # C1: 后端路由函数 vs 契约路径一致性（Python 路由文件）
        if target_file.endswith('.py') and page_routes:
            c1_pass, c1_error = self._l0_contract_c1_route_check(
                target_file, code_content, page_routes)
            if not c1_pass:
                return False, c1_error

        # C2: HTML form action / href vs 契约路径一致性（HTML 文件）
        if target_file.endswith('.html') and page_routes:
            c2_pass, c2_error = self._l0_contract_c2_html_url_check(
                target_file, code_content, page_routes)
            if not c2_pass:
                return False, c2_error

        # C3: template_folder 路径 vs 实际目录结构一致性（Flask app 文件）
        if target_file.endswith('.py') and sandbox_dir:
            c3_pass, c3_error = self._l0_contract_c3_template_folder_check(
                target_file, code_content, sandbox_dir)
            if not c3_pass:
                return False, c3_error

        return True, ""

    def _l0_contract_c1_route_check(self, target_file: str, code_content: str,
                                    page_routes: list) -> Tuple[bool, str]:
        """L0.C1: 检查 Python 路由文件中注册的路径是否与 page_routes 契约一致。
        仅对包含路由注册的文件生效（routes.py / app.py 等）。"""
        import re as _re

        # 只检查包含路由注册的文件
        has_route_registration = (
            'add_url_rule' in code_content or
            '@app.' in code_content or
            '@router.' in code_content or
            '@bp.' in code_content or
            'route(' in code_content
        )
        if not has_route_registration:
            return True, ""

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
                suggestion = f"正确值应为 template_folder='templates'，或直接删除此参数（Flask 默认就是 templates/）。"
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
                       project_spec: dict = None) -> Tuple[bool, str]:
        """
        评估文件草稿（v3 L0+L0C+L1 管线）

        Args:
            target_file: 目标文件路径
            description: 任务描述
            code_content: Engine 传入的已缝合代码
            sandbox_dir: 沙盒工作目录
            module_interfaces: 跨文件接口契约（来自 Manager 规划书）
            project_spec: 完整项目规划书（含 page_routes / template_contracts）

        返回:
            is_pass (bool): 是否审查通过
            feedback (str): 修改建议/报错 或 简短评语
        """
        if not code_content:
            return False, "没有找到该文件的代码内容（Engine 未传入 code_content）"

        logger.info(f"🛡️ Reviewer 正在审查文件: {target_file}")
        global_broadcaster.emit_sync("Reviewer", "review_start",
            f"开始审查目标文件: {target_file}", {"target": target_file})

        # === Step 1: L0 静态检查 (0 LLM) ===
        expected_symbols = self._extract_expected_symbols(target_file, module_interfaces)
        l0_pass, l0_error = self._l0_static_check(
            target_file, code_content, sandbox_dir, expected_symbols)

        if not l0_pass:
            logger.warning(f"❌ Reviewer L0 驳回: {l0_error[:200]}")
            global_broadcaster.emit_sync("Reviewer", "review_fail",
                f"L0 静态检查未通过", {"feedback": l0_error})
            return False, l0_error

        global_broadcaster.emit_sync("Reviewer", "l0_pass", "✅ L0 静态检查通过")

        # === Step 1.5: L0-Contract 契约校验 (0 LLM) ===
        lc_pass, lc_error = self._l0_contract_check(
            target_file, code_content, sandbox_dir, project_spec)
        if not lc_pass:
            logger.warning(f"❌ Reviewer L0-Contract 驳回: {lc_error[:200]}")
            global_broadcaster.emit_sync("Reviewer", "review_fail",
                f"L0-Contract 契约校验未通过", {"feedback": lc_error})
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
