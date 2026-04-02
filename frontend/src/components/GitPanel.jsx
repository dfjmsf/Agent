import React, { useState, useEffect, useCallback } from 'react';
import { GitBranch, GitCommit, RefreshCw, ChevronRight, Clock, FileText, Plus } from 'lucide-react';
import { fetchGitStatus, fetchGitLog, fetchGitDiff, initGitRepo } from '../services/api';

/**
 * GitPanel - Git 版本管理面板
 * 显示 commit 历史、点击查看 diff。
 */
export default function GitPanel({ currentProjectId }) {
  const [status, setStatus] = useState(null);
  const [commits, setCommits] = useState([]);
  const [selectedCommit, setSelectedCommit] = useState(null);
  const [diffText, setDiffText] = useState('');
  const [loading, setLoading] = useState(false);
  const [diffLoading, setDiffLoading] = useState(false);
  const [initLoading, setInitLoading] = useState(false);

  // 拉取 git 状态和 log
  const refresh = useCallback(async () => {
    if (!currentProjectId || currentProjectId === 'default_project') return;
    setLoading(true);
    try {
      const [statusData, logData] = await Promise.all([
        fetchGitStatus(currentProjectId),
        fetchGitLog(currentProjectId),
      ]);
      setStatus(statusData);
      setCommits(logData.commits || []);
    } catch (e) {
      console.error('Git refresh failed:', e);
      setStatus({ initialized: false });
      setCommits([]);
    } finally {
      setLoading(false);
    }
  }, [currentProjectId]);

  useEffect(() => {
    refresh();
    setSelectedCommit(null);
    setDiffText('');
  }, [currentProjectId, refresh]);

  // 查看 commit diff
  const handleSelectCommit = async (commit) => {
    setSelectedCommit(commit.hash);
    setDiffLoading(true);
    try {
      const data = await fetchGitDiff(currentProjectId, commit.hash);
      setDiffText(data.diff || '无 diff 输出');
    } catch (e) {
      setDiffText(`获取 diff 失败: ${e.message}`);
    } finally {
      setDiffLoading(false);
    }
  };

  // 手动初始化 git
  const handleInit = async () => {
    setInitLoading(true);
    try {
      await initGitRepo(currentProjectId);
      await refresh();
    } catch (e) {
      console.error('Git init failed:', e);
    } finally {
      setInitLoading(false);
    }
  };

  // diff 染色渲染
  const renderDiff = (text) => {
    return text.split('\n').map((line, i) => {
      let className = 'diff-line';
      if (line.startsWith('+') && !line.startsWith('+++')) className += ' diff-add';
      else if (line.startsWith('-') && !line.startsWith('---')) className += ' diff-del';
      else if (line.startsWith('@@')) className += ' diff-hunk';
      else if (line.startsWith('diff ') || line.startsWith('index ')) className += ' diff-meta';
      return <div key={i} className={className}>{line}</div>;
    });
  };

  // 未初始化状态
  if (status && !status.initialized && !loading) {
    return (
      <div className="git-panel">
        <div className="git-not-init">
          <GitBranch size={40} color="#3bc7c7" strokeWidth={1} />
          <h3>Git 未初始化</h3>
          <p>该项目尚未创建 Git 仓库。</p>
          <button className="git-init-btn" onClick={handleInit} disabled={initLoading}>
            <Plus size={14} />
            {initLoading ? '初始化中...' : '初始化 Git 仓库'}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="git-panel">
      {/* 左侧 Commit 列表 */}
      <div className="git-sidebar">
        <div className="git-sidebar-header">
          <GitBranch size={13} color="#3bc7c7" />
          <span>COMMITS</span>
          <button className="git-refresh-btn" onClick={refresh} disabled={loading}>
            <RefreshCw size={12} className={loading ? 'spinning' : ''} />
          </button>
        </div>
        <div className="git-commit-list">
          {commits.length === 0 ? (
            <div className="git-empty">
              <Clock size={18} color="var(--text-dim)" strokeWidth={1} />
              <span>{loading ? '加载中...' : '暂无 commit 记录'}</span>
            </div>
          ) : (
            commits.map((c) => (
              <div
                key={c.hash}
                className={`git-commit-item ${selectedCommit === c.hash ? 'active' : ''}`}
                onClick={() => handleSelectCommit(c)}
              >
                <div className="git-commit-row1">
                  <GitCommit size={12} color={selectedCommit === c.hash ? '#3bc7c7' : 'var(--text-dim)'} />
                  <span className="git-commit-hash">{c.short_hash}</span>
                  <ChevronRight size={12} className="git-commit-arrow" />
                </div>
                <div className="git-commit-msg">{c.message}</div>
                <div className="git-commit-meta">
                  <Clock size={10} /> {c.date?.split(' ').slice(0, 2).join(' ')}
                  {c.stats && <> · <FileText size={10} /> {c.stats}</>}
                </div>
              </div>
            ))
          )}
        </div>
      </div>

      {/* 右侧 Diff 预览 */}
      <div className="git-diff-area">
        {selectedCommit ? (
          <>
            <div className="git-diff-header">
              <span className="git-diff-title">
                Diff: {commits.find(c => c.hash === selectedCommit)?.short_hash}
              </span>
              <span className="git-diff-msg">
                {commits.find(c => c.hash === selectedCommit)?.message}
              </span>
            </div>
            <div className="git-diff-content">
              {diffLoading ? (
                <div className="git-diff-loading">加载 diff 中...</div>
              ) : (
                <pre className="git-diff-pre">{renderDiff(diffText)}</pre>
              )}
            </div>
          </>
        ) : (
          <div className="git-diff-welcome">
            <GitCommit size={36} color="#3bc7c7" strokeWidth={1} />
            <h3>版本变更</h3>
            <p>点击左侧 commit 查看代码 diff</p>
          </div>
        )}
      </div>
    </div>
  );
}
