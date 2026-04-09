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

from core.engine import AstreaEngine
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

    # v1.3: 启动时检测未完成的 Checkpoint
    try:
        from core.database import list_pending_checkpoints
        pending = list_pending_checkpoints()
        if pending:
            logger.info(f"🔄 检测到 {len(pending)} 个未完成的 Checkpoint:")
            for cp in pending:
                logger.info(f"   📌 {cp['project_id']} (更新于 {cp['updated_at']})")
            # 通知前端（WebSocket 延迟发送）
            async def _notify_pending():
                await asyncio.sleep(2)  # 等待前端 WebSocket 连接
                global_broadcaster.emit_sync("System", "pending_checkpoints",
                    f"发现 {len(pending)} 个未完成的项目可恢复", {"checkpoints": pending})
            asyncio.create_task(_notify_pending())
    except Exception as e:
        logger.warning(f"⚠️ Checkpoint 检测异常: {e}")

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

def run_project_thread(prompt: str, out_dir: str, project_id: str, mode: str = "auto"):
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
        logger.info(f"后台线程：AstreaEngine 启动 (Project: {project_id}, mode={mode})...")
        engine = AstreaEngine(project_id=project_id)
        success, final_dir = engine.run(prompt, out_dir or None, mode=mode)
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
    return {"status": "started", "message": "AstreaEngine Activated in Background Thread"}


# ============================================================
# PM Agent 对话端点 (Phase 2.1)
# ============================================================

from agents.pm import PMAgent, PMResponse
from dataclasses import asdict

_pm_instances: Dict[str, PMAgent] = {}

def _get_pm_instance(project_id: str) -> PMAgent:
    """获取或创建 PM Agent 实例（进程内缓存，按 project_id 隔离）"""
    if project_id not in _pm_instances:
        _pm_instances[project_id] = PMAgent(project_id)
    return _pm_instances[project_id]

class ChatReq(BaseModel):
    message: str
    project_id: str = "default_project"

class ActionReq(BaseModel):
    action: str  # "confirm" | "reject" | "rollback_confirm" | "rollback_cancel"
    project_id: str = "default_project"

@app.post("/api/chat")
async def chat_with_pm(req: ChatReq):
    """PM Agent 对话入口"""
    pm = _get_pm_instance(req.project_id)
    response = pm.chat(req.message)
    return asdict(response)

@app.post("/api/chat/action")
async def chat_action(req: ActionReq):
    """确定性按钮动作（confirm/reject，零 LLM 分类）"""
    pm = _get_pm_instance(req.project_id)
    response = pm.handle_action(req.action)

    if response.is_executing:
        # 从 PM 获取已确认的需求文本 + 执行模式，启动 Engine
        user_req = getattr(pm, 'confirmed_requirement', None) or "用户确认执行"
        mode = getattr(pm, 'confirmed_mode', 'auto')
        t = threading.Thread(
            target=run_project_thread,
            args=(user_req, None, req.project_id, mode)
        )
        t.start()

    return asdict(response)

@app.get("/api/chat/history")
async def chat_history(project_id: str, limit: int = 50):
    """获取指定项目的 PM 对话历史（从 FTS5 加载）"""
    pm = _get_pm_instance(project_id)
    store = pm._get_store()
    if not store:
        return {"messages": []}
    try:
        records = store.get_recent(limit=limit)
        messages = []
        for r in records:
            messages.append({
                "role": "pm" if r["role"] == "pm" else "user",
                "content": r["content"],
                "round_id": r["round_id"],
            })
        return {"messages": messages}
    except Exception as e:
        logger.warning(f"⚠️ 加载对话历史失败: {e}")
        return {"messages": []}


class ResumeReq(BaseModel):
    project_id: str

@app.post("/api/project/resume")
async def resume_project(req: ResumeReq):
    """从 Checkpoint 恢复中断的项目"""
    engine = AstreaEngine.resume(req.project_id)
    if not engine:
        return {"status": "error", "message": f"未找到项目 {req.project_id} 的 Checkpoint"}

    def _resume_thread():
        lock = _get_project_lock(req.project_id)
        if not lock.acquire(blocking=False):
            global_broadcaster.emit_sync("System", "error", f"项目 {req.project_id} 已有任务在执行中")
            return
        try:
            # 继续执行
            engine._phase_execution()
        except Exception as e:
            global_broadcaster.emit_sync("System", "error", f"恢复执行异常: {str(e)}")
        finally:
            lock.release()

    t = threading.Thread(target=_resume_thread)
    t.start()
    return {"status": "resumed", "project_id": req.project_id}

@app.get("/api/project/checkpoints")
async def get_pending_checkpoints():
    """列出所有可恢复的 Checkpoint"""
    from core.database import list_pending_checkpoints
    return {"checkpoints": list_pending_checkpoints()}


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
    """
    base_projects_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects")
    project_dir = os.path.join(base_projects_dir, project_id)
    
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


# --- Git 版本管理 API ---

@app.get("/api/project/git/status")
async def git_status_api(project_id: str):
    """获取项目 git 仓库状态"""
    from tools.git_ops import git_status
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects", project_id)
    if not os.path.isdir(base_dir):
        return {"error": f"项目不存在: {project_id}"}
    return git_status(base_dir)


@app.get("/api/project/git/log")
async def git_log_api(project_id: str, max_count: int = 30):
    """获取项目 commit 历史列表"""
    from tools.git_ops import git_log
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects", project_id)
    if not os.path.isdir(base_dir):
        return {"error": f"项目不存在: {project_id}"}
    return {"commits": git_log(base_dir, max_count)}


@app.get("/api/project/git/diff")
async def git_diff_api(project_id: str, commit: str):
    """获取指定 commit 的 diff"""
    from tools.git_ops import git_diff
    # 安全校验：commit hash 只允许十六进制字符
    if not all(c in '0123456789abcdefABCDEF' for c in commit):
        return {"error": "非法 commit hash"}
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects", project_id)
    if not os.path.isdir(base_dir):
        return {"error": f"项目不存在: {project_id}"}
    return {"diff": git_diff(base_dir, commit)}


@app.post("/api/project/git/init")
async def git_init_api(req: GraduateReq):
    """手动初始化项目的 git 仓库并做首次 commit"""
    from tools.git_ops import git_init, git_commit
    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects", req.project_id)
    if not os.path.isdir(base_dir):
        return {"error": f"项目不存在: {req.project_id}"}
    ok = git_init(base_dir)
    if ok:
        git_commit(base_dir, f"初始化: {req.project_id}")
    return {"status": "ok" if ok else "failed", "project_id": req.project_id}


# --- 逆向扫描 API (Phase 1.3) ---

class ScanReq(BaseModel):
    project_id: str  # MVP: projects/ 下的项目 ID


@app.post("/api/project/scan")
async def scan_project_api(req: ScanReq):
    """逆向扫描已有项目，生成 project_spec"""
    from tools.project_scanner import ProjectScanner
    from agents.manager import ManagerAgent

    base_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "projects", req.project_id)
    if not os.path.isdir(base_dir):
        return {"error": f"项目不存在: {req.project_id}"}

    try:
        # Step 1: 确定性扫描（零 LLM）
        scanner = ProjectScanner(base_dir)
        scan_result = scanner.scan()

        # Step 2: LLM 合成（1 次调用）
        manager = ManagerAgent(req.project_id)
        spec = manager._generate_spec_from_scan(scan_result)

        return {
            "status": "ok",
            "project_id": req.project_id,
            "scan_summary": {
                "tech_stack": scan_result.get("tech_stack", []),
                "file_count": len(scan_result.get("files", [])),
                "route_count": len(scan_result.get("routes", [])),
                "model_count": len(scan_result.get("models", [])),
                "entry": scan_result.get("entry", {}),
                "config_files": scan_result.get("config_files", []),
            },
            "project_spec": spec,
        }
    except FileNotFoundError as e:
        return {"error": str(e)}
    except Exception as e:
        logger.error(f"逆向扫描异常: {e}")
        return {"error": f"扫描失败: {str(e)}"}


# --- 模型配置 API ---

_AGENT_ROLES = ["MODEL_PLANNER", "MODEL_CODER", "MODEL_REVIEWER", "MODEL_SYNTHESIZER", "MODEL_AUDITOR", "MODEL_PM", "MODEL_PLANNER_LITE", "MODEL_TECH_LEAD"]
_ROLE_LABELS = {
    "MODEL_PLANNER": "规划师 (Manager)",
    "MODEL_CODER": "编码器 (Coder)",
    "MODEL_REVIEWER": "审查员 (Reviewer)",
    "MODEL_SYNTHESIZER": "综合器 (Synthesizer)",
    "MODEL_AUDITOR": "审计员 (Auditor)",
    "MODEL_PM": "项目经理 (PM)",
    "MODEL_PLANNER_LITE": "规划组 (PlannerLite)",
    "MODEL_TECH_LEAD": "技术骨干 (TechLead)",
}


@app.get("/api/config/models")
async def get_model_config():
    """获取当前模型配置和可用 Provider 列表"""
    from core.llm_client import default_llm

    # 当前各 Agent 的模型配置
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    current = {}
    for role in _AGENT_ROLES:
        current[role] = os.getenv(role, "未设置")

    # 收集所有 Provider 和它们的模型
    providers = []
    for p in default_llm.providers:
        providers.append({
            "name": p.name,
            "models": p.models,
        })

    return {
        "agents": {role: {"label": _ROLE_LABELS[role], "model": current[role]} for role in _AGENT_ROLES},
        "providers": providers,
    }


class ModelConfigUpdate(BaseModel):
    """模型配置更新请求"""
    config: Dict[str, str]  # e.g. {"MODEL_PLANNER": "qwen3-max", "MODEL_CODER": "deepseek-chat"}


@app.put("/api/config/models")
async def update_model_config(req: ModelConfigUpdate):
    """更新 .env 中的模型配置并热重载"""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

    # 读取现有 .env
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return {"error": ".env 文件不存在"}

    # 逐行更新
    updated_keys = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        matched = False
        for key, value in req.config.items():
            if key in _AGENT_ROLES and stripped.startswith(f"{key}="):
                new_lines.append(f"{key}={value}\n")
                updated_keys.add(key)
                matched = True
                break
        if not matched:
            new_lines.append(line)

    # 写回
    try:
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(new_lines)
    except Exception as e:
        return {"error": f"写入失败: {str(e)}"}

    # 热重载环境变量到当前进程
    for key, value in req.config.items():
        if key in _AGENT_ROLES:
            os.environ[key] = value

    logger.info(f"✅ 模型配置已更新: {req.config}")
    return {"status": "ok", "updated": dict(req.config),
            "note": "配置已写入.env并热载，新的项目生成将使用更新后的模型。"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False)

