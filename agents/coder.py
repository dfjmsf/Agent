import os
import re
import json
import logging
from typing import Optional, Dict, Any, List
from core.llm_client import default_llm
from core.prompt import Prompts
from core.state_manager import global_state_manager
from core.ws_broadcaster import global_broadcaster
from core.database import recall, get_recent_events

logger = logging.getLogger("CoderAgent")

class CoderAgent:
    """
    编码 Agent (Coder)
    专职：接收一个确定的任务目标和当前虚拟文件系统的上下文，只输出极简、纯净的代码文本。
    支持两种模式：
      - 首次生成：直接输出完整代码
      - 修复模式：使用 edit_file Function Calling 做差量编辑（匹配失败自动 fallback 全量覆写）
    """
    def __init__(self, project_id: str = "default_project"):
        self.model = os.getenv("MODEL_CODER", "qwen3-coder-plus")
        self.project_id = project_id

    def _clean_markdown(self, raw_text: str) -> str:
        """
        极度严苛的 Markdown 代码块清洗。
        大模型很容易忽略要求，加上 ```python 前后缀。
        如果包含，我们必须把它剥离出来，否则丢进沙盒直接报 SyntaxError。
        """
        pattern = re.compile(r"```(?:python|py)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
        match = pattern.search(raw_text)
        if match:
            return match.group(1).strip()
        
        lines = [line for line in raw_text.split("\n") if not line.strip().startswith("```")]
        return "\n".join(lines).strip()

    def _build_memory_hint(self, target_file: str, description: str) -> str:
        """构建长短期记忆提示，按 scope 分组注入"""
        memory_hint = ""
        
        # 长期记忆 → 全局通用架构智慧（recall 返回 List[Dict]）
        past_tips = recall(f"{target_file} {description}", n_results=5, project_id=self.project_id, caller="Coder")
        if past_tips:
            tips_str = "\n".join([f"  {i+1}. {tip['content']}" for i, tip in enumerate(past_tips)])
            memory_hint = f"\n\n【🌍 全局通用架构智慧 (Global Experience)】\n{tips_str}"
        
        # 短期记忆 → 项目专属经验（必须绝对服从）
        experience_events = get_recent_events(
            project_id=self.project_id, limit=3,
            event_types=["experience_project"], caller="Coder"
        )
        if experience_events:
            exp_hints = "\n".join([f"  {i+1}. {e.content[:200]}" for i, e in enumerate(experience_events)])
            memory_hint += f"\n\n【📦 本项目最高优先级规则 (Project Experience - 必须绝对服从)】\n{exp_hints}"
        
        # 短期记忆 → 项目文件树
        file_tree_events = get_recent_events(
            project_id=self.project_id, limit=1,
            event_types=["file_tree"], caller="Coder"
        )
        if file_tree_events:
            memory_hint += f"\n\n【📂 当前项目文件结构】\n{file_tree_events[0].content[:500]}"
        
        return memory_hint

    def _generate_full(self, target_file: str, description: str, vfs, memory_hint: str, task_meta: dict = None) -> str:
        """首次生成：输出完整代码文件"""
        vfs_dict = vfs.get_all_vfs()
        
        # 按 dependencies 过滤 VFS 上下文
        dep_files = self._resolve_dependency_files(task_meta) if task_meta else None
        vfs_context = []
        for file_path, content in vfs_dict.items():
            if file_path != target_file:
                # 如果有依赖列表，只注入依赖文件；否则 fallback 到全量
                if dep_files is not None and file_path not in dep_files:
                    continue
                preview = content[:800] + "\n...[省略]" if len(content) > 800 else content
                vfs_context.append(f"--- [依赖文件: {file_path}] ---\n{preview}\n")
                
        vfs_str = "".join(vfs_context) if vfs_context else "当前无依赖文件，你是写的第一个文件。"

        # 注入项目规划书
        project_spec = task_meta.get("project_spec", "无规划书") if task_meta else "无规划书"

        system_content = Prompts.CODER_SYSTEM.format(
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
        
        clean_code = self._clean_markdown(response_msg.content)
        vfs.save_draft(target_file, clean_code)
        
        logger.info(f"✅ Coder 全量生成完成 ({len(clean_code)} bytes)")
        return clean_code

    def _fix_with_editor(self, target_file: str, description: str, feedback: str, vfs, memory_hint: str) -> str:
        """
        修复模式：使用 edit_file Function Calling 做差量编辑。
        如果 LLM 不使用工具或 edits 匹配失败，自动 fallback 到全量覆写。
        """
        current_code = vfs.get_draft(target_file) or ""
        
        system_content = Prompts.CODER_FIX_SYSTEM.format(
            target_file=target_file,
            current_code=current_code,
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
                            success, msg = vfs.apply_edits(target_file, edits)
                            if success:
                                updated_code = vfs.get_draft(target_file)
                                logger.info(f"🔧 [Editor] 差量编辑成功: {msg}")
                                global_broadcaster.emit_sync("Coder", "edit_done", f"差量修复完成: {msg}", {"code": updated_code})
                                return updated_code
                            else:
                                logger.warning(f"⚠️ [Editor] 差量编辑部分失败: {msg}，尝试 fallback")
                    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
                        logger.warning(f"⚠️ [Editor] 工具参数解析失败: {e}，fallback 全量覆写")

        # Fallback: 全量覆写
        logger.warning(f"⚠️ [Editor] Fallback 全量覆写模式")
        return self._fallback_full_rewrite(target_file, description, feedback, vfs, memory_hint)

    def _fallback_full_rewrite(self, target_file: str, description: str, feedback: str, vfs, memory_hint: str, task_meta: dict = None) -> str:
        """降级方案：和原来一样全量重写"""
        vfs_dict = vfs.get_all_vfs()
        
        # 按 dependencies 过滤 VFS 上下文
        dep_files = self._resolve_dependency_files(task_meta) if task_meta else None
        vfs_context = []
        for file_path, content in vfs_dict.items():
            if file_path != target_file:
                if dep_files is not None and file_path not in dep_files:
                    continue
                preview = content[:800] + "\n...[省略]" if len(content) > 800 else content
                vfs_context.append(f"--- [依赖文件: {file_path}] ---\n{preview}\n")
        vfs_str = "".join(vfs_context) if vfs_context else "当前无依赖文件。"

        # 注入项目规划书
        project_spec = task_meta.get("project_spec", "无规划书") if task_meta else "无规划书"

        system_content = Prompts.CODER_SYSTEM.format(
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
        
        clean_code = self._clean_markdown(response_msg.content)
        vfs.save_draft(target_file, clean_code)
        
        logger.info(f"✅ Coder Fallback 全量重写完成 ({len(clean_code)} bytes)")
        return clean_code

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

    def generate_code(self, target_file: str, description: str, feedback: Optional[str] = None, task_meta: dict = None) -> str:
        """
        生成或修复代码（统一入口）
        
        判定模式：
        1. feedback≠None → Reviewer 退回修复（Editor Tools 差量编辑）
        2. 文件已存在于 VFS → 跨任务修改已有文件（Editor Tools 差量编辑）
        3. 文件不存在 → 首次生成（全量输出）
        """
        vfs = global_state_manager.get_vfs(self.project_id)
        memory_hint = self._build_memory_hint(target_file, description)
        existing_code = vfs.get_draft(target_file)
        
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
            result = self._fix_with_editor(target_file, description, edit_instruction, vfs, memory_hint)
        else:
            result = self._generate_full(target_file, description, vfs, memory_hint, task_meta=task_meta)
        
        global_broadcaster.emit_sync("Coder", "coding_done", f"{target_file} 编写完毕", {"code": result})
        return result

