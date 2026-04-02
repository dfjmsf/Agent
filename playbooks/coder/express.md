# Express.js 后端编码规范

## 项目结构
```
src/
  index.js / server.js    # 入口文件（app 创建 + 启动）
  routes/
    users.js              # 用户路由模块
    posts.js              # 帖子路由模块
  models/
    User.js               # 数据模型
  middleware/
    errorHandler.js       # 全局错误处理中间件
  config/
    db.js                 # 数据库连接配置
  package.json
```

## 核心规则

### 1. App 创建与启动
```javascript
const express = require('express');
const cors = require('cors');
const app = express();

// 中间件
app.use(cors());
app.use(express.json());
app.use(express.urlencoded({ extended: true }));

// 路由注册
const userRoutes = require('./routes/users');
app.use('/api/users', userRoutes);

// 全局错误处理（必须在路由之后）
app.use((err, req, res, next) => {
    console.error(err.stack);
    res.status(500).json({ error: 'Internal Server Error' });
});

const PORT = process.env.PORT || 5001;
app.listen(PORT, () => {
    console.log(`Server running on port ${PORT}`);
});
```
- CORS 必须配置（前后端分离）
- `express.json()` 必须在路由之前
- 错误处理中间件必须在路由之后（4 个参数：err, req, res, next）
- **禁止 8000 端口**

### 2. 路由模块化（Router）
```javascript
// routes/users.js
const express = require('express');
const router = express.Router();

// GET /api/users
router.get('/', async (req, res, next) => {
    try {
        const users = await User.findAll();
        res.json(users);
    } catch (err) {
        next(err);  // 传递给全局错误处理
    }
});

// POST /api/users
router.post('/', async (req, res, next) => {
    try {
        const { username, email } = req.body;
        const user = await User.create({ username, email });
        res.status(201).json(user);
    } catch (err) {
        next(err);
    }
});

// PUT /api/users/:id
router.put('/:id', async (req, res, next) => {
    try {
        const user = await User.findByPk(req.params.id);
        if (!user) return res.status(404).json({ error: 'Not Found' });
        await user.update(req.body);
        res.json(user);
    } catch (err) {
        next(err);
    }
});

// DELETE /api/users/:id
router.delete('/:id', async (req, res, next) => {
    try {
        const user = await User.findByPk(req.params.id);
        if (!user) return res.status(404).json({ error: 'Not Found' });
        await user.destroy();
        res.status(204).send();
    } catch (err) {
        next(err);
    }
});

module.exports = router;
```
- 每个路由处理器用 `async/await` + `try/catch` + `next(err)`
- **禁止** 在路由中直接 `throw` 而不 `catch`（会导致进程崩溃）
- 参数校验在路由内完成，不依赖外部中间件
- 404 返回 JSON，不返回 HTML

### 3. 数据库集成（Sequelize ORM）
```javascript
// config/db.js
const { Sequelize } = require('sequelize');
const path = require('path');

const sequelize = new Sequelize({
    dialect: 'sqlite',
    storage: path.join(__dirname, '..', 'data.db'),
    logging: false,
});

module.exports = sequelize;

// models/User.js
const { DataTypes } = require('sequelize');
const sequelize = require('../config/db');

const User = sequelize.define('User', {
    username: { type: DataTypes.STRING(50), unique: true, allowNull: false },
    email: { type: DataTypes.STRING(100) },
}, {
    timestamps: true,  // 自动 createdAt/updatedAt
});

module.exports = User;
```

### 4. 数据库初始化
```javascript
// index.js 启动时同步表结构
const sequelize = require('./config/db');

async function startServer() {
    await sequelize.sync();  // 创建表（幂等）
    app.listen(PORT, () => console.log(`Server on ${PORT}`));
}

startServer();
```
- `sequelize.sync()` 必须在 `app.listen()` 之前
- 开发阶段用 `sync({ alter: true })` 自动更新表结构

### 5. 请求参数获取
```javascript
req.body          // POST/PUT 请求体（需 express.json() 中间件）
req.params.id     // 路由参数 /users/:id
req.query.page    // 查询参数 ?page=1
req.headers       // 请求头
```

### 6. 响应格式统一
```javascript
// 成功
res.json({ data: result });
res.status(201).json(created);

// 错误
res.status(400).json({ error: 'Invalid data' });
res.status(404).json({ error: 'Not Found' });
res.status(500).json({ error: 'Server Error' });

// 无内容
res.status(204).send();
```
- **禁止** `res.send(JSON.stringify(data))`，必须用 `res.json()`
- **禁止** 返回 HTML 错误页面，必须返回 JSON

### 7. 静态文件服务（前后端同仓）
```javascript
const path = require('path');
app.use(express.static(path.join(__dirname, '..', 'frontend', 'dist')));

// SPA fallback
app.get('*', (req, res) => {
    res.sendFile(path.join(__dirname, '..', 'frontend', 'dist', 'index.html'));
});
```
- 静态文件中间件必须在 API 路由之后（避免冲突）
- SPA fallback 必须是最后一个路由

### 8. package.json 依赖
```json
{
  "dependencies": {
    "express": "^4.18.0",
    "cors": "^2.8.5",
    "sequelize": "^6.35.0",
    "sqlite3": "^5.1.0"
  },
  "scripts": {
    "start": "node src/index.js",
    "dev": "node src/index.js"
  }
}
```
