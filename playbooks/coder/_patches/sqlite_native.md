# SQLite 原生数据库集成规范（补丁）

> ⚠️ **当项目使用原生 `sqlite3` 时，必须严格遵守以下规范！**

### 1. 数据库路径铁律
- SQLite 路径必须基于 `__file__`，以确保无论在何处运行项目都能正确找到文件：
```python
import os
import sqlite3

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.db")
```

### 2. 必须在启动时初始化（创建表）
> **致命铁律：不初始化 = `no such table` 错误 = 所有 API/页面 崩溃！**
- `models.py` 中必须定义 `init_db()` 函数，用来执行 `CREATE TABLE IF NOT EXISTS`。
- **必须在 `main.py` 或 `app.py` 启动环境时最先调用 `init_db()`**。

### 3. 🚫 种子数据铁律（违反 = 严重崩溃）
> ⚠️ **绝对禁止在 `init_db()` 中向主业务表（如 expenses, orders, articles）INSERT 种子数据！**
- 主表通常包含 NOT NULL 的外键或必填业务字段，直接插入假数据极易触发 `IntegrityError`。
- 哪怕有一个 `INSERT` 失败，整个 `init_db` 事务回滚，会导致之后永远报 `no such table`！
- **允许的正确做法**：仅向只读配置表（如 `categories`, `tags`）插入预设分类数据，例如：
  ```python
  def init_db():
      # ... 建表语句 ...
      for name in ['餐饮', '交通', '购物', '娱乐']:
          cursor.execute("INSERT OR IGNORE INTO categories (name) VALUES (?)", (name,))
      conn.commit()
  ```

### 4. 获取返回结果（Dict 格式）
原生 `sqlite3.Row` 默认像元组，需要转为字典以方便前端序列化或模板渲染：
```python
conn.row_factory = sqlite3.Row
cursor = conn.cursor()
cursor.execute('SELECT * FROM users')
rows = cursor.fetchall()
return [dict(row) for row in rows]  # 转为标准字典列表
```
