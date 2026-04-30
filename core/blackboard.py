"""
Blackboard — 系统的"中央作战指挥室"与"唯一真理之源 (SSOT)"

v1.3 核心基建：
- 运行态 (热数据): Pydantic 数据模型驻留内存，享受类型提示和校验
- 持久态 (冷数据): PostgreSQL JSONB 存储 Checkpoint，支持断点续传
- 状态机驱动: 严格的 status 字段控制 Agent 唤醒顺序
- 契约前置: Manager 的规划书/API 契约钉在黑板上，作为所有 Agent 的标准
"""
import os
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
    DELIVERED_WITH_WARNINGS = "DELIVERED_WITH_WARNINGS"  # 已交付，但集成验证未完全通过
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
    task_type: Optional[str] = None
    node_key: Optional[str] = None
    group_id: Optional[str] = None
    topo_index: Optional[int] = None
    ready_rank: int = 0
    write_targets: List[str] = Field(default_factory=list)

    # --- 骨架先行 sub_tasks (Phase 0) ---
    sub_tasks: List[Dict[str, str]] = Field(default_factory=list)  # [{"sub_id","type","description"}]
    current_sub_task_index: int = 0  # 当前执行到哪个 sub_task

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

    # --- TechLead 仲裁状态 (Phase 2.1) ---
    tech_lead_invoked: bool = False               # 是否已唤醒过 TechLead（每个 task 最多 1 次）
    tech_lead_feedback: Optional[str] = None      # TechLead 的修复指令

    def log_action(self, message: str):
        """追加一条轨迹记录"""
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.action_trajectory.append(f"[{ts}] {message}")

    def log_error(self, error: str):
        """追加一条错误日志"""
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.error_logs.append(f"[{ts}] {error}")


class CompletedTaskRecord(BaseModel):
    """任务账本条目 — 一次子任务完成的不可变记录 (Phase 2.1)"""
    task_id: str
    target_file: str
    description: str                        # 任务意图（用户可读的语义标签）
    git_hash: Optional[str] = None          # 对应的 Git commit hash
    completed_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class CrossFilePivotRecord(BaseModel):
    """跨文件 pivot 账本条目 — 记录一次定向追因与调度结果。"""
    source_task_id: str
    importer_file: str
    provider_file: str
    missing_symbol: str = ""
    pivot_stage: str
    pivot_source: str
    verdict_type: str = ""
    provider_task_action: str = ""
    resolved: bool = False
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class OpenIssue(BaseModel):
    """未闭环问题台账条目 — 跨轮持续追踪的问题/技术债/回归记录。

    写入时机：
    - QA 端点失败 → category="qa_failure"
    - Task 熔断 → category="fuse"
    - 手动标注 → category="tech_debt"
    消费时机：
    - continue 模式启动时 Manager 读取所有 status="open" 条目
    - extend 模式读取作为"已知风险"提示
    """
    issue_id: str                                       # "issue_001"
    category: str                                       # "qa_failure" | "fuse" | "tech_debt" | "regression"
    summary: str                                        # "POST /api/orders 返回 500 KeyError"
    related_files: List[str] = Field(default_factory=list)
    related_endpoint: Optional[str] = None              # "POST /api/orders"
    first_seen_round: int = 0                           # 首次出现的轮次
    last_seen_round: int = 0                            # 最近一次仍存在的轮次
    repair_attempts: int = 0                            # 修复尝试次数
    status: str = "open"                                # "open" | "resolved" | "regressed"
    resolution_note: Optional[str] = None               # 修复时的简要说明
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


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
    dag_metadata: Dict[str, Any] = Field(default_factory=dict)
    user_requirement: Optional[str] = None
    out_dir: Optional[str] = None
    failure_context: Dict[str, Any] = Field(default_factory=dict)

    # --- Phase 0.3: Observer 全局快照 ---
    global_schema: Dict[str, Any] = Field(default_factory=dict)
    # 格式: {"models.py": [{"name":"User","fields":["id:int","name:str"],"table":"users"}]}
    global_routes: Dict[str, Any] = Field(default_factory=dict)
    # 格式: {"routes.py": [{"method":"GET","path":"/api/users","function":"get_users"}]}

    # --- Phase 2.1: Task Ledger (任务账本) ---
    completed_tasks: List[CompletedTaskRecord] = Field(default_factory=list)
    cross_file_pivots: List[CrossFilePivotRecord] = Field(default_factory=list)

    # --- Phase 5.5: 多轮对话基建 ---
    open_issues: List[OpenIssue] = Field(default_factory=list)           # 烂账账本：跨轮未闭环问题追踪
    project_snapshot: Dict[str, Any] = Field(default_factory=dict)      # 实时地图：project_scanner 缓存
    round_history: List[Dict[str, Any]] = Field(default_factory=list)   # 演进脚印：每轮结算的结构化摘要

    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    # --- Phase 2.1: 文件持久化 ---

    def save_to_disk(self, project_dir: str, filename: str = "blackboard_state.json") -> bool:
        """
        序列化到 .astrea/blackboard_state.json。
        触发时机：每轮 task commit 后。
        """
        import os
        astrea_dir = os.path.join(project_dir, ".astrea")
        os.makedirs(astrea_dir, exist_ok=True)
        save_path = os.path.join(astrea_dir, filename)
        try:
            json_str = self.model_dump_json(indent=2)
            with open(save_path, "w", encoding="utf-8") as f:
                f.write(json_str)
            logger.info(f"💾 BlackboardState 已持久化 → {save_path} ({len(json_str)} bytes)")
            return True
        except Exception as e:
            logger.error(f"❌ BlackboardState 持久化失败: {e}")
            return False

    @classmethod
    def load_from_disk(cls, project_dir: str) -> 'BlackboardState | None':
        """
        从 .astrea/blackboard_state.json 反序列化恢复。
        恢复时机：PM 收到该项目消息时自动加载。
        """
        import os
        save_path = os.path.join(project_dir, ".astrea", "blackboard_state.json")
        if not os.path.isfile(save_path):
            return None
        try:
            with open(save_path, "r", encoding="utf-8") as f:
                json_str = f.read()
            state = cls.model_validate_json(json_str)
            logger.info(f"📂 BlackboardState 已从磁盘恢复: {save_path}")
            return state
        except Exception as e:
            logger.error(f"❌ BlackboardState 反序列化失败: {e}")
            return None


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
        if status not in (ProjectStatus.FAILED, ProjectStatus.DELIVERED_WITH_WARNINGS) and self._state.failure_context:
            self._state.failure_context = {}
        self._touch()
        logger.info(f"📌 项目状态扭转: {old.value} → {status.value}")
        self.checkpoint()

    def set_user_requirement(self, requirement: str):
        """记录用户需求（项目启动时写入一次）"""
        self._state.user_requirement = requirement
        self._touch()

    def set_out_dir(self, out_dir: str, project_name: str = None):
        """设置项目输出目录（项目启动时写入一次）"""
        self._state.out_dir = out_dir
        if project_name:
            self._state.project_name = project_name
        self._touch()

    def record_failure_context(self, reason: str, error_message: str = None,
                               extra_context: Optional[Dict[str, Any]] = None):
        """记录失败态摘要，便于失败后直接从本地快照复盘。"""
        status_summary: Dict[str, int] = {}
        fused_task_ids: List[str] = []
        open_task_ids: List[str] = []

        for task in self._state.tasks:
            status_key = task.status.value if isinstance(task.status, Enum) else str(task.status)
            status_summary[status_key] = status_summary.get(status_key, 0) + 1
            if task.status == TaskStatus.FUSED:
                fused_task_ids.append(task.task_id)
            if task.status != TaskStatus.DONE:
                open_task_ids.append(task.task_id)

        context = {
            "reason": reason,
            "error_message": error_message,
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "project_status": self._state.project_status.value,
            "fused_task_ids": fused_task_ids,
            "open_task_ids": open_task_ids,
            "task_status_summary": status_summary,
        }
        if extra_context:
            context.update(extra_context)
            context["reason"] = reason
            context["error_message"] = error_message
            context["saved_at"] = datetime.now(timezone.utc).isoformat()
            context["project_status"] = self._state.project_status.value
            context["fused_task_ids"] = fused_task_ids
            context["open_task_ids"] = open_task_ids
            context["task_status_summary"] = status_summary
        self._state.failure_context = context
        self._touch()

    def find_task_by_file(self, target_file: str) -> Optional[TaskItem]:
        """按 target_file 查找已存在的 task（用于 TechLead 跨文件打回）"""
        def _norm(p: str) -> str:
            return os.path.normpath(p).replace('\\', '/')
        
        target = _norm(target_file)
        for t in self._state.tasks:
            if _norm(t.target_file) == target:
                return t
        return None

    def set_project_spec(self, spec: dict, spec_text: str, project_name: str = None):
        """Manager 贴上规划书和 API 契约"""
        self._state.project_spec = spec
        self._state.spec_text = spec_text
        if project_name:
            self._state.project_name = project_name
        self._touch()
        logger.info(f"📌 规划书已贴上黑板: {project_name or 'unnamed'}")
        self.checkpoint()

    def set_tasks(self, tasks: List[Dict[str, Any]], dag_metadata: Optional[Dict[str, Any]] = None):
        """Manager 贴上任务列表"""
        task_items = []
        for t in tasks:
            item = TaskItem(
                task_id=t.get("task_id", f"task_{len(task_items)+1}"),
                target_file=t.get("target_file", ""),
                description=t.get("description", ""),
                dependencies=t.get("dependencies", []),
                tech_stack=t.get("tech_stack"),
                task_type=t.get("task_type"),
                node_key=t.get("node_key"),
                group_id=t.get("group_id"),
                topo_index=t.get("topo_index"),
                ready_rank=t.get("ready_rank", 0),
                write_targets=t.get("write_targets", []),
                sub_tasks=t.get("sub_tasks", []),
                draft_action=t.get("draft_action"),
                tech_lead_invoked=t.get("tech_lead_invoked", False),
                tech_lead_feedback=t.get("tech_lead_feedback", ""),
            )
            task_items.append(item)
        self._state.tasks = task_items
        self._state.dag_metadata = dag_metadata or {}
        self._touch()
        if self._state.dag_metadata:
            logger.info(
                "📌 任务列表已贴上黑板: %s 个子任务 (DAG: %s 节点 / %s 边)",
                len(task_items),
                self._state.dag_metadata.get("node_count", len(task_items)),
                self._state.dag_metadata.get("edge_count", 0),
            )
        else:
            logger.info(f"📌 任务列表已贴上黑板: {len(task_items)} 个子任务")
        self.checkpoint()

    # --- Phase 0.3: 全局快照 ---

    def append_tasks(self, tasks: List[Dict[str, Any]], group_id: Optional[str] = None,
                     dag_metadata: Optional[Dict[str, Any]] = None):
        """追加一组任务，供 continue/extend 模式在保留历史账本时注入新任务。"""
        # v4.1: 收集已有 task_id，防止 ID 冲突导致死循环
        existing_ids = {t.task_id for t in self._state.tasks}

        # v5.3: 记录重命名映射，用于级联更新 dependencies
        rename_map: Dict[str, str] = {}

        new_items: List[TaskItem] = []
        for t in tasks:
            raw_id = t.get("task_id", f"task_{len(self._state.tasks)+len(new_items)+1}")
            # 冲突检测：若 ID 已存在，加 group 前缀确保唯一
            if raw_id in existing_ids:
                prefix = (group_id or "appended").replace(":", "_")
                new_id = f"{prefix}_{raw_id}"
                rename_map[raw_id] = new_id
                raw_id = new_id
            existing_ids.add(raw_id)

            item = TaskItem(
                task_id=raw_id,
                target_file=t.get("target_file", ""),
                description=t.get("description", ""),
                dependencies=t.get("dependencies", []),
                tech_stack=t.get("tech_stack"),
                task_type=t.get("task_type"),
                node_key=t.get("node_key"),
                group_id=t.get("group_id") or group_id,
                topo_index=t.get("topo_index"),
                ready_rank=t.get("ready_rank", 0),
                write_targets=t.get("write_targets", []),
                sub_tasks=t.get("sub_tasks", []),
                draft_action=t.get("draft_action"),
            )
            new_items.append(item)

        # v5.3: 级联更新 dependencies 中被重命名的 task_id 引用
        # 防止 Extend 任务的依赖指向 Round 1 的同名历史任务导致跨轮次死锁
        if rename_map:
            renamed_count = 0
            for item in new_items:
                updated_deps = [rename_map.get(dep, dep) for dep in item.dependencies]
                if updated_deps != item.dependencies:
                    renamed_count += 1
                    item.dependencies = updated_deps
            if renamed_count:
                logger.info(
                    "🔗 [append_tasks] 级联更新 %d 个任务的 dependencies (重命名: %s)",
                    renamed_count,
                    ", ".join(f"{k}→{v}" for k, v in rename_map.items()),
                )

        self._state.tasks.extend(new_items)
        if dag_metadata:
            if not self._state.dag_metadata:
                self._state.dag_metadata = {}
            self._state.dag_metadata[f"group:{group_id or 'default'}"] = dag_metadata
        self._touch()
        logger.info("追加任务: %s 个, group=%s", len(new_items), group_id or "")
        self.checkpoint()

    def update_global_snapshot(self, file_path: str, truth_dir: str):
        """
        增量更新全局快照：对单个文件提取 schema/routes。
        在 Engine commit 到真理区后调用。
        """
        try:
            from tools.observer import Observer
            obs = Observer(truth_dir)

            # 提取数据模型
            schema = obs.extract_schema(file_path)
            if schema:
                self._state.global_schema[file_path] = schema
            elif file_path in self._state.global_schema:
                del self._state.global_schema[file_path]

            # 提取路由
            routes = obs.extract_routes(file_path)
            if routes:
                self._state.global_routes[file_path] = routes
            elif file_path in self._state.global_routes:
                del self._state.global_routes[file_path]

            total_models = sum(len(v) for v in self._state.global_schema.values())
            total_routes = sum(len(v) for v in self._state.global_routes.values())
            if schema or routes:
                logger.info(f"📊 全局快照更新: {file_path} → {len(schema)} 模型, {len(routes)} 路由 "
                            f"(全局: {total_models} 模型, {total_routes} 路由)")
        except Exception as e:
            logger.warning(f"⚠️ 全局快照更新异常: {e}")

    def get_global_snapshot_text(self) -> str:
        """格式化全局快照为文本（注入 Coder prompt）"""
        parts = []
        if self._state.global_schema:
            parts.append("【全局数据模型】")
            for file_path, models in self._state.global_schema.items():
                for m in models:
                    table = f" (表: {m['table']})" if m.get('table') else ""
                    fields = ", ".join(m.get("fields", []))
                    line = f"  {file_path} → {m['name']}{table}: {fields}"
                    # P1-a: 展示 to_dict key（当与 Column 名有差异时）
                    td_keys = m.get("to_dict_keys")
                    parts.append(line)
                    if td_keys:
                        parts.append(f"    ⚠️ to_dict() 可用字段（模板/前端只能使用这些 key）: [{', '.join(td_keys)}]")

        if self._state.global_routes:
            parts.append("【全局 API 路由】")
            for file_path, routes in self._state.global_routes.items():
                for r in routes:
                    line = f"  {file_path} → {r['method']} {r['path']} → {r.get('function', '?')}"
                    # P1-d: endpoint 信息（当 endpoint ≠ function 时额外标注）
                    ep = r.get('endpoint')
                    if ep and ep != r.get('function'):
                        line += f" (endpoint: {ep})"
                    parts.append(line)

        return "\n".join(parts) if parts else ""

    # --- Phase 5.5: 烂账账本 (Open Issues) ---

    def upsert_issue(self, category: str, summary: str,
                     related_files: List[str] = None,
                     related_endpoint: str = None,
                     current_round: int = 0) -> OpenIssue:
        """追加或更新一条未闭环问题。

        如果已存在同 related_endpoint（或同 summary）的 open 条目，
        则更新 last_seen_round 和 repair_attempts；否则新建。
        """
        # 查找已有条目（优先按 endpoint 匹配，其次按 summary）
        # 注意：必须包含 resolved 状态，以支持"修复后再次失败"的回归检测
        existing = None
        for issue in self._state.open_issues:
            if issue.status not in ("open", "regressed", "resolved"):
                continue
            if related_endpoint and issue.related_endpoint == related_endpoint:
                existing = issue
                break
            if issue.summary == summary and issue.category == category:
                existing = issue
                break

        now = datetime.now(timezone.utc).isoformat()
        if existing:
            existing.last_seen_round = current_round
            existing.repair_attempts += 1
            existing.updated_at = now
            # 如果之前 resolved 又出现了 → 标记回归
            if existing.status == "resolved":
                existing.status = "regressed"
                existing.category = "regression"
                logger.warning(
                    "🔴 [烂账] 回归检测: %s (首见 R%d → 修复后 R%d 再次出现)",
                    existing.summary[:60], existing.first_seen_round, current_round,
                )
            logger.info("📋 [烂账] 更新: %s (尝试 %d 次, R%d)", existing.summary[:60], existing.repair_attempts, current_round)
            self._touch()
            return existing

        # 新建条目
        issue_id = f"issue_{len(self._state.open_issues) + 1:03d}"
        issue = OpenIssue(
            issue_id=issue_id,
            category=category,
            summary=summary,
            related_files=related_files or [],
            related_endpoint=related_endpoint,
            first_seen_round=current_round,
            last_seen_round=current_round,
            repair_attempts=0,
            status="open",
            created_at=now,
            updated_at=now,
        )
        self._state.open_issues.append(issue)
        logger.info("📋 [烂账] 新增: [%s] %s (R%d)", issue_id, summary[:60], current_round)
        self._touch()
        return issue

    def resolve_issues_by_endpoint(self, endpoint: str, current_round: int = 0):
        """将指定端点的所有 open 问题标记为 resolved。"""
        for issue in self._state.open_issues:
            if issue.related_endpoint == endpoint and issue.status in ("open", "regressed"):
                issue.status = "resolved"
                issue.resolution_note = f"QA R{current_round} 验证通过"
                issue.updated_at = datetime.now(timezone.utc).isoformat()
                logger.info("✅ [烂账] 已闭环: %s → resolved (R%d)", issue.summary[:60], current_round)
        self._touch()

    def get_open_issues(self) -> List[OpenIssue]:
        """获取所有未闭环问题（status=open 或 regressed）。"""
        return [i for i in self._state.open_issues if i.status in ("open", "regressed")]

    def get_open_issues_text(self) -> str:
        """格式化未闭环问题清单为文本（注入 Manager/Engine prompt）。"""
        issues = self.get_open_issues()
        if not issues:
            return ""
        lines = ["【⚠️ 未闭环问题台账 (Open Issues)】"]
        for issue in issues:
            tag = "🔴 回归" if issue.status == "regressed" else "🟡 待修"
            lines.append(
                f"  {tag} [{issue.issue_id}] {issue.summary} "
                f"(首见 R{issue.first_seen_round}, 修复尝试 {issue.repair_attempts} 次, "
                f"文件: {', '.join(issue.related_files) or '未知'})"
            )
        return "\n".join(lines)

    # --- Phase 5.5: 演进脚印 (Round History) ---

    def append_round_summary(self, mode: str, user_intent: str,
                             current_round: int = 0, extra: Dict[str, Any] = None):
        """每轮结算时追加一条结构化摘要。"""
        tasks = self._state.tasks
        done_count = sum(1 for t in tasks if t.status == TaskStatus.DONE)
        fused_count = sum(1 for t in tasks if t.status == TaskStatus.FUSED)
        fused_files = [t.target_file for t in tasks if t.status == TaskStatus.FUSED]

        summary = {
            "round": current_round,
            "mode": mode,
            "user_intent": (user_intent or "")[:200],
            "tasks_total": len(tasks),
            "tasks_done": done_count,
            "tasks_fused": fused_count,
            "fused_files": fused_files,
            "open_issues_count": len(self.get_open_issues()),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if extra:
            summary.update(extra)
        self._state.round_history.append(summary)
        logger.info(
            "📜 [演进] R%d 记录: mode=%s, done=%d, fused=%d, open_issues=%d",
            current_round, mode, done_count, fused_count, summary["open_issues_count"],
        )
        self._touch()

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
        # 轻量 checkpoint：保存草稿防崩溃丢失
        self.checkpoint()

    def clear_draft(self, task_id: str):
        """清除草稿（commit 到 VFS 后）"""
        task = self.get_task(task_id)
        if task:
            task.code_draft = None
            # task.draft_action = None  # 保留 draft_action，否则 get_execution_summary 统计 files_modified 永远为 0
            self._touch()

    def increment_retry(self, task_id: str) -> int:
        """增加重试计数"""
        task = self.get_task(task_id)
        if task:
            task.retry_count += 1
            self._touch()
            # retry >= 2 时 checkpoint，防崩溃后从错误计数重启
            if task.retry_count >= 2:
                self.checkpoint()
            return task.retry_count
        return 0

    def _dependencies_satisfied(self, task: TaskItem) -> bool:
        for dep_id in task.dependencies:
            dep_task = self.get_task(dep_id)
            if dep_task is None or dep_task.status != TaskStatus.DONE:
                return False
        return True

    def _in_group(self, task: TaskItem, group_id: Optional[str]) -> bool:
        return group_id is None or task.group_id == group_id

    def get_ready_tasks(self, limit: Optional[int] = None,
                        group_id: Optional[str] = None) -> List[TaskItem]:
        """按最终 DAG 顺序返回当前 ready 的 TODO 任务。"""
        ready_tasks: List[TaskItem] = []
        for task in self._state.tasks:
            if not self._in_group(task, group_id):
                continue
            if task.status != TaskStatus.TODO:
                continue
            if self._dependencies_satisfied(task):
                ready_tasks.append(task)
                if limit is not None and len(ready_tasks) >= limit:
                    break
        return ready_tasks

    def get_next_runnable_task(self, group_id: Optional[str] = None) -> Optional[TaskItem]:
        """
        依赖图调度：找到第一个前置任务全部 DONE 的 TODO 任务。
        保证"先写后端 API，再写前端调用"的严丝合缝。
        """
        ready_tasks = self.get_ready_tasks(limit=1, group_id=group_id)
        return ready_tasks[0] if ready_tasks else None

    def all_tasks_done(self, group_id: Optional[str] = None) -> bool:
        """检查是否所有任务都已完成"""
        return all(
            t.status in (TaskStatus.DONE, TaskStatus.FUSED)
            for t in self._state.tasks
            if self._in_group(t, group_id)
        )

    def has_fused_tasks(self, group_id: Optional[str] = None) -> bool:
        """检查是否有熔断的任务"""
        return any(
            t.status == TaskStatus.FUSED
            for t in self._state.tasks
            if self._in_group(t, group_id)
        )

    # --- Phase 2.1: Task Ledger ---

    def record_completed_task(self, task_id: str, target_file: str,
                              description: str, git_hash: str = None):
        """向账本追加一条完成记录"""
        record = CompletedTaskRecord(
            task_id=task_id,
            target_file=target_file,
            description=description,
            git_hash=git_hash,
        )
        self._state.completed_tasks.append(record)
        self._touch()
        logger.info(f"📒 Ledger 记录: [{task_id}] {target_file} → {git_hash or 'no-hash'}")
        self.checkpoint()

    def record_cross_file_pivot(
        self,
        source_task_id: str,
        importer_file: str,
        provider_file: str,
        missing_symbol: str,
        pivot_stage: str,
        pivot_source: str,
        verdict_type: str,
        provider_task_action: str,
        resolved: bool,
    ):
        """记录一次跨文件 pivot 事件。"""
        record = CrossFilePivotRecord(
            source_task_id=source_task_id,
            importer_file=importer_file,
            provider_file=provider_file,
            missing_symbol=missing_symbol,
            pivot_stage=pivot_stage,
            pivot_source=pivot_source,
            verdict_type=verdict_type,
            provider_task_action=provider_task_action,
            resolved=resolved,
        )
        self._state.cross_file_pivots.append(record)
        self._touch()
        logger.info(
            "📒 Cross-file Pivot: task=%s importer=%s provider=%s action=%s resolved=%s",
            source_task_id,
            importer_file,
            provider_file,
            provider_task_action,
            resolved,
        )
        self.checkpoint()

    # --- P0 修复：统一状态变更 API（封杀直赋） ---

    def reopen_task(self, task_id: str, fix_instruction: str = "",
                    reset_retry: bool = False, reorder_before: str = None):
        """
        TechLead 打回任务。
        统一入口，取代 _reopen_guilty_task 中的直接赋值。

        Args:
            task_id: 被打回的任务 ID
            fix_instruction: TechLead 的修复指令
            reset_retry: 是否重置 retry_count（默认否，保留历史）
            reorder_before: 将此任务移到指定 task_id 之前
        """
        task = self.get_task(task_id)
        if not task:
            logger.error(f"❌ reopen_task: 找不到任务 {task_id}")
            return

        old_status = task.status
        task.status = TaskStatus.TODO
        if reset_retry:
            task.retry_count = 0
        if fix_instruction:
            task.tech_lead_feedback = fix_instruction
        task.tech_lead_invoked = True
        task.log_action(
            f"TechLead 打回: {old_status.value} → TODO "
            f"(retry={'重置' if reset_retry else '保留'})"
        )

        if reorder_before:
            self._move_task_before(task_id, reorder_before)

        self._touch()
        logger.info(f"📌 [{task_id}] reopen: {old_status.value} → TODO")
        self.checkpoint()

    def nudge_task_for_tech_lead(self, task_id: str, fix_instruction: str,
                                 reorder_before: str = None):
        """
        给未完成任务注入 TechLead 修复指令，并确保它会先于阻塞方执行。
        """
        task = self.get_task(task_id)
        if not task:
            logger.error(f"❌ nudge_task_for_tech_lead: 找不到任务 {task_id}")
            return

        old_status = task.status
        if task.status != TaskStatus.TODO:
            task.status = TaskStatus.TODO
        task.tech_lead_feedback = fix_instruction
        task.tech_lead_invoked = True
        task.log_action(f"TechLead 定向追因注入: {old_status.value} → {task.status.value}")

        if reorder_before:
            self._move_task_before(task_id, reorder_before)

        self._touch()
        logger.info(f"📌 [{task_id}] nudge_for_tech_lead: {old_status.value} → {task.status.value}")
        self.checkpoint()

    def inject_targeted_fix_task(
        self,
        target_file: str,
        description: str,
        fix_instruction: str,
        reorder_before: str = None,
        source_task_id: str = "",
    ) -> str:
        """
        注入一个最小定向修复任务，仅允许修改 provider_file。
        """
        task_id = f"pivot_fix_{len(self._state.tasks) + 1}"
        task = TaskItem(
            task_id=task_id,
            target_file=target_file,
            description=description,
            write_targets=[target_file],
            tech_lead_feedback=fix_instruction,
            tech_lead_invoked=True,
        )
        task.log_action(
            f"TechLead 注入最小修复任务"
            + (f" (source={source_task_id})" if source_task_id else "")
        )

        insert_index = len(self._state.tasks)
        if reorder_before:
            dst = next((i for i, t in enumerate(self._state.tasks) if t.task_id == reorder_before), None)
            if dst is not None:
                insert_index = dst
        self._state.tasks.insert(insert_index, task)
        self._touch()
        logger.info("📌 注入最小修复任务: %s -> %s", task_id, target_file)
        self.checkpoint()
        return task_id

    def reset_task_for_fix(self, task_id: str, new_description: str,
                           feedback: str, reset_retry: bool = True):
        """
        集成测试失败后重置任务。
        统一入口，取代 IntegrationManager.retry_with_replan 中的直接赋值。
        """
        task = self.get_task(task_id)
        if not task:
            logger.error(f"❌ reset_task_for_fix: 找不到任务 {task_id}")
            return

        old_status = task.status
        task.description = new_description
        task.status = TaskStatus.TODO
        if reset_retry:
            task.retry_count = 0
        task.tech_lead_feedback = feedback
        task.log_action(f"IntegrationManager 重置: {old_status.value} → TODO")

        self._touch()
        logger.info(f"📌 [{task_id}] reset_for_fix: {old_status.value} → TODO")
        self.checkpoint()

    def mark_task_error(self, task_id: str, error_msg: str):
        """
        标记任务错误并打回到 TODO（集成测试失败时）。
        统一入口，取代 IntegrationManager.run_integration_test 中的直接赋值。
        """
        task = self.get_task(task_id)
        if not task:
            return
        task.log_error(error_msg)
        task.status = TaskStatus.TODO
        self._touch()
        logger.info(f"📌 [{task_id}] mark_error → TODO")
        self.checkpoint()

    def unlock_fill_mode(self, task_id: str):
        """
        解锁 Fill 模式骨架约束。
        统一入口，取代 ProjectObserver.build_task_meta 中的直接赋值。
        """
        task = self.get_task(task_id)
        if not task:
            return
        if task.sub_tasks:
            task.sub_tasks = []
            task.log_action("解除骨架约束（退出 Fill 模式）")
            self._touch()
            logger.info(f"🔓 [{task_id}] Fill 模式解锁")

    def rollback_fill_to_skeleton(self, task_id: str, reason: str):
        """将任务从 fill 阶段回退到 skeleton 阶段，保留原始 sub_tasks。"""
        task = self.get_task(task_id)
        if not task or not task.sub_tasks:
            return

        task.current_sub_task_index = 0
        task.code_draft = None
        task.draft_action = None
        task.log_action(f"回退到 skeleton 阶段: {reason[:200]}")
        self._touch()
        logger.info(f"↩️ [{task_id}] Fill 回退到 skeleton")
        self.checkpoint()

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

    # --- v4.0: 执行摘要（供 PM 回传环）---

    def get_execution_summary(self, group_id: str = None) -> dict:
        """
        生成本轮执行的结构化摘要（供 PM 回传环使用）。
        group_id: 可选，仅统计指定 group 的任务（extend/continue 模式）。
                  为 None 时统计所有任务（create/patch 模式）。
        """
        if group_id:
            # 只统计当前轮次新增的任务
            tasks = [t for t in self._state.tasks if t.group_id == group_id]
        else:
            tasks = self._state.tasks
        done = [t for t in tasks if t.status == TaskStatus.DONE]
        fused = [t for t in tasks if t.status == TaskStatus.FUSED]
        rejected = [t for t in tasks if t.status == TaskStatus.REJECTED]
        open_issues = self._state.open_issues

        return {
            "completed_descriptions": [t.description for t in done],
            "fused_tasks": [t.task_id for t in fused],
            "rejected_tasks": [t.task_id for t in rejected],
            "files_created": len(set(t.target_file for t in done if t.draft_action == "create")),
            "files_modified": len(set(t.target_file for t in done if t.draft_action in ("modify", "rewrite"))),
            "total_tasks": len(tasks),
            "done_count": len(done),
            "fused_count": len(fused),
            "rejected_count": len(rejected),
            "open_issues": [
                {"category": iss.category, "summary": iss.summary}
                for iss in open_issues[:5]  # 最多 5 条
            ],
        }

    # --- 内部工具 ---

    def _touch(self):
        """更新修改时间"""
        self._state.updated_at = datetime.now(timezone.utc).isoformat()

    def _move_task_before(self, task_id: str, reorder_before: str):
        tasks = self._state.tasks
        src = next((i for i, t in enumerate(tasks) if t.task_id == task_id), None)
        dst = next((i for i, t in enumerate(tasks) if t.task_id == reorder_before), None)
        if src is not None and dst is not None and src > dst:
            tasks.insert(dst, tasks.pop(src))
            logger.info(f"⚖️ 任务重排: {task_id} 移到 {reorder_before} 之前")

    def __repr__(self):
        s = self._state
        return (f"<Blackboard project={s.project_id} "
                f"status={s.project_status.value} "
                f"tasks={len(s.tasks)}>")
