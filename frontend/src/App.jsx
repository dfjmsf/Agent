import React, { useState, useEffect, useRef } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import Editor from '@monaco-editor/react';
import {
  Terminal,
  Code2,
  Cpu,
  Play,
  SquareTerminal,
  CheckCircle2,
  XCircle,
  FolderTree,
  Paperclip,
  X
} from 'lucide-react';
import './index.css';

const WS_URL = "ws://127.0.0.1:8000/ws";
const API_URL = "http://127.0.0.1:8000/api/generate";

function App() {
  const [prompt, setPrompt] = useState("");
  const [isGenerating, setIsGenerating] = useState(false);
  const [tasks, setTasks] = useState([]);
  const [activeTask, setActiveTask] = useState("");

  // States targeted at specific panes
  const [coderCode, setCoderCode] = useState("// 正在聆听架构师指令...\n");
  const [reviewerLogs, setReviewerLogs] = useState([]);

  const [activeRole, setActiveRole] = useState(null); // 'Coder', 'Reviewer', 'Manager'

  const ws = useRef(null);
  const logsEndRef = useRef(null);

  // --- Artifact Explorer States ---
  const [projectFiles, setProjectFiles] = useState(null);
  const [activeFile, setActiveFile] = useState(null); // {path: "", name: ""}
  const [artifactCode, setArtifactCode] = useState("// 点击左侧目录树中的文件查看内容...");
  const [ideTerminalOutput, setIdeTerminalOutput] = useState("等待执行指令...\n");
  const [isIdeRunning, setIsIdeRunning] = useState(false);
  const [stdinInput, setStdinInput] = useState("");
  const [showStdinInput, setShowStdinInput] = useState(false);

  // Context Upload states
  const [attachedFiles, setAttachedFiles] = useState([]);
  const fileInputRef = useRef(null);

  useEffect(() => {
    connectWebSocket();
    fetchProjectFiles(); // Fetch immediately on load

    return () => {
      if (ws.current) ws.current.close();
    };
  }, []);

  const fetchProjectFiles = async () => {
    try {
      const res = await fetch("http://127.0.0.1:8000/api/project/files");
      if (res.ok) {
        const data = await res.json();
        setProjectFiles(data);
      }
    } catch (e) {
      console.error("Failed to fetch project files", e);
    }
  };

  const loadFileContent = async (filePath, fileName) => {
    try {
      const res = await fetch(`http://127.0.0.1:8000/api/project/file?path=${encodeURIComponent(filePath)}`);
      if (res.ok) {
        const data = await res.json();
        if (data.error) {
          setArtifactCode(`// 获取文件失败: ${data.error}`);
        } else {
          setArtifactCode(data.content);
          setActiveFile({ path: filePath, name: fileName });
        }
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
      const res = await fetch("http://127.0.0.1:8000/api/project/run", {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ code: artifactCode, stdin_data: stdinInput || null })
      });

      if (res.ok) {
        const data = await res.json();
        let outStr = `[${new Date().toLocaleTimeString()}] 执行完毕 (退出码: ${data.returncode})\n`;
        if (data.stdout) outStr += `\n[STDOUT]\n${data.stdout}\n`;
        if (data.stderr) outStr += `\n[STDERR]\n${data.stderr}\n`;
        setIdeTerminalOutput(outStr);
      } else {
        setIdeTerminalOutput(`[错误] API 请求失败 (HTTP ${res.status})`);
      }
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
      // File node
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

  useEffect(() => {
    // Auto scroll reviewer logs
    if (logsEndRef.current) {
      logsEndRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [reviewerLogs]);

  const connectWebSocket = () => {
    ws.current = new WebSocket(WS_URL);

    ws.current.onopen = () => {
      console.log("WebSocket connected");
      appendLog("System", "info", "系统已连接，等待分配任务...");
    };

    ws.current.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data);
        handleAgentMessage(msg);
      } catch (e) {
        console.error("Parse WS error", e);
      }
    };

    ws.current.onclose = () => {
      console.log("WebSocket disconnected. Reconnecting in 3s...");
      setTimeout(connectWebSocket, 3000);
    };
  };

  const handleAgentMessage = (msg) => {
    const { agent_role, action_type, content, payload } = msg;

    setActiveRole(agent_role);

    if (action_type === 'plan_ready') {
      const planTasks = payload.plan?.tasks || [];
      setTasks(planTasks);
      appendLog("Manager", "info", `规划完成: ${planTasks.length} 个任务`);
    }
    else if (action_type === 'task_start') {
      setActiveTask(payload.task?.task_id || "未知任务");
      appendLog("Manager", "info", content);
    }
    else if (action_type === 'coding_start') {
      setCoderCode(`// === 任务目标: ${payload.target || "未知"} ===\n// Coder AI 正在极简沉浸式输出纯净代码...\n// 请稍候...`);
    }
    else if (action_type === 'coding_done') {
      setCoderCode(payload.code || "// 编码完成但无有效代码返回");
      appendLog("Coder", "success", content);
    }
    else if (action_type === 'sandbox_start') {
      appendLog("Reviewer", "warning", content);
      appendLog("Reviewer", "info", `>> 执行脚本代码:\n${payload.test_code?.substring(0, 100)}...`);
    }
    else if (action_type === 'sandbox_end') {
      appendLog("Reviewer", "info", `>> 退出码: ${payload.result?.returncode}`);
      if (payload.result?.stderr) {
        appendLog("Reviewer", "error", payload.result.stderr);
      } else {
        appendLog("Reviewer", "success", payload.result?.stdout?.substring(0, 200));
      }
    }
    else if (action_type === 'review_pass') {
      appendLog("Reviewer", "success", "✓ 审核通过！合并入主分支。");
      setActiveRole("Manager"); // Return control
    }
    else if (action_type === 'review_fail') {
      appendLog("Reviewer", "error", `✗ 打回原稿！原因: ${payload.feedback}`);
      setActiveRole("Coder"); // Give control back
    }
    else if (action_type === 'file_tree_update') {
      fetchProjectFiles();
    }
    else if (action_type === 'success' || action_type === 'error') {
      setIsGenerating(false);
      appendLog("System", action_type === 'success' ? "success" : "error", content);
      setActiveRole(null);
      if (action_type === 'success') {
        fetchProjectFiles(); // Promptly fetch after success
      }
    }
    else {
      // generic logging
      appendLog(agent_role, "info", content);
    }
  };

  const appendLog = (role, type, text) => {
    const timestamp = new Date().toLocaleTimeString();
    setReviewerLogs(prev => [...prev, { id: Date.now() + Math.random(), timestamp, role, type, text }]);
  };

  const handleStart = async () => {
    if (!prompt.trim()) return;
    setIsGenerating(true);
    setTasks([]);
    setReviewerLogs([]);
    setCoderCode("// 正在加载全局架构配置...\n");
    setActiveTask("");

    let finalPrompt = prompt;
    if (attachedFiles.length > 0) {
      const contextStr = attachedFiles.map(f => 
        `【挂载的安全上下文文件】\n文件绝对路径: ${f.path}\n数据结构预览 (局部): \n${f.preview}\n`
      ).join('\n---------------------\n');
      finalPrompt = `${contextStr}\n\n用户需求:\n${prompt}`;
    }

    try {
      const res = await fetch(API_URL, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({ prompt: finalPrompt })
      });
      if (!res.ok) {
        throw new Error("API Connection Failed");
      }
    } catch (e) {
      setIsGenerating(false);
      appendLog("System", "error", `无法联系到引擎：${e.message}`);
    }
  };

  const handleFileUpload = async (e) => {
    const files = Array.from(e.target.files);
    if (!files || files.length === 0) return;

    for (const file of files) {
      appendLog("System", "info", `正在上传并解析文件: ${file.name}...`);
      const formData = new FormData();
      formData.append('file', file);

      try {
        const res = await fetch("http://127.0.0.1:8000/api/upload", {
          method: 'POST',
          body: formData
        });

        if (res.ok) {
          const data = await res.json();
          if (data.error) {
             appendLog("System", "error", `上传失败: ${data.error}`);
          } else {
             setAttachedFiles(prev => [...prev, data]);
             appendLog("System", "success", `文件已挂载：获得 Schema 视图`);
          }
        } else {
          appendLog("System", "error", `上传接口报错 HTTP ${res.status}`);
        }
      } catch (err) {
        appendLog("System", "error", `上传崩溃: ${err.message}`);
      }
    }
    // reset input
    if (fileInputRef.current) fileInputRef.current.value = "";
  };

  return (
    <div className="app-container">

      {/* --- Global Title Banner --- */}
      <motion.div
        className="global-title-bar"
        initial={{ y: -50, opacity: 0 }}
        animate={{ y: 0, opacity: 1 }}
      >
        <div className="title">PROJECT.A.G.E.N.T</div>
      </motion.div>

      <div className="main-content-row">
        {/* --- Left Sidebar: Agents & Controls --- */}
        <div className="left-sidebar">

          {/* Task Tree Pane */}
          <motion.div
            className="glass-panel col-tree"
            initial={{ x: -50, opacity: 0 }}
            animate={{ x: 0, opacity: 1 }}
            transition={{ delay: 0.1 }}
          >
            <div className="panel-header">
              <FolderTree className="dot-manager" size={18} />
              <span style={{ color: 'var(--color-manager)' }}>VFS & Task Tree</span>
            </div>
            <div className="tree-content">
              {tasks.length === 0 ? (
                <div style={{ color: '#555', fontSize: 13 }}>等待 Manager 规划架构...</div>
              ) : (
                tasks.map((task, idx) => (
                  <div
                    key={idx}
                    className={`tree-item ${activeTask === task.task_id ? 'active' : ''}`}
                  >
                    {task.target_file}
                  </div>
                ))
              )}
            </div>
          </motion.div>

          {/* Coder Brain Pane */}
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

          {/* Reviewer Sandbox Pane */}
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

          {/* Bottom Input Area */}
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
                    <button className="remove-btn" onClick={() => setAttachedFiles(prev => prev.filter((_, idx) => idx !== i))}>
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
              onChange={e => setPrompt(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault();
                  handleStart();
                }
              }}
              disabled={isGenerating}
            />
            
            <div className="input-actions-row">
              <input 
                type="file" 
                ref={fileInputRef} 
                style={{display: "none"}} 
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
                onClick={handleStart}
                disabled={isGenerating || !prompt}
              >
                {isGenerating ? "EXECUTING..." : "GENERATE"}
              </button>
            </div>
          </motion.div>

        </div>

        {/* --- Right Workspace: Mini VSCode (Artifacts & Output) --- */}
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
                  defaultLanguage={activeFile?.name.endsWith('.js') ? 'javascript' : activeFile?.name.endsWith('.css') ? 'css' : 'python'}
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
      </div>
    </div>
  );
}

export default App;
