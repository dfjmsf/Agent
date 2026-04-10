# SSR 及跨文件数据契约规范（补丁）

> ⚠️ **当项目采用 SSR 模板渲染（如 Flask+Jinja, Django+DTL）时，必须严格遵守以下法则！**

### 1. 🔗 SSR 表单提交 ↔ 后端路由一致性铁律
> 表单的**提交路径**和**请求方法**、**字段名称**必须与后端路由**100% 对齐**，否则会导致 405 错误或 KeyError。

1. **`action` 和 `method` 对齐**：
   ```html
   <!-- ✅ 前端：纯 GET 请求无需 action -->
   <form method="GET">
   
   <!-- ✅ 前端：POST 提交，明确 action="/add" -->
   <form method="POST" action="/add">
   ```
   ```python
   # ✅ 后端：路由必须响应 POST，路径为 /add
   @app.route('/add', methods=['POST'])
   ```

2. **表单 `name` 属性 ↔ `request.form` 键名对齐**：
   - 编写 `request.form['xxx']` 后，必须全局搜索 HTML，确认确实存在 `<input name="xxx">`！
   - 写错一个字母 ＝ 提交流程崩溃报 400 Bad Request。

### 2. 🔗 SQL 别名 ↔ 模板变量一致性铁律
> 违反此铁律会导致模板引擎抛出 `UndefinedError`！

- 模板中 `{{ item.xxx }}` 的 `xxx` **必须**与 SQL 的 `SELECT ... AS xxx` 的别名完全一致！
- **后端传递给前端的是一个字典（dict），并不是可以无限嵌套的对象（如 `item.category.name` 是非法的，除非你自己手动构造嵌套字典）。**

**❌ 致命错误示例**：
```python
# SQL 中并没有 'total'，只有 'total_amount'
cursor.execute('SELECT SUM(amount) AS total_amount FROM expenses')
```
```html
<!-- HTML 错误引用了不存在的变量名 -->
<td>{{ item.total }}</td> 
<!-- 嵌套调用也是错误的，SQL 返回的只是一维字典 dict -->
<td>{{ item.category.name }}</td>
```

**✅ 正确做法清单**：
1. 先写好 SQL 查询：`SELECT c.name AS category_name, SUM(e.amount) AS total_amount ...`
2. HTML 中紧贴上述别名调用：`{{ item.category_name }}` 和 `{{ item.total_amount }}`，**一字不差**！

### 3. 🚫 序列化数据禁止调用 Python 方法
> 如果后端用了 `to_dict()` 或 `dict(row)` 转换，所有字段都是 `str/int/float/dict`，**不再是 Python 对象**！

- `{{ expense.timestamp.strftime('%Y-%m-%d') }}` → ❌ 崩溃（`str` 没有 `strftime` 方法）
- `{{ expense.timestamp }}` → ✅ 直接显示字符串
- **规则：Jinja 模板中只做 `{{ xxx }}` 显示和 `{% for %}` 循环，禁止调用 `.strftime()` / `.lower()` 等 Python 方法**

### 4. 🚫 禁止引用不存在的模板文件
- `{% extends "base.html" %}` 和 `{% include "header.html" %}` 会引用其他模板文件。
- **如果 `base.html` / `header.html` 不在当前项目的文件列表中 → 绝对禁止使用 `extends`/`include`！**
- 所有 HTML 内容必须写在单个 `index.html` 中（包含完整的 `<!DOCTYPE html>` 结构）。
- `{% extends "base.html" %}` + `base.html` 不存在 = `TemplateNotFound` 崩溃！

### 5. 🔗 render_template() 顶层变量名一致性铁律
> 违反此铁律会导致模板循环为空或直接 `UndefinedError`！

- `render_template('index.html', summary=data)` 传入的关键字参数名是 `summary`。
- 模板中 `{% for item in summaries %}` 引用的是 `summaries` — **不一致！**
- **自查**：写完 `render_template(xxx, key1=..., key2=...)` 后，打开模板文件，确认所有 `{% for x in YYY %}` 和 `{% if YYY %}` 中的 `YYY` 都在 `key1, key2, ...` 里面。
