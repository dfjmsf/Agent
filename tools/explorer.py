"""
Explorer Tools — 文件系统探索工具集
提供目录浏览、文件读取、内容搜索和目录树获取能力。
为 Agent Function Calling 提供底层实现。
"""
import os
import re
import logging
from typing import List, Dict, Any, Optional

logger = logging.getLogger("Explorer")


def _safe_resolve(base_path: str, rel_path: str) -> Optional[str]:
    """
    路径安全验证：确保解析后的绝对路径不会逃逸出 base_path。
    返回安全的绝对路径，或 None（如果检测到路径逃逸）。
    """
    base = os.path.abspath(base_path)
    target = os.path.abspath(os.path.join(base, rel_path))
    if not target.startswith(base):
        logger.error(f"🚫 路径逃逸拦截: base={base}, target={target}")
        return None
    return target


def list_directory(base_path: str, rel_path: str = ".") -> Dict[str, Any]:
    """
    列出目录内容（文件名 + 类型 + 大小）。
    
    返回: {"success": bool, "entries": [...], "error": str?}
    """
    abs_path = _safe_resolve(base_path, rel_path)
    if not abs_path:
        return {"success": False, "error": "路径安全验证失败"}
    
    if not os.path.isdir(abs_path):
        return {"success": False, "error": f"目录不存在: {rel_path}"}
    
    entries = []
    try:
        for name in sorted(os.listdir(abs_path)):
            full = os.path.join(abs_path, name)
            entry = {"name": name}
            if os.path.isdir(full):
                entry["type"] = "directory"
                entry["children"] = len(os.listdir(full))
            else:
                entry["type"] = "file"
                entry["size"] = os.path.getsize(full)
            entries.append(entry)
        
        logger.info(f"📂 list_directory({rel_path}) → {len(entries)} 项")
        return {"success": True, "entries": entries}
    except Exception as e:
        return {"success": False, "error": str(e)}


def read_file(
    base_path: str,
    rel_path: str,
    start_line: Optional[int] = None,
    end_line: Optional[int] = None
) -> Dict[str, Any]:
    """
    读取文件内容，支持可选的行范围（1-indexed, inclusive）。
    
    返回: {"success": bool, "content": str, "total_lines": int, "error": str?}
    """
    abs_path = _safe_resolve(base_path, rel_path)
    if not abs_path:
        return {"success": False, "error": "路径安全验证失败"}
    
    if not os.path.isfile(abs_path):
        return {"success": False, "error": f"文件不存在: {rel_path}"}
    
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        
        total = len(lines)
        
        if start_line is not None or end_line is not None:
            s = max(1, start_line or 1) - 1  # 转为 0-indexed
            e = min(total, end_line or total)
            selected = lines[s:e]
            content = "".join(selected)
        else:
            content = "".join(lines)
        
        logger.info(f"📄 read_file({rel_path}) → {total} 行")
        return {"success": True, "content": content, "total_lines": total}
    except Exception as e:
        return {"success": False, "error": str(e)}


def search_in_files(
    base_path: str,
    query: str,
    file_pattern: Optional[str] = None,
    max_results: int = 30
) -> Dict[str, Any]:
    """
    在文件中搜索关键字（支持正则表达式）。
    
    返回: {"success": bool, "matches": [{"file": ..., "line": ..., "content": ...}]}
    """
    base = os.path.abspath(base_path)
    if not os.path.isdir(base):
        return {"success": False, "error": f"基础路径不存在: {base_path}"}
    
    try:
        pattern = re.compile(query, re.IGNORECASE)
    except re.error:
        # 如果不是有效正则就当纯文本搜索
        pattern = re.compile(re.escape(query), re.IGNORECASE)
    
    matches = []
    try:
        for root, dirs, files in os.walk(base):
            # 跳过隐藏目录和常见无关目录
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ('__pycache__', 'node_modules', '.git', 'venv', '.venv')]
            
            for fname in files:
                if file_pattern and not re.match(file_pattern.replace("*", ".*"), fname):
                    continue
                
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, base)
                
                try:
                    with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                        for i, line in enumerate(f, 1):
                            if pattern.search(line):
                                matches.append({
                                    "file": rel.replace("\\", "/"),
                                    "line": i,
                                    "content": line.rstrip()[:200]
                                })
                                if len(matches) >= max_results:
                                    break
                except (UnicodeDecodeError, PermissionError):
                    continue
                
                if len(matches) >= max_results:
                    break
            if len(matches) >= max_results:
                break
        
        logger.info(f"🔎 search_in_files('{query}') → {len(matches)} 条匹配")
        return {"success": True, "matches": matches}
    except Exception as e:
        return {"success": False, "error": str(e)}


def get_file_tree(base_path: str, max_depth: int = 3) -> Dict[str, Any]:
    """
    递归获取目录树结构（带深度限制）。
    
    返回: {"success": bool, "tree": str}
    """
    base = os.path.abspath(base_path)
    if not os.path.isdir(base):
        return {"success": False, "error": f"目录不存在: {base_path}"}
    
    lines = []
    SKIP_DIRS = {'.git', '__pycache__', 'node_modules', '.venv', 'venv', '.idea', '.vscode'}
    
    def _walk(path: str, prefix: str, depth: int):
        if depth > max_depth:
            return
        
        try:
            entries = sorted(os.listdir(path))
        except PermissionError:
            return
        
        dirs = [e for e in entries if os.path.isdir(os.path.join(path, e)) and e not in SKIP_DIRS and not e.startswith('.')]
        files = [e for e in entries if os.path.isfile(os.path.join(path, e)) and not e.startswith('.')]
        
        items = [(d, True) for d in dirs] + [(f, False) for f in files]
        
        for i, (name, is_dir) in enumerate(items):
            is_last = (i == len(items) - 1)
            connector = "└── " if is_last else "├── "
            icon = "📁 " if is_dir else "📄 "
            lines.append(f"{prefix}{connector}{icon}{name}")
            
            if is_dir:
                extension = "    " if is_last else "│   "
                _walk(os.path.join(path, name), prefix + extension, depth + 1)
    
    root_name = os.path.basename(base)
    lines.append(f"📁 {root_name}/")
    _walk(base, "", 1)
    
    tree_str = "\n".join(lines)
    logger.info(f"🌳 get_file_tree() → {len(lines)} 节点")
    return {"success": True, "tree": tree_str}
