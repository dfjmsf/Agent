# Flask-SQLAlchemy ORM 编码规范（补丁）

## 适用场景
当项目 `architecture_contract.orm_mode == "flask_sqlalchemy"` 时，**强制注入**此规范。
此补丁与 `sqlalchemy_orm.md` 互斥，二者不可同时生效。

## 核心规则

### 1. 初始化方式（唯一正确写法）
```python
from flask_sqlalchemy import SQLAlchemy
import os

db = SQLAlchemy()

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")
```

### 2. 模型定义（使用 db.Model 基类）
```python
class Expense(db.Model):
    __tablename__ = "expenses"
    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    amount = db.Column(db.Float, nullable=False)
    category = db.Column(db.String(50), nullable=False)
    date = db.Column(db.String(20), nullable=False)
    note = db.Column(db.Text, default="")

    def to_dict(self):
        return {
            "id": self.id,
            "amount": self.amount,
            "category": self.category,
            "date": self.date,
            "note": self.note,
        }
```

### 3. to_dict() 铁律
- **每个模型必须实现 `to_dict()` 方法**，返回纯 JSON 可序列化的字典
- DateTime 字段必须 `.isoformat()` 转换
- 路由层调用 `model.to_dict()` 返回数据，**禁止手动构建字典**

### 4. init_db() 的正确实现
```python
def init_db(app):
    """在 Flask app 上下文中创建所有表"""
    db.init_app(app)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{DB_PATH}"
    app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    with app.app_context():
        db.create_all()
```

**⚠️ 注意**：Flask-SQLAlchemy 的 `init_db` 需要接收 `app` 参数！
如果 `module_interfaces` 中定义了 `init_db() -> None`（无参数），可以这样兼容：
```python
# 如果 app.py 用 init_db() 无参调用，在 models.py 中缓存 app 引用
_app = None

def init_db(app=None):
    """初始化数据库（兼容有参/无参调用）"""
    global _app
    if app is not None:
        _app = app
        db.init_app(app)
        app.config.setdefault("SQLALCHEMY_DATABASE_URI", f"sqlite:///{DB_PATH}")
        app.config.setdefault("SQLALCHEMY_TRACK_MODIFICATIONS", False)
    if _app is not None:
        with _app.app_context():
            db.create_all()
```

### 5. CRUD 函数（独立暴露）
```python
def create_expense(amount, category, date, note=""):
    """创建支出记录"""
    expense = Expense(
        amount=float(amount),
        category=category,
        date=date,
        note=note or "",
    )
    db.session.add(expense)
    db.session.commit()
    return expense.to_dict()

def get_all_expenses():
    """获取所有支出"""
    return [e.to_dict() for e in Expense.query.order_by(Expense.id.desc()).all()]

def get_expense_by_id(expense_id):
    """按 ID 获取单条支出"""
    expense = Expense.query.get(expense_id)
    return expense.to_dict() if expense else None

def update_expense(expense_id, amount, category, date, note=""):
    """更新支出"""
    expense = Expense.query.get(expense_id)
    if expense:
        expense.amount = float(amount)
        expense.category = category
        expense.date = date
        expense.note = note or ""
        db.session.commit()

def delete_expense(expense_id):
    """删除支出"""
    expense = Expense.query.get(expense_id)
    if expense:
        db.session.delete(expense)
        db.session.commit()
```

### 6. Session 管理
- Flask-SQLAlchemy **自动管理 session 生命周期**，不需要手动 `close()`
- 使用 `db.session` 全局访问，不要创建 `SessionLocal()`
- **禁止** `from sqlalchemy.orm import sessionmaker`
- **禁止** `from sqlalchemy import create_engine`

### 7. get_db() 兼容函数（可选）
如果 `module_interfaces` 要求 `get_db()` 函数，可以这样兼容：
```python
def get_db():
    """获取数据库 session（兼容接口）"""
    return db.session
```

### 8. app.py 集成示例
```python
from flask import Flask
from models import db, init_db

app = Flask(__name__)
app.secret_key = 'your-secret-key'
app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///data.db"
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)

with app.app_context():
    db.create_all()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
```

## 🚫 绝对禁止
- `from sqlalchemy import create_engine` — 使用 `db = SQLAlchemy()` 替代
- `from sqlalchemy.orm import declarative_base` — 使用 `db.Model` 替代
- `from sqlalchemy.orm import sessionmaker` — 使用 `db.session` 替代
- `Base = declarative_base()` — 使用 `db.Model` 替代
- `SessionLocal = sessionmaker(...)` — 使用 `db.session` 替代
- `engine = create_engine(...)` — Flask-SQLAlchemy 自动管理引擎
