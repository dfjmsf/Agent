import React, { useState, useEffect, useCallback } from 'react';
import { Settings, Save, RefreshCw, Cpu, Check, AlertCircle } from 'lucide-react';
import { fetchModelConfig, updateModelConfig } from '../services/api';

/**
 * ModelSettings - 模型配置面板
 * 展示各 Agent 角色的当前模型，支持下拉切换和保存。
 */

// Agent 角色图标颜色
const ROLE_COLORS = {
  MODEL_PLANNER: '#b14aed',
  MODEL_CODER: '#3fb950',
  MODEL_REVIEWER: '#f0883e',
  MODEL_SYNTHESIZER: '#3bc7c7',
  MODEL_AUDITOR: '#f85149',
};

export default function ModelSettings() {
  const [config, setConfig] = useState(null);
  const [editedConfig, setEditedConfig] = useState({});
  const [allModels, setAllModels] = useState([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saveResult, setSaveResult] = useState(null); // 'ok' | 'error'

  const loadConfig = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchModelConfig();
      setConfig(data);

      // 初始化编辑状态
      const initial = {};
      for (const [key, val] of Object.entries(data.agents)) {
        initial[key] = val.model;
      }
      setEditedConfig(initial);

      // 聚合所有可用模型
      const models = new Set();
      for (const p of data.providers) {
        for (const m of p.models) {
          models.add(m);
        }
      }
      setAllModels([...models].sort());
    } catch (e) {
      console.error('Failed to load model config:', e);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadConfig();
  }, [loadConfig]);

  const handleModelChange = (role, model) => {
    setEditedConfig(prev => ({ ...prev, [role]: model }));
    setSaveResult(null);
  };

  const hasChanges = () => {
    if (!config) return false;
    return Object.entries(editedConfig).some(
      ([key, val]) => config.agents[key]?.model !== val
    );
  };

  const handleSave = async () => {
    if (!hasChanges()) return;
    setSaving(true);
    setSaveResult(null);
    try {
      // 只发送变更的字段
      const changes = {};
      for (const [key, val] of Object.entries(editedConfig)) {
        if (config.agents[key]?.model !== val) {
          changes[key] = val;
        }
      }
      const result = await updateModelConfig(changes);
      if (result.status === 'ok') {
        setSaveResult('ok');
        await loadConfig(); // 刷新
        setTimeout(() => setSaveResult(null), 3000);
      } else {
        setSaveResult('error');
      }
    } catch (e) {
      console.error('Failed to save model config:', e);
      setSaveResult('error');
    } finally {
      setSaving(false);
    }
  };

  // 找到模型所属的 Provider
  const getProviderForModel = (modelName) => {
    if (!config) return null;
    for (const p of config.providers) {
      if (p.models.includes(modelName.toLowerCase())) return p.name;
    }
    return '未知';
  };

  if (loading) {
    return (
      <div className="ms-loading">
        <RefreshCw size={20} className="spinning" color="var(--text-dim)" />
        <span>加载模型配置...</span>
      </div>
    );
  }

  if (!config) {
    return (
      <div className="ms-loading">
        <AlertCircle size={20} color="#f85149" />
        <span>无法加载模型配置</span>
      </div>
    );
  }

  return (
    <div className="ms-panel">
      {/* Header */}
      <div className="ms-header">
        <Settings size={14} color="#3bc7c7" />
        <span className="ms-header-title">模型配置</span>
        <div className="ms-header-actions">
          <button className="ms-refresh-btn" onClick={loadConfig}>
            <RefreshCw size={12} />
          </button>
          <button
            className={`ms-save-btn ${hasChanges() ? 'active' : ''} ${saveResult === 'ok' ? 'saved' : ''}`}
            onClick={handleSave}
            disabled={!hasChanges() || saving}
          >
            {saving ? <RefreshCw size={12} className="spinning" /> :
             saveResult === 'ok' ? <Check size={12} /> : <Save size={12} />}
            {saving ? '保存中...' : saveResult === 'ok' ? '已保存' : '保存变更'}
          </button>
        </div>
      </div>

      {/* Provider 概览 */}
      <div className="ms-providers">
        <div className="ms-section-label">可用 PROVIDERS</div>
        <div className="ms-provider-list">
          {config.providers.map((p) => (
            <div key={p.name} className="ms-provider-chip">
              <Cpu size={11} />
              <span>{p.name}</span>
              <span className="ms-provider-count">{p.models.length}</span>
            </div>
          ))}
        </div>
      </div>

      {/* Agent 模型映射 */}
      <div className="ms-agents">
        <div className="ms-section-label">AGENT 模型映射</div>
        {Object.entries(config.agents).map(([role, info]) => {
          const currentModel = editedConfig[role] || info.model;
          const changed = info.model !== currentModel;
          return (
            <div key={role} className={`ms-agent-row ${changed ? 'changed' : ''}`}>
              <div className="ms-agent-info">
                <div
                  className="ms-agent-dot"
                  style={{ background: ROLE_COLORS[role] || '#888' }}
                />
                <div className="ms-agent-label">
                  <span className="ms-agent-name">{info.label}</span>
                  <span className="ms-agent-provider">
                    {getProviderForModel(currentModel)}
                  </span>
                </div>
              </div>
              <select
                className="ms-model-select"
                value={currentModel}
                onChange={(e) => handleModelChange(role, e.target.value)}
              >
                {allModels.map((m) => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </select>
            </div>
          );
        })}
      </div>

      {/* 提示 */}
      <div className="ms-footer">
        <span>💡 修改后立即生效于下一次项目生成，无需重启服务器。</span>
      </div>
    </div>
  );
}
