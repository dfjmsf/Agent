# SQLAlchemy ORM 编码规范（补丁）

## 适用场景
当项目使用 SQLAlchemy（配合 FastAPI、Flask 或独立使用）时，注入此规范。

## 核心规则

### 1. 模型定义
```python
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, ForeignKey, Text, Boolean
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from datetime import datetime
import os

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")
engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False)
    email = Column(String(100))
    created_at = Column(DateTime, default=datetime.utcnow)

    # 关系
    posts = relationship("Post", back_populates="author", cascade="all, delete-orphan")

    def to_dict(self):
        return {"id": self.id, "username": self.username, "email": self.email,
                "created_at": self.created_at.isoformat() if self.created_at else None}
```

### 2. to_dict() 铁律
- **每个模型必须实现 `to_dict()` 方法**，返回纯 JSON 可序列化的字典
- DateTime 字段必须 `.isoformat()` 转换
- 关系字段不要递归展开（避免循环引用），只返回 ID
- 路由层调用 `model.to_dict()` 返回数据，**禁止手动构建字典**

### 3. CRUD 函数（独立暴露）
```python
def init_db():
    """创建所有表（幂等操作）"""
    Base.metadata.create_all(engine)

def get_session():
    """获取数据库会话"""
    return SessionLocal()

def create_user(username: str, email: str = None) -> dict:
    session = get_session()
    try:
        user = User(username=username, email=email)
        session.add(user)
        session.commit()
        session.refresh(user)
        return user.to_dict()
    finally:
        session.close()

def get_all_users() -> list:
    session = get_session()
    try:
        users = session.query(User).all()
        return [u.to_dict() for u in users]
    finally:
        session.close()
```
- **必须暴露独立的 CRUD 函数**（`create_xxx`, `get_xxx`, `update_xxx`, `delete_xxx`）
- 路由层 `from models import create_user` 直接调用，不操作 session
- 每个函数内部 `get_session()` + `try/finally session.close()`
- **防御性类型转换铁律**：数值字段必须在写入前转换类型！
  ```python
  # 正确（防御来自 JSON 的字符串值）
  expense.amount = float(data.get("amount", 0))
  expense.quantity = int(data.get("quantity", 1))
  
  # 错误（直接赋值，可能收到字符串导致 StatementError）
  expense.amount = data.get("amount")  # ❌ 如果 amount="100" → 崩溃
  ```

### 4. Session 管理（防泄漏）
- **禁止** 在模块顶层创建 session（`session = SessionLocal()` ❌）
- **必须** 在每个函数内创建并关闭 session
- 使用 `try/finally` 或 `with` contextmanager 确保关闭
- FastAPI 场景推荐 `Depends(get_db)` 依赖注入

### 5. 关系处理
```python
# 一对多
class Post(Base):
    __tablename__ = "posts"
    id = Column(Integer, primary_key=True)
    title = Column(String(200), nullable=False)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    author = relationship("User", back_populates="posts")
```
- 外键 `ForeignKey` 引用表名（字符串），不是类名
- `relationship` 的 `back_populates` 双向必须对称
- 级联删除：`cascade="all, delete-orphan"`

### 6. 数据库初始化调用
- main.py / app.py 启动时 **必须调用 `init_db()`**
- 遗漏 `init_db()` 是 "no such table" 错误的第一杀手
```python
# main.py
from models import init_db
init_db()  # 必须在路由注册之前
```

### 7. 迁移注意
- 开发阶段用 `create_all()` 即可
- 生产环境应使用 Alembic（但在 ASTrea 项目中不需要）
