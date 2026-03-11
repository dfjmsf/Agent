import os
import logging
from typing import List, Dict, Any, Optional
from openai import OpenAI
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

# 设置基础日志记录
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("LLMClient")

# 全局 Token 追踪器
TOTAL_PROMPT_TOKENS = 0
TOTAL_COMPLETION_TOKENS = 0
TOKEN_WARNING_LIMIT = int(os.getenv("TOKEN_WARNING_LIMIT", 50000))

class LLMClient:
    """兼容 OpenAI 格式的 Qwen API 封装，带全局 Token 追踪功能。"""

    def __init__(self):
        api_key = os.getenv("QWEN_API_KEY")
        base_url = os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
        
        if not api_key or api_key == "your_qwen_api_key_here":
            logger.warning("⚠️ QWEN_API_KEY 未在 .env 中正确配置！")

        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
        )

    def _update_token_usage(self, usage) -> None:
        """更新全局 Token 消耗统计，并在超出阈值时触发警告。"""
        global TOTAL_PROMPT_TOKENS
        global TOTAL_COMPLETION_TOKENS
        
        if not usage:
            return

        prompt_tokens = getattr(usage, 'prompt_tokens', 0)
        completion_tokens = getattr(usage, 'completion_tokens', 0)
        
        TOTAL_PROMPT_TOKENS += prompt_tokens
        TOTAL_COMPLETION_TOKENS += completion_tokens
        
        total_session_tokens = TOTAL_PROMPT_TOKENS + TOTAL_COMPLETION_TOKENS
        
        if total_session_tokens > TOKEN_WARNING_LIMIT:
            logger.error(f"🚨 [TOKEN 告警] 当前任务消耗的 Tokens ({total_session_tokens}) 已超过警告阈值 ({TOKEN_WARNING_LIMIT})！")
            # 注：实际的内存上下文压缩逻辑将在 engine 引擎层处理
            
        logger.debug(f"单次消耗: +{prompt_tokens} (提问), +{completion_tokens} (回答)。"
                     f"全局累计: {total_session_tokens} tokens。")

    def chat_completion(
        self, 
        messages: List[Dict[str, str]], 
        model: str, 
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.2
    ) -> Any:
        """
        向大语言模型发送聊天补全请求（同步）。
        
        参数:
            messages: 消息字典列表 (包含 role 和 content)
            model: 目标 Qwen 模型名称 (如 qwen-max)
            tools: 可选的工具定义列表，用于 Function Calling
            temperature: 生成温度系数
            
        返回:
            原始响应的 message 对象 (其中可能包含 tool_calls)
        """
        # 如果 tools 为空则不传该字段，防止 API 格式校验报错
        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools

        try:
            logger.info(f"正在向模型 {model} 发送请求...")
            response = self.client.chat.completions.create(**kwargs)
            
            # 更新 Token 账户
            self._update_token_usage(response.usage)
            
            return response.choices[0].message
            
        except Exception as e:
            logger.error(f"❌ API 请求失败: {e}")
            raise e

# 默认提供的全局单例
default_llm = LLMClient()
