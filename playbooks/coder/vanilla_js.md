# 原生 JavaScript 前端编码规范

<!-- P0:START -->
## HTML 规则

1. **HTML 必须包含完整文档结构**

2. **HTML/JS DOM 一致性铁律**
   - 写 index.html 时：查看依赖文件中 app.js 的 `getElementById`/`querySelector` 引用了哪些 id/class，HTML 中必须包含这些 DOM 元素！禁止输出空壳 HTML！
   - 写 app.js 时：`getElementById` 的 id 必须与 `module_interfaces` 中约定的 DOM id 一致。
   - 写 style.css 时：不要使用 `@tailwind`/`@apply` 等需要 PostCSS 编译的语法，必须使用原生 CSS。

## JavaScript 规则

3. **CDN 引用安全铁律**
   - 引入 CDN 库时，只使用该库的核心 API，禁止假设存在未在 HTML 中显式加载的插件/扩展。

4. **表单提交铁律：禁止页面刷新**
   - HTML 中如果有 `<form>`，form 内的 `<button>` 必须设置 `type="button"`（不是 `type="submit"`），否则点击会触发表单默认提交导致页面刷新！
   - 或者在 JS 中监听 form 的 submit 事件并调用 `event.preventDefault()`。
   - 所有数据提交必须通过 JS 的 `fetch`/`XMLHttpRequest` 异步完成，禁止依赖表单的原生 submit 行为。
   - **例外：如果后端使用 Jinja 模板渲染（render_template），则表单应使用原生 `<form action="/xxx" method="post">` + `type="submit"`，由后端处理重定向。此时不需要 fetch。**

5. **JS 初始化铁律**
   - 每个 .js 文件必须在末尾包含 `DOMContentLoaded` 初始化代码
   - 如果定义了 class，必须在 `DOMContentLoaded` 回调中 new 它，禁止只定义不实例化！
   - 如果 class 中有用于创建/渲染 DOM 的方法（如 `renderEditor`、`renderUI`），必须在 constructor 或 `init()` 中调用，禁止定义了不用！

6. **事件绑定与动态 DOM 铁律 (Event Delegation)**
   - 🚨 **动态 DOM 绑定失效坑**：如果是通过 `fetch` 获取数据后用 `innerHTML` 动态拼接生成的元素，**绝对禁止在 `DOMContentLoaded` 就去 `getElementById` 绑定事件！**
   - **正确做法（事件委托）**：将事件绑定到永远存在的父容器上：
     ```javascript
     document.getElementById('list-container').addEventListener('click', (e) => {
         if (e.target.closest('.delete-btn')) {
             const id = e.target.closest('.delete-btn').dataset.id;
             this.deleteItem(id);
         }
     });
     ```
<!-- P0:END -->

<!-- P1:START -->
### HTML 文档结构模板
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

### JS 初始化模板
```javascript
document.addEventListener('DOMContentLoaded', () => new MyApp());
```
<!-- P1:END -->
