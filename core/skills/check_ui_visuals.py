import os
import json
import logging
from typing import Dict, Any

from core.llm_client import default_llm
from core.skills.base import BaseSkill
from tools.sandbox_browser import take_screenshot

logger = logging.getLogger("Skill.CheckUIVisuals")

class CheckUIVisualsSkill(BaseSkill):
    """
    QA 多模态视觉测试技能。
    利用 Playwright 给页面进行截图，然后调用大模型判图判定渲染质量和正确性。
    """
    
    def __init__(self, sandbox_dir: str):
        self.sandbox_dir = sandbox_dir

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "check_ui_visuals",
                "description": "利用无头浏览器对目标 URL 截图，并向给定的模型（如 Kimi-vision 或纯文本回退模型）提问关于网页视觉界面的排版、美观度、溢出等问题。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "要审查的目标页面的完整 URL，例如 http://127.0.0.1:5001/login",
                        },
                        "query": {
                            "type": "string",
                            "description": "向多模态模型提出的具体审美或判图请求。例如：'这是一个登录页，看看排版是否对齐、颜色是否对比合适，有没有内容溢出或组件遮挡？'",
                        }
                    },
                    "required": ["url", "query"],
                }
            }
        }

    def execute(self, **kwargs) -> str:
        url = kwargs.get("url")
        query = kwargs.get("query")

        if not url or not query:
            return "❌ 参数缺失: check_ui_visuals 需要 url 和 query"

        logger.info(f"👁️‍🗨️ [Skill:check_ui_visuals] 开始截取目标 URL: {url}")
        
        # 1. 尝试截获屏幕快照
        success, screenshot_or_error = take_screenshot(url, self.sandbox_dir)
        
        if not success:
            return f"❌ 页面截图失败: {screenshot_or_error}"

        # 2. 调用模型端点进行识别
        logger.info(f"🧠 [Skill:check_ui_visuals] 发送图文至多模态 VLM 进行评审。Query='{query[:30]}...'")
        
        # 注意: model=None 代表 fallback 到默认关联的 MODEL_QA_VISION (见实现逻辑)
        enable_thinking = os.getenv("THINKING_QA_VISION", "false").lower() == "true"
        evaluation_text = default_llm.vision_completion(
            prompt=f"这是一张刚跑起来的系统页面的全景截图，请回答以下测试诉求：\n\n{query}\n\n注意：如果发现任何例如白屏、严重的排错、代码直接以纯文本挂在页面上（无样式）等极其恶劣的问题，必须明确报错！",
            base64_image=screenshot_or_error,
            enable_thinking=enable_thinking,
        )

        logger.info(f"✅ [Skill:check_ui_visuals] VLM 反馈完成。")
        return f"[UI 多模态视觉评审反馈] :\n{evaluation_text}"
