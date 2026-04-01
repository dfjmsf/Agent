# Vite 前端项目任务拆分规则（Manager Playbook）

## 文件结构与 DAG 规则

Vite 构建项目的文件拆分必须遵循以下规则：

### 1. 必须包含的文件

| 文件 | 优先级 | 说明 |
|---|---|---|
| `package.json` | 最高 | 项目依赖定义，必须第一个生成 |
| `vite.config.js` | 高 | 构建配置 + API 代理 |
| `tailwind.config.js` | 高 | **Tailwind 项目必须！** 指定扫描的文件路径 |
| `postcss.config.js` | 高 | **Tailwind 项目必须！** PostCSS 插件配置 |
| `index.html` | 高 | 入口 HTML（在根目录） |
| `src/main.js` 或 `src/main.jsx` | 中 | 应用入口 |
| `src/App.vue` 或 `src/App.jsx` | 中 | 根组件 |
| `src/style.css` | 高 | **必须！** main.js 中 `import './style.css'` 硬引用，缺失会构建失败 |

> 🚨 **如果使用 Tailwind CSS，必须将 `tailwind.config.js` 和 `postcss.config.js` 加入任务列表，否则所有 Tailwind 类名无效，页面无样式！**

### 2. DAG 依赖关系

```
package.json → vite.config.js → tailwind.config.js → postcss.config.js
                                      ↓
                               index.html
                                      ↓
                              src/main.js(x)
                                      ↓
                              src/App.vue(jsx) → src/components/*.vue(jsx)
                                      ↓
                              src/style.css
```

- `package.json` 没有依赖，必须最先生成
- `vite.config.js` 依赖 `package.json`（因为需要知道用了什么插件）
- `src/main.js(x)` 依赖 `index.html`
- `src/App.vue(jsx)` 依赖 `src/main.js(x)`

### 3. 后端文件必须在项目根目录（禁止放入 src/！）

后端文件（`models.py`, `routes.py`, `main.py`）必须放在**项目根目录**，不是 `src/` 目录！
`src/` 目录专属前端文件（.vue/.js/.css），后端 Python 文件严禁放入 `src/`。

- `models.py` 最先（根目录）
- `routes.py` 依赖 `models.py`（根目录）
- `main.py` 依赖 `routes.py`（根目录），并在最后追加前端构建产物 `dist/` 的静态挂载

### 4. 与 CDN 模式的区别

| | CDN 模式 | Vite 模式 |
|---|---|---|
| 前端目录 | `frontend/` | `src/` |
| 入口 HTML | `frontend/index.html` | `index.html`（根目录） |
| 构建 | 不需要 | `npm run build` → `dist/` |
| CDN 引入 | `<script src="https://unpkg.com/vue@3/...">` | 不需要（npm 管理） |
| 后端挂载 | 挂载 `frontend/` | 挂载 `dist/` |
| package.json | 不需要 | 必须 |
