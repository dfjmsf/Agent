import asyncio
import json
from typing import List

class WebSocketBroadcaster:
    def __init__(self):
        self.active_connections: List = []
        self.main_loop = None

    async def connect(self, websocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        # Convert message to json string
        text_data = json.dumps(message, ensure_ascii=False)
        for connection in self.active_connections:
            try:
                await connection.send_text(text_data)
            except Exception:
                pass

    def emit_sync(self, agent_role: str, action_type: str, content: str, payload: dict = None):
        """
        供非异步的 Agent 代码调用，将消息推送到异步事件循环中广播。
        """
        msg = {
            "agent_role": agent_role,     # 'System', 'Manager', 'Coder', 'Reviewer'
            "action_type": action_type,   # 'info', 'task_start', 'code_stream', 'test_result', etc.
            "content": content,
            "payload": payload or {}
        }
        
        # 尝试使用我们通过 FastAPI startup 保存的主事件循环
        if hasattr(self, 'main_loop') and self.main_loop:
            asyncio.run_coroutine_threadsafe(self.broadcast(msg), self.main_loop)
        else:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.broadcast(msg))
            except RuntimeError:
                pass

# 全局单例广播器
global_broadcaster = WebSocketBroadcaster()
