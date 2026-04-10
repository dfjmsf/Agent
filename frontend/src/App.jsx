import React, { useState, useEffect, useCallback, useRef } from 'react';
import { motion } from 'framer-motion';
import { FolderTree } from 'lucide-react';
import './index.css';

// --- 子组件 ---
import ProjectSelector from './components/ProjectSelector';
import CoderPanel from './components/CoderPanel';
import ReviewerPanel from './components/ReviewerPanel';
import LabWorkspace from './components/LabWorkspace';
import PromptInput from './components/PromptInput';

// --- 基础设施 ---
import useWebSocket from './hooks/useWebSocket';
import { fetchProjectsList, fetchProjectFiles, startGeneration } from './services/api';

function App() {
  const [prompt, setPrompt] = useState("");
  const [isGenerating, setIsGenerating] = useState(false);
  const [tasks, setTasks] = useState([]);
  const [activeTask, setActiveTask] = useState("");

  // Project Management States
  const [projectsList, setProjectsList] = useState([]);
  const [currentProjectId, setCurrentProjectId] = useState("default_project");

  // States targeted at specific panes
  const [coderCode, setCoderCode] = useState("// 正在聆听架构师指令...\n");
  const [reviewerLogs, setReviewerLogs] = useState([]);

  const [activeRole, setActiveRole] = useState(null);

  // Artifact Explorer States
  const [projectFiles, setProjectFiles] = useState(null);

  // Context Upload states
  const [attachedFiles, setAttachedFiles] = useState([]);

  // Ref to avoid stale closures in WebSocket callback
  const currentProjectIdRef = useRef(currentProjectId);
  useEffect(() => {
    currentProjectIdRef.current = currentProjectId;
  }, [currentProjectId]);

  // --- 日志追加 ---
  const appendLog = useCallback((role, type, text) => {
    const timestamp = new Date().toLocaleTimeString();
    setReviewerLogs(prev => {
      const newLogs = [...prev, { id: Date.now() + Math.random(), timestamp, role, type, text }];
      if (currentProjectIdRef.current && currentProjectIdRef.current !== "default_project") {
        localStorage.setItem(`reviewerLogs_${currentProjectIdRef.current}`, JSON.stringify(newLogs));
      }
      return newLogs;
    });
  }, []);

  // --- 加载历史日志 ---
  useEffect(() => {
    if (currentProjectId !== "default_project") {
      const saved = localStorage.getItem(`reviewerLogs_${currentProjectId}`);
      if (saved) {
        try {
          setReviewerLogs(JSON.parse(saved));
        } catch(e) {
          setReviewerLogs([]);
        }
      } else {
        setReviewerLogs([]);
      }
    }
  }, [currentProjectId]);

  // --- 拉取项目文件树 ---
  const refreshProjectFiles = useCallback(async (projectId) => {
    try {
      const data = await fetchProjectFiles(projectId);
      setProjectFiles(data);
    } catch (e) {
      console.error("Failed to fetch project files", e);
    }
  }, []);

  // --- WebSocket 消息处理 ---
  const handleAgentMessage = useCallback((msg) => {
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
      setActiveRole("Manager");
    }
    else if (action_type === 'project_renamed') {
      const { old_id, new_id } = payload;
      setProjectsList(prev => prev.map(p => (p === old_id ? new_id : p)));
      if (currentProjectIdRef.current === old_id || currentProjectIdRef.current === 'default_project') {
        setCurrentProjectId(new_id);
      }
      appendLog("System", "success", content);
    }
    else if (action_type === 'file_tree_update') {
      refreshProjectFiles(currentProjectIdRef.current);
    }
    else if (action_type === 'success' || action_type === 'error') {
      setIsGenerating(false);
      appendLog("System", action_type === 'success' ? "success" : "error", content);
      setActiveRole(null);
      if (action_type === 'success') {
        refreshProjectFiles(currentProjectIdRef.current);
      }
    }
    else {
      appendLog(agent_role, "info", content);
    }
  }, [appendLog, refreshProjectFiles]);

  // --- 初始化 WebSocket ---
  const { isConnected } = useWebSocket(handleAgentMessage);

  // WebSocket 连接状态日志
  useEffect(() => {
    if (isConnected) {
      appendLog("System", "info", "系统已连接，等待分配任务...");
    }
  }, [isConnected, appendLog]);

  // --- 初始化：拉取项目列表 ---
  useEffect(() => {
    (async () => {
      try {
        const data = await fetchProjectsList();
        setProjectsList(data);
        if (data.length > 0 && currentProjectId === "default_project") {
          setCurrentProjectId(data[0]);
        }
      } catch (e) {
        console.error("Failed to fetch projects list", e);
      }
    })();
  }, []);

  // --- 项目切换时仅刷新文件树 (不清空状态，防止 rename/初始化时误清) ---
  useEffect(() => {
    if (currentProjectId !== "default_project" || projectsList.length > 0) {
      refreshProjectFiles(currentProjectId);
    }
  }, [currentProjectId, refreshProjectFiles]);

  // --- 用户手动切换项目时清空状态 ---
  const handleProjectChange = useCallback((newProjectId) => {
    if (newProjectId === currentProjectId) return;
    setCurrentProjectId(newProjectId);
    setTasks([]);
    setCoderCode("// 正在聆听架构师指令...\n");
    // reviewerLogs 会在副作用中自动加载目标项目的独立日志
    setActiveTask("");
  }, [currentProjectId]);

  // --- 生成按钮 ---
  const handleStart = async () => {
    if (!prompt.trim()) return;
    setIsGenerating(true);
    setTasks([]);
    // 当开始全新生成时，清空当前项目的沙盒日志
    setReviewerLogs([]);
    if (currentProjectId !== "default_project") {
      localStorage.removeItem(`reviewerLogs_${currentProjectId}`);
    }
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
      await startGeneration(finalPrompt, currentProjectId);
    } catch (e) {
      setIsGenerating(false);
      appendLog("System", "error", `无法联系到引擎：${e.message}`);
    }
  };

  // --- 新建项目回调 ---
  const handleProjectCreated = (newId) => {
    setProjectsList(prev => [newId, ...prev]);
    setCurrentProjectId(newId);
  };

  return (
    <div className="app-container">

      {/* --- Global Title Banner --- */}
      <motion.div
        className="global-title-bar"
        initial={{ y: -50, opacity: 0 }}
        animate={{ y: 0, opacity: 1 }}
      >
        <div className="title">ASTrea.A.G.E.N.T</div>
        <ProjectSelector
          projectsList={projectsList}
          currentProjectId={currentProjectId}
          onProjectChange={handleProjectChange}
          onProjectCreated={handleProjectCreated}
          onLog={appendLog}
        />
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
          <CoderPanel coderCode={coderCode} activeRole={activeRole} />

          {/* Reviewer Sandbox Pane */}
          <ReviewerPanel reviewerLogs={reviewerLogs} activeRole={activeRole} />

          {/* Bottom Input Area */}
          <PromptInput
            prompt={prompt}
            onPromptChange={setPrompt}
            attachedFiles={attachedFiles}
            onAttachedFilesChange={setAttachedFiles}
            isGenerating={isGenerating}
            onGenerate={handleStart}
            onLog={appendLog}
          />

        </div>

        {/* --- Right Workspace: Lab (Tabs) --- */}
        <LabWorkspace projectFiles={projectFiles} currentProjectId={currentProjectId} />
      </div>
    </div>
  );
}

export default App;
