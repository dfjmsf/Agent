"""
AST 显微镜 — Python ast 与 tree-sitter 双擎适配器 (Phase 2.4)

核心能力：
1. list_symbols(source, lang) → 列出顶层符号及精确起止行号
2. extract_slice(source, lang, symbol_name) → 提取代码切片 + 上下文
3. find_relevant_slice(source, description, lang) → 语义匹配最佳切片

语言路由策略：
- Python → 内置 ast 模块（语义级，理解 docstring/装饰器/类型注解）
- JS/JSX/TS/TSX/HTML/CSS/Vue → tree-sitter（语法级 CST）
"""
import ast
import os
import re
import logging
from typing import List, Dict, Optional

logger = logging.getLogger("ASTMicroscope")

# ============================================================
# tree-sitter 延迟加载（避免未安装时崩溃）
# ============================================================

_TS_AVAILABLE = False
_TS_LANGUAGES = {}

def _init_treesitter():
    """延迟初始化 tree-sitter 语言对象（仅执行一次）"""
    global _TS_AVAILABLE, _TS_LANGUAGES
    if _TS_LANGUAGES:
        return True
    try:
        from tree_sitter import Language
        import tree_sitter_javascript as tsjs
        import tree_sitter_html as tshtml
        import tree_sitter_css as tscss
        import tree_sitter_typescript as tsts

        _TS_LANGUAGES["javascript"] = Language(tsjs.language())
        _TS_LANGUAGES["html"] = Language(tshtml.language())
        _TS_LANGUAGES["css"] = Language(tscss.language())
        _TS_LANGUAGES["typescript"] = Language(tsts.language_typescript())
        _TS_LANGUAGES["tsx"] = Language(tsts.language_tsx())
        _TS_AVAILABLE = True
        logger.info("🔬 tree-sitter 初始化成功: JS/HTML/CSS/TS/TSX")
        return True
    except ImportError as e:
        logger.warning(f"⚠️ tree-sitter 未安装，前端 AST 显微镜不可用: {e}")
        _TS_AVAILABLE = False
        return False


# ============================================================
# 语言检测
# ============================================================

def detect_lang(file_path: str) -> str:
    """根据文件扩展名推断语言"""
    ext = os.path.splitext(file_path)[1].lower()
    LANG_MAP = {
        ".py": "python",
        ".js": "javascript", ".jsx": "javascript",
        ".ts": "typescript", ".tsx": "tsx",
        ".html": "html", ".htm": "html",
        ".css": "css",
        ".vue": "vue",
        ".svelte": "html",
    }
    return LANG_MAP.get(ext, "unknown")


# ============================================================
# 核心类
# ============================================================

class ASTMicroscope:
    """
    AST 显微镜 — 双擎语言路由解析器。

    Python: 使用内置 ast 模块（精度最高）
    前端:   使用 tree-sitter（JS/HTML/CSS/TS/TSX/Vue）
    """

    # ------ 公开 API ------

    def list_symbols(self, source: str, lang: str) -> List[Dict]:
        """
        列出所有顶层符号（函数、类、组件、CSS 规则等）。

        返回: [
            {"name": "handleSubmit", "type": "function",  "start_line": 45, "end_line": 78},
            {"name": "UserCard",     "type": "class",     "start_line": 80, "end_line": 120},
            {"name": ".btn-primary", "type": "css_rule",  "start_line": 15, "end_line": 22},
        ]
        """
        if lang == "python":
            return self._list_symbols_python(source)
        elif lang == "vue":
            return self._list_symbols_vue(source)
        else:
            return self._list_symbols_treesitter(source, lang)

    def extract_slice(self, source: str, lang: str, symbol_name: str,
                      context_lines: int = 5) -> Optional[Dict]:
        """
        精准提取单个符号的代码切片 + 上下文。

        返回: {
            "name": "handleSubmit",
            "start_line": 45, "end_line": 78,
            "code": "完整符号代码",
            "context_before": "上方 N 行",
            "context_after": "下方 N 行",
        }
        """
        symbols = self.list_symbols(source, lang)
        target = None
        for sym in symbols:
            if sym["name"] == symbol_name:
                target = sym
                break

        if not target:
            return None

        lines = source.splitlines()
        s = target["start_line"] - 1  # 转为 0-indexed
        e = target["end_line"]

        # 上下文
        ctx_before_start = max(0, s - context_lines)
        ctx_after_end = min(len(lines), e + context_lines)

        return {
            "name": target["name"],
            "type": target["type"],
            "start_line": target["start_line"],
            "end_line": target["end_line"],
            "code": "\n".join(lines[s:e]),
            "context_before": "\n".join(lines[ctx_before_start:s]),
            "context_after": "\n".join(lines[e:ctx_after_end]),
        }

    def requires_scope_expansion(self, source: str, description: str, lang: str) -> bool:
        """
        作用域扩展探测器 — 检测任务描述是否要求新增当前代码中不存在的符号。

        原理：从 description 中提取看起来像函数/类/变量名的标识符（snake_case 或 camelCase），
        与 existing symbols 对比。如果 description 中出现了 2 个以上既有符号中不存在的名称，
        或出现了明确的"新增"/"添加"/"实现"语义关键词搭配未知函数名，则判定需要扩展作用域。

        场景举例：
        - description = "添加 get_expense_by_id 和 update_expense 函数"
        - existing symbols = [get_all_expenses, create_expense]
        → get_expense_by_id, update_expense 不在 symbols 中 → 返回 True
        """
        symbols = self.list_symbols(source, lang)
        if not symbols:
            return False  # 无符号表 → 无法判断，不干预

        # 收集既有符号名（小写，含 Class.method 拆分）
        existing_names = set()
        for sym in symbols:
            existing_names.add(sym["name"].lower())
            # 处理 Class.method 格式
            if "." in sym["name"]:
                parts = sym["name"].split(".")
                for p in parts:
                    existing_names.add(p.lower())

        # 从 description 中提取候选函数/类名
        # 规则：3 字符以上的 snake_case 或 camelCase 标识符
        candidates = set(re.findall(r'[a-zA-Z_][a-zA-Z0-9_]{2,}', description))

        # 过滤掉常见的自然语言词汇和技术关键词（非函数名）
        NOISE_WORDS = {
            'the', 'and', 'for', 'with', 'from', 'that', 'this', 'not', 'are', 'was',
            'will', 'can', 'has', 'have', 'been', 'but', 'all', 'any', 'each', 'into',
            'def', 'class', 'import', 'return', 'function', 'const', 'let', 'var',
            'async', 'await', 'export', 'default', 'module', 'require',
            'str', 'int', 'bool', 'list', 'dict', 'none', 'true', 'false',
            'file', 'code', 'bug', 'fix', 'add', 'new', 'get', 'set', 'put', 'post',
            'delete', 'patch', 'update', 'create', 'remove', 'error', 'test',
            'route', 'model', 'view', 'template', 'api', 'url', 'http', 'json',
            'request', 'response', 'data', 'type', 'name', 'value', 'key',
            '添加', '新增', '实现', '修复', '修改', '删除', '函数', '方法',
        }

        # 筛选出看起来像函数/变量定义名的标识符（含下划线或驼峰结构）
        new_symbols = set()
        for candidate in candidates:
            c_lower = candidate.lower()
            if c_lower in NOISE_WORDS:
                continue
            if c_lower in existing_names:
                continue
            # 必须看起来像真正的标识符：含下划线、或含大小写混合（驼峰）
            is_snake = '_' in candidate
            is_camel = bool(re.search(r'[a-z][A-Z]', candidate))
            is_class_like = candidate[0].isupper() and len(candidate) > 3
            if is_snake or is_camel or is_class_like:
                new_symbols.add(candidate)

        if new_symbols:
            logger.info(
                f"🔍 [Scope Expansion] 描述中发现未知符号: {sorted(new_symbols)} "
                f"(既有: {sorted(s['name'] for s in symbols[:5])}{'...' if len(symbols) > 5 else ''})"
            )

        # 判定阈值：1 个以上未知的结构化标识符即认为需要扩展
        return len(new_symbols) >= 1

    def find_relevant_slice(self, source: str, description: str, lang: str,
                            context_lines: int = 10) -> Optional[Dict]:
        """
        根据任务描述，自动匹配最相关的符号并返回切片。

        匹配策略：将 description 中的关键词与符号名做模糊匹配。
        如果没有命中任何符号，返回 None（调用方走全量降级）。
        """
        symbols = self.list_symbols(source, lang)
        if not symbols:
            return None

        desc_lower = description.lower()

        # 提取 description 中的关键词
        # 关键修复：先从原始文本中提取驼峰拆分词汇（如 viewSnippet → view, snippet），
        # 再与小写整词合并。之前只从小写文本提取，viewsnippet 作为一个整词无法匹配。
        raw_words = set(re.findall(r'[a-zA-Z_]\w+', description))
        keywords = set()
        for word in raw_words:
            keywords.add(word.lower())
            # 驼峰拆分：viewSnippet → {view, snippet}
            camel_parts = re.findall(r'[A-Z][a-z]+|[a-z]+', word)
            for part in camel_parts:
                keywords.add(part.lower())

        best_score = 0
        best_sym = None

        for sym in symbols:
            name_lower = sym["name"].lower()
            score = 0

            # 精确包含（完整函数名出现在 description 中）
            if name_lower in desc_lower:
                score += 10

            # 关键词匹配
            name_parts = set(re.findall(r'[a-z]+', name_lower))
            overlap = keywords & name_parts
            score += len(overlap) * 2

            # 驼峰拆分匹配
            camel_parts = set(p.lower() for p in re.findall(r'[A-Z][a-z]+|[a-z]+', sym["name"]))
            overlap2 = keywords & camel_parts
            score += len(overlap2) * 3

            if score > best_score:
                best_score = score
                best_sym = sym

        if best_sym and best_score >= 2:
            result = self.extract_slice(source, lang, best_sym["name"], context_lines)
            if result:
                logger.info(f"🔬 显微镜定位: {best_sym['name']} (score={best_score}, L{best_sym['start_line']}-{best_sym['end_line']})")
            return result

        return None

    # ------ Python (内置 ast) ------

    def _list_symbols_python(self, source: str) -> List[Dict]:
        """用 Python ast 模块提取顶层符号"""
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []

        symbols = []
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append({
                    "name": node.name,
                    "type": "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function",
                    "start_line": node.lineno,
                    "end_line": node.end_lineno or node.lineno,
                })
            elif isinstance(node, ast.ClassDef):
                symbols.append({
                    "name": node.name,
                    "type": "class",
                    "start_line": node.lineno,
                    "end_line": node.end_lineno or node.lineno,
                })
                # 类内方法也列出（二级符号）
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        symbols.append({
                            "name": f"{node.name}.{child.name}",
                            "type": "method",
                            "start_line": child.lineno,
                            "end_line": child.end_lineno or child.lineno,
                        })
        return symbols

    # ------ tree-sitter 通用前端 ------

    def _list_symbols_treesitter(self, source: str, lang: str) -> List[Dict]:
        """用 tree-sitter 解析前端语言的顶层符号"""
        if not _init_treesitter():
            logger.warning("⚠️ tree-sitter 不可用，返回空符号表")
            return []

        ts_lang = _TS_LANGUAGES.get(lang)
        if not ts_lang:
            logger.warning(f"⚠️ 不支持的语言: {lang}")
            return []

        from tree_sitter import Parser
        parser = Parser(ts_lang)
        tree = parser.parse(source.encode("utf-8"))

        if lang in ("javascript", "typescript", "tsx"):
            return self._extract_js_symbols(tree.root_node, source)
        elif lang == "css":
            return self._extract_css_symbols(tree.root_node, source)
        elif lang == "html":
            return self._extract_html_symbols(tree.root_node, source)
        else:
            return []

    def _extract_js_symbols(self, root, source: str) -> List[Dict]:
        """
        提取 JS/TS/JSX/TSX 的顶层符号:
        - function_declaration / generator_function_declaration
        - class_declaration
        - lexical_declaration (const/let 组件或箭头函数)
        - export_statement (包装以上节点)
        """
        symbols = []
        source_lines = source.splitlines()

        for child in root.children:
            node = child

            # 剥离 export 包装
            if node.type == "export_statement":
                inner = self._get_export_inner(node)
                if inner:
                    node = inner

            if node.type in ("function_declaration", "generator_function_declaration"):
                name = self._ts_get_name(node, "name")
                if name:
                    symbols.append({
                        "name": name,
                        "type": "function",
                        "start_line": child.start_point[0] + 1,
                        "end_line": child.end_point[0] + 1,
                    })

            elif node.type == "class_declaration":
                name = self._ts_get_name(node, "name")
                if name:
                    symbols.append({
                        "name": name,
                        "type": "class",
                        "start_line": child.start_point[0] + 1,
                        "end_line": child.end_point[0] + 1,
                    })
                    # 类内方法
                    body = node.child_by_field_name("body")
                    if body:
                        for m in body.children:
                            if m.type == "method_definition":
                                mname = self._ts_get_name(m, "name")
                                if mname:
                                    symbols.append({
                                        "name": f"{name}.{mname}",
                                        "type": "method",
                                        "start_line": m.start_point[0] + 1,
                                        "end_line": m.end_point[0] + 1,
                                    })

            elif node.type == "lexical_declaration":
                # const App = () => { ... }  或 const router = express.Router()
                for decl in node.children:
                    if decl.type == "variable_declarator":
                        vname = self._ts_get_name(decl, "name")
                        if vname:
                            sym_type = "variable"
                            # 检查值是否为箭头函数 / 函数表达式
                            value = decl.child_by_field_name("value")
                            if value and value.type in ("arrow_function", "function", "function_expression"):
                                sym_type = "function"
                            elif value and value.type == "call_expression":
                                sym_type = "variable"
                            symbols.append({
                                "name": vname,
                                "type": sym_type,
                                "start_line": child.start_point[0] + 1,
                                "end_line": child.end_point[0] + 1,
                            })

            elif node.type == "expression_statement":
                # 顶层 IIFE 或 module.exports = ...
                symbols.append({
                    "name": f"<expr:L{child.start_point[0]+1}>",
                    "type": "expression",
                    "start_line": child.start_point[0] + 1,
                    "end_line": child.end_point[0] + 1,
                })

        return symbols

    def _extract_css_symbols(self, root, source: str) -> List[Dict]:
        """提取 CSS 规则集选择器"""
        symbols = []
        for child in root.children:
            if child.type == "rule_set":
                # 取选择器文本
                selectors = child.child_by_field_name("selectors")
                if not selectors:
                    # fallback: 取第一个子节点
                    for sub in child.children:
                        if sub.type not in ("block", "{", "}"):
                            selectors = sub
                            break
                name = source[selectors.start_byte:selectors.end_byte].strip() if selectors else f"<rule:L{child.start_point[0]+1}>"
                symbols.append({
                    "name": name,
                    "type": "css_rule",
                    "start_line": child.start_point[0] + 1,
                    "end_line": child.end_point[0] + 1,
                })
            elif child.type == "media_statement":
                name = "@media"
                for sub in child.children:
                    if sub.type == "feature_query" or sub.type == "keyword_query":
                        name = f"@media {source[sub.start_byte:sub.end_byte].strip()}"
                        break
                symbols.append({
                    "name": name,
                    "type": "css_media",
                    "start_line": child.start_point[0] + 1,
                    "end_line": child.end_point[0] + 1,
                })
            elif child.type in ("keyframes_statement", "import_statement", "charset_statement"):
                name = source[child.start_byte:child.end_byte].split("{")[0].strip()[:60]
                symbols.append({
                    "name": name,
                    "type": f"css_{child.type.replace('_statement', '')}",
                    "start_line": child.start_point[0] + 1,
                    "end_line": child.end_point[0] + 1,
                })
        return symbols

    def _extract_html_symbols(self, root, source: str) -> List[Dict]:
        """
        提取 HTML 中有意义的块元素（递归遍历，不限于顶层）。
        提取规则：
        - 所有 <script> 和 <style> 标签（无论嵌套深度）
        - 带 id= 或 class= 的块级元素（div/section/nav/form/table 等）
        - <!DOCTYPE> 声明
        - 最大深度 5 避免噪音过多
        """
        symbols = []
        BLOCK_TAGS = {"div", "section", "nav", "header", "footer", "main",
                      "form", "table", "aside", "article", "template", "ul", "ol"}

        def _walk(node, depth=0):
            if depth > 5:
                return

            if node.type == "doctype":
                symbols.append({
                    "name": "<!DOCTYPE>",
                    "type": "html_doctype",
                    "start_line": node.start_point[0] + 1,
                    "end_line": node.end_point[0] + 1,
                })
                return

            if node.type == "element":
                tag_info = self._html_get_tag_info(node, source)
                if tag_info:
                    tag_name, attrs = tag_info

                    if tag_name in ("script", "style"):
                        symbols.append({
                            "name": f"<{tag_name}>",
                            "type": f"html_{tag_name}",
                            "start_line": node.start_point[0] + 1,
                            "end_line": node.end_point[0] + 1,
                        })
                        return  # 不递归进 script/style 内部

                    # 带 id/class 的块元素，或本身就是有意义的块标签
                    if tag_name in BLOCK_TAGS or attrs:
                        label = f"<{tag_name}"
                        if attrs:
                            label += f" {attrs}"
                        label += ">"
                        symbols.append({
                            "name": label,
                            "type": "html_element",
                            "start_line": node.start_point[0] + 1,
                            "end_line": node.end_point[0] + 1,
                        })

            # 递归子节点
            for child in node.children:
                _walk(child, depth + 1)

        _walk(root)
        return symbols

    # ------ Vue SFC 嵌套解析 ------

    def _list_symbols_vue(self, source: str) -> List[Dict]:
        """
        Vue SFC 解析：
        1. 先用 tree-sitter-html 解析出 <template>/<script>/<style> 顶层块
        2. 对 <script> 内容用 JS/TS 解析器深层提取函数/组件
        3. 对 <style> 内容用 CSS 解析器深层提取选择器

        所有行号经过偏移矫正，保持与原文件一致。

        注意：tree-sitter-html 将 <script> 和 <style> 解析为特殊的
        script_element 和 style_element 节点（而非普通 element），
        内部内容为 raw_text 节点。
        """
        if not _init_treesitter():
            return []

        symbols = []

        from tree_sitter import Parser
        html_parser = Parser(_TS_LANGUAGES["html"])
        tree = html_parser.parse(source.encode("utf-8"))

        for child in tree.root_node.children:

            # --- <template> 等普通 element ---
            if child.type == "element":
                tag_info = self._html_get_tag_info(child, source)
                if not tag_info:
                    continue
                tag_name, _ = tag_info
                symbols.append({
                    "name": f"<{tag_name}>",
                    "type": f"vue_{tag_name}",
                    "start_line": child.start_point[0] + 1,
                    "end_line": child.end_point[0] + 1,
                })

            # --- <script> (script_element) ---
            elif child.type == "script_element":
                symbols.append({
                    "name": "<script>",
                    "type": "vue_script",
                    "start_line": child.start_point[0] + 1,
                    "end_line": child.end_point[0] + 1,
                })
                # 提取 raw_text 内容做深层 JS/TS 解析
                raw_text_node = None
                for sub in child.children:
                    if sub.type == "raw_text":
                        raw_text_node = sub
                        break
                if raw_text_node:
                    inner_text = source[raw_text_node.start_byte:raw_text_node.end_byte]
                    line_offset = raw_text_node.start_point[0]  # 0-indexed

                    # 检测 lang="ts"
                    start_tag_text = ""
                    for sub in child.children:
                        if sub.type == "start_tag":
                            start_tag_text = source[sub.start_byte:sub.end_byte]
                            break
                    is_ts = 'lang="ts"' in start_tag_text
                    inner_lang = "typescript" if is_ts else "javascript"

                    inner_symbols = self._list_symbols_treesitter(inner_text, inner_lang)
                    for sym in inner_symbols:
                        sym["start_line"] += line_offset
                        sym["end_line"] += line_offset
                        sym["name"] = f"script::{sym['name']}"
                        symbols.append(sym)

            # --- <style> (style_element) ---
            elif child.type == "style_element":
                symbols.append({
                    "name": "<style>",
                    "type": "vue_style",
                    "start_line": child.start_point[0] + 1,
                    "end_line": child.end_point[0] + 1,
                })
                # 提取 raw_text 内容做深层 CSS 解析
                raw_text_node = None
                for sub in child.children:
                    if sub.type == "raw_text":
                        raw_text_node = sub
                        break
                if raw_text_node:
                    inner_text = source[raw_text_node.start_byte:raw_text_node.end_byte]
                    line_offset = raw_text_node.start_point[0]

                    inner_symbols = self._list_symbols_treesitter(inner_text, "css")
                    for sym in inner_symbols:
                        sym["start_line"] += line_offset
                        sym["end_line"] += line_offset
                        sym["name"] = f"style::{sym['name']}"
                        symbols.append(sym)

        return symbols

    # ------ 工具方法 ------

    @staticmethod
    def _ts_get_name(node, field: str = "name") -> str:
        """从 tree-sitter 节点中提取命名字段的文本"""
        name_node = node.child_by_field_name(field)
        if name_node:
            return name_node.text.decode("utf-8") if isinstance(name_node.text, bytes) else name_node.text
        return ""

    @staticmethod
    def _get_export_inner(export_node):
        """从 export_statement 中剥离出被导出的内部声明节点"""
        for child in export_node.children:
            if child.type in ("function_declaration", "class_declaration",
                              "lexical_declaration", "variable_declaration",
                              "generator_function_declaration",
                              "expression_statement"):
                return child
            # export default function/class
            if child.type == "default":
                continue
            if child.type in ("function", "function_expression", "arrow_function"):
                return child
        return None

    @staticmethod
    def _html_get_tag_info(element_node, source: str):
        """从 HTML element 节点获取 (tag_name, key_attrs_str)"""
        start_tag = None
        for child in element_node.children:
            if child.type in ("start_tag", "self_closing_tag"):
                start_tag = child
                break
        if not start_tag:
            return None

        tag_name = None
        attrs = []
        for child in start_tag.children:
            if child.type == "tag_name":
                tag_name = source[child.start_byte:child.end_byte]
            elif child.type == "attribute":
                attr_text = source[child.start_byte:child.end_byte]
                if any(k in attr_text for k in ("id=", "class=", ":class=", "v-if=", "v-for=")):
                    attrs.append(attr_text)
        if not tag_name:
            return None
        return tag_name.lower(), " ".join(attrs)[:80]

    @staticmethod
    def _html_get_inner_text(element_node, source: str):
        """
        提取 HTML 元素的内部文本内容（如 <script>...</script> 中间部分）。
        返回 (inner_text, line_offset) — offset 用于矫正嵌套解析的行号。
        """
        # 找到 start_tag 和 end_tag 之间的 raw_text 或其他内容节点
        start_byte = None
        end_byte = None
        line_offset = 0

        for child in element_node.children:
            if child.type in ("start_tag", "self_closing_tag"):
                start_byte = child.end_byte
                line_offset = child.end_point[0]  # 0-indexed
            elif child.type == "end_tag":
                end_byte = child.start_byte

        if start_byte is not None and end_byte is not None and end_byte > start_byte:
            inner = source[start_byte:end_byte]
            # 去掉首行的换行
            if inner.startswith("\n"):
                inner = inner[1:]
                line_offset += 1
            return inner, line_offset

        return None, 0
