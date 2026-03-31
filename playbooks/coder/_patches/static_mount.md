# 后端挂载前端静态文件规则（入口文件专用补丁）

## 挂载规则

1. **如果项目包含 frontend/ 目录，入口文件必须挂载静态文件服务**
   ```python
   from fastapi.staticfiles import StaticFiles
   import os
   
   current_dir = os.path.dirname(os.path.abspath(__file__))
   frontend_path = os.path.join(current_dir, "..", "frontend")
   
   if os.path.exists(frontend_path):
       app.mount("/", StaticFiles(directory=frontend_path, html=True), name="static")
   ```

2. **🚨 致命警告：`app.mount("/", ...)` 必须放在所有 API 路由注册的最后！**
   - 如果在最前面 mount `/`，StaticFiles 会拦截所有请求（包括 `/api/xxx`），导致全部 404！
   - 正确顺序：先注册所有 `/api/` 路由 → 最后挂载静态文件。

3. **原因**
   - 前端代码部署在后端同端口，前端发起的 API 请求使用相对路径（如 `/api/xxx`）。
   - 如果不挂载前端，用户访问根路径 `/` 时会直接 404。

4. **Vite 构建项目：挂载 dist/ 目录**
   - 如果项目使用 Vite 构建（有 `package.json` + `vite.config.js`），构建产物在 `dist/` 目录。
   - 入口文件应优先检测 `dist/`，其次 `frontend/`：
   ```python
   current_dir = os.path.dirname(os.path.abspath(__file__))
   
   # 优先 Vite 构建产物
   dist_path = os.path.join(current_dir, "dist")
   frontend_path = os.path.join(current_dir, "frontend")
   
   static_dir = None
   if os.path.isdir(dist_path):
       static_dir = dist_path
   elif os.path.isdir(frontend_path):
       static_dir = frontend_path
   
   if static_dir:
       app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
   ```
