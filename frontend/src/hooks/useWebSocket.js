import { useEffect, useRef, useState } from 'react';
import { WS_URL } from '../services/api';

/**
 * WebSocket 连接自定义 Hook
 * 封装连接管理、自动重连、消息解析逻辑
 *
 * @param {function} onMessage - 收到消息时的回调 (parsed JSON object)
 * @returns {{ isConnected: boolean }}
 */
export default function useWebSocket(onMessage) {
  const [isConnected, setIsConnected] = useState(false);
  const ws = useRef(null);
  const onMessageRef = useRef(onMessage);

  // 保持 onMessage 回调最新，避免 stale closure
  useEffect(() => {
    onMessageRef.current = onMessage;
  }, [onMessage]);

  useEffect(() => {
    let isMounted = true;

    function connect() {
      if (!isMounted) return;
      const socket = new WebSocket(WS_URL);
      ws.current = socket;

      socket.onopen = () => {
        if (isMounted) {
          setIsConnected(true);
          console.log("WebSocket connected");
        }
      };

      socket.onmessage = (event) => {
        try {
          const msg = JSON.parse(event.data);
          onMessageRef.current?.(msg);
        } catch (e) {
          console.error("Parse WS error", e);
        }
      };

      socket.onclose = () => {
        if (isMounted) {
          setIsConnected(false);
          console.log("WebSocket disconnected. Reconnecting in 3s...");
          setTimeout(connect, 3000);
        }
      };
    }

    connect();

    return () => {
      isMounted = false;
      if (ws.current) ws.current.close();
    };
  }, []);

  return { isConnected };
}
