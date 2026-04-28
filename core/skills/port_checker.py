"""
PortCheckerSkill — 端口检测技能

从原 SkillRunner._skill_check_port 原样提取。

✅ 无状态，可公共化。
"""
import socket
import logging

from core.skills.base import BaseSkill

logger = logging.getLogger("SkillRunner")


class PortCheckerSkill(BaseSkill):
    """端口检测 — 检查指定端口是否在监听"""

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "check_port",
                "description": "检查指定端口是否正在监听。用于确认服务是否已启动就绪。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "port": {
                            "type": "integer",
                            "description": "要检查的端口号（如 5001）"
                        },
                    },
                    "required": ["port"],
                },
            },
        }

    def execute(self, **kwargs) -> str:
        port = kwargs["port"]
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            result = s.connect_ex(("127.0.0.1", port))
            s.close()
            if result == 0:
                return f"端口 {port} 正在监听 ✅"
            else:
                return f"端口 {port} 未监听 ❌"
        except Exception as e:
            return f"端口检查失败: {e}"
