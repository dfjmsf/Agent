"""
Observer — 只读代码观察者

v1.3 核心基建：
- 为 Coder/Reviewer 的 Tool Calling 提供底层实现
- 本身不调用大模型，100% 忠实于物理硬盘的代码现状
- 三大能力：目录树 (get_tree) | 代码骨架 (get_skeleton) | 精确读取 (read_file)
- 防爆机制：大文件拒读，强制大模型精确制导
"""
import os
import ast
import re
import logging
from typing import Optional, List, Dict, Any

logger = logging.getLogger("Observer")

# 防爆：防止大模型读取巨型文件炸掉 Token
MAX_READ_LINES = 300

# 跳过的目录
SKIP_DIRS = {
    '.git', '__pycache__', 'node_modules', '.venv', 'venv',
    '.idea', '.vscode', '.sandbox', '.pytest_cache', 'dist',
}

# 跳过的文件（二进制/锁文件/巨型文件）
SKIP_FILES = {
    'package-lock.json', 'yarn.lock', 'pnpm-lock.yaml',
    '.DS_Store', 'Thumbs.db',
}


class Observer:
    """
    只读代码观察者 — Agent 的 Tool Calling 工具箱。

    核心原则：
    - 绝对只读，没有任何修改 VFS 的权限
    - 不调用大模型，零 Token 消耗
    - 所有输出 100% 忠实于物理硬盘
    """

    def __init__(self, vfs_root: str):
        """
        Args:
            vfs_root: VFS 真理区的根目录（项目根路径）
        """
        self.vfs_root = os.path.abspath(vfs_root)
        if not os.path.isdir(self.vfs_root):
            os.makedirs(self.vfs_root, exist_ok=True)

    def _safe_resolve(self, rel_path: str) -> Optional[str]:
        """路径安全验证：防止路径逃逸"""
        target = os.path.abspath(os.path.join(self.vfs_root, rel_path))
        if not target.startswith(self.vfs_root):
            logger.error(f"🚫 路径逃逸拦截: {rel_path}")
            return None
        return target

    # ============================================================
    # 1. get_tree — 目录树
    # ============================================================

    def get_tree(self, max_depth: int = 3) -> str:
        """
        递归获取目录树结构。

        Args:
            max_depth: 最大递归深度，默认 3

        Returns:
            格式化的目录树字符串
        """
        lines = []

        def _walk(path: str, prefix: str, depth: int):
            if depth > max_depth:
                return
            try:
                entries = sorted(os.listdir(path))
            except PermissionError:
                return

            dirs = [e for e in entries
                    if os.path.isdir(os.path.join(path, e))
                    and e not in SKIP_DIRS and not e.startswith('.')]
            files = [e for e in entries
                     if os.path.isfile(os.path.join(path, e))
                     and not e.startswith('.') and e not in SKIP_FILES]

            items = [(d, True) for d in dirs] + [(f, False) for f in files]

            for i, (name, is_dir) in enumerate(items):
                is_last = (i == len(items) - 1)
                connector = "└── " if is_last else "├── "
                icon = "📁 " if is_dir else "📄 "
                lines.append(f"{prefix}{connector}{icon}{name}")

                if is_dir:
                    extension = "    " if is_last else "│   "
                    _walk(os.path.join(path, name), prefix + extension, depth + 1)

        root_name = os.path.basename(self.vfs_root)
        lines.append(f"📁 {root_name}/")
        _walk(self.vfs_root, "", 1)

        tree_str = "\n".join(lines)
        logger.info(f"🌳 get_tree() → {len(lines)} 节点")
        return tree_str

    # ============================================================
    # 2. get_skeleton — 代码骨架提取
    # ============================================================

    def get_skeleton(self, file_path: str) -> str:
        """
        提取代码骨架：签名 + docstring + 类属性，函数体替换为 ...

        Python 文件: 使用 ast 模块解析
        前端文件: 使用正则提取关键节点

        Args:
            file_path: 相对于 vfs_root 的文件路径

        Returns:
            Markdown 格式的代码骨架
        """
        abs_path = self._safe_resolve(file_path)
        if not abs_path or not os.path.isfile(abs_path):
            return f"Error: 文件不存在: {file_path}"

        ext = os.path.splitext(file_path)[1].lower()

        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                source = f.read()
        except Exception as e:
            return f"Error: 读取失败: {e}"

        if ext == ".py":
            return self._skeleton_python(source, file_path)
        elif ext in ('.html', '.htm', '.vue', '.svelte'):
            return self._skeleton_html(source, file_path)
        elif ext in ('.js', '.jsx', '.ts', '.tsx'):
            return self._skeleton_js(source, file_path)
        elif ext == '.css':
            return self._skeleton_css(source, file_path)
        else:
            # 未知类型：返回前 50 行
            lines = source.split('\n')[:50]
            return f"# {file_path} (未知类型，前50行)\n" + "\n".join(lines)

    def _skeleton_python(self, source: str, file_path: str) -> str:
        """Python AST 骨架提取"""
        try:
            tree = ast.parse(source)
        except SyntaxError as e:
            return f"# {file_path} (语法错误: {e})\n{source[:500]}"

        lines = [f"# {file_path}"]

        # 模块级 import
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                lines.append(ast.get_source_segment(source, node) or "")

        # 模块级变量赋值
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Assign):
                segment = ast.get_source_segment(source, node)
                if segment:
                    # 只保留短赋值（常量/配置）
                    if len(segment) < 200:
                        lines.append(segment)

        # 模块级函数和类
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
                lines.append("")
                lines.append(self._format_function(node, source, indent=""))
            elif isinstance(node, ast.ClassDef):
                lines.append("")
                lines.append(self._format_class(node, source))

        result = "\n".join(lines)
        logger.info(f"👁️ get_skeleton({file_path}) → {len(result)} chars (Python AST)")
        return result

    def _format_function(self, node, source: str, indent: str = "") -> str:
        """格式化函数签名 + 装饰器 + docstring"""
        parts = []

        # 提取装饰器（如 @app.route('/api/notes'), @router.get('/items')）
        for decorator in node.decorator_list:
            dec_src = ast.get_source_segment(source, decorator)
            if dec_src:
                parts.append(f"{indent}@{dec_src}")

        # 构建完整签名
        func_type = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"

        # 提取参数
        args_parts = []
        all_args = node.args

        # 处理普通参数
        num_defaults = len(all_args.defaults)
        num_args = len(all_args.args)
        for i, arg in enumerate(all_args.args):
            arg_str = arg.arg
            if arg.annotation:
                ann = ast.get_source_segment(source, arg.annotation)
                if ann:
                    arg_str += f": {ann}"
            # 检查是否有默认值
            default_idx = i - (num_args - num_defaults)
            if default_idx >= 0:
                default = ast.get_source_segment(source, all_args.defaults[default_idx])
                if default:
                    arg_str += f" = {default}"
            args_parts.append(arg_str)

        # *args
        if all_args.vararg:
            v = all_args.vararg
            s = f"*{v.arg}"
            if v.annotation:
                ann = ast.get_source_segment(source, v.annotation)
                if ann:
                    s += f": {ann}"
            args_parts.append(s)

        # **kwargs
        if all_args.kwarg:
            k = all_args.kwarg
            s = f"**{k.arg}"
            if k.annotation:
                ann = ast.get_source_segment(source, k.annotation)
                if ann:
                    s += f": {ann}"
            args_parts.append(s)

        args_str = ", ".join(args_parts)

        # 返回类型
        returns = ""
        if node.returns:
            ret = ast.get_source_segment(source, node.returns)
            if ret:
                returns = f" -> {ret}"

        sig = f"{indent}{func_type} {node.name}({args_str}){returns}:"
        parts.append(sig)

        # Docstring
        docstring = ast.get_docstring(node)
        if docstring:
            # 取第一段
            first_para = docstring.split('\n\n')[0].strip()
            if len(first_para) > 200:
                first_para = first_para[:200] + "..."
            parts.append(f'{indent}    """{first_para}"""')

        parts.append(f"{indent}    ...")

        # 递归提取有装饰器的嵌套函数（如 Flask @app.route 路由）
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.decorator_list:  # 只提取带装饰器的嵌套函数
                    parts.append(self._format_function(child, source, indent=indent + "    "))

        return "\n".join(parts)

    def _format_class(self, node, source: str) -> str:
        """格式化类定义 + 方法签名"""
        # 类声明
        bases = []
        for base in node.bases:
            b = ast.get_source_segment(source, base)
            if b:
                bases.append(b)
        bases_str = f"({', '.join(bases)})" if bases else ""
        lines = [f"class {node.name}{bases_str}:"]

        # Docstring
        docstring = ast.get_docstring(node)
        if docstring:
            first_para = docstring.split('\n\n')[0].strip()
            if len(first_para) > 200:
                first_para = first_para[:200] + "..."
            lines.append(f'    """{first_para}"""')

        # 类级变量
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Assign):
                segment = ast.get_source_segment(source, child)
                if segment and len(segment) < 200:
                    lines.append(f"    {segment.strip()}")
            elif isinstance(child, ast.AnnAssign):
                segment = ast.get_source_segment(source, child)
                if segment and len(segment) < 200:
                    lines.append(f"    {segment.strip()}")

        # 方法
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                lines.append("")
                lines.append(self._format_function(child, source, indent="    "))

        return "\n".join(lines)

    def _skeleton_html(self, source: str, file_path: str) -> str:
        """HTML/Vue/Svelte 骨架提取"""
        lines = [f"# {file_path} (HTML骨架)"]

        # <script> 标签内容
        script_match = re.findall(
            r'<script[^>]*>(.*?)</script>',
            source, re.DOTALL | re.IGNORECASE
        )
        if script_match:
            for i, script in enumerate(script_match):
                lines.append(f"\n## <script> #{i+1}")
                # 提取 function/const/let/var/class 声明
                for line in script.split('\n'):
                    stripped = line.strip()
                    if re.match(r'^(export\s+)?(default\s+)?(function|const|let|var|class|import|from)\s', stripped):
                        lines.append(f"  {stripped}")
                    elif re.match(r'^(export\s+)?default\s*\{', stripped):
                        lines.append(f"  {stripped}")

        # <style> 标签（只提取选择器）
        style_match = re.findall(
            r'<style[^>]*>(.*?)</style>',
            source, re.DOTALL | re.IGNORECASE
        )
        if style_match:
            lines.append(f"\n## <style>")
            for style in style_match:
                selectors = re.findall(r'^([.#\w][\w\-\s,.#:>\[\]=~*+]+)\s*\{', style, re.MULTILINE)
                for sel in selectors[:30]:  # 最多 30 个选择器
                    lines.append(f"  {sel.strip()} {{ ... }}")

        # 主要 HTML 结构标签（含 id/class）
        tags = re.findall(r'<(div|section|nav|header|footer|main|form|table|ul|ol)\s[^>]*(?:id|class)="([^"]*)"', source)
        if tags:
            lines.append(f"\n## HTML 结构")
            for tag, attr in tags[:20]:
                lines.append(f"  <{tag} ...=\"{attr}\">")

        # 交互元素 ID 提取（input, textarea, button, select, a）
        interactive_ids = re.findall(r'<(?:input|textarea|button|select|a)\s[^>]*id="([^"]*)"', source)
        if interactive_ids:
            lines.append(f"\n## 交互元素 ID")
            for eid in interactive_ids:
                lines.append(f"  #{eid}")
        result = "\n".join(lines)
        logger.info(f"👁️ get_skeleton({file_path}) → {len(result)} chars (HTML)")
        return result

    def _skeleton_js(self, source: str, file_path: str) -> str:
        """JavaScript/TypeScript 骨架提取"""
        lines = [f"# {file_path} (JS/TS骨架)"]

        for line_text in source.split('\n'):
            stripped = line_text.strip()

            # import 语句
            if re.match(r'^import\s', stripped):
                lines.append(stripped)
            # export + 声明
            elif re.match(r'^export\s+(default\s+)?(function|const|let|var|class|async\s+function|interface|type|enum)\s', stripped):
                # 找到函数/类名和签名
                sig = re.match(r'^(export\s+(?:default\s+)?(?:async\s+)?(?:function|const|let|var|class|interface|type|enum)\s+\w+[^{;]*)', stripped)
                if sig:
                    lines.append(sig.group(1).rstrip() + " { ... }")
                else:
                    lines.append(stripped[:150])
            # 顶层 function/class/const
            elif re.match(r'^(function|class|const|let|var|async\s+function)\s+\w+', stripped):
                sig = re.match(r'^((?:async\s+)?(?:function|class|const|let|var)\s+\w+[^{;]*)', stripped)
                if sig:
                    lines.append(sig.group(1).rstrip() + " { ... }")
                else:
                    lines.append(stripped[:150])

        result = "\n".join(lines)
        logger.info(f"👁️ get_skeleton({file_path}) → {len(result)} chars (JS/TS)")
        return result

    def _skeleton_css(self, source: str, file_path: str) -> str:
        """CSS 骨架提取：只保留选择器"""
        lines = [f"# {file_path} (CSS骨架)"]

        # @import
        for m in re.findall(r'^(@import\s+[^;]+;)', source, re.MULTILINE):
            lines.append(m)

        # CSS 变量 / 自定义属性
        for m in re.findall(r'^(\s*--[\w-]+\s*:.*?;)', source, re.MULTILINE):
            lines.append(m.strip())

        # 选择器
        selectors = re.findall(r'^([.#\w@][\w\-\s,.#:>\[\]=~*+()]+)\s*\{', source, re.MULTILINE)
        for sel in selectors[:50]:
            lines.append(f"{sel.strip()} {{ ... }}")

        result = "\n".join(lines)
        logger.info(f"👁️ get_skeleton({file_path}) → {len(result)} chars (CSS)")
        return result

    # ============================================================
    # 3. read_file — 精确读取 (防爆)
    # ============================================================

    def read_file(self, file_path: str, start_line: int = None, end_line: int = None) -> str:
        """
        精确读取文件内容。

        【防爆机制】未指定行范围且文件超过 MAX_READ_LINES 时，
        返回报错强制大模型分段读取。

        Args:
            file_path: 相对于 vfs_root 的文件路径
            start_line: 起始行号 (1-indexed, inclusive)
            end_line: 结束行号 (1-indexed, inclusive)

        Returns:
            文件内容字符串，或错误信息
        """
        abs_path = self._safe_resolve(file_path)
        if not abs_path or not os.path.isfile(abs_path):
            return f"Error: 文件不存在: {file_path}"

        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()
        except Exception as e:
            return f"Error: 读取失败: {e}"

        total = len(all_lines)

        # 防爆：未指定范围且文件过大
        if start_line is None and end_line is None:
            if total > MAX_READ_LINES:
                return (f"Error: 文件过大({total}行)，超过限制({MAX_READ_LINES}行)。"
                        f"请使用 start_line 和 end_line 参数分段读取。"
                        f"例如: read_file(\"{file_path}\", start_line=1, end_line={MAX_READ_LINES})")

        # 计算行范围
        s = max(1, start_line or 1) - 1  # 转为 0-indexed
        e = min(total, end_line or total)

        # 防爆：请求范围过大
        if e - s > MAX_READ_LINES:
            return (f"Error: 请求范围过大({e - s}行)，超过限制({MAX_READ_LINES}行)。"
                    f"请缩小 start_line 到 end_line 的范围。")

        selected = all_lines[s:e]
        content = "".join(selected)

        logger.info(f"📄 read_file({file_path}) → 行{s+1}-{e}/{total}")
        return content

    # ============================================================
    # 4. search_in_files — 关键词搜索 (可选工具)
    # ============================================================

    def search_in_files(self, query: str, file_pattern: str = None, max_results: int = 20) -> str:
        """
        在项目文件中搜索关键字。

        Args:
            query: 搜索关键词（支持正则）
            file_pattern: 文件名过滤（如 "*.py"）
            max_results: 最大结果数

        Returns:
            搜索结果字符串
        """
        try:
            pattern = re.compile(query, re.IGNORECASE)
        except re.error:
            pattern = re.compile(re.escape(query), re.IGNORECASE)

        matches = []
        for root, dirs, files in os.walk(self.vfs_root):
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith('.')]

            for fname in files:
                if fname in SKIP_FILES:
                    continue
                if file_pattern and not re.match(file_pattern.replace("*", ".*"), fname):
                    continue

                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, self.vfs_root).replace("\\", "/")

                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        for i, line in enumerate(f, 1):
                            if pattern.search(line):
                                matches.append(f"  {rel}:{i}: {line.rstrip()[:120]}")
                                if len(matches) >= max_results:
                                    break
                except (UnicodeDecodeError, PermissionError):
                    continue

                if len(matches) >= max_results:
                    break
            if len(matches) >= max_results:
                break

        if not matches:
            return f"未找到匹配 '{query}' 的内容"

        result = f"搜索 '{query}' → {len(matches)} 条结果:\n" + "\n".join(matches)
        logger.info(f"🔎 search_in_files('{query}') → {len(matches)} 条")
        return result


# ============================================================
# 5. Tool Calling JSON Schema
# ============================================================

OBSERVER_TOOL_SCHEMA = [
    {
        "type": "function",
        "function": {
            "name": "get_tree",
            "description": "获取项目目录树结构。用于了解项目的文件组织方式。",
            "parameters": {
                "type": "object",
                "properties": {
                    "max_depth": {
                        "type": "integer",
                        "description": "最大递归深度，默认3层",
                        "default": 3
                    }
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_skeleton",
            "description": "提取指定文件的代码骨架（函数签名、类定义、docstring），不包含函数实现体。用于了解文件的接口契约，极省 Token。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "相对于项目根目录的文件路径，如 'src/storage.py'"
                    }
                },
                "required": ["file_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "精确读取文件内容。大文件(超过300行)必须指定 start_line 和 end_line 分段读取。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "相对于项目根目录的文件路径"
                    },
                    "start_line": {
                        "type": "integer",
                        "description": "起始行号(1-indexed)，大文件必须指定"
                    },
                    "end_line": {
                        "type": "integer",
                        "description": "结束行号(1-indexed)，大文件必须指定"
                    }
                },
                "required": ["file_path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "search_in_files",
            "description": "在项目文件中搜索关键字或正则表达式。用于快速定位特定代码片段。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词或正则表达式"
                    },
                    "file_pattern": {
                        "type": "string",
                        "description": "文件名过滤，如 '*.py'，可选"
                    }
                },
                "required": ["query"]
            }
        }
    }
]
