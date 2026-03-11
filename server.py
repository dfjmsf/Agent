import os
import sys
import threading
import logging
import asyncio
from typing import Optional
from fastapi import FastAPI, BackgroundTasks, WebSocket, WebSocketDisconnect, UploadFile, File
import shutil
import json
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import time

# 确保能找到 core 和 agents 模块
sys.path.append(str(os.path.dirname(os.path.abspath(__file__))))

from agents.manager import ManagerAgent
from core.state_manager import global_state
from core.ws_broadcaster import global_broadcaster

# 设置基础的控制台日志输出格式
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("FastAPIServer")

app = FastAPI(title="Multi-Agent Coding Framework API")

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

# 允许跨域请求供 React 独立运行
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup_event():
    # 捕获主线程的 Asyncio Loop
    global_broadcaster.main_loop = asyncio.get_running_loop()
    
    # 启动 Watchdog 守护线程监控 projects 目录
    projects_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects")
    if not os.path.exists(projects_dir):
        os.makedirs(projects_dir)
        
    event_handler = ProjectDirectoryEventHandler()
    observer.schedule(event_handler, projects_dir, recursive=True)
    observer.start()
    logger.info(f"👀 Watchdog 已启动，正在静默监控目录: {projects_dir}")

@app.on_event("shutdown")
async def shutdown_event():
    observer.stop()
    observer.join()

class ProjectRequest(BaseModel):
    prompt: str
    out_dir: Optional[str] = None

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await global_broadcaster.connect(websocket)
    try:
        while True:
            # 保持连接不阻塞，纯为推送日志接收端
            data = await websocket.receive_text()
    except WebSocketDisconnect:
        global_broadcaster.disconnect(websocket)

def run_project_thread(prompt: str, out_dir: str):
    """
    由于我们的 Agent 系统是同步阻塞写的，将其放入隔离的线程中运行。
    但是它内部依旧可以调用 global_broadcaster.emit_sync 推给 asyncio 队列。
    """
    logger.info("后台协程：开始启动 Manager 同步阻塞流程...")

    manager = ManagerAgent()
    try:
        success, final_dir = manager.run_project(prompt, out_dir or None)
        if not success:
            logger.error(f"项目生成失败，输出目录: {final_dir}")
    except Exception as e:
        global_broadcaster.emit_sync("System", "error", f"项目生成异常：{str(e)}")

@app.post("/api/generate")
async def start_generation(req: ProjectRequest, bg_tasks: BackgroundTasks):
    """
    前端点击生成后的触发端点。通过后台任务启动庞大的大模型同步阻塞流水线。
    """
    t = threading.Thread(target=run_project_thread, args=(req.prompt, req.out_dir))
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

@app.get("/api/project/files")
async def get_project_files():
    """
    获取最近生成的一个项目的物理目录结构
    """
    base_projects_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects")
    if not os.path.exists(base_projects_dir):
        return {"name": "projects", "type": "directory", "children": []}
    
    # 找到最新生成的项目目录
    projects = [os.path.join(base_projects_dir, d) for d in os.listdir(base_projects_dir) if os.path.isdir(os.path.join(base_projects_dir, d))]
    if not projects:
        return {"name": "projects", "type": "directory", "children": []}
    
    latest_project = max(projects, key=os.path.getmtime)
    
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

    return build_tree(latest_project)

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

@app.post("/api/project/run")
async def run_project_code(req: RunRequest):
    """
    使用 Reviewer 沙盒安全执行前端传来的代码
    """
    try:
        from tools.sandbox import PythonSandbox
        sandbox = PythonSandbox()
        result = sandbox.execute_code(req.code, stdin_data=req.stdin_data)
        
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

if __name__ == "__main__":
    import uvicorn
    # 为了防止 Windows 上部分多进程阻塞，这里禁用了 reload 并以纯净模式跑
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)
