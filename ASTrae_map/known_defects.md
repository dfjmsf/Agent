# ASTrea 已知缺陷台账

> 最后审计时间: 2026-04-29
> 基于 v1.7.0 Unlimited Architecture 全量代码审计

---

## 缺陷分级说明

| 级别 | 含义 | 处理要求 |
|------|------|----------|
| 🔴 P0 | 架构性缺陷，影响系统稳定性 | Ulimite 迁移前必须修复 |
| 🟡 P1 | 功能性缺陷，影响用户体验或正确性 | 近期迭代修复 |
| 🟢 P2 | 代码质量问题，不影响功能 | 优化迭代 |

---

## 🔴 P0: 架构性缺陷

### P0-1: PM 意图路由语义理解不足

**位置**: `agents/pm.py` → `_classify_intent()` → `_tool_call_route()` + `core/prompt.py` → `PM_ROUTE_TOOLS` / `PM_ROUTE_SYSTEM`

**现象**: LLM Tool Calling 路由在以下场景误判：
- 项目已存在时，将"修改功能"误判为 `create` 模式
- 模糊表述（如"把这个改一下"）偶发路由到 `reply_to_chat` 而非 `execute_project_task`
- 技术提问（如"为什么端口是 5001？"）被误路由为 `modify`

**根因**: 
1. 工具描述语义边界不锐利 — `execute_project_task` 承载 5 种 mode，description 过于宽泛，LLM 难以精准区分
2. 路由 system prompt 缺少强制推理链 — LLM 直接跳到 tool call，未经过「理解意图 → 判断场景 → 选择工具」的显式推理
3. 负面示例覆盖不足 — 边界 case（如"这个接口有问题"是提问还是修复请求）缺少明确裁定

**影响**: 用户发送修改请求时，可能触发全量覆写（数据丢失风险）；提问被误判为执行操作浪费资源

**修复方向（Prompt 工程，与 Ulimite Master 自主决策对齐）**:

1. **工具描述锐化** — 重写 `PM_ROUTE_TOOLS` 中每个工具的 description，明确「适用场景」和「绝对禁用场景」，缩小语义模糊区
2. **CoT 强制推理链** — 在 `PM_ROUTE_SYSTEM` 中要求 LLM 先输出结构化推理再选工具：
   ```
   推理步骤：
   1. 用户意图分类：提问/讨论 | 要求执行 | 确认/否定 | 闲聊
   2. 是否涉及代码变更：是 → 什么类型的变更 | 否 → reply_to_chat
   3. 项目当前状态：是否有待确认方案 | 项目是否存在
   4. 最终路由选择 + 理由
   ```
3. **负面示例扩充** — 补充边界 case 裁定（如："这个接口有问题" → 需追问是报告 bug 还是纯粹提问）
4. **最小确定性安全网** — 仅保留 `_project_exists() → 封杀 create` 一条守卫（防止不可逆数据丢失），其余路由决策完全交给 LLM

**~~已废弃方案~~**: 双重路由投票（治标不治本）、大量硬编码守卫（与 Ulimite 自主决策方向矛盾）

**状态**: ✅ 已修复（v5.0 CoT 强制推理 + 语义锐化）
- `PM_ROUTE_TOOLS`: 6 个工具全部注入 `reasoning` 必填参数 + description 改为「适用/禁用」双栏
- `PM_ROUTE_SYSTEM`: 加入 4 步 CoT 决策框架 + 6 条边界 case 裁定
- `_tool_call_route`: 提取 reasoning 字段打日志，供调试追溯

---

### P0-2: TaskRunner 上下文碎片化

**位置**: `core/task_runner.py` → `_invoke_coder()`

**现象**: 长任务链（>5 个 Task）后期的 Coder 调用丢失早期规划上下文，导致：
- 后端 API 与前端模板字段名不一致
- 路由路径重复注册

**根因**: 
1. `ProjectObserver.build_task_meta()` 依赖 `global_schema` + `global_routes` 快照注入，但快照仅包含已 commit 文件的 AST 摘要
2. 尚未 commit 的 Task（正在执行中的兄弟任务）的上下文不可见
3. 用户在 PM 阶段确认的 `confirm_message` 未透传到 Coder prompt

**影响**: 中大型项目（>8 文件）的跨文件一致性降低

**修复方案（v4.5 Completed Tasks Context Injection）**:
1. `core/project_observer.py`: 新增 `_build_completed_tasks_context()` — 从 `Blackboard.state.completed_tasks` 账本聚合已完成任务的语义摘要（最近 15 条，排除当前 task_id）
2. `core/project_observer.py`: `build_task_meta()` 中注入 `completed_context` 字段到 `task_meta` 字典
3. `agents/coder.py`: 三条路径（`_build_memory_hint` / `_build_fix_hint_with_snapshot` / Fill 模式精简上下文）均消费 `completed_context`，注入 Coder 的 system prompt

**状态**: ✅ 已修复（v4.5 Completed Tasks Context Injection）

---

### P0-3: AST 切片死锁导致 Coder-Reviewer 熔断

**位置**: `core/project_observer.py` → `build_task_meta()` + `tools/ast_microscope.py` → `find_relevant_slice()` + `core/task_runner.py` → `_should_disable_ast_slice_for_feedback()`

**现象**: 当任务描述要求新增函数（如 `get_expense_by_id`、`update_expense`）时，AST 显微镜仍然选中已有函数（如 `get_all_expenses` L44-52）进行切片注入，Coder 被锁入单函数视窗无法创建新定义 → Reviewer 驳回 → 5 轮熔断

**根因**: 
1. `find_relevant_slice` 基于关键词匹配选择已有符号，假设"修改已有函数"，不处理"新增函数"场景
2. `_should_disable_ast_slice_for_feedback` 缺少"缺少函数/未定义"的反馈信号检测
3. `build_task_meta` 中 AST 切片分支（`elif True:`）无条件执行，缺少 scope expansion 守卫

**影响**: 所有需要新增函数/方法/类的 extend/modify 任务都可能触发 5 轮熔断

**修复方案（v4.4 Scope Expansion）**:
1. `tools/ast_microscope.py`: 新增 `requires_scope_expansion()` — 提取 description 中的标识符与既有符号表对比，发现未知函数名则返回 True
2. `core/project_observer.py`: AST 切片前新增 scope expansion 检测分支 — 命中则跳过切片，让 Coder 看到全文件上下文
3. `core/task_runner.py`: 扩展 `_should_disable_ast_slice_for_feedback` — 增加"缺少函数/未定义"等 Reviewer 反馈信号作为第二道防线

**状态**: ✅ 已修复（v4.4 Scope Expansion 双层防御）

---

### P0-4: Extend Mode DAG 环依赖与运行时死锁

**位置**: `core/engine/modes/extend.py` → `_normalize_extend_plan()` + `core/task_dag_builder.py` → `_inject_ssr_template_edges()`

**现象**:
1. DAG 构建失败: `检测到环依赖或不可完成 DAG: ['app.py', 'models.py', 'templates/base.html', ...]`
2. 降级后 Extend Mode 仍执行失败: TechLead pivot 注入的运行时依赖与二次归一化注入的依赖形成死锁

**根因（三层叠加）**:
1. **暴力全量依赖注入** — `_normalize_extend_plan` 将所有 `new_task_ids` 强制注入为每个 weld 任务的依赖。当 new file 的 LLM 声明依赖中包含某个 weld 文件，就会形成双向环
2. **二次归一化覆盖** — DAG 降级清零所有依赖后，二次 `_normalize_extend_plan` 又重新注入 weld→new 全量依赖，与 TechLead 运行时 pivot 注入的反向依赖形成死锁
3. **SSR 规则方向冲突** — `_inject_ssr_template_edges` 对 weld 类型的 route 节点（如 `app.py`）仍注入 `route→template` 确定性边，但 LLM 合理声明了反向依赖（templates 先创建，app.py 后焊接），两条规则方向冲突形成环

**影响**: 所有包含 weld route 文件 + new template 的 Extend Mode 规划都会 DAG 失败

**修复方案（v5.1 Extend DAG Decoupling）**:
1. `_normalize_extend_plan`: 移除暴力 `new_task_ids` 注入逻辑
2. 新增 `_normalize_extend_plan_metadata_only`: DAG 归一化后仅刷新 metadata，不触碰 dependencies
3. `_inject_ssr_template_edges`: 对 `task_type == "weld"` 的 route 节点跳过 SSR 边注入（weld 文件已存在于磁盘，Reviewer L0.6-C 可直接读取已有版本校验）
4. 遗漏的跨文件依赖由 TechLead 运行时 pivot 机制补偿

**状态**: ✅ 已修复（v5.1 Extend DAG Decoupling + SSR Weld 感知）

---

## 🟡 P1: 功能性缺陷

### P1-1: Reviewer 文件体积过大

**位置**: `agents/reviewer.py` (163KB, 系统最大文件)

**现象**: 单文件承载 L0 确定性检查 + L1 LLM 审计 + 沙箱执行 + Vue SFC 解析 + 跨文件合约比对

**影响**: 
- 维护成本极高，修改一处 L0 规则可能引发 L1 回归
- 无法独立测试单个审查层级

**建议**: 拆分为 `reviewer_l0.py` (确定性) + `reviewer_l1.py` (LLM) + `reviewer_infra.py` (沙箱/解析)

**状态**: 📋 Ulimite 迁移时处理

---

### P1-2: PM 备忘录异步竞态

**位置**: `agents/pm.py` → `_async_archive()`

**现象**: `_archiving_event.wait(timeout=3.0)` 超时时，上一轮备忘录更新可能丢失

**根因**: 异步归档使用 daemon 线程 + Event 信号，超时后跳过等待

**影响**: 多轮快速对话时，决策备忘录可能不完整（概率较低）

**状态**: 📋 监控中

---

### P1-3: Patch Mode TechLead 调查耗时

**位置**: `core/engine/modes/patch.py` → TechLead 前置调查

**现象**: 每次 Patch 都执行完整的 TechLead 白盒调查，即使是简单的文案修改

**影响**: 简单修改的响应时间被不必要拉长（+5-10s）

**建议**: 引入复杂度分级，简单修改跳过 TechLead

**状态**: 📋 待优化

---

### P1-4: DAG 降级静默

**位置**: `core/engine/pipeline.py` → `finalize_plan_with_dag()`

**现象**: `TaskDagBuildError` 异常时降级为 LLM 原始顺序，但降级原因仅写日志，未向用户提示

**影响**: 用户不知道任务调度已降级，可能导致依赖乱序

**状态**: 📋 待修复

---

### P1-5: Extend Prompt 前端工程化引导缺失

**位置**: `core/prompt.py` → `MANAGER_EXTEND_SYSTEM` (L94-185) + `agents/manager.py` → `plan_extend()`

**现象**: 用户明确要求 "Vue 3 (Composition API) + Tailwind CSS" 工程化项目时，Manager 有时生成完整 Vite + Vue SFC 工程（11 文件），有时退化为 CDN `<script>` 引入的单文件 `static/index.html`（22KB）

**证据对比**:
- 成功项目 `20260428_211627_new_project`: Round 2 产出 8 个 extend 任务（package.json、vite.config.js、tailwind.config.js、postcss.config.js、main.js、App.vue + 3 个 SFC 组件）
- 失败项目 `20260430_112744_new_project`: Round 2 仅 2 个 extend 任务（static/index.html + main.py weld）

**根因**:
1. `MANAGER_EXTEND_SYSTEM` prompt 中**零前端工程化约束** — 无任何关于 npm 脚手架、SFC 文件结构、构建链配置的引导
2. LLM 在复杂上下文下倾向选择"最小路径"（CDN 单文件），prompt 未阻止这种退化
3. 成功案例完全依赖 LLM 偶然高质量输出，无确定性保障

**影响**: 前端框架需求的实现质量不稳定，CDN 模式生成的 SPA 无法被 QA 验证（→ 联动 P1-6）

**修复方向**:
1. ✅ 在 `MANAGER_EXTEND_SYSTEM` 中注入前端工程化铁律（第 8 条）— 禁止 CDN 降级，强制工程化文件结构
2. `plan_extend()` 中增加后置校验：检测用户需求含前端框架关键词但规划只有 1 个 HTML 文件时，触发 replan（留 Ulimite）

**状态**: ⚡ Prompt 约束已注入（v5.2），后置校验待 Ulimite

---

### P1-6: QA Agent SPA 验证盲区

**位置**: `agents/qa_agent.py` → `run_integration_test()` + `core/skills/sandbox_http.py`

**现象**: QA Agent 对 CDN 模式的 SPA 页面（Vue `<script>` 引入）发送 HTTP GET `/`，收到 HTML 全文但**无法执行 JavaScript**，反复尝试 14 步后超步数判定失败

**根因**:
1. `sandbox_http` 是纯 HTTP 客户端，不具备 JS 执行能力
2. QA Agent 的 LLM 决策循环无法识别"这个 HTML 响应已经是成功的静态资源返回"
3. 缺少对纯前端页面的降级验证策略（如：HTML 可达 + 200 状态码 = PASS）

**影响**: 所有 CDN 模式 SPA 的 Extend Mode 均会被误判为集成测试失败

**修复方向**:
1. 短期：QA prompt 注入"对于 GET / 返回 HTML 的页面，验证 HTTP 200 + Content-Type 即可 PASS"
2. 长期（Ulimite）：引入 Headless Browser / Playwright 验证层

**状态**: 📋 Ulimite 迁移时处理

---

## 🟢 P2: 代码质量问题

### P2-1: _engine_backup.py 残留

**位置**: `core/_engine_backup.py` (120KB)

**现象**: 旧版 Engine 单文件备份仍保留在仓库中

**建议**: 确认无依赖后删除

---

### P2-2: 测试脚本散落根目录

**位置**: 根目录下 `_run_*`, `_test_*`, `_verify_*` 共 ~40 个文件

**建议**: 统一迁移至 `tests/` 目录

---

### P2-3: 重复的 project_scanner

**位置**: `core/project_scanner.py` (11KB) 与 `tools/project_scanner.py` (15KB)

**现象**: 两个同名模块，功能部分重叠

**建议**: 合并为单一模块

---

### P2-4: Blackboard.record_failure_context 冗余写入

**位置**: `core/blackboard.py` L286-319

**现象**: `extra_context` 合并后又重复写入 `reason`, `error_message` 等字段

```python
# 当前代码 (冗余)
if extra_context:
    context.update(extra_context)
    context["reason"] = reason           # 重复赋值
    context["error_message"] = error_message  # 重复赋值
    # ... 更多重复 ...
```

**建议**: 移除冗余赋值，`context.update(extra_context)` 后无需再次覆盖

**状态**: 📋 下次触碰时修复

---

### P0-5: Extend Mode JSON 解析静默失败（致命）

**位置**: `agents/manager.py` → `plan_extend()` L1539-1562

**现象**: Extend Mode 规划时 LLM 返回 JSON 格式不合规（截断、包裹 Markdown 等），`json.loads()` 抛异常后**静默返回空 tasks**，不记录 LLM 原始输出，不尝试修复

**根因**:
1. `plan_extend()` 使用裸 `json.loads()` 一次解析，失败即判死刑
2. 同一文件中已有 `_repair_truncated_json()` 修复器（L1022），但**只被 Patch Mode 调用**，Extend Mode 未接入
3. JSON 解析失败时不记录 LLM 原始输出，事后无法诊断

**影响**: 任何 JSON 格式偏差都会导致 "Extend Mode 未生成任何任务" 的 P0 级系统故障

**修复**:
1. ✅ 复用 `_repair_truncated_json()` 修复链路
2. ✅ 记录 LLM 原始输出前 500 字符（日志级别 INFO）
3. ✅ 修复失败时记录前 1000 字符的完整错误日志

**状态**: ✅ 已修复（v5.2 热补丁）

---

### P2-4: Coder 生成 uvicorn.run(app, reload=True) 导致启动警告

**位置**: Coder Agent 生成的 `main.py` → `uvicorn.run(app, host=..., reload=True)`

**现象**: QA 启动验证报 `WARNING: You must pass the application as an import string to enable 'reload' or 'workers'.`

**根因**: uvicorn 的 `reload=True` 参数要求传入 import string（如 `"main:app"`），不能传 app 对象。Coder 缺少这条经验约束。

**影响**: P2（QA 启动验证失败导致项目标记为 warning，但代码本身不受影响）

**修复方向**:
1. 向 Playbook 或种子经验注入约束：`uvicorn.run` 搭配 `reload=True` 时必须传 import string
2. 或者在 Coder system prompt 中加入 FastAPI 启动模式约束

**状态**: 📋 待处理（经验层修复）

---

### P0-6: Extend Mode 跨轮次依赖死锁（append_tasks 重命名不级联）

**位置**: `core/blackboard.py` → `Blackboard.append_tasks()` L381-419

**现象**: Extend Mode 执行 11 个任务，前 6 个（无跨轮依赖）正常完成，第 7 个（App.vue 依赖 task_3）触发死锁 → "依赖死锁：无可运行任务但存在未完成任务" → Extend Mode 执行失败

**根因**:
1. DAG builder 为 Extend 任务分配了 `task_1~task_11` 的 ID，与 Round 1 的 `task_1~task_4` 冲突
2. `append_tasks()` 检测到冲突后将 `task_1` → `extend_2_task_1` 等重命名，**但没有级联更新其他新任务的 `dependencies` 数组**
3. Extend 任务 `task_7`（App.vue）的依赖 `task_3` 仍指向 Round 1 的 `task_3`（routes.py，状态 CODING）
4. `_dependencies_satisfied()` 严格检查 `dep_task.status == DONE`，Round 1 task_3 不是 DONE → 永远阻塞
5. 所有后续任务（App.vue、MealForm.vue、RecordList.vue、WeightForm.vue、requirements.txt weld）全部死锁

**影响**: P0（Extend Mode 在多轮项目中必定触发，6/11 任务完成后整体判定失败，前 6 个文件白写）

**修复**:
1. ✅ 在 `append_tasks()` 中引入 `rename_map` 收集所有 ID 冲突重命名
2. ✅ 循环结束后级联替换所有新任务 `dependencies` 中的旧 ID 引用
3. ✅ 记录级联更新日志（INFO 级别）

**状态**: ✅ 已修复（v5.3 热补丁）

---

## 修复优先级路线图

```
Phase 0 (已完成): P0-1 ✅ 路由 CoT → P0-2 ✅ 上下文锚点注入 → P0-3 ✅ AST 切片防御 → P0-4 ✅ Extend DAG 解环 → P0-5 ✅ Extend JSON 修复 → P0-6 ✅ 依赖级联死锁
Phase 1 (迁移): P1-1 Reviewer 拆分 → P1-5 ⚡前端工程化 Prompt → P1-6 QA SPA 验证 → P2-1/P2-2/P2-3 清理
Phase 2 (优化): P1-3 TechLead 分级 → P1-4 DAG 降级提示 → P2-4 uvicorn reload 经验
```

