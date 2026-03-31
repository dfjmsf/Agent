# FastAPI 后端编码规范

## 框架规则

1. **POST/PUT 必须用 Pydantic BaseModel 接收 JSON Body**
   - 禁止直接写 `async def create_item(title: str, content: str)` — 这会被 FastAPI 解析为 query parameter，导致前端发 JSON body 时返回 422！
   - 正确写法：
   ```python
   from pydantic import BaseModel
   class CreateItemRequest(BaseModel):
       title: str
       content: str
       tags: str = ""
   
   @app.post("/api/items")
   async def create_item(req: CreateItemRequest):
       result = db.create_item(title=req.title, content=req.content)
   ```

2. **路由注册必须在挂载静态文件之前**
   - 如果使用 `app.mount("/", StaticFiles(...))` 挂载前端，该代码必须放在所有 `@app.get`/`@app.post` 等路由注册之后！
   - 如果在最前面 mount `/`，它会吞噬拦截后续所有的 `/api` 请求导致全部 404！

3. **CORS 中间件配置**
   ```python
   from fastapi.middleware.cors import CORSMiddleware
   app.add_middleware(
       CORSMiddleware,
       allow_origins=["*"],
       allow_credentials=True,
       allow_methods=["*"],
       allow_headers=["*"],
   )
   ```

4. **启动方式**
   ```python
   import uvicorn
   if __name__ == "__main__":
       uvicorn.run(app, host="0.0.0.0", port=5001)
   ```
   注意：不要使用 8000 端口（已被系统后端占用），推荐 5001、5002。

5. **路由函数必须带异常处理**
   ```python
   from fastapi import HTTPException
   @app.get("/api/items")
   async def get_items():
       try:
           items = get_all_items()
           return {"items": items}
       except Exception as e:
           raise HTTPException(status_code=500, detail=str(e))
   ```

6. **🚨 routes.py 必须使用 APIRouter，严禁使用 FastAPI()**

   > **致命铁律：违反将导致所有 API 返回 405 Method Not Allowed！**

   `routes.py` 中定义路由时，**必须使用 `APIRouter`**，`main.py` 通过 `app.include_router()` 注册。

   **✅ 正确写法（routes.py）：**
   ```python
   from fastapi import APIRouter, HTTPException
   
   router = APIRouter()  # ← 必须是 APIRouter
   
   @router.post("/api/items")
   async def create_item(req: CreateItemRequest):
       ...
   
   @router.get("/api/items")
   async def get_items():
       ...
   ```

   **✅ 正确写法（main.py）：**
   ```python
   from routes import router
   app.include_router(router)
   ```

   **❌ 严禁写法（会导致 405）：**
   ```python
   # routes.py 中严禁这样写！
   router = FastAPI()  # ← 错误！这是 FastAPI 应用实例，不是路由器
   # main.py 的 app.include_router() 无法注册 FastAPI 实例上的路由
   ```

7. **import 路径规范**
   - `routes.py` 导入 `models.py` 时，两种写法都可以：
     - 同级目录：`from models import ...`
     - 包导入：`from backend.models import ...`（当 main.py 在 backend/ 外时不推荐）
   - **禁止** `sys.path.append()` 操作，FastAPI 项目不需要手动修改搜索路径

8. **🚨 每个 API 函数必须有 `@router` 装饰器，否则路由不生效！**

   > **致命铁律：没有装饰器的函数只是普通 Python 函数，不会被注册为 HTTP 端点！**

   **✅ 正确写法：函数直接用 `@router.post`/`@router.get` 装饰**
   ```python
   router = APIRouter()

   @router.post("/api/recipes")
   async def create_recipe(req: CreateRecipeRequest):
       # 直接在路由函数中实现业务逻辑
       new_recipe = db_create(name=req.name, time=req.cook_time)
       return new_recipe

   @router.get("/api/recipes")
   async def get_all_recipes():
       recipes = db_get_all()
       return {"recipes": recipes}
   ```

   **❌ 严禁写法（函数没装饰器 = 路由为空 = 405）：**
   ```python
   router = APIRouter()

   # 错误！这只是普通函数，没有注册到 router 上
   def create_recipe(recipe_data: dict):
       ...

   def get_recipes():
       ...
   # router 上一个路由都没有 → 所有请求返回 405
   ```

    **规则总结：**
   - `routes.py` 中的**每个**处理 HTTP 请求的函数都**必须**有 `@router.post`/`@router.get` 等装饰器
   - 不要分"业务函数"和"路由函数"——直接在被装饰的函数中写逻辑
   - 如果确实需要拆分，必须在 `@router.xxx` 装饰的函数中**调用**业务函数

9. **🚨 SQLite 数据库必须在启动时初始化（创建表）**

   > **不初始化 = `no such table` 错误 = 所有 API 返回 500！**

   `models.py` 中定义 `init_db()` 创建表后，**必须在 `main.py` 启动时调用**：

   ```python
   # main.py
   from contextlib import asynccontextmanager
   from models import init_db

   @asynccontextmanager
   async def lifespan(app):
       init_db()  # 启动时创建表
       yield

   app = FastAPI(lifespan=lifespan)
   ```

   **或者更简单的同步写法：**
   ```python
   # main.py
   from models import init_db

   app = FastAPI()
   init_db()  # 模块加载时就创建表
   ```

   **❌ 严禁遗漏 `init_db()` 调用，否则数据库表不存在！**
