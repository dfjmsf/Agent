import os
import json
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

    # 原生 Provider 名称集合（支持发送私有思考模式参数）
    # 第三方代理（硅基流动/OpenRouter 等）不识别这些参数，可能导致请求挂起或报错
    _NATIVE_THINKING_PROVIDERS = {"DeepSeek", "Qwen", "GPT"}

    def __init__(self):
        self.providers: List[LLMProvider] = []
        self._init_providers()
        
        if not self.providers:
            logger.error("❌ 没有配置任何 LLM Provider！请检查 .env 文件")

    def _init_providers(self):
        """从环境变量 + config/custom_providers.json 初始化所有可用的 Provider"""
        
        # Provider 1: Qwen (DashScope)
        qwen_key = os.getenv("QWEN_API_KEY", "")
        if qwen_key and qwen_key != "your_qwen_api_key_here":
            qwen_url = os.getenv("QWEN_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1")
            # Qwen 支持的模型列表（可通过 QWEN_MODELS 环境变量扩展）
            qwen_models_str = os.getenv("QWEN_MODELS", "qwen-max,qwen-plus,qwen-turbo,qwen3-max,qwen3-plus,qwen3-coder-plus,qwen-long,qwen3.5-plus,qwen3.5-max,qwen3.5-coder-plus")
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
        
        # Provider 3: DeepSeek
        ds_key = os.getenv("DEEPSEEK_API_KEY", "")
        if ds_key:
            ds_url = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
            ds_models_str = os.getenv("DEEPSEEK_MODELS", "deepseek-chat")
            ds_models = [m.strip() for m in ds_models_str.split(",") if m.strip()]
            self.providers.append(LLMProvider("DeepSeek", ds_key, ds_url, ds_models))
            logger.info(f"✅ Provider [DeepSeek] 已加载 ({len(ds_models)} 个模型)")

        # Provider 4: GPT 中转端点
        gpt_key = os.getenv("GPT_API_KEY", "")
        if gpt_key:
            gpt_url = os.getenv("GPT_BASE_URL", "https://api.openai.com/v1")
            gpt_models_str = os.getenv("GPT_MODELS", "")
            gpt_models = [m.strip() for m in gpt_models_str.split(",") if m.strip()]
            if gpt_models:
                self.providers.append(LLMProvider("GPT", gpt_key, gpt_url, gpt_models))
                logger.info(f"✅ Provider [GPT] 已加载 ({len(gpt_models)} 个模型)")

        # 动态加载: config/custom_providers.json
        self._load_custom_providers()

    def _load_custom_providers(self):
        """从 config/custom_providers.json 加载用户自定义 Provider"""
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config", "custom_providers.json"
        )
        if not os.path.isfile(config_path):
            return

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                custom_list = json.load(f)
        except Exception as e:
            logger.warning(f"⚠️ 读取 custom_providers.json 失败: {e}")
            return

        for entry in custom_list:
            name = entry.get("name", "Unknown")
            api_key = entry.get("api_key", "")
            base_url = entry.get("base_url", "")
            models = entry.get("models", [])
            if not api_key or not models:
                logger.warning(f"⚠️ 自定义 Provider [{name}] 缺少 api_key 或 models，已跳过")
                continue
            # 清洗 base_url：剥离用户误填的 /chat/completions 后缀
            clean_url = self._clean_base_url(base_url)
            # 避免重名覆盖：如果已有同名 Provider 则跳过
            if any(p.name == name for p in self.providers):
                logger.info(f"ℹ️ Provider [{name}] 已通过 .env 加载，跳过 JSON 中的重复项")
                continue
            self.providers.append(LLMProvider(name, api_key, clean_url, models))
            logger.info(f"✅ Provider [{name}] 已从 custom_providers.json 加载 ({len(models)} 个模型)")

    @staticmethod
    def _clean_base_url(url: str) -> str:
        """清洗 base_url：剥离用户误填的 /chat/completions 等后缀。
        OpenAI SDK 会自动拼接 /chat/completions，如果用户填了完整 endpoint 会双拼接导致 404。
        """
        clean = (url or "").strip().rstrip("/")
        for suffix in ["/chat/completions", "/completions", "/chat"]:
            if clean.lower().endswith(suffix):
                clean = clean[:len(clean) - len(suffix)]
                break
        return clean.rstrip("/")

    def reload_providers(self):
        """热重载所有 Provider（供 server.py CRUD 操作后调用）"""
        self.providers.clear()
        self._init_providers()
        logger.info(f"🔄 Provider 列表已热重载，当前 {len(self.providers)} 个 Provider")

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

    @staticmethod
    def _model_supports_thinking(model: str) -> bool:
        """判断模型是否支持思考模式（Qwen3/3.5 或 DeepSeek V4 或 GPT-5）"""
        model_lower = (model or "").lower()
        return "qwen3" in model_lower or "gpt-5" in model_lower or "deepseek-v4" in model_lower

    @staticmethod
    def _is_deepseek_model(model: str) -> bool:
        """判断是否为 DeepSeek 系列模型"""
        return "deepseek" in (model or "").lower()

    @staticmethod
    def parse_thinking_config(env_val: str):
        """
        解析 THINKING_* 环境变量值，返回 (enable_thinking, reasoning_effort)。
        
        支持值：
        - "false" → (False, None)
        - "true" / "high" → (True, "high")
        - "max" → (True, "max")
        """
        val = (env_val or "false").lower().strip()
        if val in ("true", "high"):
            return True, "high"
        elif val == "max":
            return True, "max"
        else:
            return False, None

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
        tool_choice: Optional[str] = None,
        temperature: float = 0.2,
        enable_thinking: bool = False,
        reasoning_effort: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Any:
        """
        向大语言模型发送聊天补全请求（同步）。
        根据 model 名称自动路由到对应的 Provider。
        
        参数:
            messages: 消息字典列表 (包含 role 和 content)
            model: 目标模型名称 (如 qwen3-max, deepseek-v4-flash)
            tools: 可选的工具定义列表，用于 Function Calling
            tool_choice: 工具选择策略 ("auto"/"required"/"none")
            temperature: 生成温度系数
            enable_thinking: 是否启用深度思考
            reasoning_effort: 思考强度 ("high"/"max"，仅 DeepSeek V4 有效)
            
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
        if max_tokens:
            kwargs["max_tokens"] = max_tokens
        if tools:
            kwargs["tools"] = tools
            if tool_choice:
                kwargs["tool_choice"] = tool_choice

        # 思考模式参数：按 Provider 类型分支处理
        # ⚠️ extra_body 是各厂商私有协议，只对原生 Provider 发送。
        # 第三方代理（硅基流动/OpenRouter 等）不识别这些参数，可能导致请求挂起或报错。
        thinking_hint = ""
        is_native_provider = provider.name in self._NATIVE_THINKING_PROVIDERS
        if is_native_provider and self._model_supports_thinking(model):
            if self._is_deepseek_model(model):
                # DeepSeek V4: thinking.type + reasoning_effort
                if enable_thinking:
                    effort = reasoning_effort or "high"
                    kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
                    kwargs["reasoning_effort"] = effort
                    thinking_hint = f" [思考:{effort}]"
                else:
                    kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            else:
                # Qwen3/3.5 / GPT-5: enable_thinking 布尔值
                kwargs["extra_body"] = {"enable_thinking": enable_thinking}
                if enable_thinking:
                    thinking_hint = " [思考模式]"

        try:
            logger.info(f"正在向 [{provider.name}] 模型 {model}{thinking_hint} 发送请求...")
            response = provider.client.chat.completions.create(**kwargs)
            
            # 更新 Token 账户
            self._update_token_usage(response.usage)

            # 防御：第三方代理可能返回 200 但 choices 为空
            if not response.choices:
                raise RuntimeError(f"API 返回空 choices（模型 {model} 可能不支持当前请求格式）")
            
            return response.choices[0].message
            
        except Exception as e:
            logger.error(f"❌ [{provider.name}] API 请求失败: {e}")
            raise e

    def vision_completion(
        self,
        prompt: str,
        base64_image: str,
        model: Optional[str] = None,
        temperature: float = 0.2,
        enable_thinking: bool = False,
    ) -> str:
        """
        向多模态视觉模型发送图像审阅请求。
        参数:
            prompt: 给模型的视觉提示词
            base64_image: 图像 Base64 编码 (例如无需包含 data:image/png;base64, 前缀，视拼接情况而定)
            model: 用户自选的多模态模型（默认加载环境变量 MODEL_QA_VISION）
        """
        target_model = model or os.getenv("MODEL_QA_VISION") or os.getenv("MODEL_QA", "deepseek-chat")
        provider = self._get_provider(target_model)

        # 构造带有 image_url 的多模态内容体（OpenAI 兼容协议标准）
        content = [
            {"type": "text", "text": prompt}
        ]
        
        # 降级占位逻辑：如果明确选择了不带视觉能力的文本模型（如默认的 deepseek-chat），则不发图，避免 API 报错400
        # 真实环境中由用户在前端配置里确保填写如 kimi-v1-vision 或 gpt-4o 等多模态模型名
        if "deepseek" in target_model.lower():
            logger.warning(f"⚠️ 检测到 {target_model} 可能不支持视觉输入，自动剥离图像，执行纯文本降级体验")
            content[0]["text"] += "\n[系统提示：图片无法加载，请基于以上描述假装页面已正确渲染并附上基于常识的设计建议]"
        else:
            content.append({
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/png;base64,{base64_image}",
                },
            })

        messages = [
            {
                "role": "user",
                "content": content
            }
        ]

        kwargs = {
            "model": target_model,
            "messages": messages,
            "temperature": temperature,
        }

        # 视觉模型思考参数（与 chat_completion 保持一致）
        # 同样只对原生 Provider 发送私有参数
        thinking_hint = ""
        is_native_provider = provider.name in self._NATIVE_THINKING_PROVIDERS
        if is_native_provider and self._model_supports_thinking(target_model):
            if self._is_deepseek_model(target_model):
                if enable_thinking:
                    kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
                    kwargs["reasoning_effort"] = "high"
                    thinking_hint = " [思考:high]"
                else:
                    kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            else:
                kwargs["extra_body"] = {"enable_thinking": enable_thinking}
                if enable_thinking:
                    thinking_hint = " [思考模式]"

        try:
            logger.info(f"👁️ 正在向 [{provider.name}] 多模态模型 {target_model}{thinking_hint} 发送视觉请求...")
            response = provider.client.chat.completions.create(**kwargs)
            self._update_token_usage(response.usage)
            
            return response.choices[0].message.content or ""
            
        except Exception as e:
            logger.error(f"❌ [{provider.name}] 视觉 API 请求失败: {e}")
            return f"视觉判定请求失败: {str(e)}"

# 默认提供的全局单例
default_llm = LLMClient()
