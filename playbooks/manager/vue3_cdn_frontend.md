# Vue 3 CDN 前端项目任务拆分规则

## 文件结构要求

1. 前端必须拆分为至少 3 个文件：
   - `frontend/app.js` — Vue 3 应用逻辑（使用 `Vue.createApp` + `data/methods/computed`）
   - `frontend/style.css` — 样式
   - `frontend/index.html` — 页面结构（必须引入 Vue 3 CDN 和 app.js，包含 `<div id="app">` 挂载点，内部使用 Vue 模板语法 `v-model`/`v-for`/`@click` 等）

2. HTML 中必须引入 Vue 3 CDN：`<script src="https://unpkg.com/vue@3/dist/vue.global.js"></script>`，并且该 script 标签必须在 app.js 引用之前！

## DAG 依赖规则

3. **前端 DAG 铁律：JS/CSS 先于 HTML！**
   - `app.js` 和 `style.css` 必须先写，`index.html` 最后写。
   - 原因：index.html 需要根据 app.js 的 `data()` 返回值和 `methods` 来编写 Vue 模板（`v-model`、`@click` 等），如果 HTML 先写就会与 JS 脱节。

## 重要注意事项

4. **禁止在任务描述中要求使用 getElementById/querySelector**
   - Vue 3 项目的数据绑定全部通过 `v-model`/`v-for`/`v-if` 等模板语法实现。
   - 任务描述中应明确指出"使用 Vue 3 响应式系统"而非"使用 DOM 操作"。

5. **HTML 中使用 Vue 模板语法**
   - index.html 的 task description 中必须注明：使用 Vue 模板语法（`v-model`、`v-for`、`@click`、`@submit.prevent` 等），内容写在 `<div id="app">` 内。

## 示例任务列表

```json
{
  "task_3": {
    "target_file": "frontend/app.js",
    "description": "前端 Vue 3 应用逻辑，使用 Vue.createApp 创建应用，定义 data/methods/computed，通过 fetch 调用后端 API，最后 app.mount('#app')",
    "dependencies": ["task_2"]
  },
  "task_4": {
    "target_file": "frontend/style.css",
    "description": "前端样式",
    "dependencies": ["task_2"]
  },
  "task_5": {
    "target_file": "frontend/index.html",
    "description": "前端页面结构：引入 Vue 3 CDN（unpkg.com/vue@3）和 app.js，包含 <div id='app'> 挂载点，内部使用 Vue 模板语法（v-model、v-for、@click 等）绑定 app.js 中的 data 和 methods",
    "dependencies": ["task_3", "task_4"]
  }
}
```
