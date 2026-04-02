import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { FolderTree, GitBranch, Settings, MessageSquare } from 'lucide-react';
import FileExplorer from './FileExplorer';

/**
 * LabWorkspace - 右侧实验室面板（Tab 式）
 * 替代原 ArtifactExplorer，提供可扩展的功能模块。
 */

const TABS = [
  { id: 'files', label: '文件浏览', icon: FolderTree, color: 'var(--color-manager)' },
  { id: 'git', label: 'Git 版本', icon: GitBranch, color: '#3bc7c7' },
  { id: 'settings', label: '模型设置', icon: Settings, color: '#f0db4f' },
  { id: 'pm', label: 'PM Chat', icon: MessageSquare, color: '#39ff14' },
];

export default function LabWorkspace({ projectFiles, currentProjectId }) {
  const [activeTab, setActiveTab] = useState('files');

  const renderTabContent = () => {
    switch (activeTab) {
      case 'files':
        return <FileExplorer projectFiles={projectFiles} currentProjectId={currentProjectId} />;
      case 'git':
        return (
          <div className="lab-placeholder">
            <GitBranch size={48} color="#3bc7c7" strokeWidth={1} />
            <h3>Git 版本管理</h3>
            <p>即将上线：查看 commit 历史、浏览 diff、版本回滚。</p>
            <div className="lab-placeholder-tag">Phase B</div>
          </div>
        );
      case 'settings':
        return (
          <div className="lab-placeholder">
            <Settings size={48} color="#f0db4f" strokeWidth={1} />
            <h3>模型设置</h3>
            <p>即将上线：Provider 管理、Agent 模型映射、API Key 配置。</p>
            <div className="lab-placeholder-tag">Phase C</div>
          </div>
        );
      case 'pm':
        return (
          <div className="lab-placeholder">
            <MessageSquare size={48} color="#39ff14" strokeWidth={1} />
            <h3>PM Agent Chat</h3>
            <p>即将上线：自然语言对话、需求澄清、规划审批。</p>
            <div className="lab-placeholder-tag">Phase D</div>
          </div>
        );
      default:
        return null;
    }
  };

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

      {/* Tab Content */}
      <div className="lab-content">
        <AnimatePresence mode="wait">
          <motion.div
            key={activeTab}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -8 }}
            transition={{ duration: 0.2 }}
            style={{ height: '100%' }}
          >
            {renderTabContent()}
          </motion.div>
        </AnimatePresence>
      </div>
    </motion.div>
  );
}
