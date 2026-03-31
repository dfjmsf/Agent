# 原生 HTML/JS 前端项目任务拆分规则

## 文件结构要求

1. 前端必须拆分为至少 3 个文件：
   - `frontend/app.js` — 交互逻辑（使用 getElementById 绑定 DOM 元素并调用后端 API）
   - `frontend/style.css` — 样式
   - `frontend/index.html` — 页面结构（必须包含 app.js 中 getElementById 引用的所有 DOM 元素 id，引用 style.css 和 app.js）

2. HTML 文件禁止内联 `<script>` 超过 5 行，所有 JS 逻辑必须写在独立 .js 文件中。

## DAG 依赖规则

3. **前端 DAG 铁律：JS/CSS 先于 HTML！**
   - `app.js` 和 `style.css` 必须先写，`index.html` 最后写。
   - 原因：index.html 需要根据 app.js 的 DOM 引用来创建对应的 HTML 元素，如果 HTML 先写就会产生空壳页面。

## 示例任务列表

```json
{
  "task_3": {
    "target_file": "frontend/app.js",
    "description": "前端交互逻辑，使用 getElementById 绑定 DOM 元素并调用后端 API",
    "dependencies": ["task_2"]
  },
  "task_4": {
    "target_file": "frontend/style.css",
    "description": "前端样式",
    "dependencies": ["task_2"]
  },
  "task_5": {
    "target_file": "frontend/index.html",
    "description": "前端 HTML 页面结构，必须包含 app.js 中引用的所有 DOM 元素 id，引用 style.css 和 app.js",
    "dependencies": ["task_3", "task_4"]
  }
}
```
