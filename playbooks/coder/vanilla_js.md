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
   - **例外：如果后端使用 Jinja 模板渲染（render_template），则表单应使用原生 `<form action="/xxx" method="post">` + `type="submit"`，由后端处理重定向。此时不需要 fetch。**

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

## Jinja 模板规则（如果 HTML 包含 `{% %}` / `{{ }}` 语法）

7. **Jinja 数据字段一致性铁律**
   - `{{ expense.xxx }}` 中的 xxx 必须是**后端传入数据中实际存在的字段**。
   - **查看依赖文件中 models.py 的 SQL 查询返回了哪些字段**来确定可用字段名。
   - 数据是 dict（来自 `sqlite3.Row`），不是对象！禁止用 `expense.category.name` 这种嵌套访问！
   - 如果 SQL 用了 `AS category_name`，模板中用 `{{ expense.category_name }}`，不是 `{{ expense.category.name }}`。

8. **Jinja 表单字段名一致性**
   - `<input name="xxx">` 的 name 必须与依赖文件中 routes.py 的 `request.form['xxx']` 完全一致！
   - 写 HTML 前**先查看 routes.py 中的 request.form key**，严格对齐。

9. **Jinja 禁止在序列化数据上调用方法**
   - 如果后端用了 `to_dict()` 转换数据，所有字段都是 str/int/float/dict，**不是 Python 对象**！
   - `{{ expense.timestamp.strftime('%Y-%m-%d') }}` → ❌ 崩溃（str 没有 strftime）
   - `{{ expense.timestamp }}` → ✅ 直接显示字符串
   - **规则：Jinja 模板中只做 `{{ xxx }}` 显示和 `{% for %}` 循环，禁止调用 `.strftime()` / `.lower()` 等 Python 方法**

10. **禁止引用不存在的模板文件**
    - `{% extends "base.html" %}` 和 `{% include "header.html" %}` 会引用其他模板文件。
    - **如果 base.html / header.html 不在当前项目的文件列表中 → 绝对禁止使用 extends/include！**
    - 所有 HTML 内容必须写在单个 index.html 中（包含完整的 `<!DOCTYPE html>` 结构）。
    - `{% extends "base.html" %}` + base.html 不存在 = `TemplateNotFound` 崩溃！
