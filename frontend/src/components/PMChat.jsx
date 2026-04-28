/**
 * PMChat — PM Agent 对话组件 (v4.0 纯对话驱动)
 *
 * 核心功能：
 * - 消息气泡列表（左 PM / 右用户）
 * - plan.md Markdown 渲染
 * - 加载状态指示
 */
import { useState, useRef, useEffect, useCallback } from 'react';
import { chatWithPM, fetchChatHistory } from '../services/api';

const WELCOME_MSG = { role: 'pm', content: '👋 你好！我是 ASTrea 项目经理。告诉我你想做什么项目，我来帮你规划。', plan_md: null };

// localStorage 持久化 key
const getChatStorageKey = (projectId) => `astrea_pm_chat_${projectId}`;

export default function PMChat({ currentProjectId }) {
  const [messages, setMessages] = useState([WELCOME_MSG]);
  const [input, setInput] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const messagesEndRef = useRef(null);
  const inputRef = useRef(null);

  const messagesProjectRef = useRef(currentProjectId);
  const isTransitioningRef = useRef(false);

  // 项目切换时：从 localStorage 恢复（含 plan_md/actions），降级从后端加载
  useEffect(() => {
    if (!currentProjectId) return;
    setInput('');
    setIsLoading(false);

    // 🔒 锁定：阻止持久化 effect 在过渡期写入旧数据
    isTransitioningRef.current = true;

    // 立即清空为初始状态
    setMessages([WELCOME_MSG]);

    // 标记 messages 归属的项目
    messagesProjectRef.current = currentProjectId;

    // 优先从 localStorage 恢复（包含完整的 plan_md、actions 等字段）
    const storageKey = getChatStorageKey(currentProjectId);
    const cached = localStorage.getItem(storageKey);
    if (cached) {
      try {
        const parsed = JSON.parse(cached);
        if (Array.isArray(parsed) && parsed.length > 0) {
          setMessages(parsed);
          // 解锁：用 setTimeout 让 React 有机会完成 state 更新
          setTimeout(() => { isTransitioningRef.current = false; }, 0);
          return;
        }
      } catch (e) { /* JSON 解析失败，降级到后端 */ }
    }

    // 降级：从后端 FTS5 加载
    fetchChatHistory(currentProjectId)
      .then(data => {
        if (messagesProjectRef.current !== currentProjectId) return;
        if (data.messages && data.messages.length > 0) {
          setMessages([WELCOME_MSG, ...data.messages]);
        }
      })
      .catch(() => {})
      .finally(() => {
        // 无论成功失败，解锁持久化
        setTimeout(() => { isTransitioningRef.current = false; }, 0);
      });
  }, [currentProjectId]);

  // messages 变更时自动持久化到 localStorage
  // ⚠️ 只依赖 [messages]，不依赖 currentProjectId！
  // 使用 messagesProjectRef 确定写入哪个项目的 key
  useEffect(() => {
    // 过渡期间不写入（防止旧 messages 写到新项目 key 下）
    if (isTransitioningRef.current) return;
    const projectId = messagesProjectRef.current;
    if (!projectId) return;
    if (messages.length <= 1) return;
    const storageKey = getChatStorageKey(projectId);
    try {
      localStorage.setItem(storageKey, JSON.stringify(messages));
    } catch (e) {
      console.warn('PMChat: localStorage 写入失败', e);
    }
  }, [messages]);


  // 自动滚动到底部
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  // v4.1: 监听 PM 执行完成的引导性回复（来自 WebSocket → App.jsx 自定义事件）
  useEffect(() => {
    const handler = (e) => {
      setMessages(prev => [...prev, {
        role: 'pm',
        content: e.detail.content,
        plan_md: null,
      }]);
    };
    window.addEventListener('pm-guided-reply', handler);
    return () => window.removeEventListener('pm-guided-reply', handler);
  }, []);


  // 发送消息
  const handleSend = useCallback(async () => {
    const msg = input.trim();
    if (!msg || isLoading) return;

    // 追加用户消息
    setMessages(prev => [...prev, { role: 'user', content: msg }]);
    setInput('');
    setIsLoading(true);

    try {
      const res = await chatWithPM(msg, currentProjectId);
      setMessages(prev => [...prev, {
        role: 'pm',
        content: res.reply,
        plan_md: res.plan_md || null,
        intent: res.intent,
      }]);
    } catch (e) {
      setMessages(prev => [...prev, {
        role: 'pm',
        content: `❌ 通信失败：${e.message}`,
      }]);
    } finally {
      setIsLoading(false);
    }
  }, [input, isLoading, currentProjectId]);


  // 回车发送
  const handleKeyDown = (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  return (
    <div className="pm-chat">
      {/* 消息列表 */}
      <div className="pm-chat-messages">
        {messages.map((msg, idx) => (
          <div key={idx} className={`pm-msg ${msg.role === 'user' ? 'pm-msg-user' : 'pm-msg-pm'}`}>
            <div className="pm-msg-avatar">
              {msg.role === 'user' ? '👤' : '🤖'}
            </div>
            <div className="pm-msg-body">
              <div className="pm-msg-content">{msg.content}</div>

              {/* plan.md 渲染 */}
              {msg.plan_md && (
                <div className="pm-plan-card">
                  <pre className="pm-plan-md">{msg.plan_md}</pre>
                </div>
              )}


            </div>
          </div>
        ))}

        {/* 加载指示器 */}
        {isLoading && (
          <div className="pm-msg pm-msg-pm">
            <div className="pm-msg-avatar">🤖</div>
            <div className="pm-msg-body">
              <div className="pm-msg-loading">
                <span className="pm-dot"></span>
                <span className="pm-dot"></span>
                <span className="pm-dot"></span>
              </div>
            </div>
          </div>
        )}

        <div ref={messagesEndRef} />
      </div>

      {/* 输入框 */}
      <div className="pm-chat-input-area">
        <textarea
          ref={inputRef}
          className="pm-chat-input"
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="输入消息... (Enter 发送, Shift+Enter 换行)"
          rows={2}
          disabled={isLoading}
        />
        <button
          className="pm-send-btn"
          onClick={handleSend}
          disabled={!input.trim() || isLoading}
        >
          发送
        </button>
      </div>
    </div>
  );
}
