import os
import re
import json
import logging
from typing import Optional, Dict, Any, List
from core.llm_client import default_llm
from core.prompt import Prompts
from core.ws_broadcaster import global_broadcaster
from core.database import recall, get_recent_events, recall_project_experience, infer_domain

logger = logging.getLogger("CoderAgent")

class CoderAgent:
    """
    编码 Agent (Coder) — v1.3 新架构

    职责：接收 Engine 传入的任务目标、Observer 上下文和记忆，输出 XML 格式代码。
    不再直接读写 VFS，所有上下文由 Engine 注入。

    支持两种模式：
      - 首次生成：输出完整代码文件（<astrea_file action="create">）
      - 修复模式：使用 edit_file Function Calling 做差量编辑
        （匹配失败自动 fallback 全量覆写 <astrea_file action="rewrite">）
    """
    def __init__(self, project_id: str = "default_project"):
        self.model = os.getenv("MODEL_CODER", "qwen3-coder-plus")
        _et, _re = default_llm.parse_thinking_config(os.getenv("THINKING_CODER", "false"))
        self.enable_thinking = _et
        self._reasoning_effort = _re
        self.project_id = project_id
        self._last_recalled_ids: List[int] = []  # 最近一次 recall 的记忆 IDs
        self._last_coder_mode: str = "unknown"  # "create" | "editor" | "fallback_rewrite"

    # --- 前端文件后缀集合 ---
    FRONTEND_EXTENSIONS = {'.html', '.htm', '.css', '.js', '.jsx', '.ts', '.tsx', '.vue', '.svelte'}

    def _extract_xml_code(self, raw_text: str, target_file: str) -> str:
        """
        从 LLM 输出中提取 <astrea_file> XML 标签内的代码。
        支持多文件输出（未来扩展），当前取 target_file 匹配的第一个。
        如果 XML 提取失败，fallback 到旧的 markdown 清洗。
        """
        # 主路径：XML 提取
        pattern = re.compile(
            r'<astrea_file\s+path="([^"]+)"[^>]*>(.*?)</astrea_file>',
            re.DOTALL
        )
        matches = pattern.findall(raw_text)
        
        if matches:
            # 优先匹配 target_file
            for path, content in matches:
                if path.strip() == target_file:
                    logger.info(f"📦 XML 提取成功: {path}")
                    return content.strip()
            # 如果没有精确匹配，取第一个
            path, content = matches[0]
            logger.info(f"📦 XML 提取成功 (首个): {path}")
            return content.strip()
        
        # Fallback：旧的 markdown 清洗
        logger.warning("⚠️ XML 标签未找到，fallback 到 markdown 清洗")
        return self._clean_markdown_legacy(raw_text)
    
    def _clean_markdown_legacy(self, raw_text: str) -> str:
        """旧版 markdown 清洗（仅作 fallback）"""
        md_pattern = re.compile(r"```(?:python|py|html|css|javascript|js)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
        match = md_pattern.search(raw_text)
        if match:
            return match.group(1).strip()
        lines = [line for line in raw_text.split("\n") if not line.strip().startswith("```")]
        return "\n".join(lines).strip()
    
    def _get_coder_prompt(self, target_file: str) -> str:
        """根据文件后缀路由到对应的 Coder prompt"""
        ext = os.path.splitext(target_file)[1].lower()
        if ext in self.FRONTEND_EXTENSIONS:
            logger.info(f"🎨 路由到前端工程师 (ext={ext})")
            return Prompts.CODER_FRONTEND_SYSTEM
        else:
            logger.info(f"⚙️ 路由到后端工程师 (ext={ext})")
            return Prompts.CODER_BACKEND_SYSTEM

    def _build_memory_hint(self, target_file: str, description: str, task_meta: dict = None) -> str:
        """
        构建长短期记忆提示（首次生成专用）。

        v1.3：项目文件树和依赖文件上下文由 Engine 通过 task_meta 注入（来自 Observer）。
        本方法只负责 RAG 记忆召回 + TDD 滑动窗口。
        """
        memory_hint = ""
        self._last_recalled_ids = []
        
        # 长期记忆 → 1+1 精简召回（1条技术栈专用 + 1条通用保底，宁缺毋滥）
        tech_stacks = None
        if task_meta:
            spec = task_meta.get("project_spec", "")
            if isinstance(spec, dict):
                tech_stacks = spec.get("tech_stack", None)
        past_tips = recall(
            f"{target_file} {description}", n_results=1,
            project_id=self.project_id, caller="Coder",
            tech_stacks=tech_stacks,
            domain=infer_domain(target_file),
            similarity_threshold=0.80,
        )
        if past_tips:
            # 缓存 recalled IDs（过滤 id > 0 的有效 ID）
            self._last_recalled_ids = [t["id"] for t in past_tips if t.get("id", -1) > 0]
            tips_str = "\n".join([f"  {i+1}. {tip['content']}" for i, tip in enumerate(past_tips)])
            memory_hint = f"\n\n【🌍 全局通用架构智慧 (Global Experience)】\n{tips_str}"
        
        # 短期记忆 → 项目专属经验（精简召回，1条不截断）
        exp_contents = recall_project_experience(
            query=f"{target_file} {description}",
            project_id=self.project_id, limit=1, caller="Coder"
        )
        if exp_contents:
            exp_hints = "\n".join([f"  {i+1}. {c}" for i, c in enumerate(exp_contents)])
            memory_hint += f"\n\n【📦 本项目最高优先级规则 (Project Experience - 必须绝对服从)】\n{exp_hints}"
        
        # 短期记忆 → 滑动窗口：最近 3 轮 TDD 上下文（避免重蹈覆辙）
        tdd_events = get_recent_events(
            project_id=self.project_id, limit=3,
            event_types=["round_pass", "round_fail"], caller="Coder"
        )
        if tdd_events:
            tdd_hints = "\n".join([f"  {e.content[:500]}" for e in tdd_events])
            memory_hint += f"\n\n【🔄 最近 TDD 轮次记录（你的近期尝试，务必避免重复犯错）】\n{tdd_hints}"
        
        # v1.3: 项目文件树由 Observer 提供（通过 task_meta 注入）
        observer_tree = task_meta.get("observer_tree", "") if task_meta else ""
        if observer_tree:
            memory_hint += f"\n\n【📂 当前项目文件结构 (Observer)】\n{observer_tree}"
        
        # Phase 0.3: 全局快照（已 commit 的数据模型 + API 路由）
        global_snapshot = task_meta.get("global_snapshot", "") if task_meta else ""
        if global_snapshot:
            memory_hint += f"\n\n【📊 全局快照 — 已完成文件的数据模型与路由（必须对齐）】\n{global_snapshot}"
        
        return memory_hint

    def _build_fix_hint(self, target_file: str, description: str, observer_context: str = "") -> str:
        """
        构建修复模式专用的精简记忆（不重新召回全局 RAG）。

        修复模式核心信息来自 Reviewer 的 feedback，不需要重复注入首次生成时
        已经看过的全局经验。只保留：
        - 项目专属经验（可能包含本次踩坑相关的规则）
        - 最近 1 条 TDD 事件（最新的报错，避免重蹈覆辙）
        - 依赖文件实际签名（精准注入，保证接口匹配）
        """
        fix_hint = ""
        
        # 项目专属经验（轻量，~200 tokens）
        exp_contents = recall_project_experience(
            query=f"{target_file} {description}",
            project_id=self.project_id, limit=2, caller="Coder-Fix"
        )
        if exp_contents:
            exp_hints = "\n".join([f"  {i+1}. {c[:200]}" for i, c in enumerate(exp_contents)])
            fix_hint += f"\n\n【📦 本项目规则 (必须绝对服从)】\n{exp_hints}"
        
        # 最近 1 条 TDD 事件（最新报错）
        tdd_events = get_recent_events(
            project_id=self.project_id, limit=1,
            event_types=["round_fail"], caller="Coder-Fix"
        )
        if tdd_events:
            fix_hint += f"\n\n【🔄 最近一次失败记录】\n  {tdd_events[0].content[:500]}"
        
        # 依赖文件实际签名（精准注入）
        if observer_context:
            fix_hint += f"\n\n【📐 依赖文件实际签名（import/调用必须与此一致）】\n{observer_context}"
        
        return fix_hint

    def _build_fix_hint_with_snapshot(self, target_file: str, description: str,
                                      observer_context: str = "", task_meta: dict = None) -> str:
        """
        增强版 fix_hint：在基础 fix_hint 之上注入全局快照。
        修复模式中跨文件字段不一致（L0.6/L0.13 场景）时，Coder 需要看到已提交文件的 schema/routes。
        """
        fix_hint = self._build_fix_hint(target_file, description, observer_context)
        
        # 全局快照注入（跨文件修复的关键信息）
        global_snapshot = task_meta.get("global_snapshot", "") if task_meta else ""
        if global_snapshot:
            fix_hint += f"\n\n【📊 全局快照 — 已完成文件的数据模型与路由（字段名必须与此对齐）】\n{global_snapshot}"
        
        return fix_hint

    def _generate_full(self, target_file: str, description: str, memory_hint: str,
                       observer_context: str = "", task_meta: dict = None) -> str:
        """
        首次生成：输出完整代码文件。

        v1.3：依赖文件上下文由 Engine 通过 observer_context 注入（Observer 的骨架提取结果）。
        """
        # 注入项目规划书
        project_spec = task_meta.get("project_spec", "无规划书") if task_meta else "无规划书"

        # v1.3: 依赖文件上下文来自 Observer（骨架 + 关键片段）
        vfs_str = observer_context if observer_context else "当前无依赖文件，你是写的第一个文件。"

        # Playbook: 技术栈编码规范（由 Engine 按文件类型动态加载）
        playbook = task_meta.get("playbook", "") if task_meta else ""
        # P0.5: 用户项目潜规则（空字符串 = 零噪音）
        user_rules_block = task_meta.get("user_rules_block", "") if task_meta else ""

        # Phase 0: Fill 模式 — 使用骨架填充 prompt
        if task_meta and task_meta.get("is_fill_mode") and task_meta.get("skeleton_code"):
            logger.info(f"🔧 Fill 模式: 使用 CODER_FILL_SYSTEM")
            system_content = Prompts.CODER_FILL_SYSTEM.format(
                skeleton_code=task_meta["skeleton_code"],
                project_spec=project_spec,
                vfs_context=vfs_str,
                coder_playbook=playbook,
                user_rules_block=user_rules_block,
            )
            user_prompt = "请将骨架中的所有 `...` 占位替换为完整的业务实现。输出完整文件代码。"
        else:
            system_content = self._get_coder_prompt(target_file).format(
                target_file=target_file,
                description=description,
                memory_hint=memory_hint,
                project_spec=project_spec,
                vfs_context=vfs_str,
                playbook=playbook,
                user_rules_block=user_rules_block,
            )
            user_prompt = "请开始编写该文件的代码。只输出这一个文件的代码内容。"

        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_prompt}
        ]

        response_msg = default_llm.chat_completion(
            messages=messages,
            model=self.model,
            temperature=0.2,
            enable_thinking=self.enable_thinking,
            reasoning_effort=self._reasoning_effort,
        )

        # v1.3: 返回原始 LLM 输出，由 Engine 负责 XML 提取和 VFS 写入
        raw_output = response_msg.content
        logger.info(f"✅ Coder 全量生成完成 ({len(raw_output)} bytes)")
        return raw_output

    @staticmethod
    def _add_line_numbers(code: str) -> str:
        """给 Editor 视图添加 1-indexed 行号，便于 LLM 使用行号定位。"""
        lines = code.split("\n")
        width = len(str(max(1, len(lines))))
        return "\n".join(
            f"{line_no:>{width}} | {line}"
            for line_no, line in enumerate(lines, start=1)
        )

    @staticmethod
    def _allows_controlled_full_file_edit(target_file: str, text: str) -> bool:
        """判断当前 modify/weld 是否属于可控的大范围前端结构替换。"""
        ext = os.path.splitext(target_file)[1].lower()
        if ext not in {'.html', '.htm', '.vue', '.css', '.scss', '.less'}:
            return False

        lowered = str(text or "").lower()
        markers = (
            "重写", "重构", "重新设计", "整体", "布局", "结构", "html结构",
            "dom", "容器", "区域", "rewrite", "rebuild", "restructure", "layout",
        )
        return any(marker in lowered for marker in markers)

    def _fix_with_editor(self, target_file: str, description: str, feedback: str,
                         existing_code: str, memory_hint: str, task_meta: dict = None) -> str:
        """
        修复模式：使用 edit_file Function Calling 做差量编辑。
        如果 LLM 不使用工具或 edits 匹配失败，自动 fallback 到全量覆写。

        v1.3：existing_code 由 Engine 传入（来自 VfsUtils 真理区或 Blackboard 草稿），
        不再从 StateManager VFS 读取。
        """
        controlled_full_edit = bool(
            task_meta
            and task_meta.get("force_modify")
            and self._allows_controlled_full_file_edit(target_file, f"{description}\n{feedback}")
        )
        full_edit_hint = ""
        if controlled_full_edit:
            full_edit_hint = (
                "\n\n【受控整文件修改许可】\n"
                "当前任务明确要求重写前端结构。允许使用一次 `edit_file` 行号编辑替换整个文件，"
                "但必须仍然调用工具：`start_line=1`，`end_line=当前文件最后一行号`，"
                "`replace=完整新文件内容`。禁止直接输出完整文件文本。\n"
            )

        system_content = Prompts.CODER_FIX_SYSTEM.format(
            target_file=target_file,
            current_code=self._add_line_numbers(existing_code),
            feedback=feedback
        ) + full_edit_hint + memory_hint

        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": "请使用 edit_file 工具精准修复上述 bug。"}
        ]

        logger.info(f"🔧 Coder [Editor 模式] 正在差量修复: {target_file}")
        global_broadcaster.emit_sync("Coder", "edit_start", f"正在差量修复 {target_file}", {"target": target_file})

        response_msg = default_llm.chat_completion(
            messages=messages,
            model=self.model,
            temperature=0.1,
            tools=Prompts.CODER_EDIT_TOOL_SCHEMA,
            enable_thinking=self.enable_thinking,
            reasoning_effort=self._reasoning_effort,
        )

        # 检查 LLM 是否使用了 edit_file 工具
        if response_msg.tool_calls:
            for tool_call in response_msg.tool_calls:
                if tool_call.function.name == "edit_file":
                    try:
                        args = json.loads(tool_call.function.arguments)
                        edits = args.get("edits", [])
                        
                        # 防御：LLM 有时返回双重序列化的 edits（字符串而非列表）
                        if isinstance(edits, str):
                            try:
                                parsed = json.loads(edits)
                                if isinstance(parsed, list):
                                    logger.info(f"🔧 [Editor] edits 双重序列化，已修复解析")
                                    edits = parsed
                                else:
                                    logger.warning(f"⚠️ [Editor] edits 解析后非列表: {type(parsed).__name__}")
                                    edits = []
                            except (json.JSONDecodeError, TypeError):
                                logger.warning(f"⚠️ [Editor] edits 是不可解析的字符串，跳过: {edits[:100]}")
                                edits = []
                        
                        if edits:
                            # v1.3: 在内存中应用编辑，不写 VFS
                            patched_code = self._apply_edits_in_memory(existing_code, edits)
                            if patched_code is not None:
                                logger.info(f"🔧 [Editor] 差量编辑成功")
                                self._last_coder_mode = "editor"
                                global_broadcaster.emit_sync("Coder", "edit_done", f"差量修复完成", {"code": patched_code})
                                return patched_code
                            else:
                                logger.warning(f"⚠️ [Editor] 差量编辑匹配失败，尝试 fallback")
                    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
                        logger.warning(f"⚠️ [Editor] 工具参数解析失败: {e}，fallback 全量覆写")

        # Fallback: 基于现有代码的保守覆写
        logger.warning(f"⚠️ [Editor] Fallback 保守覆写模式")
        self._last_coder_mode = "controlled_full_rewrite" if controlled_full_edit else "fallback_rewrite"
        return self._fallback_full_rewrite(
            target_file, description, feedback,
            existing_code=existing_code,
            observer_context=task_meta.get("observer_context", "") if task_meta else "",
            memory_hint=memory_hint, task_meta=task_meta
        )

    def _apply_edits_in_memory(self, code: str, edits: list) -> Optional[str]:
        """
        在内存中应用差量编辑（不触碰任何文件系统）。
        返回编辑后的代码，如果零有效编辑返回 None → 触发 fallback。

        行号定位优先；文本匹配兜底。
        文本匹配策略（从严到松）：
        L1: rstrip 精确匹配（原有逻辑）
        L2: strip 归一化匹配（容忍 leading whitespace 差异：tab vs space）
        L3: 压缩空白匹配（将所有连续空白压缩为单个空格后比较）
        """
        lines = code.split("\n")
        success_count = 0
        fail_count = 0
        normalized_edits = []

        for edit in edits:
            # 防御：LLM 有时返回字符串而非字典
            if isinstance(edit, str):
                try:
                    edit = json.loads(edit)
                except (json.JSONDecodeError, TypeError):
                    logger.warning(f"⚠️ [Editor] 跳过非法 edit (str): {edit[:80]}")
                    fail_count += 1
                    continue
            if not isinstance(edit, dict):
                logger.warning(f"⚠️ [Editor] 跳过非法 edit (type={type(edit).__name__})")
                fail_count += 1
                continue
            normalized_edits.append(edit)

        def _strip_visual_line_numbers(text: str) -> str:
            if not text:
                return text
            raw_lines = text.split("\n")
            nonblank = [line for line in raw_lines if line.strip()]
            if not nonblank:
                return text
            if not all(re.match(r"^\s*\d+\s+\|\s?", line) for line in nonblank):
                return text
            return "\n".join(
                re.sub(r"^\s*\d+\s+\|\s?", "", line, count=1)
                for line in raw_lines
            )

        def _replacement_lines(replace_text: str) -> list:
            replace_text = _strip_visual_line_numbers(replace_text)
            if replace_text == "":
                return []
            return replace_text.split("\n")

        def _coerce_line(value: Any) -> Optional[int]:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        def _collapse(text: str) -> str:
            return re.sub(r"\s+", " ", text.strip())

        def _find_text_match(search_lines: list, start_index: int = 0):
            if len(search_lines) > len(lines):
                return None

            max_start = len(lines) - len(search_lines) + 1

            # L1: rstrip 精确匹配
            for idx in range(start_index, max_start):
                if all(
                    lines[idx + offset].rstrip() == search_line.rstrip()
                    for offset, search_line in enumerate(search_lines)
                ):
                    return idx, "L1-exact"

            # L2: strip 归一化匹配
            for idx in range(start_index, max_start):
                if all(
                    lines[idx + offset].strip() == search_line.strip()
                    for offset, search_line in enumerate(search_lines)
                ):
                    return idx, "L2-strip"

            # L3: 压缩空白匹配
            for idx in range(start_index, max_start):
                if all(
                    _collapse(lines[idx + offset]) == _collapse(search_line)
                    for offset, search_line in enumerate(search_lines)
                ):
                    return idx, "L3-fuzzy"

            return None

        line_edits = []
        text_edits = []
        for edit in normalized_edits:
            has_start = edit.get("start_line") is not None
            has_end = edit.get("end_line") is not None
            if has_start or has_end:
                start_line = _coerce_line(edit.get("start_line"))
                end_line = _coerce_line(edit.get("end_line"))
                if start_line is None or end_line is None:
                    logger.warning(f"⚠️ [Editor] 行号 edit 缺少有效 start_line/end_line: {edit}")
                    fail_count += 1
                    continue
                line_edits.append((start_line, end_line, edit.get("replace", "")))
            else:
                text_edits.append(edit)

        # 行号 edit 必须倒序应用，防止前一次替换改变后续行号。
        for start_line, end_line, replace in sorted(line_edits, key=lambda item: item[0], reverse=True):
            if start_line < 1 or end_line < start_line or end_line > len(lines):
                logger.warning(
                    f"⚠️ [Editor] 行号范围非法: L{start_line}-{end_line}, "
                    f"文件总行数 {len(lines)}"
                )
                fail_count += 1
                continue
            replace_lines = _replacement_lines(replace)
            lines[start_line - 1:end_line] = replace_lines
            success_count += 1
            logger.debug(f"🔧 [Editor] 行号 edit 成功: L{start_line}-{end_line}")

        for edit in text_edits:
            search = _strip_visual_line_numbers(edit.get("search", ""))
            replace = edit.get("replace", "")
            replace_all = bool(edit.get("replace_all", False))
            if not search:
                logger.warning("⚠️ [Editor] 跳过无 search/行号的 edit")
                fail_count += 1
                continue

            search_lines = search.split("\n")
            # 跳过空行首尾（LLM 经常多输出一个空行）
            while search_lines and not search_lines[0].strip():
                search_lines.pop(0)
            while search_lines and not search_lines[-1].strip():
                search_lines.pop()
            if not search_lines:
                fail_count += 1
                continue

            replace_lines = _replacement_lines(replace)
            cursor = 0
            applied_this_edit = 0
            while True:
                match = _find_text_match(search_lines, cursor)
                if not match:
                    break

                index, matched_level = match
                lines[index:index + len(search_lines)] = replace_lines
                success_count += 1
                applied_this_edit += 1

                if matched_level == "L2-strip":
                    logger.info("🔧 [Editor] L2 strip 匹配命中 (可能存在缩进差异)")
                elif matched_level == "L3-fuzzy":
                    logger.info("🔧 [Editor] L3 模糊匹配命中 (空白差异较大)")
                logger.debug(f"🔧 [Editor] edit 匹配成功 [{matched_level}]")

                if not replace_all:
                    break

                cursor = index + len(replace_lines)
                if not replace_lines:
                    cursor = index
                if cursor > len(lines):
                    break
                if applied_this_edit >= 1000:
                    logger.warning("⚠️ [Editor] replace_all 超过 1000 次，强制停止")
                    break

            if applied_this_edit == 0:
                fail_count += 1
                # 诊断日志：帮助定位匹配失败原因
                first_search = search_lines[0].strip() if search_lines else ""
                similar_lines = [
                    (idx, l) for idx, l in enumerate(lines)
                    if first_search and first_search in l
                ]
                if similar_lines:
                    idx, sim = similar_lines[0]
                    logger.warning(
                        f"⚠️ [Editor] search 首行 '{first_search[:60]}' "
                        f"在 L{idx} 有近似但不精确匹配: '{sim.strip()[:60]}'"
                    )
                else:
                    logger.warning(
                        f"⚠️ [Editor] search 首行 '{first_search[:60]}' "
                        f"在代码中完全找不到近似行"
                    )

        result = "\n".join(lines)
        logger.info(f"🔧 [Editor] 内存编辑: {success_count} 成功, {fail_count} 失败")

        if success_count == 0:
            return None  # 零有效编辑 = 无实质修改，触发 fallback

        return result

    # ============================================================
    # Coder A-4: 函数级切片精准修复
    # ============================================================

    @staticmethod
    def _splice_code(full_code: str, new_slice: str,
                     start_line: int, end_line: int) -> str:
        """
        精准行号替换：将原文件的 [start_line, end_line] 区间替换为 new_slice。
        start_line / end_line 均为 1-indexed（与 AST 显微镜输出一致）。
        """
        lines = full_code.split("\n")
        new_lines = new_slice.split("\n")
        # start_line-1 是 0-indexed 起始, end_line 是 0-indexed 的下一行
        result_lines = lines[:start_line - 1] + new_lines + lines[end_line:]
        return "\n".join(result_lines)

    def _fix_with_slice(self, target_file: str, ast_slice: dict,
                        feedback: str, full_code: str,
                        fix_hint: str = "", task_meta: dict = None) -> str:
        """
        函数级切片精准修复 (Coder A-4)。

        核心逻辑：
        1. 只把出 Bug 的函数体 (~15-50 行) 发给 LLM，而非整个文件
        2. 要求 LLM 只输出修复后的完整函数代码
        3. 用行号精准回填，其余代码纹丝不动

        如果切片修复失败，自动降级到 Editor 差量编辑。
        """
        func_name = ast_slice.get("name", "unknown")
        func_code = ast_slice.get("code", "")
        start_line = ast_slice.get("start_line", 1)
        end_line = ast_slice.get("end_line", 1)
        ctx_before = ast_slice.get("context_before", "")
        ctx_after = ast_slice.get("context_after", "")

        logger.info(
            f"🔬 [Slice] 函数级切片修复: {target_file} → {func_name} "
            f"L{start_line}-{end_line} ({len(func_code)} chars)"
        )
        global_broadcaster.emit_sync(
            "Coder", "slice_fix",
            f"切片修复 {target_file}:{func_name}(L{start_line}-{end_line})",
            {"target": target_file, "function": func_name}
        )

        # 构建精简 Prompt — 只注入函数切片和上下文
        system_content = (
            f"你是一位资深代码修复专家。\n\n"
            f"文件 `{target_file}` 中的 `{func_name}` (第{start_line}-{end_line}行) 存在 Bug。\n"
            f"以下是该函数前后的上下文和函数体。\n\n"
        )
        if ctx_before:
            system_content += f"【上方上下文（只读参考，不要修改）】\n```\n{ctx_before}\n```\n\n"

        system_content += f"【需要修复的函数/代码段】\n```\n{func_code}\n```\n\n"

        if ctx_after:
            system_content += f"【下方上下文（只读参考，不要修改）】\n```\n{ctx_after}\n```\n\n"

        system_content += (
            f"【Reviewer/沙盒的诊断报告】\n{feedback}\n\n"
            f"{fix_hint}\n\n"
            f"【输出要求】\n"
            f"1. 只输出修复后的 `{func_name}` 的完整代码（从定义/声明开始到结束）\n"
            f"2. 不要输出文件中其他部分的代码\n"
            f"3. 不要添加任何不相关的新函数\n"
            f"4. 用 <astrea_file path=\"{target_file}\" action=\"slice\"> 标签包裹\n"
        )

        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": "请修复上述函数的 Bug，只输出修复后的完整函数代码。"}
        ]

        response_msg = default_llm.chat_completion(
            messages=messages,
            model=self.model,
            temperature=0.1,
            enable_thinking=self.enable_thinking,
            reasoning_effort=self._reasoning_effort,
        )

        raw_output = response_msg.content
        # 提取切片代码
        sliced_code = self._extract_xml_code(raw_output, target_file)

        if not sliced_code or len(sliced_code.strip()) < 5:
            logger.warning("⚠️ [Slice] LLM 输出为空或过短，降级到 Editor 差量编辑")
            return self._fix_with_editor(
                target_file, ast_slice.get("name", ""), feedback,
                existing_code=full_code,
                memory_hint=fix_hint, task_meta=task_meta
            )

        # 精准行号回填
        try:
            patched = self._splice_code(full_code, sliced_code, start_line, end_line)
            logger.info(
                f"✅ [Slice] 切片回填成功: {func_name} "
                f"L{start_line}-{end_line} → {len(sliced_code.splitlines())} 行"
            )
            self._last_coder_mode = "slice"
            global_broadcaster.emit_sync(
                "Coder", "slice_done",
                f"切片修复完成: {func_name}",
                {"code": patched}
            )
            return patched
        except Exception as e:
            logger.warning(f"⚠️ [Slice] 回填异常: {e}，降级到 Editor")
            return self._fix_with_editor(
                target_file, ast_slice.get("name", ""), feedback,
                existing_code=full_code,
                memory_hint=fix_hint, task_meta=task_meta
            )

    def _fallback_full_rewrite(self, target_file: str, description: str, feedback: str,
                               existing_code: str = "", observer_context: str = "",
                               memory_hint: str = "", task_meta: dict = None) -> str:
        """降级方案：基于现有代码的保守覆写（注入原始代码防止功能丢失）"""
        project_spec = task_meta.get("project_spec", "无规划书") if task_meta else "无规划书"
        vfs_str = observer_context if observer_context else "当前无依赖文件。"
        playbook = task_meta.get("playbook", "") if task_meta else ""
        user_rules_block = task_meta.get("user_rules_block", "") if task_meta else ""

        system_content = self._get_coder_prompt(target_file).format(
            target_file=target_file,
            description=description,
            memory_hint=memory_hint,
            project_spec=project_spec,
            vfs_context=vfs_str,
            playbook=playbook,
            user_rules_block=user_rules_block,
        )

        if task_meta and task_meta.get("is_fill_mode"):
            skeleton_code = task_meta.get("skeleton_code") or existing_code
            user_prompt = (
                f"【骨架锁定全文件填充】以下是 {target_file} 的完整骨架/当前代码。\n"
                "你必须输出修改后的完整文件代码，并严格遵守：\n"
                "1. 必须补完所有函数体中的 `...`，最终代码中禁止保留 `...` 占位。\n"
                "2. 禁止删除、重命名任何既有函数、类、导入、路由装饰器或 URL 路径。\n"
                "3. 禁止新增无关 handler；如果需要注册路由，只能围绕已有函数补齐装饰器。\n"
                "4. 除补全函数体和必要导入外，不要改动骨架结构。\n\n"
                f"【Reviewer 反馈】\n{feedback}\n\n"
                f"【骨架/当前完整代码】\n```\n{skeleton_code}\n```\n\n"
                "请输出完整文件代码。"
            )
        elif existing_code:
            # Patch Mode 保守覆写：注入原始代码，要求只改必要部分
            user_prompt = (
                f"【⚠️ 保守修改要求】以下是 {target_file} 的完整现有代码，"
                f"请你 **只修改下面描述中要求的部分**，其余代码必须原封不动地保留！\n\n"
                f"【需要修改的内容】:\n{feedback}\n\n"
                f"【现有完整代码（必须在此基础上修改，不允许重写）】:\n```\n{existing_code}\n```\n\n"
                f"请输出修改后的完整文件代码。"
            )
        else:
            user_prompt = (
                f"【🚨 紧急修复要求】你之前生成的代码被 Reviewer 测试出错了！\n"
                f"以下是沙盒运行报错或审查人的建议：\n\n{feedback}\n\n"
                f"请修复上述 bug，并重新输出该文件的完整纯净代码！不能偷懒只输出片段！"
            )

        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_prompt}
        ]

        response_msg = default_llm.chat_completion(
            messages=messages,
            model=self.model,
            temperature=0.2,
            enable_thinking=self.enable_thinking,
            reasoning_effort=self._reasoning_effort,
        )
        
        raw_output = response_msg.content
        logger.info(f"✅ Coder Fallback 全量重写完成 ({len(raw_output)} bytes)")
        return raw_output

    def _resolve_dependency_files(self, task_meta: dict) -> set:
        """
        从 task_meta 中解析出当前 task 的依赖文件集合。
        通过 dependencies (task_id 列表) + all_tasks 反查 target_file。
        """
        if not task_meta:
            return set()
        
        deps = task_meta.get("dependencies", [])
        all_tasks = task_meta.get("all_tasks", [])
        
        if not deps or not all_tasks:
            return set()
        
        # 构建 task_id → target_file 映射
        id_to_file = {t.get("task_id", ""): t.get("target_file", "") for t in all_tasks}
        
        dep_files = set()
        for dep_id in deps:
            if dep_id in id_to_file:
                dep_files.add(id_to_file[dep_id])
        
        return dep_files

    def generate_code(self, target_file: str, description: str,
                      feedback: Optional[str] = None, task_meta: dict = None) -> str:
        """
        生成或修复代码（统一入口）— v1.3 新架构

        所有上下文由 Engine 通过 task_meta 注入：
        - task_meta["observer_context"]: Observer 提取的依赖文件骨架
        - task_meta["observer_tree"]: Observer 提取的项目文件树
        - task_meta["existing_code"]: 真理区/Blackboard 中的当前代码
        - task_meta["project_spec"]: 规划书文本

        判定模式：
        1. feedback≠None → Reviewer 退回修复（Editor Tools 差量编辑）
        2. existing_code 不为空 → 跨任务修改已有文件（Editor Tools 差量编辑）
        3. 文件不存在 → 首次生成（全量输出）
        """
        existing_code = (task_meta or {}).get("existing_code", "")
        force_modify = bool((task_meta or {}).get("force_modify"))
        observer_context = (task_meta or {}).get("observer_context", "")
        
        if feedback:
            mode = "Reviewer退回修复"
            edit_instruction = feedback
        elif (task_meta or {}).get("is_fill_mode"):
            # 骨架填充模式：骨架已在真理区，但要走 Fill prompt 而非 Editor
            mode = "首次生成"
            edit_instruction = None
        elif force_modify:
            mode = "强制局部编辑(weld/modify)"
            edit_instruction = (
                f"【任务要求】\n{description}\n\n"
                "【物理强制约束】该任务被标记为 weld (焊接) 或 modify。你必须走局部修复流程（edit_file 工具或保留原有未改动代码）。"
                "请严格只修改目标部分，绝对禁止摧毁原有文件的其他上下文！"
            )
        elif existing_code:
            mode = "跨任务修改已有文件"
            edit_instruction = f"【任务要求】\n{description}"
        else:
            mode = "首次生成"
            edit_instruction = None
            self._last_coder_mode = "create"
        
        logger.info(f"💻 Coder 正在编码... 目标文件: {target_file}, 模式: {mode}")
        global_broadcaster.emit_sync("Coder", "coding_start", f"[{mode}] 正在为 {target_file} 编写代码", {"target": target_file})
        
        if edit_instruction:
            ext = os.path.splitext(target_file)[1].lower()
            retry_count = (task_meta or {}).get("retry_count", 0)
            fix_hint = self._build_fix_hint_with_snapshot(target_file, description, observer_context, task_meta)

            # Fill 模式重试：feedback 包含 L0.0（多函数残留 ...）或 L0.C1（路由未注册），
            # AST 切片只给一个函数视窗会导致其他函数永远填不满，
            # 必须走全量重写让 Coder 看到整个文件
            is_fill = (task_meta or {}).get("is_fill_mode", False)
            if retry_count >= 3 and not force_modify:
                logger.info(f"🔄 [重试{retry_count}] 跳过 Editor，直接全量重写: {target_file}")
                self._last_coder_mode = "fallback_rewrite"
                result = self._fallback_full_rewrite(
                    target_file, description, edit_instruction,
                    existing_code=existing_code,
                    observer_context=observer_context,
                    memory_hint=fix_hint, task_meta=task_meta
                )
            elif is_fill:
                # Fill 模式重试 → 强制全量重写（不走 AST 切片 / Editor）
                logger.info(f"🔧 [Fill 重试] 骨架填充修复，全量重写: {target_file}")
                self._last_coder_mode = "fill_retry_rewrite"
                result = self._fallback_full_rewrite(
                    target_file, description, edit_instruction,
                    existing_code=existing_code,
                    observer_context=observer_context,
                    memory_hint=fix_hint, task_meta=task_meta
                )
            elif mode == "跨任务修改已有文件" and ext in self.FRONTEND_EXTENSIONS:
                # Patch Mode 首次触碰前端文件 → 直接走保守覆写
                # 前端样式修改（如 dark: 前缀）散布在文件各处，
                # search/replace 极易只改第一处就停、或插入重复代码破坏 HTML 结构
                logger.info(f"🎨 [Patch Mode] 前端文件保守覆写: {target_file}")
                self._last_coder_mode = "conservative_rewrite"
                result = self._fallback_full_rewrite(
                    target_file, description, edit_instruction,
                    existing_code=existing_code,
                    observer_context=observer_context,
                    memory_hint=fix_hint, task_meta=task_meta
                )
            elif (
                task_meta and task_meta.get("ast_slice")
                and not (force_modify and ext in {'.vue', '.html', '.htm', '.css', '.scss', '.less'})
            ):
                # Coder A-4: AST 切片就位 → 函数级精准修复（最优路径）
                result = self._fix_with_slice(
                    target_file,
                    ast_slice=task_meta["ast_slice"],
                    feedback=edit_instruction,
                    full_code=existing_code,
                    fix_hint=fix_hint,
                    task_meta=task_meta,
                )
            else:
                # 通用路径：无 AST 切片 → 默认走 Editor 差量编辑
                logger.info(f"✏️ [Editor] 差量编辑: {target_file}")
                result = self._fix_with_editor(
                    target_file, description, edit_instruction,
                    existing_code=existing_code,
                    memory_hint=fix_hint, task_meta=task_meta
                )
        else:
            # 首次生成
            is_fill = (task_meta or {}).get("is_fill_mode", False)
            if is_fill:
                # Fill 模式：精简上下文（骨架 + 依赖签名 + Playbook，跳过完整 RAG）
                logger.info(f"🔧 Fill 模式: 精简上下文（跳过全局 RAG）")
                memory_hint = ""
                # 注入依赖签名和项目文件树
                observer_tree = (task_meta or {}).get("observer_tree", "")
                if observer_tree:
                    memory_hint += f"\n\n【📂 项目文件结构】\n{observer_tree}"
                # 注入 global_snapshot（to_dict 字段信息 — 模板生成的关键依赖）
                global_snapshot = (task_meta or {}).get("global_snapshot", "")
                if global_snapshot:
                    memory_hint += f"\n\n【📊 全局快照 — 已完成文件的数据模型与路由（必须对齐）】\n{global_snapshot}"
            else:
                # 完整记忆（含全局 RAG 3+1 + 项目经验 + TDD 窗口）
                memory_hint = self._build_memory_hint(target_file, description, task_meta)
            result = self._generate_full(
                target_file, description,
                memory_hint=memory_hint,
                observer_context=observer_context,
                task_meta=task_meta
            )
        
        global_broadcaster.emit_sync("Coder", "coding_done", f"{target_file} 编写完毕", {"code": result})
        return result
