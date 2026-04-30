import React, { useState } from 'react';
import { motion } from 'framer-motion';
import { FolderTree, GitBranch, Settings, MessageSquare } from 'lucide-react';
import FileExplorer from './FileExplorer';
import GitPanel from './GitPanel';
import ModelSettings from './ModelSettings';
import PMChat from './PMChat';

/**
 * LabWorkspace - 右侧实验室面板（Tab 式）
 *
 * 关键设计：所有 Tab 内容始终挂载（不卸载），通过 CSS display 控制可见性。
 * 这确保 PMChat 在切换到其他 Tab 时不会丢失正在飞行的 HTTP 响应。
 */

const TABS = [
  { id: 'files', label: '文件浏览', icon: FolderTree, color: 'var(--color-manager)' },
  { id: 'git', label: 'Git 版本', icon: GitBranch, color: '#3bc7c7' },
  { id: 'settings', label: '模型设置', icon: Settings, color: '#f0db4f' },
  { id: 'pm', label: 'PM Chat', icon: MessageSquare, color: '#39ff14' },
];

export default function LabWorkspace({ projectFiles, currentProjectId }) {
  const [activeTab, setActiveTab] = useState('files');

  return (
    <motion.div
      className="right-workspace"
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      transition={{ delay: 0.5 }}
    >
      {/* Tab Bar */}
      <div className="lab-tab-bar">
        {TABS.map(tab => {
          const Icon = tab.icon;
          const isActive = activeTab === tab.id;
          return (
            <button
              key={tab.id}
              className={`lab-tab ${isActive ? 'active' : ''}`}
              onClick={() => setActiveTab(tab.id)}
              style={{
                '--tab-color': tab.color,
              }}
            >
              <Icon size={14} color={isActive ? tab.color : 'var(--text-dim)'} />
              <span>{tab.label}</span>
              {isActive && (
                <motion.div
                  className="lab-tab-indicator"
                  layoutId="activeTab"
                  style={{ background: tab.color }}
                />
              )}
            </button>
          );
        })}
      </div>

      {/* Tab Content — 全部预挂载，CSS display 控制可见性 */}
      <div className="lab-content">
        <div style={{ height: '100%', display: activeTab === 'files' ? 'block' : 'none' }}>
          <FileExplorer projectFiles={projectFiles} currentProjectId={currentProjectId} />
        </div>
        <div style={{ height: '100%', display: activeTab === 'git' ? 'block' : 'none' }}>
          <GitPanel currentProjectId={currentProjectId} />
        </div>
        <div style={{ height: '100%', display: activeTab === 'settings' ? 'block' : 'none' }}>
          <ModelSettings />
        </div>
        <div style={{ height: '100%', display: activeTab === 'pm' ? 'block' : 'none' }}>
          <PMChat currentProjectId={currentProjectId} />
        </div>
      </div>
    </motion.div>
  );
}
