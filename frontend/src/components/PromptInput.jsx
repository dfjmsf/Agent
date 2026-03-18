import React, { useRef } from 'react';
import { motion } from 'framer-motion';
import { Paperclip, X } from 'lucide-react';
import { uploadContextFile } from '../services/api';

/**
 * 底部输入区 - Prompt 输入框 + 附件管理 + GENERATE 按钮
 */
export default function PromptInput({
  prompt,
  onPromptChange,
  attachedFiles,
  onAttachedFilesChange,
  isGenerating,
  onGenerate,
  onLog
}) {
  const fileInputRef = useRef(null);

  const handleFileUpload = async (e) => {
    const files = Array.from(e.target.files);
    if (!files || files.length === 0) return;

    for (const file of files) {
      onLog("System", "info", `正在上传并解析文件: ${file.name}...`);
      try {
        const data = await uploadContextFile(file);
        if (data.error) {
          onLog("System", "error", `上传失败: ${data.error}`);
        } else {
          onAttachedFilesChange(prev => [...prev, data]);
          onLog("System", "success", `文件已挂载：获得 Schema 视图`);
        }
      } catch (err) {
        onLog("System", "error", `上传崩溃: ${err.message}`);
      }
    }
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  return (
    <motion.div
      className="glass-panel bottom-input-bar"
      initial={{ y: 50, opacity: 0 }}
      animate={{ y: 0, opacity: 1 }}
      transition={{ delay: 0.4 }}
    >
      {attachedFiles.length > 0 && (
        <div className="attached-files-container">
          {attachedFiles.map((f, i) => (
            <div key={i} className="attached-file-pill">
              <Paperclip size={12} />
              <span className="file-name">{f.filename}</span>
              <button
                className="remove-btn"
                onClick={() => onAttachedFilesChange(prev => prev.filter((_, idx) => idx !== i))}
              >
                <X size={12} />
              </button>
            </div>
          ))}
        </div>
      )}
      <textarea
        className="prompt-input"
        placeholder="指派新任务... (Shift+Enter 换行，Enter 发送)"
        value={prompt}
        onChange={e => onPromptChange(e.target.value)}
        onKeyDown={e => {
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            onGenerate();
          }
        }}
        disabled={isGenerating}
      />

      <div className="input-actions-row">
        <input
          type="file"
          ref={fileInputRef}
          style={{ display: "none" }}
          onChange={handleFileUpload}
          multiple
        />
        <button
          className="btn-attach"
          onClick={() => fileInputRef.current?.click()}
          disabled={isGenerating}
          title="挂载数据 / 文本资料"
        >
          <Paperclip size={18} />
        </button>

        <button
          className="btn-generate"
          style={{ flex: 1 }}
          onClick={onGenerate}
          disabled={isGenerating || !prompt}
        >
          {isGenerating ? "EXECUTING..." : "GENERATE"}
        </button>
      </div>
    </motion.div>
  );
}
