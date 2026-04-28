"""
FileReaderSkill — 文件读取技能

从原 SkillRunner._skill_read_file 提取，参数化 base_dir。
- QA 使用时：base_dir = sandbox_dir（沙盒隔离）
- PM 使用时：base_dir = project_root（读取用户需求文档等）

Phase 2 PM A-1 增强：
  - 三种读取模式：概览 / 符号定位 / 行号定位
  - 总是附带文件头 import 区（前 10 行）
  - PM 无需读全文件，精准读取函数/类/选择器的代码段 ±10 行

✅ 无状态，可公共化（通过不同 base_dir 实例化）。
"""
import os
import re
import logging

from core.skills.base import BaseSkill

logger = logging.getLogger("SkillRunner")

FILE_READ_MAX = 50000  # 字符
CONTEXT_LINES = 10     # 上下文行数
HEADER_LINES = 10      # 文件头行数（import 区）


class FileReaderSkill(BaseSkill):
    """文件读取 — 受限于 base_dir 的安全读取，支持精准定位"""

    def __init__(self, base_dir: str):
        self.base_dir = os.path.abspath(base_dir)

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": (
                    "读取项目中指定文件的代码内容。支持三种模式：\n"
                    "1. 仅传 file_path → 返回文件头（前10行）+ 总行数提示\n"
                    "2. 传 file_path + symbol（函数名/类名/CSS选择器）→ 返回该符号的完整代码 + 上下10行 + 文件头import区\n"
                    "3. 传 file_path + line_start + line_end → 返回指定行范围 + 上下10行 + 文件头import区"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "相对于项目根目录的文件路径（如 'app.py'、'templates/index.html'）"
                        },
                        "symbol": {
                            "type": "string",
                            "description": "要查看的函数名、类名或 CSS 选择器（如 'add_expense'、'.card'）。可选。"
                        },
                        "line_start": {
                            "type": "integer",
                            "description": "起始行号（1-indexed）。可选，与 line_end 配合使用。"
                        },
                        "line_end": {
                            "type": "integer",
                            "description": "结束行号（1-indexed）。可选，与 line_start 配合使用。"
                        },
                    },
                    "required": ["file_path"],
                },
            },
        }

    def execute(self, **kwargs) -> str:
        file_path = kwargs["file_path"]
        symbol = kwargs.get("symbol")
        line_start = kwargs.get("line_start")
        line_end = kwargs.get("line_end")

        # 安全护栏: 路径校验
        normalized = os.path.normpath(file_path)
        if normalized.startswith("..") or os.path.isabs(normalized):
            return f"错误: 禁止访问项目目录之外的路径 '{file_path}'"

        full_path = os.path.join(self.base_dir, normalized)
        abs_full = os.path.abspath(full_path)

        # 二次校验: 确认在 base_dir 内
        if not abs_full.startswith(self.base_dir):
            return f"错误: 路径越界 '{file_path}'"

        if not os.path.isfile(abs_full):
            return f"文件不存在: {file_path}"

        try:
            with open(abs_full, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception as e:
            return f"读取失败: {e}"

        total_lines = len(lines)

        # === 兼容模式：QA 等旧调用方无 symbol/line_start → 返回全文件（保持向后兼容）===
        if not symbol and line_start is None:
            content = "".join(lines[:FILE_READ_MAX])
            if len(content) >= FILE_READ_MAX:
                content += "\n... (文件过大，已截断)"
            return content

        # === 精准模式 1: 按 symbol 定位 ===
        if symbol:
            found_start, found_end = self._find_symbol(lines, symbol)
            if found_start is None:
                return f"未找到符号 '{symbol}'。文件共 {total_lines} 行，请尝试传入 line_start/line_end 精确指定。"
            line_start, line_end = found_start, found_end

        # === 精准模式 2/3: 按行号读取 ===
        if line_start is not None:
            if line_end is None:
                line_end = min(line_start + 30, total_lines)

            # 加上下文
            actual_start = max(1, line_start - CONTEXT_LINES)
            actual_end = min(total_lines, line_end + CONTEXT_LINES)

            # 构建输出
            parts = []

            # 文件头（import 区）— 仅当目标区间不包含头部时才附加
            if actual_start > HEADER_LINES:
                header = "".join(lines[:HEADER_LINES])
                parts.append(f"=== {file_path} 头部 (L1-{HEADER_LINES}) ===\n{header}")

            # 目标代码段（带行号标注）
            target_lines = []
            for idx in range(actual_start - 1, actual_end):
                prefix = ">" if line_start <= idx + 1 <= line_end else " "
                target_lines.append(f"{prefix} {idx + 1:>4}: {lines[idx]}")
            target = "".join(target_lines)
            parts.append(f"=== {file_path} (L{actual_start}-{actual_end}, 共{total_lines}行) ===\n{target}")

            return "\n".join(parts)

        # Fallback
        content = "".join(lines[:FILE_READ_MAX])
        return content

    @staticmethod
    def _find_symbol(lines: list, symbol: str) -> tuple:
        """在文件中查找 symbol 的行号范围，返回 (start, end) 均为 1-indexed"""
        symbol_lower = symbol.lower().strip('.')

        for i, line in enumerate(lines):
            stripped = line.strip()

            # Python: def xxx / class xxx
            if stripped.startswith(('def ', 'class ')):
                # 提取函数/类名
                m = re.match(r'(?:def|class)\s+(\w+)', stripped)
                if m and m.group(1).lower() == symbol_lower:
                    end = FileReaderSkill._find_block_end(lines, i)
                    return (i + 1, end + 1)

            # CSS: .xxx { / #xxx { / tag {
            if '{' in stripped:
                selector = stripped.split('{')[0].strip()
                # 精确匹配选择器
                if symbol_lower in selector.lower():
                    end = FileReaderSkill._find_css_block_end(lines, i)
                    return (i + 1, end + 1)

            # HTML: id="xxx" / class="xxx"
            if f'id="{symbol}"' in stripped or f"id='{symbol}'" in stripped:
                return (i + 1, min(i + 20, len(lines)))
            if f'class="{symbol}"' in stripped or f"class='{symbol}'" in stripped:
                return (i + 1, min(i + 20, len(lines)))

        return (None, None)

    @staticmethod
    def _find_block_end(lines: list, start_idx: int) -> int:
        """找到 Python 代码块结束行（基于缩进）"""
        if start_idx >= len(lines):
            return start_idx
        first_line = lines[start_idx]
        base_indent = len(first_line) - len(first_line.lstrip())
        end = start_idx
        for j in range(start_idx + 1, len(lines)):
            line = lines[j]
            if not line.strip():
                continue
            current_indent = len(line) - len(line.lstrip())
            if current_indent <= base_indent:
                break
            end = j
        return end

    @staticmethod
    def _find_css_block_end(lines: list, start_idx: int) -> int:
        """找到 CSS 代码块结束行（匹配 {}）"""
        depth = 0
        for j in range(start_idx, len(lines)):
            depth += lines[j].count('{')
            depth -= lines[j].count('}')
            if depth <= 0 and j > start_idx:
                return j
        return min(start_idx + 20, len(lines) - 1)
