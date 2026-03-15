<!-- markdownlint-disable MD033 MD041 -->
<div align="center">

# 🌌 𝔸𝕊𝕋𝕣𝕖𝕒
**The Autonomous Multi-Agent AI Software Engineer System**

[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-blue.svg?logo=python)](https://www.python.org/)
[![React 19](https://img.shields.io/badge/React-19-61dafb.svg?logo=react)](https://reactjs.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-009688.svg?logo=fastapi)](https://fastapi.tiangolo.com/)
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-16+-336791.svg?logo=postgresql)](https://www.postgresql.org/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

*A Multi-Agent framework driven by Test-Driven Development (TDD), Ephemeral Sandboxes, Hybrid RAG Memory, and Knowledge Distillation.*

[📖 English](#) | [🇨🇳 简体中文](#)

</div>

---

## 🌟 什么是 ASTrea？ (What is ASTrea?)

**ASTrea** 是一个工业级、开箱即用的**多智能体 (Multi-Agent) 全自动代码开发系统**。  
它致力于解决传统单一大模型在长篇代码生成时固有的"幻觉累积"、"上下文长度灾难"以及"写完即抛、无法调试"的痛点。

只需向 ASTrea 的 Web UI 输入一句话需求（例如："写一个完整的带 SQLite 数据库的任务管理系统 API"），系统便会启动基于**管理、编码、审查、知识提炼**四位一体的 TDD 工作流，依靠物理沙盒验证每一行代码，直至完美交付。

---

## 🔥 核心特性 (Key Features)

### 1. 四智能体协同工作流 (Agent Orchestration)
* **🧠 Manager (架构总管)**: 不写具体代码，只负责宏观架构设计与 Task 分拆，收集里程碑数据。
* **💻 Coder (开发极客)**: 专注于 Manager 下发的单一文件任务进行代码生成。支持**三级模糊匹配差量编辑**（精确→空白归一化→difflib 模糊），显著减少全量重写。
* **🛡️ Reviewer (沙盒质检)**: 自带运行时沙盒机制。自动读取 Coder 代码，撰写测试脚本并真实执行。不通过则打回附带 stderr 错误堆栈重写，成功才允许合并入主干。
* **🧪 Synthesizer (知识提炼者)**: 在代码工作流成功或熔断后对本次工作流进行总结，生成 **Contrastive Pair (对比对)** 或 **Anti-pattern (反模式)**，由 LLM 自主判定存储范围 (global/project)。

### 2. 混合记忆架构 (Hybrid Memory Architecture)

ASTrea 采用**短期 + 长期**双层记忆系统，设计哲学为**"短期与项目共存亡，长期宁缺毋滥"**：

| 记忆类型 | 存储 | 生命周期 | 内容 |
|---|---|---|---|
| **短期记忆** | PostgreSQL `session_events` | 与 `project_id` 共存亡 | File Tree、Project Experience、TDD 事件 |
| **长期记忆** | PostgreSQL `memories` + pgvector | 跨项目永存 | 全局通用架构智慧 (Global Experience) |

**长期记忆三阶段双路召回**：
1. **向量粗排** — pgvector 余弦相似度 ≥ 0.6，Top 15
2. **BM25 粗排** — jieba 分词 + BM25Okapi 关键词检索，Top 15
3. **合并去重 → Rerank 精排** — DashScope `gte-rerank-v2`，最终 Top 5

**按 Scope 分组注入 Prompt**：
```
【🌍 全局通用架构智慧 (Global Experience)】        ← 长期记忆
【📦 本项目最高优先级规则 (必须绝对服从)】          ← 短期记忆
【📂 当前项目文件结构】                             ← 短期记忆
```

**项目经验毕业机制**：项目经验默认只存短期记忆，用户在前端点击 **🎓 项目交付** 按钮后，经用户二次确认方可升级为全局长期记忆。

### 3. 多重宇宙并行架构 (Multi-Project Isolation)
* 引入了 `project_id` 级别的绝对上下文隔离。前端支持"无缝切换宇宙 (新建项目)"。
* 内存中配备了专为高并发设计的 **VFS (虚拟文件系统) LRU 池**，修改只停留在脏数据内存草稿箱，通过 Review 才会物理落盘，拒绝沙盒磁盘污染与 OOM。

### 4. 三级模糊匹配 Editor (Smart Diff Editing)
Coder Agent 的差量编辑使用三级匹配策略，大幅减少不必要的全量重写：

| 级别 | 策略 | 解决什么 |
|:---:|---|---|
| L1 | 精确字符串匹配 | 正常情况 |
| L2 | 空白归一化匹配 | Tab↔空格、多余空白、行尾差异 |
| L3 | difflib 模糊匹配 (≥60%) | 微小缩进错位、LLM 复制偏差 |
| 兜底 | 全量重写 | 以上全部失败才触发 |

### 5. 阅后即焚安全沙盒 (Ephemeral Subprocess Sandbox)
* Windows/Linux 双兼容的隔离层。每次代码测试都在纯净的临时目录中跑，跑完即擦除。
* 包含 **Token 截断拦截** 与 **连续 5 次熔断机制 (MAX_RETRIES)**，并在失败 3 次后自动下发高压警告。

### 6. 高颜值赛博风大屏 (Mini-VSCode WebUI)
* 基于 React 19 + Framer Motion 构建的暗色深邃全栈面板。
* 集成 **Monaco Editor**，实时查看项目文件树变化，左侧支持终端流式输出（Agent 思考过程通过 WebSocket 推送）。

---

## 🛠️ 技术栈 (Tech Stack)

| 层级 | 技术与框架 | 用途说明 |
| :--- | :--- | :--- |
| **Frontend** | React 19, Vite, Vanilla CSS, Lucide | 高刷动效 UI 与全局状态管理 |
| **Editor** | `@monaco-editor/react` | VFS 内存状态与物理代码实时映射展示 |
| **Backend** | Python 3.11+, FastAPI, Uvicorn, WebSockets | 异步高并发底座、全双工通信 |
| **LLM Core** | OpenAI Python SDK | 默认对接阿里云百炼 DashScope 千问系列 |
| **Database** | PostgreSQL 16+ (Docker), pgvector, SQLAlchemy | 向量记忆 + 短期事件流 |
| **Retrieval** | jieba, rank-bm25, DashScope Rerank | 三阶段双路混合 RAG 召回 |
| **Watcher** | Watchdog | 后端项目文件夹反向热重载推送 |

---

## 📦 快速部署 (Quick Start)

### 1. 克隆与后端环境准备
```bash
git clone https://github.com/your-username/ASTrea.git
cd ASTrea

# 创建并激活虚拟环境 (推荐)
python -m venv .venv
# Windows: .venv\Scripts\activate | Unix: source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

### 2. 启动 PostgreSQL (Docker)
```bash
docker run -d --name astrea-pg \
  -e POSTGRES_USER=astrea \
  -e POSTGRES_PASSWORD=astrea123 \
  -e POSTGRES_DB=astrea_memory \
  -p 5432:5432 \
  pgvector/pgvector:pg16
```

### 3. 填写环境变量
在根目录创建 `.env` 文件：

```ini
QWEN_API_KEY=sk-xxxx你的真实秘钥xxx
QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1

# 模型矩阵分配 (分工合作)
MODEL_PLANNER=qwen3-max          # 需要极致的架构理解力
MODEL_CODER=qwen3-coder-plus     # 需要代码生成的专业性
MODEL_REVIEWER=qwen3-max         # 需要火眼金睛查错排错
MODEL_SYNTHESIZER=qwen3-max      # 知识提炼需要深度理解力
MODEL_RERANKER=gte-rerank-v2     # Rerank 精排模型

# 数据库连接
DATABASE_URL=postgresql://astrea:astrea123@localhost:5432/astrea_memory

# Token 消费告警线 (单次请求)
TOKEN_WARNING_LIMIT=50000
```

### 4. 前端环境构建
```bash
cd frontend
npm install
```

### 5. 启动 ASTrea !

**终端 1 (Backend):**
```bash
python server.py
```
*(默认监听 8000 端口，包含 WebSocket 服务与 FastAPI 数据通道)*

**终端 2 (Frontend):**
```bash
cd frontend
npm run dev
```
*(默认开启 Vite 热重载！浏览器打开 `http://localhost:5173` 即可)*

---

## 🎮 使用手册 (Usage Guide)

### 💻 方式一：Web 全局大屏 (推荐)
1. 进入前端页面后，点击右上角 **+ 新建宇宙**，系统会在物理磁盘开辟一个绝缘沙盒。
2. 在左下的聊天框用自然语言描述您的愿景，哪怕是 "写一个爬取网页表格并支持导出 Excel 的本地工具"。
3. 点击 **GENERATE**。
4. 靠在椅背上，看着系统自动打通上下文、进行子任务列表分拆、动态更名您的宇宙文件夹、不断在各种 Error 中自我修正和复盘，直到最终交付！
5. 对项目满意后，点击 **🎓 项目交付** 将踩坑经验升级为全局智慧。

### ⌨️ 方式二：极客终端 CLI 模式
```bash
# 场景 1：最常用的一句话生成
python main.py --prompt "写一个完整的基于 FastAPI 和 SQLAlchemy 的任务管理后台"

# 场景 2：基于长篇 PRD (需求文档) 直接驱动
python main.py --file prd_v1_final.txt

# 场景 3：自定义输出落盘位置
python main.py --prompt "一个 Python 贪吃蛇小游戏" --out_dir "D:/test_snake"
```



---

## 🤝 贡献与参与 (Contributing)
如果你也有关于智能体基建的想法，欢迎提交 PR 与 Issue！
ASTrea 的目标永远是做最 **轻量、透明、防爆** 的实用型开源 AI Software Engineer。

<div align="center">
<b>"If it works on my sandbox, it ships."</b><br>
<i>— ASTrea Reviewer Agent</i>
</div>
