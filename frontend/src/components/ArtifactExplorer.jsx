import React, { useState } from 'react';
import { motion } from 'framer-motion';
import Editor from '@monaco-editor/react';
import { Terminal, Code2, Cpu, Play, FolderTree } from 'lucide-react';
import { loadFileContent as apiLoadFile, runProjectCode, getLanguageFromFilename } from '../services/api';

/**
 * Artifact Explorer - 右侧工作区 (文件树 + Monaco Editor + Terminal + STDIN)
 */
export default function ArtifactExplorer({ projectFiles, currentProjectId }) {
  const [activeFile, setActiveFile] = useState(null);
  const [artifactCode, setArtifactCode] = useState("// 点击左侧目录树中的文件查看内容...");
  const [ideTerminalOutput, setIdeTerminalOutput] = useState("等待执行指令...\n");
  const [isIdeRunning, setIsIdeRunning] = useState(false);
  const [stdinInput, setStdinInput] = useState("");
  const [showStdinInput, setShowStdinInput] = useState(false);

  const loadFileContent = async (filePath, fileName) => {
    try {
      const data = await apiLoadFile(filePath);
      if (data.error) {
        setArtifactCode(`// 获取文件失败: ${data.error}`);
      } else {
        setArtifactCode(data.content);
        setActiveFile({ path: filePath, name: fileName });
      }
    } catch (e) {
      console.error("Failed to load file content", e);
    }
  };

  const handleRunArtifactCode = async () => {
    if (!artifactCode || artifactCode.startsWith("// 点击") || isIdeRunning) return;

    setIsIdeRunning(true);
    setIdeTerminalOutput(`[${new Date().toLocaleTimeString()}] 正在沙盒中隔离执行代码...\n`);

    try {
      const data = await runProjectCode(artifactCode, currentProjectId, stdinInput || null);
      let outStr = `[${new Date().toLocaleTimeString()}] 执行完毕 (退出码: ${data.returncode})\n`;
      if (data.stdout) outStr += `\n[STDOUT]\n${data.stdout}\n`;
      if (data.stderr) outStr += `\n[STDERR]\n${data.stderr}\n`;
      setIdeTerminalOutput(outStr);
    } catch (e) {
      setIdeTerminalOutput(`[致命错误] 无法连接到执行引擎: ${e.message}`);
    } finally {
      setIsIdeRunning(false);
    }
  };

  // 递归渲染文件树
  const renderTree = (node) => {
    if (node.type === 'directory') {
      return (
        <div key={node.path} style={{ marginLeft: 10 }}>
          <div className="folder-node">
            <FolderTree size={14} color="#f0db4f" /> {node.name}
          </div>
          <div>
            {node.children && node.children.map(child => renderTree(child))}
          </div>
        </div>
      );
    } else {
      const isActive = activeFile?.path === node.path;
      return (
        <div
          key={node.path}
          className={`file-node ${isActive ? 'active' : ''}`}
          style={{ marginLeft: 25 }}
          onClick={() => loadFileContent(node.path, node.name)}
        >
          <Code2 size={13} color={isActive ? "var(--color-manager)" : "#8b92a5"} /> {node.name}
        </div>
      );
    }
  };

  const editorLanguage = getLanguageFromFilename(activeFile?.name);

  return (
    <motion.div
      className="right-workspace"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ delay: 0.5 }}
    >
      <div className="ide-toolbar">
        <Terminal size={16} color="var(--color-manager)" />
        <span>ARTIFACT EXPLORER & SANDBOX</span>
        <button
          className="run-btn"
          onClick={handleRunArtifactCode}
          disabled={isIdeRunning || !activeFile}
        >
          <Play size={14} fill="currentColor" />
          {isIdeRunning ? "EXECUTING..." : "RUN"}
        </button>
      </div>

      <div className="ide-main">
        {/* Left File Tree */}
        <div className="ide-sidebar">
          <div className="ide-sidebar-header">
            <span>EXPLORER</span>
          </div>
          <div className="ide-sidebar-content">
            {projectFiles ? renderTree(projectFiles) : (
              <div style={{ padding: 15, color: '#555', fontSize: 13 }}>加载目录中...</div>
            )}
          </div>
        </div>

        {/* Right Editor & Terminal */}
        <div className="ide-editor-area">
          <div className="ide-tabs">
            <div className="ide-tab">
              <Code2 size={13} color="var(--color-manager)" />
              {activeFile ? activeFile.name : 'Welcome'}
            </div>
          </div>

          <div className="ide-editor-container">
            <Editor
              height="100%"
              language={editorLanguage}
              theme="vs-dark"
              value={artifactCode}
              onChange={(value) => setArtifactCode(value)}
              options={{
                readOnly: false,
                minimap: { enabled: true },
                fontSize: 14,
                fontFamily: "'Fira Code', monospace",
                scrollBeyondLastLine: false,
              }}
            />
          </div>

          <div className="ide-terminal">
            <div className="ide-terminal-header">
              <Cpu size={14} color="var(--color-coder)" /> &nbsp; EXECUTION TERMINAL
              <button
                className="stdin-toggle-btn"
                onClick={() => setShowStdinInput(!showStdinInput)}
              >
                {showStdinInput ? '▼ STDIN' : '▶ STDIN'}
              </button>
            </div>
            {showStdinInput && (
              <div className="stdin-input-area">
                <textarea
                  className="stdin-textarea"
                  placeholder="在此输入程序所需的 stdin 数据（每行对应一次 input() 调用）..."
                  value={stdinInput}
                  onChange={e => setStdinInput(e.target.value)}
                  rows={3}
                />
              </div>
            )}
            <div className="ide-terminal-content">
              {ideTerminalOutput}
            </div>
          </div>
        </div>
      </div>
    </motion.div>
  );
}
