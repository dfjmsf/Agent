/**
 * PMChat — PM Agent 对话组件
 *
 * 核心功能：
 * - 消息气泡列表（左 PM / 右用户）
 * - plan.md Markdown 渲染
 * - confirm/reject 确定性按钮
 * - 加载状态指示
 */
import { useState, useRef, useEffect, useCallback } from 'react';
import { chatWithPM, chatAction, fetchChatHistory } from '../services/api';

const WELCOME_MSG = { role: 'pm', content: '👋 你好！我是 ASTrea 项目经理。告诉我你想做什么项目，我来帮你规划。', plan_md: null, actions: null };

export default function PMChat({ currentProjectId }) {
  const [messages, setMessages] = useState([WELCOME_MSG]);
  const [input, setInput] = useState('');
  const [isLoading, setIsLoading] = useState(false);
  const messagesEndRef = useRef(null);
  const inputRef = useRef(null);

  // 项目切换时：清空 → 加载历史
  useEffect(() => {
    if (!currentProjectId) return;
    setInput('');
    setIsLoading(false);

    // 从后端加载持久化的对话历史
    fetchChatHistory(currentProjectId)
      .then(data => {
        if (data.messages && data.messages.length > 0) {
          setMessages([WELCOME_MSG, ...data.messages]);
        } else {
          setMessages([WELCOME_MSG]);
        }
      })
      .catch(() => {
        setMessages([WELCOME_MSG]);
      });
  }, [currentProjectId]);

  // 自动滚动到底部
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

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
        actions: res.actions || null,
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

  // 处理按钮点击
  const handleAction = useCallback(async (actionId) => {
    setIsLoading(true);
    // 追加一个用户"点击"消息
    const labelMap = {
      confirm: '✅ 确认执行', reject: '❌ 修改需求',
      patch_confirm: '✅ 确认修改', patch_cancel: '取消修改',
      rollback_confirm: '✅ 确认回滚', rollback_cancel: '取消回滚',
    };
    const label = labelMap[actionId] || actionId;
    setMessages(prev => [...prev, { role: 'user', content: label }]);

    try {
      const res = await chatAction(actionId, currentProjectId);
      setMessages(prev => [...prev, {
        role: 'pm',
        content: res.reply,
        plan_md: res.plan_md || null,
        actions: res.actions || null,
        intent: res.intent,
      }]);
    } catch (e) {
      setMessages(prev => [...prev, {
        role: 'pm',
        content: `❌ 操作失败：${e.message}`,
      }]);
    } finally {
      setIsLoading(false);
    }
  }, [currentProjectId]);

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

              {/* 确定性按钮 */}
              {msg.actions && !isLoading && idx === messages.length - 1 && (
                <div className="pm-actions">
                  {msg.actions.map(action => (
                    <button
                      key={action.id}
                      className={`pm-action-btn ${action.style === 'primary' ? 'pm-btn-primary' : 'pm-btn-secondary'}`}
                      onClick={() => handleAction(action.id)}
                    >
                      {action.label}
                    </button>
                  ))}
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
