# ASTrea 长期演进路线图

> 最后更新：2026-04-08
> 状态：明确了“内功基建 → 技能外推 → 表现层封装”的新三步走战略。

---

## 🟢 当前系统能力基线（v1.4 契约版）

| 维度 | 能力 |
|---|---|
| 项目规模 | 10-15 文件，单页或 SSR 应用 |
| 核心架构 | **Contract-First** (契约驱动)，消灭幻影引用 |
| 成功闭环 | 依靠 P0/P1/P2 强级层规范与 L0-Contract 确定性规则拦截 |

### 防御体系 (五层)
```text
预防层 → Playbook 规范 + 动态 Addon 补丁
感知层 → Observer 全局快照 (API / Schema 路由提取)
拦截层 → Reviewer L0-Contract 静态审计 (0 LLM 消耗)
自愈层 → Reviewer 阻断重试 + 短期记忆注入
档案层 → Git 全量管控 + FTS5 SQLite 向量级对话检索
```

---

## ✅ 已完成阶段 (Phase 0 ~ Phase 1)

*   **Phase 0: 复杂项目支撑与流控**
    *   Manager 两阶段规划 + SubTasks 骨架先行拆解
    *   Observer 全局快照，黑板（Blackboard）管理
*   **Phase 1: 契约网与 Prompt 架构**
    *   PM / Manager / Coder 的 `P0铁律/P1契约/P2指南` 严格分层结构
    *   现有开源项目逆向扫描生成 `project_spec` 架构映射
    *   Reviewer P2 完工：新增 `L0.C1~C3` 契约级强校验，断绝运行时路径失配
*   **Phase 1.5: 开发者控制台改造**
    *   弃用旧沙盒，改造 LabWorkspace 实验室：含 Git 日志面板、只读文件树、和无缝模型切换热装载。

---

## 🟡 Phase 2: 后端基建收官与记忆内化（巩固内功）

> **当前核心焦点**：不急于放开自由度，先将系统的自我纠错与记忆回溯能力打磨到极致。

### 2.1 Task Ledger (任务账本与时空穿梭) ⭐️
- **描述**：当前 Git 只是盲目执行了 Commit 保存，缺乏语义联系。
- **动作**：
  - 在 Blackboard 建立 `completed_tasks` 时序账本。
  - 将每次子任务的意图、修改的文件与当前 `Git Hash` 进行强绑定。
  - Ledger 的语义回退能力（查询 + 回滚执行）见 **2.5.3 Engine 智慧升级 - 能力 D**。

### 2.2 Auto Dream (空闲记忆整合器)
- **描述**：借鉴 Claude Code KAIROS 架构，解决 Synthesizer 长期运行后产生的记忆碎片化问题。
- **动作**：
  - 引擎闲置时后台自动苏醒，扫描散落的报错经验。
  - 四阶段整合：提取相似报错 → 整合解决冲突 → 高评分提纯 → 清理无效废料 (Score < 0.3)。
- **关键设计约束**：
  - **AMC 继承策略**：整合后新记忆继承簇内最高分者的 S/U/R，合并永不降级。
  - **世代上限 `merge_generation ≤ 2`**：防止整合记忆无限雪球膨胀。
  - **LLM 拒绝权**：如果碎片虽相似但各有独特技术要点，LLM 可输出 `NO_MERGE` 拒绝合并。
  - **压缩硬约束**：整合后字数 ≤ 原始碎片总字数的 80%，防止 LLM 注水。
  - **召回预算制**：未来将 `n_results=3` 硬编码改为"字符预算制"（约 600 字上限），彻底避免整合记忆导致 Token 膨胀。
- **优先级**：低于 AST 显微镜，在记忆库膨胀到 50+ 条时启动。

### 2.2.1 记忆安全防线（反截断投毒）
- **问题**：当前经验注入存在 `[:200]` 硬截断，可能将"千万不要用 X=True，必须设为 False"截断为"千万不要用 X"，制造毒经验。
- **L1 预防（Synthesizer Prompt）**：将经验产出字数从"300字以内"收紧为 **200字以内**，从源头管控。
- **L2 兜底（`_write_experience` 硬阀门）**：content 超过 **250字** 的记忆直接丢弃不写入数据库。经验产出量大，宁丢勿毒。

### 2.3 User Preferences & P0.5 本地潜规则引擎 (专属定制)
- **描述**：通过显式文件挂载实现"专属私有化数字员工"。
- ~~隐式偏好追踪~~：**已取消**，由 P0.5 显式规则覆盖（见 2.5.1 决策）。
- **动作 (显式 P0.5 加载器)**：
  - 支持读取用户手写的项目级配置文件（如根目录的 `.astrea.md` 或 `.cursorrules`）。
  - **动态动态路由 (Token 安全)**：仅当文件存在且有内容时，才动态拼接出一个至高无上的【P0.5 层】挂载于 P0 (系统铁律) 之后与 P1 (项目契约) 之前。
  - **零噪音约束**：如果用户未配置潜规则文件，Prompt 渲染时将一刀切地舍弃该挂载点块，坚决消除无视内容的虚空声明，保护大语言模型的珍贵“注意力”。

### 2.4 AST 显微镜 (Tree-sitter 微观切片定位) ⭐️⭐️
- **描述**：彻底终结前端等大型文件修改时的“瞎子摸象 (全量读写)”，引入**路线A**的靶向外科手术机制。
- **动作**：
  - 引入 `tree-sitter` 在 Observer 层建立前端 DOM 与各种语言的严密结构化解析。
  - **感知瘦身**：当 PM/Manager 接到“修改按钮”等局部需求时，利用 AST 快速锁定目标节点（如 `<button>`），提取仅包含上下 20 行的精准代码片段。
  - **执行提效**：原先扔给 Coder 2000 行大文件，现缩减至 30 行精准切片。Coder 只需要输出标准的 `SEARCH/REPLACE block`。
  - **精准缝合**：对接现存超强的 `CodePatcher` 三级降级匹配机制，实现极速、 100% 确定性的代码更新，且极大降低大模型成本和幻觉率。


### 2.5 指挥链重构与增量管线 (Command Chain v2) ⭐️⭐️⭐️

> **目标**：解决当前"地基完工但上层管道断裂"的核心矛盾。底层武器已铸好（Ledger + AST 显微镜），但指挥链（PM -> PlannerLite -> Manager -> Engine）既简陋又割裂，无法将用户意图精准翻译为最小化执行指令。

#### 2.5.0 架构总览（对外统一人格，对内分层指挥）
```
用户 <-> PM（化身/前台：理解意图、维持人格、翻译需求）
            |
            v  结构化指令（mode=create/patch/rollback）
         Engine（智慧总指挥：有判断力的中枢，取代纯状态机）
            |
            v  task 列表
         Manager / Coder / Reviewer / ...
```
- **不新建 Agent**，而是升级现有角色的职责边界
- **PM**：从"只认新项目的前台"升级为"ASTrea 的化身"，用户始终觉得在跟一个人对话
- **Engine**：从"纯状态机传送带"升级为"有判断力的总指挥"，能决定 create/patch/rollback

#### 2.5.1 PM 化身升级（Avatar Mode）— 双层架构细化
- **现状**：PM 只有"新建项目"一条路径，回复死板无人味
- **目标**：PM 成为 ASTrea 的高情商化身——准确路由 + 灵动对话 + 情绪价值

**双层架构**：
```
用户消息
   |
   v
Layer 1: 路由嗅探器（Router）
  - 极速、便宜、不深度思考
  - 输出: { route, confidence, context_needs }
  - 实现: 硬正则(覆盖70%) + 便宜模型(30%)
  - 成本: < 200 token, < 1秒
   |
   v  { route: "patch", confidence: 0.95, context_needs: ["frontend_skeleton"] }
Layer 2: 人格化身（Persona）
  - 高情商工作伙伴，提供情绪价值
  - 按路由结果调整语气 + 按 context_needs 拉取上下文
  - 成本: 正常 LLM 调用
```

**五层路由**：

| 路由 | 触发语义 | 后续动作 |
|---|---|---|
| `create` | "做一个/帮我建/新建..." | PlannerLite -> 确认 -> Manager |
| `patch` | "改/加/删/调整..." + 项目已存在 | 确认范围 -> Manager (patch mode) |
| `rollback` | "改回去/撤销/恢复..." | 查 Ledger -> Engine git revert |
| `chat` | 闲聊/提问/感谢/技术咨询 | 直接对话，不触发执行 |
| `clarify` | 意图模糊/置信度低 | 追问澄清后重新路由 |

**Layer 2 上下文按需注入**（避免全量灌入导致 Token 爆炸）：
  - 由 Layer 1 的 `context_needs` 字段决定拉取哪些信息
  - 可选来源：Blackboard 进度 / Observer 骨架 / plan.md 技术栈
  - 用户闲聊时不注入任何项目上下文

**人格设计约束**：
  - 高情商工作伙伴：有活人感，让用户有参与感
  - 用户正确时正面肯定，错误时委婉指正并说明原因
  - emoji **极度克制**：仅允许表情类（😊🤔😅），每条消息最多 1 个，大多数回复无 emoji，禁止装饰类 emoji（🎯✨🚀📝💡🔄）
  - 不做隐式偏好追踪——由 P0.5（.astrea.md）覆盖，避免功能重复

#### 2.5.2 PlannerLite -> Manager 合同绑定（plan.md 重构）— 细化
- **现状**：PlannerLite 生成 plan.md（含文件树），Manager 完全不看 plan.md，从原始 spec_text 重新规划
- **问题**：用户审核了一份方案，实际执行的是另一份
- **目标**：plan.md 从"给用户看的 PPT"升级为三位一体的核心文档

**plan.md 的三个读者**：

| 读者 | 阅读方式 | 看到什么 |
|---|---|---|
| **用户** | 前端方案面板渲染 Markdown | 详细的技术栈 + 功能规划，清晰美观 |
| **PM** | 读取后口语化归纳输出到聊天 | "我帮您规划好了，详细内容在方案面板里" |
| **Manager** | 注入 Prompt 作为执行合同 | 严格遵循技术栈和功能，不得擅自增减 |

**格式**：纯 Markdown，不用 YAML（Manager 是 LLM，直接读懂 Markdown）：
```markdown
# 待办清单应用

## 技术栈
- **后端**：Flask
- **前端**：Jinja2 模板 + Vanilla JS
- **数据库**：SQLite（内存模式）

## 核心功能
1. **待办列表展示** — 首页渲染全部待办项
2. **添加待办** — 表单提交，标题必填
3. **删除待办** — 每项旁有删除按钮
```

**内容规范**：
  - 技术栈（用户未指定时 PlannerLite 填默认值，PM 顺带追问确认）
  - 核心功能（编号列表，每项一句话描述）
  - **不含文件树**（文件拆分是 Manager 的专业工作）

**PM 完整交互流程**：
```
路由 → 可行性预判 → 调用 PlannerLite 生成 plan.md
  → PM 读取 plan.md → 在聊天中输出口语化归纳
  → 用户在方案面板查看 plan.md 渲染详情
  → 用户确认/修改/拒绝
  → 确认后 Manager 读取 plan.md 生成 task 列表
```

**Manager Prompt 铁律**：
  "以下是用户确认过的项目方案（plan.md），严格遵循技术栈和功能清单，不得擅自更换或增减。"

#### 2.5.3 Engine 智慧升级（Smart Commander）— 细化
- **现状**：Engine 是纯状态机，收到 task 就逐个执行 Coder -> Reviewer，不区分新建/修改/回滚
- **目标**：从"无脑传送带"升级为"三条线路的调度中心"，根据 mode 选择线路，每条配备专属质量检查点

**三模式架构**：
```
收到指令 { mode, plan_md, ... }
  |
  +-- create（现有流程，几乎不改）
  |     Manager 读 plan.md -> task 列表 -> 逐个 Coder(首次生成) -> Reviewer -> commit
  |
  +-- patch（核心改造 ⭐）
  |     Manager 读 plan.md(patch)
  |       + 注入当前项目文件清单 + AST 显微镜骨架摘要
  |       + Prompt 铁律：只生成需修改的 task（action="modify"），禁止全部重建
  |       -> 只生成需改的 1-2 个 task
  |       -> 读取 existing_code
  |       -> AST 显微镜切片（>50行自动触发）
  |       -> Coder(Editor 靶向修复)
  |       -> 变动率检查 -> Reviewer -> commit
  |
  +-- rollback（新增）
        PM 传来 { keyword: "按钮颜色" }
          -> Engine.query_ledger() 定位 commit
          -> PM 向用户确认
          -> Engine.rollback_to() 执行 git revert
```

**能力 A：mode 路由**（前置，其他能力都依赖它）
  - Engine 入口根据 mode 字段走不同分支
  - 难度 🟢 极低（10 行 if 分支）

**能力 B：代码变动率监控**（patch 模式下生效）
  - Coder 产出后，计算与 existing_code 的差异率
  - **关键约束：感知 CodePatcher 降级模式**，避免误触发：

  | 情况 | diff 率 | Coder 模式 | 处理 |
  |---|---|---|---|
  | Editor 成功 | <20% | editor | 正常放行 |
  | Editor 失败 -> 保守覆写 | >80% | fallback_rewrite | **合法降级，放行** |
  | Coder 抽风重写整文件 | >80% | editor | **告警 + 回退重试** |

  - 难度 🟡 中（需要 CodePatcher 向上传出 mode 信息）

**能力 C：AST 切片触发放宽**（patch 模式下生效）
  - 触发条件从 `existing_code and feedback` 改为 `existing_code and len(lines) > 50`
  - 首次修改已有大文件时也能触发切片
  - 依赖上方 patch 流程中 Manager 正确设置 `action="modify"`
  - 难度 🟢 极低（改 1 个 if 条件）

**能力 D：Ledger 查询与回滚**（rollback 模式下生效）
  - `query_ledger(keyword)` — 在 Blackboard 的 completed_tasks 列表中搜索匹配的历史操作
  - `rollback_to(commit_hash)` — 执行 git revert，需设计整项目 vs 单文件回滚策略
  - 难度 🟡 中（Ledger 查询简单，git 回滚策略需设计）

**实施顺序**：A(mode 路由) -> C(切片放宽) -> B(变动率监控) -> D(Ledger 回滚)

#### 2.5.4 前后端 API 契约检查（L0.9）
- **现象**：前端 `fetch('DELETE')` 调接口，后端用 `redirect()` 响应 -> 302 跟随后 405
- **构想 A（预防）**：Playbook 铁律"前端使用 fetch/axios 时，后端必须返回 JSON，禁止 redirect"
- **构想 B（拦截）**：Reviewer 新增 L0.9 -- 扫描前端 `fetch` URL -> 检查对应后端路由是否返回 `jsonify`

---

## 🔵 Phase 3: 外部延伸与主动环境交互（长手脚 - Skill）

> **目标**：在 Coder 层被紧紧束缚（保证确定性）的同时，为 PM 和外部 DevOps 层赋予“神级能力”，打通对现实世界的干预。

### 3.1 PM 知识触角 (Intelligence Skills)
- 赋予 PM Agent `Web_Search` 和 `API_Scrape` 技能。
- 允许其在遭遇未见过的刁钻包、第三方前沿库时，自行翻阅 GitHub 官网与文档，最终总结为 `Addon` 补丁交给 Manager 排兵布阵。

### 3.2 独立 QA/DevOps Agent (从纯脚本向动态执行进化)
- 专门创建一个全新的高权限实体，终结当前低能的盲狙方案（即放弃写入死板的 `test.py` 纯脚本代码，改为持 Tool 下场实时博弈）。赋予其真实的 Terminal `run_bash` 对话权。
- 负责：沙箱初始环境、动态安装依赖包，实时捕获运行时 Crash 并定位分析。
- **动态 Error-Back 机制**：内建 ReAct 反思闭环，能在终端敲错命令后自行根据返回日志修正。
- **防暴走阀门**：严格套用局部 Max_Steps 熔断策略（防死亡螺旋），尝试无果则强行拉阀，提取遗言（Bug报告）上报。
- **隔离铁律 (Sandboxed Execution)**：只具备跑环境和探测的权利，**绝对褫夺改写源码（edit_file）的权限**，不破坏 Coder 纯净的 L0 契约网。

### 3.3 UI 自动化验收 (End-to-End Skills)
- 引进 Puppeteer / Playwright 工具，让系统自己能打开在跑的本地网页，验证元素是否溢出，控制台是否有红字。

---

## 🟣 Phase 4: 极客产品化与 C 端剥离（好卖相）

> **目标**：内核封固后，向最终用户交付。用最干练的降噪理念包装复杂流程。

### 4.1 独立 C 端前端 (frontend-user)
- 放弃现有的开发者 Lab 界面，面向小白设计。
- 交互三栏结构（参考 Cursor）：全局折叠对话流 + 右侧计划进度大盘看板 + 下拉文件 Diff。

### 4.2 终极 CLI 沉浸式交互
- 致敬 `Claude Code` 与 `Aider`：弃用所有的 Web 中间件。
- 在用户自己熟悉的 Terminal 里，实现基于 ASTrea 强大后端的命令式快问快答与本地注入，贴身肉搏式开发体验。

---

## 决策记录

| 日期 | 决策 | 理由 |
|---|---|---|
| 2026-03-31 | Phase 0（Manager/Coder/Observer）最高优先级 | 是项目规模上限的直接瓶颈 |
| 2026-03-31 | 大文件标记基于结构复杂度 | 避免前端误标记 |
| 2026-03-31 | 前端永不走 Skeleton-First | 前端靠拆组件解决 |
| 2026-03-31 | Observer 全局快照放黑板 | 增量更新，全局可见，零 LLM 成本 |
| 2026-03-31 | plan.md 只给用户看 | 是 Manager spec 的美化投影，回流是冗余 |
| 2026-03-31 | B 用确定性检查而非 LLM | 用户是最好的审批者 |
| 2026-03-31 | Git 初始化在真理区而非 sandbox | sandbox 会被清理，真理区是正本 |
| 2026-03-31 | 对话 SQLite 放 .astrea/ | 与项目绑定，不放 sandbox |
| 2026-03-31 | FTS5 而非向量检索 | 对话场景关键词匹配够用，零 LLM 成本 |
| 2026-03-31 | PM 自动提取 user_preferences | 不需要用户手动设置 |
| 2026-03-31 | Reviewer/Integration 按需升级 | 当前瓶颈不在验证层 |
| 2026-03-31 | Playbook 并行推进 | 不阻塞主线 |
| 2026-03-31 | Reviewer 不用 Playbook | 确定性 AST 检查路线 |
| 2026-03-31 | DAG 并行延后 | 当前规模收益仅 1.2x |
| 2026-03-31 | Memory 自动升级低优先级 | 有价值但不紧急 |
| 2026-03-31 | C 端前端放 Phase 2 收尾 | 依赖 PM + 规划组 + 多轮对话后端能力 |
| 2026-03-31 | 两套前端独立共存 | 面向不同用户群，不互相干扰 |
| 2026-04-01 | 借鉴 Claude Code Auto Dream | Synthesizer/Auditor 已有 70%，补充空闲触发 + 裁剪 |
| 2026-04-01 | Prompt 架构重构列入 Phase 1 | 身份声明薄弱 + 规则无优先级 + 废话浪费 token |
| 2026-04-01 | CLI 模式放 Phase 3 | Web 界面已覆盖主要场景，CLI 依赖 PM 层 |
| 2026-04-01 | sub_tasks 由 Manager 输出而非 Engine 判断 | 借鉴 CC TodoWrite，规划级决策更可控 |
| 2026-04-01 | Prompt P0/P1/P2 分层列入 Phase 1.2 | Playbook 覆盖 Spec 导致 Vue 3 文件结构错误，根因是信息扁平化 |
| 2026-04-01 | Composition API 路由到 Vite 模式 | PlaybookLoader PRIORITY_KEYWORDS 新增 composition |
| 2026-04-01 | 多 Provider LLM 路由列入 Phase 2.5 | 中转站延迟高 + 模型切换需编辑 .env 太麻烦，需前端 UI 热切换 |
| 2026-04-02 | Reviewer 跨文件校验从 Phase 3.2 前移至 1.1 | Playbook 解决 90% 问题后，剩余 10%（to_dict 字段/form name 不匹配）只能靠确定性检查兜底 |
| 2026-04-02 | DeepSeek V3.2 不适合 Coder 角色 | 测试熔断，qwen3-coder-plus 仍是 Coder 唯一选择 |
| 2026-04-02 | DeepSeek V3.2 用于 Reviewer + Synthesizer | 指令遵循好 + 价格低，成本降 40-60% |
| 2026-04-02 | Git 版本管理用整个项目一次 commit | 当前无 agent 自主调用 git 做针对性修改，全量 commit 保证 MVP 可靠 |
| 2026-04-02 | 开发者前端"装修"而非"重建" | 在现有 frontend/ 上增量改造，避免第二套前端的环境污染 |
| 2026-04-02 | Git 工具零 Python 依赖（subprocess 调 git） | 避免 GitPython 等重依赖，保持系统轻量 |
| 2026-04-02 | 模型设置 MVP 先做 Agent 映射，Provider 管理延后 | 切模型是高频需求，加 Provider 是低频需求 |
| 2026-04-02 | PM Agent 二层路由（硬正则 + 软 LLM） | 硬路由零成本覆盖 80% 场景，LLM 只处理模糊意图 |
| 2026-04-02 | PlannerLite 与 Manager 分离 | PM → PlannerLite（便宜）→ 用户确认后才调 Manager（贵），省 token |
| 2026-04-02 | 确定性按钮而非 LLM 判断确认/拒绝 | 零成本 + 消除 LLM 误判用户意图的风险 |
| 2026-04-02 | 跨文件冲突用 TechLead Agent 而非脚本打回 | L0.6 只知“不匹配”不知“谁错”，需要 LLM 理解业务语义才能仲裁 |
| 2026-04-08 | **[战略升级]** 划定「基建 -> Skill -> 产品」三步走轴线 | 没有稳固的任务时空回溯作为托底，过早给智能体赋予 Skill 极易破坏现有契约生态导致崩溃。 |
| 2026-04-08 | 敲定 Coder 绝对禁触 Skill 原则 | 坚持 Coder 作为纯函数执行者，维持系统的收敛与确定性，Skill 只赋予上游(PM) 和 下游(QA)。 |
| 2026-04-08 | 将 Tree-sitter 微观切片确定为下一代代码编辑基建 | 废除前端代码全量丢给大模型的弊病，大幅提升代码匹配确定性并拯救上下文资源。 |
| 2026-04-08 | AST 显微镜真正发挥需 PM+Manager 增量改造配合 | 底层切片已就绪但上游把所有文件当新建，切片永远不触发 |
| 2026-04-08 | L0.7 normalize 改为通配符替换而非删除 | `{{ todo.id }}` 应替换为 DYNVAR 保留路径结构，否则对带参路由永远误报 |
| 2026-04-08 | TechLead JSON 解析加控制字符清理 + strict=False 二级降级 | LLM 输出含非法控制字符导致 json.loads 崩溃，需容错 |
| 2026-04-08 | **[架构]** PM 定位为"化身"而非"全能体" | 多 Agent 架构不适合硬塞单体，对外统一人格 + 对内分层指挥更稳健 |
| 2026-04-08 | **[架构]** Engine 升级为智慧总指挥，不新建 Supervisor Agent | Engine 已处于信息交汇点，注入判断力比新建 Agent 更省通信开销 |
| 2026-04-08 | **[架构]** plan.md 只写技术栈+功能，不写文件树 | PlannerLite 用便宜模型猜文件结构经常出错，文件拆分交给 Manager 专业决策 |
| 2026-04-08 | ~~plan.md 只给用户看~~ -> plan.md 升级为 Manager 的作战合同 | 旧决策导致 PlannerLite 与 Manager 完全割裂，用户审核的方案与执行的方案不一致 |
