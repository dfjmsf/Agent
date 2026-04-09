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

# template_folder 必须指向 templates 目录的正确相对路径！
# 如果 app.py 在 src/ 而 templates/ 在项目根目录：
app = Flask(__name__, template_folder='../templates')

init_db()

app.add_url_rule('/', 'index', index, methods=['GET'])
app.add_url_rule('/add', 'add_expense', add_expense_route, methods=['POST'])

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

### 2. template_folder 路径铁律
- `Flask(__name__)` 默认在 `app.py 所在目录/templates/` 找模板
- 如果 app.py 在 `src/`，templates 在项目根的 `templates/`：
  **必须** `Flask(__name__, template_folder='../templates')`
- **无论在 app.py 还是 routes.py 的 create_app() 中创建 Flask 实例，都必须配 template_folder！**
- 路径错误 → `TemplateNotFound` 错误！

```python
# ✅ 正确：无论在哪创建都带 template_folder
def create_app():
    app = Flask(__name__, template_folder='../templates')
    return app

# ❌ 致命错误：
def create_app():
    app = Flask(__name__)  # 找不到模板！
    return app
```


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

### 6. 数据库集成
- SQLite 路径必须基于 `__file__`：
```python
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")
```

**🚫 种子数据铁律（违反 = 前端空白，严重 bug）**

1. 凡涉及"分类/类型/标签"功能，**必须创建独立的 categories 表**，不能只在主表用 TEXT 字段：
```python
# ❌ 致命错误：category 是 TEXT → 新数据库时分类列表为空！
cursor.execute('CREATE TABLE IF NOT EXISTS expenses (category TEXT)')

# ✅ 正确：独立 categories 表 + 外键关联
cursor.execute('CREATE TABLE IF NOT EXISTS categories (id INTEGER PRIMARY KEY, name TEXT UNIQUE)')
cursor.execute('CREATE TABLE IF NOT EXISTS expenses (category_id INTEGER REFERENCES categories(id))')
```

2. `init_db()` **必须**包含种子数据，否则前端下拉框为空：
```python
def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS categories (...)')
    # 必须有种子数据！
    cursor.execute("INSERT OR IGNORE INTO categories (name) VALUES ('餐饮')")
    cursor.execute("INSERT OR IGNORE INTO categories (name) VALUES ('交通')")
    cursor.execute("INSERT OR IGNORE INTO categories (name) VALUES ('购物')")
    cursor.execute("INSERT OR IGNORE INTO categories (name) VALUES ('娱乐')")
    conn.commit()
    conn.close()
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

### 10. 跨文件数据一致性（模式 A 极重要）

#### 10.1 routes.py → index.html 数据字段一致性
`render_template('index.html', expenses=expenses)` 传入的数据是什么结构，
index.html 中 `{{ expense.xxx }}` 就只能引用该结构中存在的字段！

**关键：查看 models.py 中数据查询函数的返回结构来决定模板中能用什么字段。**

```python
# models.py 中的 get_all_expenses() 返回：
# SELECT e.id, e.amount, e.description, e.date, c.name as category_name
# → 返回的 dict 有: id, amount, description, date, category_name
```
```html
<!-- index.html 中只能用这些字段！ -->
{{ expense.amount }}         ✅ 存在
{{ expense.category_name }}  ✅ 存在（SQL AS 别名）
{{ expense.category.name }}  ❌ 不存在！expense 是 dict 不是对象！
{{ expense.category_id }}    ❌ SQL 没有 SELECT 这个字段！
```

#### 10.2 routes.py 的 request.form key 必须查 HTML
写 routes.py 的 `request.form['xxx']` 时：
**先查看依赖文件中 index.html 的 `<input name="xxx">` / `<select name="xxx">`，name 必须完全一致！**

如果还没有 index.html（先写 routes.py），则：
- 在 routes.py 的注释中标注每个 form key
- 后续写 index.html 时严格参照

#### 10.3 index.html 的表单 name 必须查 routes.py
写 index.html 的 `<input name="xxx">` 时：
**先查看依赖文件中 routes.py 的 `request.form['xxx']`，name 必须完全一致！**

#### 10.4 to_dict() 序列化后的数据类型
如果 models.py 使用 `to_dict()` 将 ORM 对象转为 dict：
- `datetime` 字段变成了 **字符串**（isoformat），不能再调 `.strftime()`！
- `relationship` 字段变成了**嵌套 dict**，不是 ORM 对象

**二选一**：
- 方案 A（推荐）：`to_dict()` 中直接格式化好日期字符串，模板直接 `{{ expense.date }}`
- 方案 B：routes.py 传原始 ORM 对象给模板（不调 to_dict），模板可以用 `.strftime()`

```python
# 方案 A: to_dict 中格式化（推荐）
def to_dict(self):
    return {
        "date": self.timestamp.strftime('%Y-%m-%d %H:%M'),  # ← 这里格式化
        "category_name": self.category.name,  # ← 展平嵌套关系
    }
```
```html
<!-- 模板直接显示，不调方法 -->
{{ expense.date }}          ✅ 字符串直接显示
{{ expense.category_name }} ✅ 展平后的字符串
{{ expense.timestamp.strftime(...) }} ❌ 崩溃！str 没有 strftime！
{{ expense.category.name }}  ❌ 嵌套 dict 可能不一致！
```
