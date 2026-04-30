import React, { useState, useEffect, useCallback } from 'react';
import { Settings, Save, RefreshCw, Cpu, Check, AlertCircle, Plus, Trash2, X } from 'lucide-react';
import { fetchModelConfig, updateModelConfig, fetchCustomProviders, addCustomProvider, deleteCustomProvider } from '../services/api';

/**
 * ModelSettings - 模型配置面板
 * 展示各 Agent 角色的当前模型，支持下拉切换、思考模式 Toggle 和自定义 Provider 管理。
 */

// Agent 角色图标颜色
const ROLE_COLORS = {
  MODEL_PLANNER: '#b14aed',
  MODEL_CODER: '#3fb950',
  MODEL_REVIEWER: '#f0883e',
  MODEL_SYNTHESIZER: '#3bc7c7',
  MODEL_AUDITOR: '#f85149',
  MODEL_PM: '#58a6ff',
  MODEL_PLANNER_LITE: '#d2a8ff',
  MODEL_TECH_LEAD: '#f778ba',
  MODEL_QA: '#e3b341',
  MODEL_QA_VISION: '#ff7b72',
};

export default function ModelSettings() {
  const [config, setConfig] = useState(null);
  const [editedConfig, setEditedConfig] = useState({});
  const [editedThinking, setEditedThinking] = useState({});
  // 按供应商分组的模型列表: [{ provider: string, models: string[] }]
  const [groupedModels, setGroupedModels] = useState([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saveResult, setSaveResult] = useState(null); // 'ok' | 'error'

  // 自定义 Provider 管理
  const [customProviders, setCustomProviders] = useState([]);
  const [showAddForm, setShowAddForm] = useState(false);
  const [newProvider, setNewProvider] = useState({ name: '', api_key: '', base_url: '', models: '' });
  const [addingProvider, setAddingProvider] = useState(false);

  const loadConfig = useCallback(async () => {
    setLoading(true);
    try {
      const data = await fetchModelConfig();
      setConfig(data);

      // 初始化编辑状态
      const initialModels = {};
      const initialThinking = {};
      for (const [key, val] of Object.entries(data.agents)) {
        initialModels[key] = val.model;
        initialThinking[key] = val.thinking || 'false';
      }
      setEditedConfig(initialModels);
      setEditedThinking(initialThinking);

      // 按供应商分组聚合模型
      const groups = data.providers
        .filter(p => p.models.length > 0)
        .map(p => ({
          provider: p.name,
          models: [...p.models].sort(),
        }));
      setGroupedModels(groups);
    } catch (e) {
      console.error('Failed to load model config:', e);
    } finally {
      setLoading(false);
    }
  }, []);

  const loadCustomProviders = useCallback(async () => {
    try {
      const data = await fetchCustomProviders();
      setCustomProviders(data.providers || []);
    } catch (e) {
      console.error('Failed to load custom providers:', e);
    }
  }, []);

  useEffect(() => {
    loadConfig();
    loadCustomProviders();
  }, [loadConfig, loadCustomProviders]);

  const handleModelChange = (role, model) => {
    setEditedConfig(prev => ({ ...prev, [role]: model }));
    setSaveResult(null);
  };

  const handleThinkingChange = (role, value) => {
    setEditedThinking(prev => ({ ...prev, [role]: value }));
    setSaveResult(null);
  };

  const hasChanges = () => {
    if (!config) return false;
    const modelChanged = Object.entries(editedConfig).some(
      ([key, val]) => config.agents[key]?.model !== val
    );
    const thinkingChanged = Object.entries(editedThinking).some(
      ([key, val]) => (config.agents[key]?.thinking || 'false') !== val
    );
    return modelChanged || thinkingChanged;
  };

  const handleSave = async () => {
    if (!hasChanges()) return;
    setSaving(true);
    setSaveResult(null);
    try {
      // 模型变更
      const modelChanges = {};
      for (const [key, val] of Object.entries(editedConfig)) {
        if (config.agents[key]?.model !== val) {
          modelChanges[key] = val;
        }
      }
      // 思考模式变更
      const thinkingChanges = {};
      for (const [key, val] of Object.entries(editedThinking)) {
        if ((config.agents[key]?.thinking || 'false') !== val) {
          thinkingChanges[key] = val;
        }
      }
      const result = await updateModelConfig(
        Object.keys(modelChanges).length > 0 ? modelChanges : {},
        Object.keys(thinkingChanges).length > 0 ? thinkingChanges : null
      );
      if (result.status === 'ok') {
        setSaveResult('ok');
        await loadConfig();
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

  // 自定义 Provider 操作
  const handleAddProvider = async () => {
    if (!newProvider.name || !newProvider.api_key || !newProvider.models) return;
    setAddingProvider(true);
    try {
      const modelsList = newProvider.models.split(',').map(m => m.trim()).filter(Boolean);
      await addCustomProvider({
        name: newProvider.name,
        api_key: newProvider.api_key,
        base_url: newProvider.base_url,
        models: modelsList,
      });
      setNewProvider({ name: '', api_key: '', base_url: '', models: '' });
      setShowAddForm(false);
      await loadConfig();
      await loadCustomProviders();
    } catch (e) {
      console.error('Failed to add provider:', e);
    } finally {
      setAddingProvider(false);
    }
  };

  const handleDeleteProvider = async (name) => {
    try {
      await deleteCustomProvider(name);
      await loadConfig();
      await loadCustomProviders();
    } catch (e) {
      console.error('Failed to delete provider:', e);
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
          <button className="ms-refresh-btn" onClick={() => { loadConfig(); loadCustomProviders(); }}>
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
          <button className="ms-add-provider-btn" onClick={() => setShowAddForm(!showAddForm)}>
            {showAddForm ? <X size={11} /> : <Plus size={11} />}
            <span>{showAddForm ? '取消' : '接入 API'}</span>
          </button>
        </div>
      </div>

      {/* 添加 Provider 表单 */}
      {showAddForm && (
        <div className="ms-add-form">
          <div className="ms-form-row">
            <input
              placeholder="提供商名称 (如 XCode)"
              value={newProvider.name}
              onChange={e => setNewProvider(prev => ({ ...prev, name: e.target.value }))}
            />
            <input
              placeholder="Base URL (如 https://api.xxx.com/v1)"
              value={newProvider.base_url}
              onChange={e => setNewProvider(prev => ({ ...prev, base_url: e.target.value }))}
            />
          </div>
          <div className="ms-form-row">
            <input
              type="password"
              placeholder="API Key (sk-...)"
              value={newProvider.api_key}
              onChange={e => setNewProvider(prev => ({ ...prev, api_key: e.target.value }))}
            />
            <input
              placeholder="模型列表 (逗号分隔，如 gpt-5.4,gpt-5.3-codex)"
              value={newProvider.models}
              onChange={e => setNewProvider(prev => ({ ...prev, models: e.target.value }))}
            />
          </div>
          <button
            className="ms-form-submit"
            onClick={handleAddProvider}
            disabled={addingProvider || !newProvider.name || !newProvider.api_key || !newProvider.models}
          >
            {addingProvider ? <RefreshCw size={12} className="spinning" /> : <Plus size={12} />}
            {addingProvider ? '添加中...' : '添加 Provider'}
          </button>
        </div>
      )}

      {/* 已保存的自定义 Provider */}
      {customProviders.length > 0 && (
        <div className="ms-custom-providers">
          <div className="ms-section-label">自定义 PROVIDERS</div>
          {customProviders.map((p) => (
            <div key={p.name} className="ms-custom-row">
              <div className="ms-custom-info">
                <span className="ms-custom-name">{p.name}</span>
                <span className="ms-custom-key">{p.api_key_masked}</span>
                <span className="ms-custom-url">{p.base_url}</span>
              </div>
              <button className="ms-custom-delete" onClick={() => handleDeleteProvider(p.name)} title="删除此 Provider">
                <Trash2 size={12} />
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Agent 模型映射 */}
      <div className="ms-agents">
        <div className="ms-section-label">AGENT 模型映射</div>
        {Object.entries(config.agents).map(([role, info]) => {
          const currentModel = editedConfig[role] || info.model;
          const currentThinking = editedThinking[role] ?? info.thinking ?? 'false';
          const modelChanged = info.model !== currentModel;
          const thinkingChanged = (info.thinking || 'false') !== currentThinking;
          const changed = modelChanged || thinkingChanged;
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
                {groupedModels.map((group) => (
                  <optgroup key={group.provider} label={group.provider}>
                    {group.models.map((m) => (
                      <option key={`${group.provider}::${m}`} value={m}>{m}</option>
                    ))}
                  </optgroup>
                ))}
              </select>
              <select
                className={`ms-thinking-select ${currentThinking !== 'false' ? 'on' : ''}`}
                value={currentThinking}
                onChange={(e) => handleThinkingChange(role, e.target.value)}
                title="深度思考模式"
              >
                <option value="false">关</option>
                <option value="high">high</option>
                <option value="max">max</option>
              </select>
            </div>
          );
        })}
      </div>

    </div>
  );
}
