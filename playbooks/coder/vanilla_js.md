# 原生 JavaScript 前端编码规范

## HTML 规则

1. **HTML 必须包含完整文档结构**
   ```html
   <!DOCTYPE html>
   <html lang="zh-CN">
   <head>
       <meta charset="UTF-8">
       <meta name="viewport" content="width=device-width, initial-scale=1.0">
       <title>页面标题</title>
       <link rel="stylesheet" href="./style.css">
   </head>
   <body>
       <!-- 页面内容 -->
       <script src="./app.js"></script>
   </body>
   </html>
   ```

2. **HTML/JS DOM 一致性铁律**
   - 写 index.html 时：查看依赖文件中 app.js 的 `getElementById`/`querySelector` 引用了哪些 id/class，HTML 中必须包含这些 DOM 元素！禁止输出空壳 HTML！
   - 写 app.js 时：`getElementById` 的 id 必须与 `module_interfaces` 中约定的 DOM id 一致。
   - 写 style.css 时：不要使用 `@tailwind`/`@apply` 等需要 PostCSS 编译的语法，必须使用原生 CSS。

## JavaScript 规则

3. **CDN 引用安全铁律**
   - 引入 CDN 库时，只使用该库的核心 API，禁止假设存在未在 HTML 中显式加载的插件/扩展。
   - 例如：引入了 markdown-it CDN，就只能用 `window.markdownit()` 的核心 API，不能假设存在 `markdownitHighlight` 等插件。
   - 如果需要扩展功能，必须先在 HTML 中加载对应的 CDN 脚本。

4. **表单提交铁律：禁止页面刷新**
   - HTML 中如果有 `<form>`，form 内的 `<button>` 必须设置 `type="button"`（不是 `type="submit"`），否则点击会触发表单默认提交导致页面刷新！
   - 或者在 JS 中监听 form 的 submit 事件并调用 `event.preventDefault()`。
   - 所有数据提交必须通过 JS 的 `fetch`/`XMLHttpRequest` 异步完成，禁止依赖表单的原生 submit 行为。

5. **JS 初始化铁律**
   - 每个 .js 文件必须在末尾包含 `DOMContentLoaded` 初始化代码：
     ```javascript
     document.addEventListener('DOMContentLoaded', () => new MyApp());
     ```
   - 如果定义了 class，必须在 `DOMContentLoaded` 回调中 new 它，禁止只定义不实例化！
   - 如果 class 中有用于创建/渲染 DOM 的方法（如 `renderEditor`、`renderUI`），必须在 constructor 或 `init()` 中调用，禁止定义了不用！

6. **事件绑定方式**
   - 使用 `document.getElementById('xxx').addEventListener('click', ...)` 绑定交互事件。
   - 或者在 HTML 中使用 `onclick="myApp.handleClick()"` 内联绑定（需确保实例挂到 window 上）。
