/**
 * API 服务层 - 统一管理所有后端接口调用
 * URL 从 Vite 环境变量读取，支持零配置部署
 */

const API_BASE = import.meta.env.VITE_API_BASE_URL || 'http://127.0.0.1:8000';

// 自动推导 WebSocket URL (http→ws, https→wss)
const wsProtocol = API_BASE.startsWith('https') ? 'wss' : 'ws';
const wsHost = API_BASE.replace(/^https?:\/\//, '');
export const WS_URL = `${wsProtocol}://${wsHost}/ws`;
export const API_URL = `${API_BASE}/api/generate`;

export async function fetchProjectsList() {
  const res = await fetch(`${API_BASE}/api/projects`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function fetchProjectFiles(projectId) {
  const res = await fetch(`${API_BASE}/api/project/files?project_id=${projectId}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function loadFileContent(filePath) {
  const res = await fetch(`${API_BASE}/api/project/file?path=${encodeURIComponent(filePath)}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function createNewProject(projectName) {
  const res = await fetch(`${API_BASE}/api/project/new`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_name: projectName })
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function startGeneration(prompt, projectId) {
  const res = await fetch(API_URL, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ prompt, project_id: projectId })
  });
  if (!res.ok) throw new Error('API Connection Failed');
  return res.json();
}

export async function uploadContextFile(file) {
  const formData = new FormData();
  formData.append('file', file);
  const res = await fetch(`${API_BASE}/api/upload`, {
    method: 'POST',
    body: formData
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function runProjectCode(code, projectId, stdinData = null) {
  const res = await fetch(`${API_BASE}/api/project/run`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code, project_id: projectId, stdin_data: stdinData })
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function graduateProject(projectId) {
  const res = await fetch(`${API_BASE}/api/project/graduate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_id: projectId })
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// --- Git 版本管理 API ---

export async function fetchGitStatus(projectId) {
  const res = await fetch(`${API_BASE}/api/project/git/status?project_id=${encodeURIComponent(projectId)}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function fetchGitLog(projectId, maxCount = 30) {
  const res = await fetch(`${API_BASE}/api/project/git/log?project_id=${encodeURIComponent(projectId)}&max_count=${maxCount}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function fetchGitDiff(projectId, commitHash) {
  const res = await fetch(`${API_BASE}/api/project/git/diff?project_id=${encodeURIComponent(projectId)}&commit=${encodeURIComponent(commitHash)}`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function initGitRepo(projectId) {
  const res = await fetch(`${API_BASE}/api/project/git/init`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_id: projectId })
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// --- 模型配置 API ---

export async function fetchModelConfig() {
  const res = await fetch(`${API_BASE}/api/config/models`);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

export async function updateModelConfig(config) {
  const res = await fetch(`${API_BASE}/api/config/models`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ config })
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

/**
 * 根据文件名推断 Monaco Editor 的语言标识
 */
export function getLanguageFromFilename(filename) {
  if (!filename) return 'python';
  const ext = filename.split('.').pop()?.toLowerCase();
  const map = {
    'py': 'python',
    'js': 'javascript',
    'jsx': 'javascript',
    'ts': 'typescript',
    'tsx': 'typescript',
    'html': 'html',
    'htm': 'html',
    'css': 'css',
    'json': 'json',
    'md': 'markdown',
    'sql': 'sql',
    'yaml': 'yaml',
    'yml': 'yaml',
    'xml': 'xml',
    'sh': 'shell',
    'bash': 'shell',
    'txt': 'plaintext',
    'ini': 'ini',
    'toml': 'ini',
    'cfg': 'ini',
  };
  return map[ext] || 'plaintext';
}
