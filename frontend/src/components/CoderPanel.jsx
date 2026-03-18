import React from 'react';
import { motion } from 'framer-motion';
import Editor from '@monaco-editor/react';
import { Code2 } from 'lucide-react';

/**
 * Coder Brain 面板 - 展示 Coder Agent 正在编写的代码 (只读)
 */
export default function CoderPanel({ coderCode, activeRole }) {
  return (
    <motion.div
      className="glass-panel col-coder"
      initial={{ x: -50, opacity: 0 }}
      animate={{ x: 0, opacity: 1 }}
      transition={{ delay: 0.2 }}
    >
      <div className="panel-header">
        <Code2 className="dot-coder" size={18} />
        <span style={{ color: 'var(--color-coder)' }}>Coder Brain</span>
        <div style={{ flexGrow: 1 }}></div>
        <div className={`status-indicator ${activeRole === 'Coder' ? 'active-coder' : ''}`}></div>
      </div>
      <div className="editor-wrapper">
        <Editor
          height="100%"
          defaultLanguage="python"
          theme="vs-dark"
          value={coderCode}
          options={{
            readOnly: true,
            minimap: { enabled: false },
            fontSize: 13,
            fontFamily: "'Fira Code', monospace",
            lineNumbersMinChars: 2,
            scrollBeyondLastLine: false,
            wordWrap: "on"
          }}
        />
      </div>
    </motion.div>
  );
}
