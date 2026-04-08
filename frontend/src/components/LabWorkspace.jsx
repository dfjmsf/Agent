import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import { FolderTree, GitBranch, Settings, MessageSquare } from 'lucide-react';
import FileExplorer from './FileExplorer';
import GitPanel from './GitPanel';
import ModelSettings from './ModelSettings';
import PMChat from './PMChat';

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
        return <GitPanel currentProjectId={currentProjectId} />;
      case 'settings':
        return <ModelSettings />;
      case 'pm':
        return <PMChat currentProjectId={currentProjectId} />;
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
