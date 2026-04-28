import React from 'react';
import { FolderTree } from 'lucide-react';
import { createNewProject, graduateProject } from '../services/api';

/**
 * 项目选择器组件 - 项目下拉选择 + 新建宇宙按钮 + 归档经验按钮
 */
export default function ProjectSelector({
  projectsList,
  currentProjectId,
  onProjectChange,
  onProjectCreated,
  onLog
}) {
  const handleNewProject = async () => {
    try {
      const data = await createNewProject("new_project");
      const newId = data.project_id;
      onProjectCreated(newId);
      onLog("System", "success", `成功开辟新宇宙: ${newId}`);
    } catch (e) {
      onLog("System", "error", `新建宇宙异常: ${e.message}`);
    }
  };

  const handleGraduate = async () => {
    if (!currentProjectId || currentProjectId === 'default_project') {
      onLog("System", "warning", "请先选择一个有效的项目");
      return;
    }
    const confirmed = window.confirm(
      `确认将「${currentProjectId}」的经验交付为全局智慧？\n\n` +
      `本项目的踩坑经验将升级为全局通用智慧，永久保留。\n` +
      `此操作不可撤销。`
    );
    if (!confirmed) return;

    try {
      const data = await graduateProject(currentProjectId);
      onLog("System", "success", `交付完成！${data.graduated_count} 条项目经验已升级为全局智慧`);
    } catch (e) {
      onLog("System", "error", `交付失败: ${e.message}`);
    }
  };

  return (
    <div className="project-selector-wrapper">
      <span className="project-label">当前宇宙:</span>
      <select
        value={currentProjectId}
        onChange={(e) => onProjectChange(e.target.value)}
        className="project-select"
      >
        {projectsList.map(p => <option key={p} value={p}>{p}</option>)}
        {projectsList.length === 0 && currentProjectId === 'default_project' && (
          <option value="default_project">default_project</option>
        )}
        {projectsList.length === 0 && currentProjectId !== 'default_project' && (
          <option value={currentProjectId}>{currentProjectId}</option>
        )}
      </select>
      <button className="btn-new-project" onClick={handleNewProject}>+ 新建宇宙</button>
      <button className="btn-graduate" onClick={handleGraduate} title="将本项目的经验交付为全局智慧">项目交付</button>
    </div>
  );
}
