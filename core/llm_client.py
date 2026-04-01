import os
import logging
from typing import List, Dict, Any, Optional
from openai import OpenAI
from dotenv import load_dotenv

# 加载环境变量
load_dotenv()

logger = logging.getLogger("LLMClient")

# 全局 Token 追踪器
TOTAL_PROMPT_TOKENS = 0
TOTAL_COMPLETION_TOKENS = 0
TOKEN_WARNING_LIMIT = int(os.getenv("TOKEN_WARNING_LIMIT", 50000))


class LLMProvider:
    """单个 LLM Provider 配置"""
    def __init__(self, name: str, api_key: str, base_url: str, models: List[str]):
        self.name = name
        self.models = [m.lower() for m in models]
        self.client = OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=300.0,
        )

    def supports(self, model: str) -> bool:
        """检查此 Provider 是否支持指定模型"""
        return model.lower() in self.models


class LLMClient:
    """多 Provider LLM 客户端，根据模型名自动路由到对应 API 端点。
    
    支持的 Provider（按优先级）：
    1. Qwen (DashScope) — QWEN_API_KEY + QWEN_BASE_URL
    2. OpenAI 兼容端点 — OPENAI_API_KEY + OPENAI_BASE_URL（可用于 Gemini 中转等）
    
    路由规则：
    - 模型名在 Provider 的 models 列表中 → 走该 Provider
    - 都不匹配 → fallback 到第一个可用的 Provider
    """

    def __init__(self):
        self.providers: List[LLMProvider] = []
        self._init_providers()
        
        if not self.providers:
            logger.error("❌ 没有配置任何 LLM Provider！请检查 .env 文件")

    def _init_providers(self):
        """从环境变量初始化所有可用的 Provider"""
        
        # Provider 1: Qwen (DashScope)
        qwen_key = os.getenv("QWEN_API_KEY", "")
        if qwen_key and qwen_key != "your_qwen_api_key_here":
            qwen_url = os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
            # Qwen 支持的模型列表（可通过 QWEN_MODELS 环境变量扩展）
            qwen_models_str = os.getenv("QWEN_MODELS", "qwen-max,qwen-plus,qwen-turbo,qwen3-max,qwen3-plus,qwen3-coder-plus,qwen-long")
            qwen_models = [m.strip() for m in qwen_models_str.split(",") if m.strip()]
            self.providers.append(LLMProvider("Qwen", qwen_key, qwen_url, qwen_models))
            logger.info(f"✅ Provider [Qwen] 已加载 ({len(qwen_models)} 个模型)")
        
        # Provider 2: OpenAI 兼容端点（Gemini 中转等）
        openai_key = os.getenv("OPENAI_API_KEY", "")
        if openai_key:
            openai_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
            openai_models_str = os.getenv("OPENAI_MODELS", "")
            openai_models = [m.strip() for m in openai_models_str.split(",") if m.strip()]
            if openai_models:
                self.providers.append(LLMProvider("OpenAI-Compatible", openai_key, openai_url, openai_models))
                logger.info(f"✅ Provider [OpenAI-Compatible] 已加载 ({len(openai_models)} 个模型)")
        
        # Provider 3: GPT 中转端点
        gpt_key = os.getenv("GPT_API_KEY", "")
        if gpt_key:
            gpt_url = os.getenv("GPT_BASE_URL", "https://api.openai.com/v1")
            gpt_models_str = os.getenv("GPT_MODELS", "")
            gpt_models = [m.strip() for m in gpt_models_str.split(",") if m.strip()]
            if gpt_models:
                self.providers.append(LLMProvider("GPT", gpt_key, gpt_url, gpt_models))
                logger.info(f"✅ Provider [GPT] 已加载 ({len(gpt_models)} 个模型)")

    def _get_provider(self, model: str) -> LLMProvider:
        """根据模型名路由到对应的 Provider"""
        for provider in self.providers:
            if provider.supports(model):
                return provider
        
        # Fallback: 用第一个可用的 Provider
        if self.providers:
            logger.warning(f"⚠️ 模型 {model} 未匹配任何 Provider，fallback 到 [{self.providers[0].name}]")
            return self.providers[0]
        
        raise RuntimeError(f"没有可用的 LLM Provider 来处理模型 {model}")

    def get_provider_client(self, provider_name: str = "Qwen"):
        """获取指定 Provider 的原始 OpenAI client（用于 Embedding/Rerank 等非 chat 接口）"""
        for p in self.providers:
            if p.name == provider_name:
                return p.client
        # Fallback
        if self.providers:
            return self.providers[0].client
        raise RuntimeError("没有可用的 Provider")

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
        
        request_total = prompt_tokens + completion_tokens
        total_session_tokens = TOTAL_PROMPT_TOKENS + TOTAL_COMPLETION_TOKENS
        
        if request_total > TOKEN_WARNING_LIMIT:
            logger.warning(f"🚨 [单次巨量 Token 告警] 本次请求竟然消耗了 {request_total} Tokens，极可能存在死循环大文件注入！")
            
        logger.info(f"🪙 [Token开销] 本次请求: {request_total} | 后端进程总累计: {total_session_tokens}")

    def chat_completion(
        self, 
        messages: List[Dict[str, str]], 
        model: str, 
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.2
    ) -> Any:
        """
        向大语言模型发送聊天补全请求（同步）。
        根据 model 名称自动路由到对应的 Provider。
        
        参数:
            messages: 消息字典列表 (包含 role 和 content)
            model: 目标模型名称 (如 qwen3-max, Gemini3.1pro, glm-5)
            tools: 可选的工具定义列表，用于 Function Calling
            temperature: 生成温度系数
            
        返回:
            原始响应的 message 对象 (其中可能包含 tool_calls)
        """
        provider = self._get_provider(model)
        
        # 如果 tools 为空则不传该字段，防止 API 格式校验报错
        kwargs = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools

        try:
            logger.info(f"正在向 [{provider.name}] 模型 {model} 发送请求...")
            response = provider.client.chat.completions.create(**kwargs)
            
            # 更新 Token 账户
            self._update_token_usage(response.usage)
            
            return response.choices[0].message
            
        except Exception as e:
            logger.error(f"❌ [{provider.name}] API 请求失败: {e}")
            raise e

# 默认提供的全局单例
default_llm = LLMClient()

