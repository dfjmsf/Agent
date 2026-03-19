import os
import sys
import threading
import logging
import asyncio
from typing import Optional, Dict
from contextlib import asynccontextmanager
from fastapi import FastAPI, BackgroundTasks, WebSocket, WebSocketDisconnect, UploadFile, File
import shutil
import json
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import time
import re
from datetime import datetime

# 确保能找到 core 和 agents 模块
sys.path.append(str(os.path.dirname(os.path.abspath(__file__))))

from agents.manager import ManagerAgent
from core.state_manager import global_state_manager
from core.ws_broadcaster import global_broadcaster

# 设置基础的控制台日志输出格式
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("FastAPIServer")

# --- Watchdog 文件系统监控配置 ---
class ProjectDirectoryEventHandler(FileSystemEventHandler):
    def __init__(self):
        super().__init__()
        self.last_emit_time = 0.0
        self.debounce_seconds = 1.0  # 防抖：由于大量生成时 I/O 密集，1秒最多推1次

    def _trigger_update(self):
        current = time.time()
        if current - self.last_emit_time > self.debounce_seconds:
            self.last_emit_time = current
            # 广播给前端：文件系统有变动，请重新拉取 REST API
            global_broadcaster.emit_sync("System", "file_tree_update", "Artifacts Updated")

    def on_created(self, event):
        self._trigger_update()

    def on_deleted(self, event):
        self._trigger_update()

    def on_modified(self, event):
        if not event.is_directory:
            self._trigger_update()

observer = Observer()

# --- Project 级别互斥锁 (防止同一项目并发生成) ---
_project_locks: Dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _get_project_lock(project_id: str) -> threading.Lock:
    """获取或创建指定项目的互斥锁"""
    with _locks_guard:
        if project_id not in _project_locks:
            _project_locks[project_id] = threading.Lock()
        return _project_locks[project_id]


# --- Lifespan 上下文管理器 (替代已弃用的 @app.on_event) ---
@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 生命周期管理：启动时初始化资源，关闭时清理资源"""
    # === Startup ===
    global_broadcaster.main_loop = asyncio.get_running_loop()

    # PostgreSQL + pgvector 初始化
    from core.database import init_db, check_health
    if check_health():
        init_db()
        logger.info("✅ PostgreSQL 连接正常，数据表已就绪")
        # 列出所有表及大小
        try:
            from core.database import engine
            from sqlalchemy import text
            with engine.connect() as conn:
                rows = conn.execute(text("""
                    SELECT c.relname AS table_name,
                           pg_size_pretty(pg_total_relation_size(c.oid)) AS total_size,
                           COALESCE(obj_description(c.oid), '') AS comment
                    FROM pg_class c
                    JOIN pg_namespace n ON n.oid = c.relnamespace
                    WHERE n.nspname = 'public' AND c.relkind = 'r'
                    ORDER BY pg_total_relation_size(c.oid) DESC
                """)).fetchall()
            TABLE_COMMENTS = {
                "memories": "长期记忆库 (AMC评分)",
                "session_events": "短期记忆/会话事件",
                "astrea_task_trajectories": "TDD轨迹表",
                "astrea_global_round": "全局逻辑时钟",
                "project_meta": "项目元数据",
            }
            table_lines = []
            for r in rows:
                name, size = r[0], r[1]
                desc = TABLE_COMMENTS.get(name, r[2] or "—")
                table_lines.append(f"    📦 {name:<30s} {size:>10s}  | {desc}")
            logger.info("📊 当前数据库表清单:\n" + "\n".join(table_lines))
        except Exception as e:
            logger.warning(f"⚠️ 表清单获取失败: {e}")
    else:
        logger.error("❌ PostgreSQL 连接失败！请确认 Docker 容器 astrea-pg 是否已启动")

    # 启动时清理残留的 sandbox venv（上次删除失败的）
    from tools.sandbox import sandbox_env
    sandbox_env.venv_manager.cleanup_stale()
    logger.info("🧹 Sandbox 残留清理完成")

    projects_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects")
    if not os.path.exists(projects_dir):
        os.makedirs(projects_dir)

    event_handler = ProjectDirectoryEventHandler()
    observer.schedule(event_handler, projects_dir, recursive=True)
    observer.start()
    logger.info(f"👀 Watchdog 已启动，正在静默监控目录: {projects_dir}")

    yield  # === 应用运行中 ===

    # === Shutdown ===
    observer.stop()
    observer.join()


app = FastAPI(title="Multi-Agent Coding Framework API", lifespan=lifespan)

# 允许跨域请求供 React 独立运行
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class NewProjectReq(BaseModel):
    project_name: str

class GenerateReq(BaseModel):
    prompt: str
    out_dir: Optional[str] = None
    project_id: str = "default_project"

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await global_broadcaster.connect(websocket)
    try:
        while True:
            # 保持连接不阻塞，纯为推送日志接收端
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        global_broadcaster.disconnect(websocket)

def run_project_thread(prompt: str, out_dir: str, project_id: str):
    """
    由于我们的 Agent 系统是同步阻塞写的，将其放入隔离的线程中运行。
    使用 project_id 级别的互斥锁防止同一项目被并发生成。
    """
    lock = _get_project_lock(project_id)
    if not lock.acquire(blocking=False):
        logger.warning(f"项目 {project_id} 已有生成任务在运行，拒绝重复提交。")
        global_broadcaster.emit_sync("System", "error", f"项目 {project_id} 已有任务在执行中，请等待完成后再提交。")
        return

    try:
        logger.info(f"后台协程：开始启动 Manager 同步阻塞流程 (Project: {project_id})...")
        manager = ManagerAgent(project_id=project_id)
        success, final_dir = manager.run_project(prompt, out_dir or None)
        if not success:
            logger.error(f"项目生成失败，输出目录: {final_dir}")
    except Exception as e:
        global_broadcaster.emit_sync("System", "error", f"项目生成异常：{str(e)}")
    finally:
        lock.release()

@app.post("/api/project/new")
async def create_new_project(req: NewProjectReq):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = re.sub(r'[^\w\-\u4e00-\u9fa5]', '_', req.project_name)
    if not safe_name.strip('_'):
        safe_name = "新建项目"
    folder_name = f"{timestamp}_{safe_name}"
    
    projects_dir = os.path.join(os.path.dirname(__file__), "projects")
    os.makedirs(projects_dir, exist_ok=True)
    
    new_dir = os.path.join(projects_dir, folder_name)
    os.makedirs(new_dir, exist_ok=True)
    
    return {"project_id": folder_name}

@app.post("/api/generate")
async def start_generation(req: GenerateReq, bg_tasks: BackgroundTasks):
    """
    前端点击生成后的触发端点。通过后台任务启动庞大的大模型同步阻塞流水线。
    """
    t = threading.Thread(target=run_project_thread, args=(req.prompt, req.out_dir, req.project_id))
    t.start()
    return {"status": "started", "message": "Multi-Agent System Activated in Background Thread"}


# --- 文件上下文挂载 (Context Uploads) ---
@app.post("/api/upload")
async def upload_context_file(file: UploadFile = File(...)):
    """处理前端上传的文件，保存并生成 Schema / 智能摘要"""
    uploads_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads")
    if not os.path.exists(uploads_dir):
        os.makedirs(uploads_dir)
        
    file_path = os.path.join(uploads_dir, file.filename)
    
    try:
        with open(file_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)
    except Exception as e:
        return {"error": f"保存文件失败: {str(e)}"}
        
    # 生成预览数据结构 (Schema Peek)
    preview = "[文件暂无可用预览]"
    ext = file.filename.lower().split('.')[-1]
    
    try:
        if ext == 'csv':
            import pandas as pd
            df = pd.read_csv(file_path, nrows=5)
            preview = "表头与前5行数据样例:\n" + df.to_csv(index=False)
        elif ext == 'json':
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read(1500)
            preview = f"JSON片段开头截取:\n{content[:1000]}..."
        elif ext in ['md', 'txt']:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read(10000)
            
            # 使用超级廉价且快速的 qwen3.5-flash 浓缩长文本
            from core.llm_client import default_llm
            msg = [
                {"role": "system", "content": "你是一个极其精简的文摘AI。请用1-2句话高度概括这段文本的核心业务含义和包含的数据重点，不要废话，直接输出提炼结果。"},
                {"role": "user", "content": f"文本内容片段：\n{content}"}
            ]
            try:
                resp = default_llm.chat_completion(msg, model="qwen3.5-flash")
                preview = f"AI 极简精细摘要:\n{resp.content}"
            except Exception as e:
                logger.warning(f"AI 生成文档摘要失败，实施降级截断: {e}")
                preview = f"文档开头:\n{content[:500]}..."
        else:
            preview = f"这是未知格式 (.{ext})，无法提供预览。AI如果确信可以处理，可尝试直接写代码读取它。"
            
    except Exception as e:
        logger.error(f"提取文件 {file.filename} 预览失败: {e}")
        preview = f"[Schema 提取异常: {str(e)}]"
        
    return {
        "filename": file.filename,
        "path": os.path.abspath(file_path).replace("\\", "/"),
        "preview": preview
    }

# --- Mini-VSCode Artifact Explorer APIs ---

@app.get("/api/projects")
async def get_all_projects_list():
    """获取所有存在的项目列表"""
    base_projects_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects")
    if not os.path.exists(base_projects_dir):
        return []
    projects = [d for d in os.listdir(base_projects_dir) if os.path.isdir(os.path.join(base_projects_dir, d))]
    # Sort by modification time descending
    projects.sort(key=lambda x: os.path.getmtime(os.path.join(base_projects_dir, x)), reverse=True)
    return projects

@app.get("/api/project/files")
async def get_project_files(project_id: str):
    """
    获取指定 project_id 项目的物理目录结构。
    为了防止 VFS 内存和磁盘不同步，先强制 flush 脏数据。
    """
    # 无状态，需要读取文件树前，强制确保刚改完的 dirty 内存刷回磁盘
    vfs = global_state_manager.get_vfs(project_id)
    base_projects_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects")
    project_dir = os.path.join(base_projects_dir, project_id)
    
    if vfs.is_dirty:
        vfs.commit_to_disk(project_dir)
    
    if not os.path.exists(project_dir):
        return {"name": project_id, "type": "directory", "children": []}
    
    def build_tree(dir_path):
        tree = {"name": os.path.basename(dir_path), "path": dir_path, "type": "directory", "children": []}
        try:
            for item in sorted(os.listdir(dir_path)):
                item_path = os.path.join(dir_path, item)
                if os.path.isdir(item_path):
                    tree["children"].append(build_tree(item_path))
                else:
                    tree["children"].append({"name": item, "path": item_path, "type": "file"})
        except Exception as e:
            logger.error(f"Error reading directory {dir_path}: {e}")
        return tree

    return build_tree(project_dir)

@app.get("/api/project/file")
async def get_project_file(path: str):
    """
    读取指定文件的内容（带路径边界安全校验）
    """
    # 安全防护：只允许访问 projects/ 和 workspace/ 目录下的文件
    project_root = os.path.dirname(os.path.abspath(__file__))
    allowed_dirs = [
        os.path.abspath(os.path.join(project_root, "projects")),
        os.path.abspath(os.path.join(project_root, "workspace")),
    ]
    abs_path = os.path.abspath(path)
    if not any(abs_path.startswith(d + os.sep) or abs_path == d for d in allowed_dirs):
        logger.warning(f"🚨 路径遍历攻击被拦截: {path}")
        return {"error": "Access denied: path outside allowed boundary"}

    if not os.path.exists(abs_path) or not os.path.isfile(abs_path):
        return {"error": "File not found"}
    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            content = f.read()
        return {"content": content}
    except Exception as e:
        return {"error": str(e)}

class RunRequest(BaseModel):
    code: str
    stdin_data: Optional[str] = None
    project_id: str = "default_project"

@app.post("/api/project/run")
async def run_project_code(req: RunRequest):
    """
    使用 Reviewer 阅后即焚沙盒安全执行前端传来的代码，强制绑定 project_id
    """
    try:
        from tools.sandbox import sandbox_env
        result = sandbox_env.execute_code(req.code, req.project_id, stdin_data=req.stdin_data)
        
        # sandbox.execute_code returns a dict
        return {
            "stdout": result.get("stdout", ""),
            "stderr": result.get("stderr", ""),
            "returncode": result.get("returncode", -1)
        }
    except Exception as e:
        return {
            "stdout": "",
            "stderr": f"Sandbox Exception: {str(e)}",
            "returncode": -1
        }

class GraduateReq(BaseModel):
    project_id: str

@app.post("/api/project/graduate")
async def graduate_project(req: GraduateReq):
    """将项目的短期经验"毕业"为全局长期记忆。"""
    from core.database import graduate_project_experience
    count = graduate_project_experience(req.project_id)
    return {"status": "ok", "graduated_count": count, "project_id": req.project_id}

if __name__ == "__main__":
    import uvicorn
    # 为了防止 Windows 上部分多进程阻塞，这里禁用了 reload 并以纯净模式跑
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)

