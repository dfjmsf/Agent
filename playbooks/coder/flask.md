# Flask 后端编码规范

## ⚠️ 最重要：选择架构模式（二选一，不可混用）

### 模式 A：Jinja 模板渲染（SSR）
- 后端用 `render_template()` 返回 HTML
- 前端用 `<form action="/add" method="post">` 提交数据
- 前端用 `{{ variable }}` 和 `{% for %}` 显示数据
- **适用于：简单 CRUD 应用**

### 模式 B：REST API（SPA）
- 后端用 `jsonify()` 返回 JSON
- 前端用 `fetch('/api/xxx')` + `JSON.parse()` 处理数据
- 前端用 JS 动态渲染 DOM
- **适用于：前后端分离应用**

### 🚫 绝对禁止
- 混用两种模式！后端 `render_template()` + 前端 `fetch()` = 必崩！
- routes.py 用 `request.form` 但 HTML 用 `fetch + JSON.stringify` = 必崩！
- **⛔ 严禁使用以下包（违反 = CSRF 崩溃 / 静默提交失败）：**
  - `flask_wtf` — 禁止 import
  - `wtforms` — 禁止 import
  - `FlaskForm` — 禁止使用
  - `CSRFProtect` — 禁止使用
  - `validate_on_submit()` — 禁止调用
  - **原因**：FlaskForm 需要 CSRF token，但 Coder 生成的 HTML 模板几乎不会包含 `{{ form.hidden_tag() }}`，导致表单提交报 400 Bad Request: CSRF token missing
  - **正确做法**：`request.form['amount']` + 手动 `if not amount: flash('错误')` 校验
- **严禁在 init_db() 中往主数据表（如 expenses）INSERT 种子数据！**
  - NOT NULL 字段缺失 → IntegrityError → commit 不执行 → CREATE TABLE 回滚 → `no such table`
  - 分类/标签数据必须用独立的 categories 表，见第 6 节

---

## 项目结构
```
src/
  models.py      # 数据模型 + CRUD 函数
  routes.py      # 路由处理函数
  app.py         # Flask app 实例 + 启动入口
templates/       # Jinja 模板（模式 A 使用）
  index.html
```

## 核心规则

### 1. App 入口文件（app.py）

#### 模式 A（Jinja 模板）：
```python
from flask import Flask
from models import init_db
from routes import index, add_expense_route

# ⚠️ 标准布局：app.py 和 templates/ 在同一目录下
# 此时 Flask 默认就能找到 templates/，不需要任何额外参数！
app = Flask(__name__)
app.secret_key = 'your-secret-key-here'

init_db()

app.add_url_rule('/', 'index', index, methods=['GET'])
app.add_url_rule('/add', 'add_expense', add_expense_route, methods=['GET', 'POST'])

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
```

#### 模式 B（REST API）：
```python
from flask import Flask, send_file
from models import init_db
import routes

app = Flask(__name__, static_folder='.', static_url_path='')

init_db()

@app.route('/')
def index():
    return send_file('index.html')

# 注册 API 路由
routes.register(app)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
```

### 2. template_folder + static_folder 路径铁律（违反 = TemplateNotFound / 404）

> ⚠️ **此条为 0 号铁律，优先级最高！每次生成 Flask() 实例时必须严格执行！**

- **默认情况**：app.py 和 templates/ 在同一目录（平铺布局）→ **不要传任何路径参数**！
- `Flask(__name__)` 默认在 **`app.py 所在目录`** 下找 `templates/` 和 `static/`
- **只有当 app.py 在子目录（如 src/）而 templates/ 在父目录时**，才需要手动指定路径

```python
# ✅ 标准布局（绝大多数情况）：app.py 和 templates/ 在同一目录
# 项目结构：app.py, templates/, models.py, routes.py 都在根目录
app = Flask(__name__)  # 不要加 template_folder！

# ❌❌❌ 致命错误：app.py 在根目录但写了 ../templates
app = Flask(__name__, template_folder='../templates')  # → TemplateNotFound！

# ✅ 仅当 app.py 在 src/ 子目录时才需要（罕见）：
app = Flask(__name__, template_folder='../templates', static_folder='../static')
```

**自查清单**（每次写 Flask() 时必须过）：
1. `app.py` 在哪个目录？
2. `templates/` 在哪个目录？
3. **如果在同一目录 → 用 `Flask(__name__)`，不加任何路径参数！**
4. 只有不在同一目录时才手动指定
2. `templates/` 在哪个目录？
3. `static/` 在哪个目录？
4. 如果三者不在同一目录 → **必须显式指定相对路径**


### 3. 路由注册
```python
# 方式一：函数式（简单项目推荐）
app.add_url_rule('/', 'index', index, methods=['GET'])

# 方式二：Blueprint（3+ 路由推荐）
from flask import Blueprint
bp = Blueprint('api', __name__, url_prefix='/api')

@bp.route('/users', methods=['GET'])
def get_users():
    ...

# app.py 注册
app.register_blueprint(bp)
```

#### 🚫 SSR 表单路由铁律（违反 = 405 Method Not Allowed）

凡是有**独立表单页面**（如 `add.html`、`edit.html`）的路由，**必须同时支持 GET 和 POST**：
- GET = 显示空白表单页面
- POST = 处理表单提交

```python
# ✅ 正确：GET 显示表单，POST 处理提交
app.add_url_rule('/add', 'add', routes.add, methods=['GET', 'POST'])

# routes.py 中必须处理两种情况：
def add():
    if request.method == 'POST':
        # 处理表单提交
        amount = float(request.form['amount'])
        add_expense(amount, ...)
        return redirect(url_for('index'))
    else:
        # GET: 显示空白表单页面
        return render_template('add.html')

# ❌ 致命错误：只注册 POST
app.add_url_rule('/add', 'add', routes.add, methods=['POST'])
# → 用户点击 <a href="/add"> 发送 GET → 405 Method Not Allowed！

# ❌ 致命错误：routes.py 中只处理 POST 逻辑，没有 GET 分支
def add():
    amount = float(request.form['amount'])  # GET 请求没有 form 数据 → 崩溃！
    ...
```

**自查**：如果存在 `templates/add.html` 或 `templates/edit.html`，对应路由必须有 `methods=['GET', 'POST']`！

### 4. 请求与响应

#### 模式 A（Jinja）：
```python
# 接收表单数据
amount = float(request.form['amount'])
# ⚠️ request.form 的 key 必须与 HTML <input name="xxx"> 完全一致！

# 返回模板
return render_template('index.html', expenses=expenses, categories=categories)

# 提交后重定向
return redirect(url_for('index'))
```

**⚠️ 表单 action 铁律**：HTML 模板中 `<form action>` **必须**使用 `url_for()` 生成 URL，**禁止**硬编码路径！

```html
<!-- ✅ 正确：用 url_for 自动解析路由 URL -->
<form action="{{ url_for('add_expense') }}" method="post">

<!-- ❌ 致命错误：硬编码路径可能与路由注册不一致 → 404 -->
<form action="/add_expense" method="post">
```

#### 模式 B（API）：
```python
# 接收 JSON
data = request.get_json()
amount = float(data['amount'])

# 返回 JSON（禁止 json.dumps，必须 jsonify）
return jsonify({"key": "value"}), 200
return jsonify({"error": "Not Found"}), 404
```

**🚫 模式 B 铁律：前端 fetch 的路由禁止 redirect()**
- 前端用 `fetch('/api/xxx', {method: 'DELETE'})` 发非 GET 请求
- 后端用 `redirect()` 响应 → 浏览器收到 302 → 自动跟随变成 GET → 405 Method Not Allowed
- **所有被 fetch/axios 调用的路由，必须返回 `jsonify()` 响应，严禁 `redirect()`**

```python
# ✅ 正确：返回 JSON
@bp.route('/api/items/<int:id>', methods=['DELETE'])
def delete_item(id):
    # ... 删除逻辑 ...
    return jsonify({"success": True}), 200

# ❌ 致命错误：fetch DELETE 收到 302 会变 GET → 405
@bp.route('/api/items/<int:id>', methods=['DELETE'])
def delete_item(id):
    # ... 删除逻辑 ...
    return redirect(url_for('index'))  # 永远不要这样做！
```

### 5. 表单字段名一致性铁律
HTML `<input name="xxx">` 或 `<select name="xxx">` 的 name 属性
**必须**与 routes.py 中 `request.form['xxx']` 的 key **完全一致**！

```html
<!-- 前端 -->
<input name="amount">           <!-- name="amount" -->
<select name="category_id">     <!-- name="category_id" -->
```
```python
# 后端 — key 必须与 HTML name 一致！
amount = request.form['amount']           # ✅ 'amount'
category_id = request.form['category_id'] # ✅ 'category_id'
category_id = request.form['category']    # ❌ 不匹配！
```

### 7. 错误处理
```python
@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not Found"}), 404

@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "Internal Server Error"}), 500
```

### 8. CORS 配置（仅模式 B 需要）
```python
@app.after_request
def add_cors(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    response.headers['Access-Control-Allow-Methods'] = 'GET,POST,PUT,DELETE,OPTIONS'
    return response
```

### 9. 启动配置
```python
if __name__ == "__main__":
    init_db()  # 有数据库就必须调用
    app.run(host="0.0.0.0", port=5001, debug=True)
```
- 端口必须与 api_contracts.base_url 中的端口一致
- **禁止** 使用 8000 端口（被系统后端占用）

### 10. SQLite 日期类型铁律

> ⚠️ SQLite 没有原生 DATE 类型！`date TEXT NOT NULL` 存进去的是字符串 `"2026-04-10"`，读出来也是字符串！

从数据库读取日期用于回填编辑表单时，**直接用字符串即可**（因为 HTML `<input type="date">` 的 value 本身就是字符串格式 `YYYY-MM-DD`）：

```python
# ✅ 正确：编辑页面回填日期
def edit_expense(expense_id):
    expense = get_expense_by_id(expense_id)
    if request.method == 'POST':
        date_str = request.form['date']  # HTML input[type=date] 返回 "2026-04-10"
        update_expense(expense_id, ..., date=date_str, ...)
        return redirect(url_for('index'))
    # GET: 传给模板，input value 直接填字符串
    return render_template('edit.html', expense=expense)
```

```html
<!-- 模板中日期回填 — expense['date'] 本身就是 "2026-04-10" 字符串，完美匹配 -->
<input type="date" name="date" value="{{ expense.date }}">
```

**关键**：既然禁止了 WTForms，就不存在 `.strftime()` 调用的问题。`request.form['date']` 拿到的是字符串，直接存进 SQLite TEXT 字段即可。
