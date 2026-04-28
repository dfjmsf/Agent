"""
JS/TS Abstract Syntax Tree Parser
Powered by tree-sitter & tree-sitter-javascript
用于提取前端代码中的网络请求 (fetch/axios) 和 属性访问 (MemberExpression).
从而为 ReviewerAgent 提供跨文件语义校验能力。
"""
import logging
import re
from typing import List, Set, Dict

logger = logging.getLogger("JSAstParser")

try:
    import tree_sitter
    import tree_sitter_javascript
    _TS_AVAILABLE = True
    _JS_LANGUAGE = tree_sitter.Language(tree_sitter_javascript.language())
except ImportError:
    _TS_AVAILABLE = False
    _JS_LANGUAGE = None


class JSAstParser:
    """提取 JS/TS 语义特征"""
    
    def __init__(self):
        self.is_available = _TS_AVAILABLE
        if self.is_available:
            self.parser = tree_sitter.Parser(_JS_LANGUAGE)

    def extract_semantic_info(self, code: str, ext: str) -> Dict[str, set]:
        """
        统一萃取接口
        返回:
        {
            "api_urls": {"/api/v1/users", "/login"}, 
            "property_access": {"userName", "userId", "data", "json", "list"}
        }
        """
        result = {
            "api_urls": set(),
            "property_access": set()
        }
        if not self.is_available:
            return result

        # Vue 文件需先抽取出 script 块内容
        if ext == ".vue":
            code = self._extract_vue_script(code)

        try:
            tree = self.parser.parse(code.encode('utf-8'))
            self._walk_and_extract(tree.root_node, result, code.encode('utf-8'))
        except Exception as e:
            logger.warning(f"JS AST Parsing failed: {e}")
            
        return result

    def _extract_vue_script(self, code: str) -> str:
        """从 Vue SFC 中提取 <script> 或 <script setup> 标签内容"""
        # 贪婪匹配可能会跨标签，这里用 DOTALL 配合截止到 </script>
        match = re.search(r'<script.*?>\s*(.*?)\s*</script>', code, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1)
        return ""

    def _walk_and_extract(self, node, result: dict, raw_bytes: bytes):
        """递归遍历 AST 提取目标特征"""
        node_type = node.type

        # 1. 提取属性访问 data.xxx -> xxx
        if node_type == "member_expression":
            # 找到 type == 'property_identifier' 的子节点
            for child in node.children:
                if child.type == "property_identifier":
                    try:
                        prop_name = raw_bytes[child.start_byte:child.end_byte].decode('utf-8')
                        result["property_access"].add(prop_name)
                    except Exception:
                        pass

        # 2. 提取 API 调用 (fetch, axios.get) 的第一个参数
        elif node_type == "call_expression":
            is_api_call = False
            
            if len(node.children) >= 2:
                callee_node = node.children[0]
                args_node = node.children[1]

                # 检测是否是 fetch()
                if callee_node.type == "identifier":
                    name = raw_bytes[callee_node.start_byte:callee_node.end_byte].decode('utf-8')
                    if name == "fetch":
                        is_api_call = True
                        
                # 检测是否是 axios.XXX() 或是 request.XXX()
                elif callee_node.type == "member_expression":
                    obj_name = ""
                    prop_name = ""
                    for child in callee_node.children:
                        if child.type == "identifier":
                            obj_name = raw_bytes[child.start_byte:child.end_byte].decode('utf-8')
                        elif child.type == "property_identifier":
                            prop_name = raw_bytes[child.start_byte:child.end_byte].decode('utf-8')
                    if obj_name in ("axios", "request", "api"):
                        is_api_call = True

                if is_api_call and args_node.type == "arguments":
                    # 抓取第一个字符串参数 (跳过 '(' )
                    for arg in args_node.children:
                        if arg.type == "string":
                            for s_child in arg.children:
                                if s_child.type == "string_fragment":
                                    url = raw_bytes[s_child.start_byte:s_child.end_byte].decode('utf-8')
                                    result["api_urls"].add(url)
                            break
                        elif arg.type == "template_string":
                            for s_child in arg.children:
                                if s_child.type == "string_fragment":
                                    # 提取模板中第一个静态片段，比如 `/api/item/${id}` 提取 `/api/item/`
                                    url = raw_bytes[s_child.start_byte:s_child.end_byte].decode('utf-8')
                                    url = url.split('?')[0] # ignore query
                                    result["api_urls"].add(url)
                                    break
                            break

        # 递归子节点
        for child in node.children:
            self._walk_and_extract(child, result, raw_bytes)
