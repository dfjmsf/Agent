import React, { useEffect, useRef } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { SquareTerminal } from 'lucide-react';

/**
 * Reviewer Sandbox 面板 - 展示审查日志与沙盒执行结果
 */
export default function ReviewerPanel({ reviewerLogs, activeRole }) {
  const logsEndRef = useRef(null);

  useEffect(() => {
    if (logsEndRef.current) {
      logsEndRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [reviewerLogs]);

  return (
    <motion.div
      className="glass-panel col-reviewer"
      initial={{ x: -50, opacity: 0 }}
      animate={{ x: 0, opacity: 1 }}
      transition={{ delay: 0.3 }}
    >
      <div className="panel-header">
        <SquareTerminal className="dot-reviewer" size={18} />
        <span style={{ color: 'var(--color-reviewer)' }}>Reviewer Sandbox</span>
        <div style={{ flexGrow: 1 }}></div>
        <div className={`status-indicator ${activeRole === 'Reviewer' ? 'active-reviewer' : ''}`}></div>
      </div>
      <div className="console-output">
        <AnimatePresence>
          {reviewerLogs.map((log) => (
            <motion.div
              key={log.id}
              initial={{ opacity: 0, x: 20 }}
              animate={{ opacity: 1, x: 0 }}
              className={`log-${log.type}`}
            >
              [{log.timestamp || '--:--:--'}] [{log.role}] {log.text}
            </motion.div>
          ))}
        </AnimatePresence>
        <div ref={logsEndRef} />
      </div>
    </motion.div>
  );
}
