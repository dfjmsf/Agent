# 🚀 Multi-Agent Coding Framework (Qwen 驱动的多智能体全自动开发系统)

这是一个基于局部交叉验证与安全沙盒隔离机制的 **多智能体 (Multi-Agent)** 代码生成框架。该框架能够将人类的一句话需求，自动转化为结构完整、低错误率的可用软件工程项目。

## 🌟 核心运行机制

告别单一大模型写代码经常出现的“幻觉累积”和“上下文灾难”。本系统将开发过程拆解为三条流水线上的履带工作流：

1. **🧠 Manager Agent (主控总管)**
   * **不写业务代码**，只负责架构设计。
   * 它将用户的宏大需求分解为一个最小单元的 `Task List`。
2. **💻 Coder Agent (开发码农)**
   * **不考虑全局，只做"盲写"**。
   * 接收 Manager 派发的一个微小任务指令，结合目前的虚拟文件系统(VFS)上下文，生成只针对单一文件的极简纯净代码。
3. **🛡️ Reviewer Agent (无情沙盒质检员)**
   * **自带 Subprocess 沙盒核弹**。不仅通过静态眼光看代码，更通过**自动生成本地测试脚本并在安全沙盒中真实运行**来检验 Coder 生成代码的正确性。
   * 未通过沙盒执行测试的代码，会被打回给 Coder 并附上错误堆栈要求重写。
   * 只有携带 Reviewer 签发 `PASS` 印章的模块，才会被 Manager 同意合并进主干树中。

## 🛠️ 技术栈 (Tech Stack)

### 前端 (Frontend)
* **核心框架**: React 19 + Vite (极速构建与热重载)
* **UI 动画**: Framer Motion (提供丝滑的面板与日志弹射动画)
* **代码编辑器**: `@monaco-editor/react` (VSCode 同款 Monaco 编辑器内核)
* **图标库**: Lucide React
* **样式**: 纯原生 CSS (Vanilla CSS) 结合 CSS Variables，实现极客/赛博朋克风界面。

### 后端 (Backend)
* **核心框架**: Python 3.11+
* **REST 路由**: FastAPI + Uvicorn (高性能异步 API 服务)
* **长连接通讯**: WebSockets (用于 Agent 思考过程的实时日志流式推送)
* **大语言模型对接**: OpenAI 官方 SDK (无缝代理阿里云百炼 DashScope 的 Qwen 系列大模型)

### 沙盒安全层 (Sandbox)
* **隔离执行**: 纯 Python 原生 `subprocess` 加上 `threading` 与超时限制，零外部依赖实现沙盒安全执行。

## ⚙️ 第一性安全原理：强迫执行隔离 (Subprocess Sandbox)

系统内所有的执行动作都被严格封禁在 `workspace/` 沙盒目录下的临时 `_run_task_*.py` 脚本中运行。即使大模型发生抽风要执行诸如格式化全盘、死循环等命令：
* 带有硬性的 `TIMEOUT` 熔断机制。
* 带有针对输出堆栈超长的 `Token Truncation` 截断保护机制（保护系统在遇到滚屏的无限报错时不会因为上下文超限而破产）。
* Git 仓库 `.gitignore` 完全屏蔽黑盒垃圾不污染源码库。

## 📦 准备工作 

1. **环境依赖**
   ```bash
   pip install -r requirements.txt
   ```
2. **配置秘钥与平台端口**
   配置根目录下的 `.env` 文件。该框架天然支持所有兼容 OpenAI 格式 API 的端点（包含且不仅局限于阿里云百炼 DashScope）。
   
   ```ini
   QWEN_API_KEY=sk-xxxx你的真实秘钥xxx
   QWEN_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
   
   # 定义各司其职的具体模型 (必须严格按照平台小写/大写规范提供可用名字)
   MODEL_PLANNER=qwen3-max
   MODEL_CODER=qwen3-coder-plus
   MODEL_REVIEWER=qwen3-max
   
   # 防破产机制：预警单次任务最高上下文开销
   TOKEN_WARNING_LIMIT=50000
   ```

## 🎮 如何使用

该工具已被封装为开箱即用的命令行应用程序 `main.py`。
生成的项目每次都会获得干净的上下文，并被归档存放于 `projects/` 目录下带时间戳与定制名称的独立子目录中。

```bash
# 场景 1：最常用的一句话快捷生成
python main.py --prompt "写一个完整的基于 FastAPI 和 SQLAlchemy 的任务管理后台"

# 场景 2：针对大型系统，通过文件输入长篇复杂的 PRD (需事先准备文本)
python main.py --file my_prd.txt

# 场景 3：自定义输出落盘位置
python main.py --prompt "写一个 Python 贪吃蛇小游戏" --out_dir "D:/test_snake_auto"
```

## 🕹️ 守护进程特性 (Protector)
由于带有智能反思，TDD 循环（开发->测试->开发）可能会卡在无法跨越的认知屏障上产生死循环。
本架构内置 `MAX_RETRIES = 5` **重试阈值熔断机制**。当同一个文件在沙盒里测试报错连续被打回 3 次后，质检员会自动发出 `高压红牌警告` 迫使 Coder 完全推翻原有的库或实现思路；失败达 5 次，系统将启动主动熔断以终止该部分任务避免无限损耗 Token。
