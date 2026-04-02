import React, { useState } from 'react';
import { Code2, FolderTree, FileText, Eye } from 'lucide-react';
import { loadFileContent as apiLoadFile } from '../services/api';

/**
 * FileExplorer - 简化版文件浏览器（取代 ArtifactExplorer）
 * 保留文件树 + 只读代码预览（用 <pre> 替代 Monaco Editor）
 */
export default function FileExplorer({ projectFiles, currentProjectId }) {
  const [activeFile, setActiveFile] = useState(null);
  const [fileContent, setFileContent] = useState(null);
  const [isLoading, setIsLoading] = useState(false);

  const loadFileContent = async (filePath, fileName) => {
    setIsLoading(true);
    try {
      const data = await apiLoadFile(filePath);
      if (data.error) {
        setFileContent(`// 获取文件失败: ${data.error}`);
      } else {
        setFileContent(data.content);
        setActiveFile({ path: filePath, name: fileName });
      }
    } catch (e) {
      setFileContent(`// 网络错误: ${e.message}`);
    } finally {
      setIsLoading(false);
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

  return (
    <div className="file-explorer">
      {/* 左侧文件树 */}
      <div className="fe-sidebar">
        <div className="fe-sidebar-header">
          <FolderTree size={13} color="var(--color-manager)" />
          <span>EXPLORER</span>
        </div>
        <div className="fe-sidebar-content">
          {projectFiles ? renderTree(projectFiles) : (
            <div className="fe-empty">
              <FileText size={24} color="var(--text-dim)" strokeWidth={1} />
              <span>选择项目后查看文件</span>
            </div>
          )}
        </div>
      </div>

      {/* 右侧代码预览 */}
      <div className="fe-preview">
        {activeFile ? (
          <>
            <div className="fe-preview-header">
              <Eye size={14} color="var(--color-manager)" />
              <span className="fe-preview-filename">{activeFile.name}</span>
              <span className="fe-preview-path">{activeFile.path}</span>
            </div>
            <div className="fe-preview-content">
              {isLoading ? (
                <div className="fe-loading">加载中...</div>
              ) : (
                <pre className="fe-code"><code>{fileContent}</code></pre>
              )}
            </div>
          </>
        ) : (
          <div className="fe-welcome">
            <Code2 size={40} color="var(--color-manager)" strokeWidth={1} />
            <h3>文件预览</h3>
            <p>点击左侧文件树中的文件查看内容</p>
          </div>
        )}
      </div>
    </div>
  );
}
