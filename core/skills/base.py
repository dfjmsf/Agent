"""
BaseSkill — Skill 抽象基类

每个 Skill 必须实现：
1. schema() → dict:  返回 OpenAI Function Calling 格式的 JSON Schema
2. execute(**kwargs) → str:  执行技能并返回文本结果

设计原则：
- 每个 Skill 类是一个自描述的原子能力单元
- schema 供 LLM 消费（函数签名），execute 供 SkillRunner 调用
- 有状态的 Skill（如 SandboxTerminal）在 __init__ 中注入上下文
- 无状态的 Skill（如 PortChecker）可直接实例化
"""
from abc import ABC, abstractmethod


class BaseSkill(ABC):
    """Skill 抽象基类 — 所有 ASTrea Tool 的公共契约"""

    @abstractmethod
    def schema(self) -> dict:
        """
        返回此 Skill 的 JSON Schema（OpenAI Function Calling 格式）。

        Returns:
            {"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}
        """
        ...

    @abstractmethod
    def execute(self, **kwargs) -> str:
        """
        执行 Skill，返回纯文本结果。

        Args:
            **kwargs: LLM 传入的参数（与 schema.parameters 对应）

        Returns:
            执行结果的文本描述
        """
        ...

    @property
    def name(self) -> str:
        """Skill 名称（默认从 schema 中提取）"""
        return self.schema()["function"]["name"]
