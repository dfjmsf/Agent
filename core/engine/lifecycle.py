"""
engine/lifecycle.py — AstreaEngine 生命周期管理

包含：构造 (__init__), resume, abort_and_rollback, _get_current_git_head,
      _next_round_number, _is_existing_project, Agent 延迟获取
"""
import os
import re
import subprocess
import logging
from typing import Optional

from core.blackboard import Blackboard, BlackboardState
from core.code_patcher import CodePatcher
from core.vfs_utils import VfsUtils
from core.ws_broadcaster import global_broadcaster

logger = logging.getLogger("AstreaEngine")

# 路径基准
_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_PROJECTS_DIR = os.path.join(_ROOT_DIR, "projects")


# ============================================================
# 构造与恢复
# ============================================================

def init_engine(engine, project_id: str):
    """AstreaEngine.__init__ 实际逻辑"""
    engine.blackboard = Blackboard(project_id)
    engine.patcher = CodePatcher()
    engine.vfs: Optional[VfsUtils] = None
    engine._shutdown = False
    engine._abort_requested = False
    engine._pre_execution_git_head: Optional[str] = None
    engine._pending_project_rename: Optional[tuple] = None
    # Agent 延迟导入（避免循环依赖）
    engine._manager = None
    engine._coder = None
    engine._reviewer = None


def resume_engine(cls, project_id: str):
    """AstreaEngine.resume 类方法实际逻辑"""
    bb = Blackboard.restore(project_id)
    if not bb:
        return None
    engine = cls.__new__(cls)
    engine.blackboard = bb
    engine.patcher = CodePatcher()
    engine.vfs = VfsUtils(bb.state.out_dir) if bb.state.out_dir else None
    engine._shutdown = False
    engine._abort_requested = False
    engine._pre_execution_git_head = None
    engine._pending_project_rename = None
    engine._manager = None
    engine._coder = None
    engine._reviewer = None
    engine.session_id = str(next_round_number(engine))
    logger.info(f"🔄 AstreaEngine 从 Checkpoint 恢复: {project_id} (Round={engine.session_id})")
    return engine


# ============================================================
# Agent 延迟获取
# ============================================================

def get_manager(engine):
    if engine._manager is None:
        from agents.manager import ManagerAgent
        engine._manager = ManagerAgent(engine.project_id)
    return engine._manager


def get_coder(engine):
    if engine._coder is None:
        from agents.coder import CoderAgent
        engine._coder = CoderAgent(engine.project_id)
    return engine._coder


def get_reviewer(engine):
    if engine._reviewer is None:
        from agents.reviewer import ReviewerAgent
        engine._reviewer = ReviewerAgent(engine.project_id)
    return engine._reviewer


# ============================================================
# Git 操作
# ============================================================

def next_round_number(engine) -> int:
    """从 Git 历史中扫描已有的最大 Round 编号，返回 N+1。无历史则返回 1。"""
    base_dir = os.path.join(_PROJECTS_DIR, engine.project_id)
    git_dir = os.path.join(base_dir, ".git")
    if not os.path.isdir(git_dir):
        return 1
    try:
        result = subprocess.run(
            ["git", "log", "--max-count=50", "--format=%s"],
            cwd=base_dir, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=5,
        )
        if result.returncode != 0 or not result.stdout:
            return 1
        max_round = 0
        for line in result.stdout.strip().split('\n'):
            m = re.search(r"\[Round (\d+)\]", line)
            if m:
                max_round = max(max_round, int(m.group(1)))
        return max_round + 1
    except Exception:
        return 1


def get_current_git_head(engine) -> Optional[str]:
    """获取当前项目的 Git HEAD commit hash。无 git 目录时返回 None。"""
    base_dir = os.path.join(_PROJECTS_DIR, engine.project_id)
    git_dir = os.path.join(base_dir, ".git")
    if not os.path.isdir(git_dir):
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=base_dir, capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception as e:
        logger.warning(f"获取 Git HEAD 失败: {e}")
    return None


# ============================================================
# 项目探测
# ============================================================

def is_existing_project(engine) -> bool:
    """判断当前项目是否是已有项目（非新建）"""
    pid = engine.project_id
    if "新建项目" in pid or "new_project" in pid or pid == "default_project":
        return False
    base_dir = os.path.join(_PROJECTS_DIR, pid)
    if not os.path.isdir(base_dir):
        return False
    ignore = {'.sandbox', '.git', '__pycache__', '.venv'}
    for item in os.listdir(base_dir):
        if item not in ignore:
            return True
    return False


# ============================================================
# 一键中止 + 自动回滚
# ============================================================

def abort_and_rollback(engine) -> dict:
    """
    一键中止当前执行并回滚到执行前状态。

    步骤:
    1. 设置 _abort_requested 标志
    2. 设置 _shutdown 标志
    3. 如果有 _pre_execution_git_head，执行 git reset --hard 回滚

    Returns:
        {"success": bool, "message": str, "rolled_back_to": str|None}
    """
    engine._abort_requested = True
    engine._shutdown = True
    logger.warning(f"⛔ 收到中止请求: project={engine.project_id}")
    global_broadcaster.emit_sync("System", "abort", "⛔ 用户中止了执行，正在回滚...")

    rolled_back_to = None
    base_dir = os.path.join(_PROJECTS_DIR, engine.project_id)

    if engine._pre_execution_git_head:
        try:
            result = subprocess.run(
                ["git", "reset", "--hard", engine._pre_execution_git_head],
                cwd=base_dir, capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                rolled_back_to = engine._pre_execution_git_head[:8]
                logger.info(f"✅ Git 回滚成功: HEAD → {rolled_back_to}")
                global_broadcaster.emit_sync("System", "info",
                    f"✅ 代码已回滚到执行前状态 ({rolled_back_to})")
            else:
                logger.error(f"Git reset 失败: {result.stderr}")
                global_broadcaster.emit_sync("System", "error",
                    f"❌ Git 回滚失败: {result.stderr[:200]}")
        except Exception as e:
            logger.error(f"Git reset 异常: {e}")
            global_broadcaster.emit_sync("System", "error", f"❌ Git 回滚异常: {e}")
    else:
        logger.info("无 Git HEAD 记录，跳过 Git 回滚（可能是新项目首次执行）")

    engine._pre_execution_git_head = None

    return {
        "success": True,
        "message": f"已中止执行" + (f"，代码已回滚到 {rolled_back_to}" if rolled_back_to else ""),
        "rolled_back_to": rolled_back_to,
    }
