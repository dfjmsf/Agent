"""
CodePatcher — 代码缝合器

v1.3 核心基建 (Engine 子模块)：
- 按 XML action 属性路由缝合逻辑 (create / rewrite / modify)
- SEARCH/REPLACE 块的三级匹配 (精确 → 空白归一化 → 模糊)
- 缝合失败抛 PatchFailedError 携带具体原因，写入 error_logs 指导 Coder 修复
"""
import re
import logging
from typing import List, Tuple, Optional, Dict, Any
from difflib import SequenceMatcher

logger = logging.getLogger("CodePatcher")


# ============================================================
# 1. 异常定义
# ============================================================

class PatchFailedError(Exception):
    """
    缝合失败异常。

    Engine 捕获后将 reason 追加到 task.error_logs，
    状态扭转为 PATCH_FAILED（回到 CODING），不唤醒 Reviewer — 省 Token。
    """
    def __init__(self, reason: str, search_block: str = ""):
        self.reason = reason
        self.search_block = search_block[:80] if search_block else ""
        super().__init__(reason)


# ============================================================
# 2. XML 提取
# ============================================================

def extract_xml_files(raw_text: str) -> List[Dict[str, str]]:
    """
    从 LLM 输出中提取所有 <astrea_file> XML 标签。

    Returns:
        [{"path": "src/main.py", "action": "create", "content": "..."}]
    """
    pattern = re.compile(
        r'<astrea_file\s+path="([^"]+)"\s+action="([^"]+)"\s*>(.*?)</astrea_file>',
        re.DOTALL
    )
    results = []
    for match in pattern.finditer(raw_text):
        path = match.group(1).strip()
        action = match.group(2).strip().lower()
        content = match.group(3).strip()
        results.append({
            "path": path,
            "action": action,
            "content": content,
        })

    # Fallback: 旧格式兼容 (无 action 属性)
    if not results:
        pattern_old = re.compile(
            r'<astrea_file\s+path="([^"]+)"\s*>(.*?)</astrea_file>',
            re.DOTALL
        )
        for match in pattern_old.finditer(raw_text):
            path = match.group(1).strip()
            content = match.group(2).strip()
            results.append({
                "path": path,
                "action": "create",  # 旧格式默认 create
                "content": content,
            })

    # Fallback 2: 内嵌 <file_path> 和 <file_content> 格式
    if not results:
        pattern_inner = re.compile(
            r'<astrea_file>.*?<file_path>([^<]+)</file_path>.*?<file_content>\s*(.*?)\s*</file_content>.*?</astrea_file>',
            re.DOTALL
        )
        for match in pattern_inner.finditer(raw_text):
            path = match.group(1).strip()
            content = match.group(2).strip()
            results.append({
                "path": path,
                "action": "create",
                "content": content,
            })

    # 最终 Fallback: markdown 代码块清洗
    if not results:
        cleaned = _clean_markdown_legacy(raw_text)
        if cleaned:
            results.append({
                "path": "",
                "action": "create",
                "content": cleaned,
            })

    return results


def _clean_markdown_legacy(raw_text: str) -> str:
    """兜底：清洗 Markdown 代码块"""
    code = raw_text.strip()
    if "```" in code:
        lines = code.split('\n')
        cleaned_lines = []
        in_block = False
        for line in lines:
            if line.strip().startswith("```"):
                in_block = not in_block
                continue
            if in_block:
                cleaned_lines.append(line)
        if cleaned_lines:
            code = '\n'.join(cleaned_lines)
            
    # 清理可能残留的 astrea_file 标签
    code = re.sub(r'<astrea_file[^>]*>', '', code)
    code = re.sub(r'</astrea_file>', '', code)
    code = re.sub(r'<file_path>[^<]*</file_path>', '', code)
    code = re.sub(r'<file_content>', '', code)
    code = re.sub(r'</file_content>', '', code)
    
    return code.strip()


# ============================================================
# 3. CodePatcher 主类
# ============================================================

class CodePatcher:
    """
    代码缝合器 — 将 Blackboard 草稿与 VFS 真理代码合并。

    action 路由：
    - create  → 直接返回 draft（新文件）
    - rewrite → 直接返回 draft（小文件全量覆写）
    - modify  → 解析 SEARCH/REPLACE 块，缝合到 vfs_code
    """

    def patch(self, vfs_code: Optional[str], draft: str, action: str) -> str:
        """
        核心缝合方法。

        Args:
            vfs_code: VFS 真理区的原始代码（create 模式可为 None）
            draft: Coder 提交的草稿
            action: "create" | "rewrite" | "modify"

        Returns:
            缝合后的完整代码

        Raises:
            PatchFailedError: 缝合失败时抛出，携带具体原因
        """
        if action == "create":
            logger.info(f"🔧 [Patcher] action=create → 直接使用草稿")
            return draft

        if action == "rewrite":
            logger.info(f"🔧 [Patcher] action=rewrite → 全量覆写")
            return draft

        if action == "modify":
            if not vfs_code:
                raise PatchFailedError(
                    "action=modify 但 VFS 中无原始代码。如果是新文件，请使用 action=\"create\"。"
                )
            return self._apply_modify(vfs_code, draft)

        # 未知 action: 降级为 create
        logger.warning(f"⚠️ [Patcher] 未知 action='{action}'，降级为 create")
        return draft

    def _apply_modify(self, vfs_code: str, draft: str) -> str:
        """
        解析 SEARCH/REPLACE 块并逐个缝合。
        """
        edits = self._parse_search_replace(draft)
        if not edits:
            raise PatchFailedError(
                "action=modify 但草稿中未找到 <<<<<<< SEARCH ... >>>>>>> REPLACE 块。"
                "请使用正确的 SEARCH/REPLACE 格式，或改为 action=\"rewrite\" 全量覆写。",
                search_block=draft[:80]
            )

        content = vfs_code
        applied = 0
        failed_reasons = []
        levels_used = []

        for i, (search, replace) in enumerate(edits):
            success, new_content, level = self._fuzzy_find_and_replace(content, search, replace)
            if success:
                content = new_content
                applied += 1
                levels_used.append(level)
                logger.info(f"🔧 [Patcher] 编辑 #{i+1} 成功 [{level}]")
            else:
                reason = (
                    f"编辑 #{i+1} 三级匹配均失败。"
                    f"SEARCH 块(前50字符): '{search[:50]}...' "
                    f"请检查缩进和空格是否与原文件完全一致。"
                )
                failed_reasons.append(reason)
                logger.warning(f"⚠️ [Patcher] {reason}")

        if failed_reasons:
            raise PatchFailedError(
                f"缝合部分失败: {applied}/{applied + len(failed_reasons)} 成功。\n"
                + "\n".join(failed_reasons),
                search_block=edits[0][0][:80] if edits else ""
            )

        logger.info(f"🔧 [Patcher] 全部 {applied} 处编辑成功 [{', '.join(levels_used)}]")
        return content

    # ============================================================
    # SEARCH/REPLACE 解析
    # ============================================================

    @staticmethod
    def _parse_search_replace(draft: str) -> List[Tuple[str, str]]:
        """
        解析 <<<<<<< SEARCH ... ======= ... >>>>>>> REPLACE 块。

        Returns:
            [(search_text, replace_text), ...]
        """
        pattern = re.compile(
            r'<{7}\s*SEARCH\s*\n(.*?)\n={7}\s*\n(.*?)\n>{7}\s*REPLACE',
            re.DOTALL
        )
        edits = []
        for match in pattern.finditer(draft):
            search = match.group(1)
            replace = match.group(2)
            edits.append((search, replace))

        return edits

    # ============================================================
    # 三级匹配引擎 (迁移自 state_manager.py)
    # ============================================================

    @staticmethod
    def _normalize_whitespace(s: str) -> str:
        """空白归一化：Tab→空格、多空格合一、去行尾空白"""
        lines = s.split('\n')
        normalized = []
        for line in lines:
            line = line.replace('\t', '    ')   # Tab → 4 空格
            line = re.sub(r'[ ]+', ' ', line)   # 多空格合一
            line = line.rstrip()                 # 去行尾空白
            normalized.append(line)
        return '\n'.join(normalized)

    def _fuzzy_find_and_replace(self, content: str, search: str, replace: str) -> Tuple[bool, str, str]:
        """
        三级匹配策略：
        Level 1: 精确匹配
        Level 2: 空白归一化后匹配
        Level 3: difflib 最佳子串模糊匹配 (similarity >= 0.6)

        Returns: (success, new_content, level_used)
        """
        # --- Level 1: 精确匹配 ---
        if search in content:
            return True, content.replace(search, replace, 1), "L1:精确"

        # --- Level 2: 空白归一化匹配 ---
        norm_search = self._normalize_whitespace(search)
        content_lines = content.split('\n')
        search_lines = norm_search.split('\n')
        search_len = len(search_lines)

        for start_idx in range(len(content_lines) - search_len + 1):
            window = content_lines[start_idx:start_idx + search_len]
            norm_window = [re.sub(r'[ ]+', ' ', l.replace('\t', '    ').rstrip()) for l in window]
            if norm_window == search_lines:
                before = '\n'.join(content_lines[:start_idx])
                after = '\n'.join(content_lines[start_idx + search_len:])
                parts = [before, replace, after]
                new_content = '\n'.join(parts)
                return True, new_content, "L2:归一化"

        # --- Level 3: difflib 模糊匹配 ---
        best_ratio = 0.0
        best_start = -1
        best_end = -1

        for window_size in range(max(1, search_len - 2), search_len + 3):
            for start_idx in range(len(content_lines) - window_size + 1):
                window_text = '\n'.join(content_lines[start_idx:start_idx + window_size])
                ratio = SequenceMatcher(None, search, window_text).ratio()
                if ratio > best_ratio:
                    best_ratio = ratio
                    best_start = start_idx
                    best_end = start_idx + window_size

        if best_ratio >= 0.6 and best_start >= 0:
            before = '\n'.join(content_lines[:best_start])
            after = '\n'.join(content_lines[best_end:])
            parts = [before, replace, after]
            new_content = '\n'.join(parts)
            return True, new_content, f"L3:模糊({best_ratio:.0%})"

        return False, content, "全部失败"
