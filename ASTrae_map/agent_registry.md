# ASTrea Agent 注册表

> 最后审计时间: 2026-04-29

---

## Agent 总览

系统共 10 个 Agent，按唤醒时序排列：

```
用户消息 → [PM] → plan 确认 → [Manager] → Spec+Tasks
                                              ↓
                                    ┌─ [TechLead] ← Patch 前置调查
                                    ↓
                              [TaskRunner] 循环:
                              ┌─ [Coder] → 代码草稿
                              ├─ [Reviewer] → L0+L1 审查
                              └─ (失败回退: TechLead 仲裁)
                                    ↓
                        [IntegrationTester] → 端点验证
                              [QA Agent] → 集成测试
                              [Auditor] → 合规审计
```

---

## 1. PMAgent — 用户唯一对话窗口

| 属性 | 值 |
|------|-----|
| **文件** | `agents/pm.py` (118KB, 2570 行) |
| **模型** | `MODEL_PM` 环境变量, 默认 `deepseek-v4-flash` |
| **唤醒时机** | 每条用户消息 |
| **核心方法** | `chat(user_message) → PMResponse` |

### 内部架构

```
chat()
  ├─ _classify_intent()     ← 100% LLM Tool Calling, 零正则
  │   └─ _tool_call_route() ← 5 个路由工具 (execute/revise/chat/clarify/search)
  ├─ _dispatch_route()      ← 根据路由结果分发
  ├─ _async_archive()       ← 后台线程: 决策备忘录更新
  └─ _trim_window_by_tokens()← 弹性 Token 滑动窗口 (4000 token 预算)
```

### 状态管理

- **状态机**: `idle` | `wait_confirm` | `wait_clarify`
- **决策备忘录 (Letta-Lite)**: 5 字段 (`tech_stack`, `features`, `design`, `pending`, `user_prefs`)
- **执行账本**: `execution_ledger` — 每轮 Engine 执行的结构化摘要
- **Phase 管理**: `project_phases[]` — 分步构建进度追踪
- **对话持久化**: FTS5 SQLite (`ConversationStore`)

### ⚠️ 已知缺陷

- `_classify_intent` 在项目已存在时偶发 `create` 误判（已有代码级兜底: `_project_exists()` 检查后降级为 `modify`）
- 缺乏路由置信度阈值机制，依赖 `temperature=0.0` 硬压

---

## 2. ManagerAgent — 项目经理

| 属性 | 值 |
|------|-----|
| **文件** | `agents/manager.py` (99KB) |
| **唤醒时机** | Phase 1 规划阶段 |
| **核心方法** | `_generate_project_spec()`, `plan_tasks()`, `plan_patch()` |

### 职责清单

1. **Spec 生成**: 用户需求 → 结构化 JSON 蓝图 (含 `tech_stack`, `features`, `api_endpoints`, `architecture_contract`)
2. **Task 拆解**: Spec → Raw Task List (含 `target_file`, `description`, `dependencies`)
3. **两阶段规划**: 大型项目 (≥12 文件预估) → 先分模块组，再逐组规划
4. **Patch 规划**: `plan_patch()` — 仅规划受影响文件
5. **Playbook 注入**: 从 `PlaybookLoader` 加载技术栈铁律

### 关键依赖

- `core/spec_compiler.py` — Spec 编译
- `core/spec_validator.py` — 合同闭环校验
- `core/playbook_loader.py` — Playbook 铁律

---

## 3. CoderAgent — 编码者

| 属性 | 值 |
|------|-----|
| **文件** | `agents/coder.py` (45KB) |
| **唤醒时机** | Phase 2 每个 Task 的 TDD 循环 |
| **核心方法** | `generate_code(target_file, description, feedback, task_meta)` |

### 编码模式

| 模式 | 触发条件 | 输出格式 |
|------|----------|----------|
| `skeleton` | `sub_tasks[0].type == "skeleton"` | 函数签名 + `...` 占位 |
| `fill` | `current_sub_task_index >= 1` | 补全骨架函数体 |
| `editor` | Patch/Weld 任务 | `SEARCH/REPLACE` 差量编辑 |
| `slice` | AST 切片可用 | 函数级局部替换 |
| `rewrite` | 新建文件 | 完整文件内容 |

### 上下文注入链

```
ProjectObserver.build_task_meta(task)
  ├─ 全局快照 (global_schema + global_routes)
  ├─ 依赖文件骨架 (Observer.get_skeleton)
  ├─ AST 切片 (ast_microscope.slice_functions)
  ├─ Playbook 铁律
  ├─ 架构契约 (architecture_contract)
  └─ TechLead 修复指令 (如有)
```

---

## 4. ReviewerAgent — 审查者

| 属性 | 值 |
|------|-----|
| **文件** | `agents/reviewer.py` (163KB, 系统最大文件) |
| **唤醒时机** | Phase 2 每个 Task 的 TDD 循环 |
| **核心方法** | `review()`, `review_skeleton()` |

### 审查层级

| 层级 | 类型 | 执行方式 | 检查项 |
|------|------|----------|--------|
| **L0** | 确定性 | AST + 正则 + 沙箱 | 语法/导入/骨架残留/路由注册/签名匹配 |
| **L0.5** | 半确定性 | 沙箱执行 | `python -c "import ..."` 启动验证 |
| **L1** | LLM 语义 | LLM 调用 | 合约审计/逻辑完备性/安全风险 |

### L0 检查清单

```
L0.0  — 骨架残留 (... / pass / TODO 占位)
L0.1  — 语法错误 (AST parse failure)
L0.2  — 导入缺失 (import 分析)
L0.3A — 架构违规 (app 实例位置/Blueprint 注册)
L0.5  — 运行时导入验证 (沙箱执行)
L0.C1 — 路由未注册 (route_topology 交叉验证)
L0.VUE— Vue SFC 结构校验
```

---

## 5. TechLeadAgent — 技术总监

| 属性 | 值 |
|------|-----|
| **文件** | `agents/tech_lead.py` (28KB) |
| **唤醒时机** | Patch 前置调查 / 跨文件冲突仲裁 |
| **核心方法** | `investigate(project_dir, task_context, target_scope)` |

### 职责

1. **Patch 前置白盒调查**: 读取项目文件 → 定位根因 → 输出修复指令
2. **跨文件仲裁**: 当 Reviewer 检测到跨文件依赖缺失时，定位 provider 文件并注入修复
3. **Scope 解析**: 通过 `core/techlead_scope.py` 精确定位受影响文件范围

---

## 6. QAAgent — QA 验证

| 属性 | 值 |
|------|-----|
| **文件** | `agents/qa_agent.py` (31KB) |
| **唤醒时机** | Phase 2.5 集成测试 |
| **核心方法** | `run_endpoint_test()` |

### 测试策略

- 从 Spec 提取 API 端点 → 构造请求 → 验证响应
- 失败端点写入 `Blackboard.upsert_issue()` (烂账账本)
- 通过端点调用 `Blackboard.resolve_issues_by_endpoint()` (闭环)

---

## 7. IntegrationTesterAgent

| 属性 | 值 |
|------|-----|
| **文件** | `agents/integration_tester.py` (50KB) |
| **核心方法** | `run_startup_check()`, `run_integration_test()` |
| **配套模块** | `core/integration_manager.py` (41KB) |

---

## 8. AuditorAgent

| 属性 | 值 |
|------|-----|
| **文件** | `agents/auditor.py` (6KB) |
| **唤醒时机** | Phase 3 结算 / 用户主动审计 |
| **配套模块** | `core/audit_guard.py` (22KB) |

---

## 9. PlannerLite (降级)

| 属性 | 值 |
|------|-----|
| **文件** | `agents/planner_lite.py` (4KB) |
| **状态** | 已降级，PM 直接通过 `_generate_plan()` 替代 |

---

## 10. Synthesizer

| 属性 | 值 |
|------|-----|
| **文件** | `agents/synthesizer.py` (9KB) |
| **职责** | 多源信息合成: 将多个 Agent 的输出聚合为统一上下文 |
