import asyncio
import json
import threading
from typing import List


class WebSocketBroadcaster:
    def __init__(self):
        self._lock = threading.Lock()
        self.active_connections: List = []
        self.main_loop = None

    async def connect(self, websocket):
        await websocket.accept()
        with self._lock:
            self.active_connections.append(websocket)

    def disconnect(self, websocket):
        with self._lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        # Convert message to json string
        text_data = json.dumps(message, ensure_ascii=False)

        # 获取当前连接快照，避免长时间持锁
        with self._lock:
            connections = list(self.active_connections)

        dead_connections = []
        for connection in connections:
            try:
                await connection.send_text(text_data)
            except Exception:
                dead_connections.append(connection)

        # 自动清理发送失败的死连接
        if dead_connections:
            with self._lock:
                for conn in dead_connections:
                    if conn in self.active_connections:
                        self.active_connections.remove(conn)

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
            try:
                asyncio.run_coroutine_threadsafe(self.broadcast(msg), self.main_loop)
            except RuntimeError:
                # Event loop 已关闭（服务器已停止），静默忽略
                pass
        else:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self.broadcast(msg))
            except RuntimeError:
                pass


# 全局单例广播器
global_broadcaster = WebSocketBroadcaster()

