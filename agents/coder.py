import os
import re
import logging
from typing import Optional, Dict, Any, List
from core.llm_client import default_llm
from core.prompt import Prompts
from core.state_manager import global_state_manager
from core.ws_broadcaster import global_broadcaster

logger = logging.getLogger("CoderAgent")

class CoderAgent:
    """
    编码 Agent (Coder)
    专职：接收一个确定的任务目标和当前虚拟文件系统的上下文，只输出极简、纯净的代码文本。
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
        # 尝试匹配 ```python ... ``` 之间的内容
        pattern = re.compile(r"```(?:python|py)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
        match = pattern.search(raw_text)
        if match:
            return match.group(1).strip()
        
        # 如果没有前缀，但是最后一行是 ```，也要剥离
        lines = [line for line in raw_text.split("\n") if not line.strip().startswith("```")]
        return "\n".join(lines).strip()

    def generate_code(self, target_file: str, description: str, feedback: Optional[str] = None) -> str:
        """
        生成或修复代码
        
        参数:
            target_file: 正在写的文件名
            description: 具体的任务描述
            feedback: (可选) 如果是重试环节，Reviewer 传回来的报错和建议
        """
        # 1. 获取全局上下文内存目录
        vfs = global_state_manager.get_vfs(self.project_id)
        vfs_dict = vfs.get_all_vfs()
        
        # 将当前的虚拟树转换为可视化的提示词文本
        vfs_context = []
        for file_path, content in vfs_dict.items():
            if file_path != target_file:  # 不把老草稿喂进去，免得它产生依赖疑惑
                # 为了节省 Token, 只展示前 30 行预览和定义，或者全量（对于小文件）
                preview = content[:800] + "\n...[省略]" if len(content) > 800 else content
                vfs_context.append(f"--- [现存文件: {file_path}] ---\n{preview}\n")
                
        vfs_str = "".join(vfs_context) if vfs_context else "当前项目是空的，你是写的第一个文件。"

        # 2. 组装输入
        system_content = Prompts.CODER_SYSTEM.format(
            target_file=target_file,
            description=description,
            vfs_context=vfs_str
        )
        
        # 3. 如果有 Reviewer 退回的 Feedback，说明现在是"修复模式"
        user_prompt = "请开始编写该文件的代码。只输出这一个文件的代码内容。"
        if feedback:
            user_prompt = f"【🚨 紧急修复要求】你之前生成的代码被 Reviewer 测试出错了！\n以下是沙盒运行报错或审查人的建议：\n\n{feedback}\n\n请修复上述 bug，并重新输出该文件的完整纯净代码！不能偷懒只输出片段！"

        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_prompt}
        ]

        logger.info(f"💻 Coder 正在疯狂编码中... 目标文件: {target_file}")
        attempt = 1
        global_broadcaster.emit_sync("Coder", "coding_start", f"正在为 {target_file} 编写代码", {"target": target_file})
        
        # 4. 请求大模型
        response_msg = default_llm.chat_completion(
            messages=messages,
            model=self.model,
            temperature=0.2
        )
        
        raw_code = response_msg.content
        
        # 5. 清洗并暂存
        clean_code = self._clean_markdown(raw_code)
        
        # 将刚才生成的草稿保存进虚拟文件系统
        vfs.save_draft(target_file, clean_code)
        
        logger.info(f"✅ Coder 编撰完成 ({len(clean_code)} bytes)")
        global_broadcaster.emit_sync("Coder", "coding_done", f"{target_file} 编写完毕", {"code": clean_code})
        
        return clean_code
