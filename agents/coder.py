import os
import re
import json
import logging
from typing import Optional, Dict, Any, List
from core.llm_client import default_llm
from core.prompt import Prompts
from core.ws_broadcaster import global_broadcaster
from core.database import recall, get_recent_events, recall_project_experience

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
        self.project_id = project_id
        self._last_recalled_ids: List[int] = []  # 最近一次 recall 的记忆 IDs

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
        
        # 长期记忆 → 3+1 双管线召回（3条技术栈专用 + 1条通用保底）
        tech_stacks = None
        if task_meta:
            spec = task_meta.get("project_spec", "")
            if isinstance(spec, dict):
                tech_stacks = spec.get("tech_stack", None)
        past_tips = recall(
            f"{target_file} {description}", n_results=3,
            project_id=self.project_id, caller="Coder",
            tech_stacks=tech_stacks
        )
        if past_tips:
            # 缓存 recalled IDs（过滤 id > 0 的有效 ID）
            self._last_recalled_ids = [t["id"] for t in past_tips if t.get("id", -1) > 0]
            tips_str = "\n".join([f"  {i+1}. {tip['content']}" for i, tip in enumerate(past_tips)])
            memory_hint = f"\n\n【🌍 全局通用架构智慧 (Global Experience)】\n{tips_str}"
        
        # 短期记忆 → 项目专属经验（轻量 RAG 语义召回）
        exp_contents = recall_project_experience(
            query=f"{target_file} {description}",
            project_id=self.project_id, limit=3, caller="Coder"
        )
        if exp_contents:
            exp_hints = "\n".join([f"  {i+1}. {c[:200]}" for i, c in enumerate(exp_contents)])
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

        system_content = self._get_coder_prompt(target_file).format(
            target_file=target_file,
            description=description,
            memory_hint=memory_hint,
            project_spec=project_spec,
            vfs_context=vfs_str
        )
        
        user_prompt = "请开始编写该文件的代码。只输出这一个文件的代码内容。"

        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_prompt}
        ]

        response_msg = default_llm.chat_completion(
            messages=messages,
            model=self.model,
            temperature=0.2
        )

        # v1.3: 返回原始 LLM 输出，由 Engine 负责 XML 提取和 VFS 写入
        raw_output = response_msg.content
        logger.info(f"✅ Coder 全量生成完成 ({len(raw_output)} bytes)")
        return raw_output

    def _fix_with_editor(self, target_file: str, description: str, feedback: str,
                         existing_code: str, memory_hint: str, task_meta: dict = None) -> str:
        """
        修复模式：使用 edit_file Function Calling 做差量编辑。
        如果 LLM 不使用工具或 edits 匹配失败，自动 fallback 到全量覆写。

        v1.3：existing_code 由 Engine 传入（来自 VfsUtils 真理区或 Blackboard 草稿），
        不再从 StateManager VFS 读取。
        """
        system_content = Prompts.CODER_FIX_SYSTEM.format(
            target_file=target_file,
            current_code=existing_code,
            feedback=feedback
        ) + memory_hint

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
            tools=Prompts.CODER_EDIT_TOOL_SCHEMA
        )

        # 检查 LLM 是否使用了 edit_file 工具
        if response_msg.tool_calls:
            for tool_call in response_msg.tool_calls:
                if tool_call.function.name == "edit_file":
                    try:
                        args = json.loads(tool_call.function.arguments)
                        edits = args.get("edits", [])
                        
                        if edits:
                            # v1.3: 在内存中应用编辑，不写 VFS
                            patched_code = self._apply_edits_in_memory(existing_code, edits)
                            if patched_code is not None:
                                logger.info(f"🔧 [Editor] 差量编辑成功")
                                global_broadcaster.emit_sync("Coder", "edit_done", f"差量修复完成", {"code": patched_code})
                                return patched_code
                            else:
                                logger.warning(f"⚠️ [Editor] 差量编辑匹配失败，尝试 fallback")
                    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
                        logger.warning(f"⚠️ [Editor] 工具参数解析失败: {e}，fallback 全量覆写")

        # Fallback: 全量覆写
        logger.warning(f"⚠️ [Editor] Fallback 全量覆写模式")
        return self._fallback_full_rewrite(
            target_file, description, feedback,
            observer_context=task_meta.get("observer_context", "") if task_meta else "",
            memory_hint=memory_hint, task_meta=task_meta
        )

    def _apply_edits_in_memory(self, code: str, edits: list) -> Optional[str]:
        """
        在内存中应用差量编辑（不触碰任何文件系统）。
        返回编辑后的代码，如果任何一个 edit 匹配失败返回 None。
        """
        lines = code.split("\n")
        success_count = 0
        fail_count = 0

        for edit in edits:
            search = edit.get("search", "")
            replace = edit.get("replace", "")
            if not search:
                continue
            
            # 在代码中查找 search 块
            search_lines = search.split("\n")
            found = False
            
            for i in range(len(lines) - len(search_lines) + 1):
                # 精确匹配
                match = True
                for j, sl in enumerate(search_lines):
                    if lines[i + j].rstrip() != sl.rstrip():
                        match = False
                        break
                if match:
                    replace_lines = replace.split("\n")
                    lines[i:i + len(search_lines)] = replace_lines
                    found = True
                    success_count += 1
                    break
            
            if not found:
                fail_count += 1

        if fail_count > 0 and success_count == 0:
            return None  # 全部失败，触发 fallback
        
        result = "\n".join(lines)
        logger.info(f"🔧 [Editor] 内存编辑: {success_count} 成功, {fail_count} 失败")
        return result

    def _fallback_full_rewrite(self, target_file: str, description: str, feedback: str,
                               observer_context: str = "", memory_hint: str = "",
                               task_meta: dict = None) -> str:
        """降级方案：全量重写"""
        # 注入项目规划书
        project_spec = task_meta.get("project_spec", "无规划书") if task_meta else "无规划书"
        vfs_str = observer_context if observer_context else "当前无依赖文件。"

        system_content = self._get_coder_prompt(target_file).format(
            target_file=target_file,
            description=description,
            memory_hint=memory_hint,
            project_spec=project_spec,
            vfs_context=vfs_str
        )

        user_prompt = f"【🚨 紧急修复要求】你之前生成的代码被 Reviewer 测试出错了！\n以下是沙盒运行报错或审查人的建议：\n\n{feedback}\n\n请修复上述 bug，并重新输出该文件的完整纯净代码！不能偷懒只输出片段！"

        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_prompt}
        ]

        response_msg = default_llm.chat_completion(
            messages=messages,
            model=self.model,
            temperature=0.2
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
        observer_context = (task_meta or {}).get("observer_context", "")
        
        if feedback:
            mode = "Reviewer退回修复"
            edit_instruction = feedback
        elif existing_code:
            mode = "跨任务修改已有文件"
            edit_instruction = f"【任务要求】\n{description}"
        else:
            mode = "首次生成"
            edit_instruction = None
        
        logger.info(f"💻 Coder 正在编码... 目标文件: {target_file}, 模式: {mode}")
        global_broadcaster.emit_sync("Coder", "coding_start", f"[{mode}] 正在为 {target_file} 编写代码", {"target": target_file})
        
        if edit_instruction:
            # 修复模式：精简记忆（不重新召回全局 RAG，节省 ~1500 tokens/次）
            fix_hint = self._build_fix_hint(target_file, description, observer_context)
            result = self._fix_with_editor(
                target_file, description, edit_instruction,
                existing_code=existing_code,
                memory_hint=fix_hint, task_meta=task_meta
            )
        else:
            # 首次生成：完整记忆（含全局 RAG 3+1 + 项目经验 + TDD 窗口）
            memory_hint = self._build_memory_hint(target_file, description, task_meta)
            result = self._generate_full(
                target_file, description,
                memory_hint=memory_hint,
                observer_context=observer_context,
                task_meta=task_meta
            )
        
        global_broadcaster.emit_sync("Coder", "coding_done", f"{target_file} 编写完毕", {"code": result})
        return result
