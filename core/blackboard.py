"""
Blackboard — 系统的"中央作战指挥室"与"唯一真理之源 (SSOT)"

v1.3 核心基建：
- 运行态 (热数据): Pydantic 数据模型驻留内存，享受类型提示和校验
- 持久态 (冷数据): PostgreSQL JSONB 存储 Checkpoint，支持断点续传
- 状态机驱动: 严格的 status 字段控制 Agent 唤醒顺序
- 契约前置: Manager 的规划书/API 契约钉在黑板上，作为所有 Agent 的标准
"""
import logging
from enum import Enum
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

from pydantic import BaseModel, Field

logger = logging.getLogger("Blackboard")


# ============================================================
# 1. 状态枚举
# ============================================================

class TaskStatus(str, Enum):
    """子任务的 TDD 状态机"""
    TODO = "TODO"                       # 待执行
    CODING = "CODING"                   # Coder 正在编码
    PATCH_FAILED = "PATCH_FAILED"       # CodePatcher 缝合失败，等待 Coder 重试
    PENDING_REVIEW = "PENDING_REVIEW"   # 缝合成功，等待 Reviewer
    REVIEWING = "REVIEWING"             # Reviewer 正在测试
    PASSED = "PASSED"                   # 测试通过，等待 Engine commit 到 VFS
    REJECTED = "REJECTED"               # 测试失败，等待 Coder 修复
    DONE = "DONE"                       # 已 commit 到 VFS 真理区
    FUSED = "FUSED"                     # 重试耗尽，熔断


class ProjectStatus(str, Enum):
    """项目级状态"""
    INIT = "INIT"                       # 初始化
    PLANNING = "PLANNING"               # Manager 正在画图纸
    EXECUTING = "EXECUTING"             # 正在执行子任务
    COMPLETED = "COMPLETED"             # 全部子任务 DONE
    FAILED = "FAILED"                   # 存在熔断的子任务


# ============================================================
# 2. Pydantic 数据模型
# ============================================================

class TaskItem(BaseModel):
    """
    单个子任务 — 黑板上的一张卡片。
    
    包含：
    - 任务描述与依赖关系
    - Coder 提交的未验证草稿 (code_draft)
    - TDD 循环的运行时轨迹和报错日志
    - RAG 召回的记忆 IDs (用于后续 Auditor 审计)
    """
    task_id: str
    target_file: str
    description: str
    dependencies: List[str] = Field(default_factory=list)
    tech_stack: Optional[str] = None

    # --- 状态机 ---
    status: TaskStatus = TaskStatus.TODO
    retry_count: int = 0

    # --- 草稿区 (未验证假设) ---
    code_draft: Optional[str] = None
    draft_action: Optional[str] = None   # "create" | "rewrite" | "modify"

    # --- 运行时轨迹 (吸收旧 event 系统) ---
    action_trajectory: List[str] = Field(default_factory=list)
    error_logs: List[str] = Field(default_factory=list)

    # --- 记忆追踪 ---
    recalled_memory_ids: List[int] = Field(default_factory=list)

    def log_action(self, message: str):
        """追加一条轨迹记录"""
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.action_trajectory.append(f"[{ts}] {message}")

    def log_error(self, error: str):
        """追加一条错误日志"""
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.error_logs.append(f"[{ts}] {error}")


class BlackboardState(BaseModel):
    """
    黑板全局状态 — 一次 model_dump_json() 即可原子化落盘。
    
    包含：
    - 项目级元数据 (project_id, project_status, project_spec)
    - 子任务列表 (tasks: List[TaskItem])
    - 时间戳
    """
    project_id: str
    project_status: ProjectStatus = ProjectStatus.INIT
    project_name: Optional[str] = None
    project_spec: Optional[Dict[str, Any]] = None    # API 契约 / 项目蓝图
    spec_text: Optional[str] = None                   # 规划书原始文本
    tasks: List[TaskItem] = Field(default_factory=list)
    user_requirement: Optional[str] = None
    out_dir: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


# ============================================================
# 3. Blackboard 管理器
# ============================================================

class Blackboard:
    """
    黑板管理器：内存态 Pydantic + PostgreSQL JSONB Checkpoint。
    
    核心原则：
    - 状态发生扭转时自动 checkpoint (model_dump_json → PG)
    - 恢复时通过反序列化即可还原全部上下文
    - 线程安全 (Engine 是单线程串行调度，无需加锁)
    """

    def __init__(self, project_id: str):
        self._state = BlackboardState(project_id=project_id)
        logger.info(f"📌 Blackboard 初始化: project={project_id}")

    @property
    def state(self) -> BlackboardState:
        return self._state

    @property
    def project_id(self) -> str:
        return self._state.project_id

    # --- 项目级操作 ---

    def set_project_status(self, status: ProjectStatus):
        """设置项目级状态"""
        old = self._state.project_status
        self._state.project_status = status
        self._touch()
        logger.info(f"📌 项目状态扭转: {old.value} → {status.value}")
        self.checkpoint()

    def set_project_spec(self, spec: dict, spec_text: str, project_name: str = None):
        """Manager 贴上规划书和 API 契约"""
        self._state.project_spec = spec
        self._state.spec_text = spec_text
        if project_name:
            self._state.project_name = project_name
        self._touch()
        logger.info(f"📌 规划书已贴上黑板: {project_name or 'unnamed'}")
        self.checkpoint()

    def set_tasks(self, tasks: List[Dict[str, Any]]):
        """Manager 贴上任务列表"""
        task_items = []
        for t in tasks:
            item = TaskItem(
                task_id=t.get("task_id", f"task_{len(task_items)+1}"),
                target_file=t.get("target_file", ""),
                description=t.get("description", ""),
                dependencies=t.get("dependencies", []),
                tech_stack=t.get("tech_stack"),
            )
            task_items.append(item)
        self._state.tasks = task_items
        self._touch()
        logger.info(f"📌 任务列表已贴上黑板: {len(task_items)} 个子任务")
        self.checkpoint()

    # --- 子任务级操作 ---

    def get_task(self, task_id: str) -> Optional[TaskItem]:
        """按 task_id 查找子任务"""
        for t in self._state.tasks:
            if t.task_id == task_id:
                return t
        return None

    def update_task_status(self, task_id: str, new_status: TaskStatus):
        """扭转子任务状态 + 自动 checkpoint"""
        task = self.get_task(task_id)
        if not task:
            logger.error(f"❌ 找不到任务: {task_id}")
            return
        old = task.status
        task.status = new_status
        task.log_action(f"状态扭转: {old.value} → {new_status.value}")
        self._touch()
        logger.info(f"📌 [{task_id}] {old.value} → {new_status.value}")
        self.checkpoint()

    def submit_draft(self, task_id: str, code_draft: str, action: str):
        """Coder 提交草稿到黑板"""
        task = self.get_task(task_id)
        if not task:
            logger.error(f"❌ 找不到任务: {task_id}")
            return
        task.code_draft = code_draft
        task.draft_action = action
        task.log_action(f"Coder 提交草稿 (action={action}, {len(code_draft)} bytes)")
        self._touch()

    def clear_draft(self, task_id: str):
        """清除草稿（commit 到 VFS 后）"""
        task = self.get_task(task_id)
        if task:
            task.code_draft = None
            task.draft_action = None
            self._touch()

    def increment_retry(self, task_id: str) -> int:
        """增加重试计数"""
        task = self.get_task(task_id)
        if task:
            task.retry_count += 1
            self._touch()
            return task.retry_count
        return 0

    def get_next_runnable_task(self) -> Optional[TaskItem]:
        """
        依赖图调度：找到第一个前置任务全部 DONE 的 TODO 任务。
        保证"先写后端 API，再写前端调用"的严丝合缝。
        """
        for task in self._state.tasks:
            if task.status == TaskStatus.TODO:
                deps_satisfied = all(
                    self.get_task(dep_id) is not None
                    and self.get_task(dep_id).status == TaskStatus.DONE
                    for dep_id in task.dependencies
                )
                if deps_satisfied:
                    return task
        return None

    def all_tasks_done(self) -> bool:
        """检查是否所有任务都已完成"""
        return all(
            t.status in (TaskStatus.DONE, TaskStatus.FUSED)
            for t in self._state.tasks
        )

    def has_fused_tasks(self) -> bool:
        """检查是否有熔断的任务"""
        return any(t.status == TaskStatus.FUSED for t in self._state.tasks)

    # --- Checkpoint 持久化 ---

    def checkpoint(self):
        """状态快照落盘 → PostgreSQL JSONB"""
        try:
            from core.database import save_checkpoint
            state_json = self._state.model_dump_json()
            save_checkpoint(self._state.project_id, state_json)
            logger.debug(f"💾 Checkpoint 已落盘: {self._state.project_id}")
        except Exception as e:
            logger.warning(f"⚠️ Checkpoint 落盘失败 (不影响运行): {e}")

    @classmethod
    def restore(cls, project_id: str) -> Optional['Blackboard']:
        """从 PostgreSQL 恢复黑板状态（断点续传）"""
        try:
            from core.database import load_checkpoint
            state_json = load_checkpoint(project_id)
            if not state_json:
                logger.info(f"📌 无可恢复的 Checkpoint: {project_id}")
                return None
            state = BlackboardState.model_validate_json(state_json)
            bb = cls.__new__(cls)
            bb._state = state
            logger.info(f"📌 Blackboard 从 Checkpoint 恢复: {project_id} "
                        f"(status={state.project_status.value}, tasks={len(state.tasks)})")
            return bb
        except Exception as e:
            logger.error(f"❌ Blackboard 恢复失败: {e}")
            return None

    def delete_checkpoint(self):
        """项目完成或放弃后，清理 Checkpoint"""
        try:
            from core.database import delete_checkpoint
            delete_checkpoint(self._state.project_id)
            logger.info(f"🗑️ Checkpoint 已清理: {self._state.project_id}")
        except Exception as e:
            logger.warning(f"⚠️ Checkpoint 清理失败: {e}")

    # --- 内部工具 ---

    def _touch(self):
        """更新修改时间"""
        self._state.updated_at = datetime.now(timezone.utc).isoformat()

    def __repr__(self):
        s = self._state
        return (f"<Blackboard project={s.project_id} "
                f"status={s.project_status.value} "
                f"tasks={len(s.tasks)}>")
