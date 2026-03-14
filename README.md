<!-- markdownlint-disable MD033 MD041 -->
<div align="center">

# 🌌 𝔸𝕊𝕋𝕣𝕖𝕒
**The Autonomous Multi-Agent AI Software Engineer System**

[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-blue.svg?logo=python)](https://www.python.org/)
[![React 19](https://img.shields.io/badge/React-19-61dafb.svg?logo=react)](https://reactjs.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-009688.svg?logo=fastapi)](https://fastapi.tiangolo.com/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

*A Multi-Agent framework driven by Test-Driven Development (TDD), Ephemeral Sandboxes, and RAG Memory.*

[📖 English](#) | [🇨🇳 简体中文](#)

</div>

---

## 🌟 什么是 ASTrea？ (What is ASTrea?)

**ASTrea** 是一个工业级、开箱即用的**多智能体 (Multi-Agent) 全自动代码开发系统**。  
它致力于解决传统单一大模型在长篇代码生成时固有的“幻觉累积”、“上下文长度灾难”以及“写完即抛、无法调试”的痛点。

只需向 ASTrea 的 Web UI 或 CLI 输入一句话需求（例如：“写一个完整的带 SQLite 数据库的任务管理系统 API”），系统便会启动基于**管理、编码、审查**三位一体的履带式工作流，依靠物理沙盒验证每一行代码，直至完美交付。

---

## 🔥 核心特性 (Key Features)

### 1. 三位一体工作流 (TDD Agent Loop)
* **🧠 Manager (架构总管)**: 不写具体代码，只负责宏观架构设计与 Task 分拆。
* **💻 Coder (开发极客)**: 不考虑全系统，专注于 Manager 下发的单一文件任务进行代码生成（盲写）。
* **🛡️ Reviewer (沙盒质检)**: 自带运行时沙盒机制。自动读取 Coder 代码，撰写测试脚本并真实执行。不通过则打回附带 stderr 错误堆栈重写，成功才允许合并入主干。

### 2. 多重宇宙并行架构 (Multi-Project Isolation)
* 引入了 `project_id` 级别的绝对上下文隔离。前端支持“无缝切换宇宙 (新建项目)”。
* 内存中配备了专为高并发设计的 **VFS (虚拟文件系统) LRU 池**，修改只停留在脏数据内存草稿箱，通过 Review 才会物理落盘，拒绝沙盒磁盘污染与 OOM。

### 3. 多模态长短时记忆 (RAG & Sliding Memory)
* **短时记忆 (SQLite)**: 自动对每个宇宙 (Project) 进行滑动窗口截断，确保模型知道刚才发生了什么。
* **长时经验 (ChromaDB)**: 所有经过沙盒千难万险测试成功的项目，都会被系统强制发起全局反思 (`_reflect_and_memorize`)，将踩坑经验萃取为长篇向量存入 ChromaDB，为未来的项目开发提供先验智慧！

### 4. 阅后即焚安全沙盒 (Ephemeral Subprocess Sandbox)
* Windows/Linux 双兼容的隔离层。利用操作系统的临时生命周期与 `PYTHONIOENCODING=utf-8`，每次代码测试都在纯净的新容器中跑，跑完即随 `with` 语境连带垃圾文件一并擦除，从第一性原理杜绝木马感染与沙盒状态膨胀。
* 包含 **Token 截断拦截** 与 **连续 5 次熔断机制 (MAX_RETRIES)**，并在失败 3 次后自动下发架构师的高压警告，防大模型死循环破产。

### 5. 高颜值赛博风大屏 (Mini-VSCode WebUI)
* 抛弃简陋的终端，ASTrea 提供了一套基于 React 19 + Framer Motion 构建的黑暗深邃全栈面板。
* 集成了真正的 VSCode 同款 **Monaco Editor**，左侧可实时查看项目文件树变化，右侧支持终端流式输出（Agent 碎碎念与架构思考同步 WebSocket 推送）。

---

## 🛠️ 技术栈 (Tech Stack)

| 层级 | 技术与框架 | 用途说明 |
| :--- | :--- | :--- |
| **Frontend** | React 19, Vite, Tailwind/Vanilla CSS, Lucide | 高刷动效 UI 与全局状态管理 |
| **Editor** | `@monaco-editor/react` | VFS 内存状态与物理代码实时映射展示 |
| **Backend** | Python 3.11+, FastAPI, Uvicorn, WebSockets | 异步高并发底座、全双工通信 |
| **LLM Core** | OpenAI Python SDK | (默认对接阿里云百炼 DashScope 的千问系列) |
| **Memory** | SQLAlchemy (SQLite), ChromaDB | 短期对话回溯 + 长期 RAG 向量检索 |
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

### 2. 填写环境变量
在根目录创建一个 `.env` 文件，填入你的 API 配置（默认全面兼容 Qwen，你也可以替换为其他任意遵循 OpenAI 规范的 API 或本地 Ollama）：

```ini
QWEN_API_KEY=sk-xxxx你的真实秘钥xxx
QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1

# 模型矩阵分配 (分工合作)
MODEL_PLANNER=qwen3-max        # 需要极致的架构理解力
MODEL_CODER=qwen3-coder-plus    # 需要代码生成的专业性
MODEL_REVIEWER=qwen3-max       # 需要火眼金睛查错排错

# Token 消费告警线 (单次请求)
TOKEN_WARNING_LIMIT=50000
```

### 3. 前端环境构建
```bash
cd frontend
npm install
```

### 4. 启动 ASTrea !
我们需要同时启动后端推流 API 与 前端页面：

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
*(默认开启 Vite 热重载环境！浏览器打开 `http://localhost:5173` 即可)*

---

## 🎮 使用手册 (Usage Guide)

### 💻 方式一：Web 全局大屏 (推荐)
1. 进入前端页面后，点击右上角 **+ 新建宇宙**，系统会在物理磁盘开辟一个绝缘沙盒。
2. 在左下的聊天框用自然语言描述您的愿景，哪怕是 “写一个爬取网页表格并支持导出 Excel 的本地工具”。
3. 点击 **GENERATE**。
4. 靠在椅背上，看着系统自动打通上下文、进行子任务列表分拆、动态更名您的宇宙文件夹、不断在各种 Error 中自我修正和复盘，直到最终交付！

### ⌨️ 方式二：极客终端 CLI 模式
如果不喜欢 UI，ASTrea 同样支持极致轻量的静默终端执行。
```bash
# 场景 1：最常用的一句话生成
python main.py --prompt "写一个完整的基于 FastAPI 和 SQLAlchemy 的任务管理后台"

# 场景 2：基于长篇 PRD (需求文档) 直接驱动
python main.py --file prd_v1_final.txt

# 场景 3：自定义输出落盘位置
python main.py --prompt "一个 Python 贪吃蛇小游戏" --out_dir "D:/test_snake"
```

---

## 📝 演进路线图 (Roadmap)
- [x] v0.5 - 跑通 Manager 逻辑拆解与 Coder -> Reviewer TDD 循环。
- [x] v0.8 - WebSocket 流式介入，完成全双工消息面板日志通信。
- [x] v0.9 - 多宇宙隔离架构，内存 VFS 支持 LRU 防内存 OOM 缓存机制。
- [x] **v1.0.0 (Current)** - UI 大规模重构，加入项目增量重命名、ChromDB RAG 与阅后即焚。
- [ ] v1.1.0 - 支持 Coder Agent 搜索使用外部浏览器进行联网 API 检索。
- [ ] v1.5.0 - 接管 Node.js/Java 等多语言容器形态执行域沙盒。

---

## 🤝 贡献与参与 (Contributing)
如果你也有关于智能体基建的想法，欢迎提交 PR 与 Issue！
ASTrea 的目标永远是做最 **轻量、透明、防爆** 的实用型开源 AI Software Engineer。

<div align="center">
<b>"If it works on my sandbox, it ships."</b><br>
<i>— ASTrea Reviewer Agent</i>
</div>
