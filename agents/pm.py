"""
PM Agent — ASTrea 化身，用户的唯一对话窗口

Phase 2.6 双层架构：
  Layer 1: 保守正则命令前缀（零 Token 确定性快车道）
  Layer 2: LLM Tool Calling 路由（原生结构化意图识别）
  Layer 3: 人格化身（高情商回复 + 上下文按需注入）

路由工具：execute_project_task / route_to_revise_plan / reply_to_chat / ask_for_clarification
状态机：idle / wait_confirm / wait_clarify
"""
import os
import re
import json
import logging
import subprocess
import threading
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any

from core.audit_guard import render_audit_report_markdown
from core.blackboard import BlackboardState
from core.llm_client import default_llm
from core.project_scanner import scan_existing_project
from core.prompt import Prompts
from core.techlead_scope import TargetScope, resolve_target_scope
from core.ws_broadcaster import global_broadcaster

logger = logging.getLogger("PMAgent")

# 滑动窗口 — Token 预算制（弹性设计：超出不截断当前对话对）
_WINDOW_TOKEN_BUDGET = 4000   # 目标 token 预算（v2: 从 3000 扩容，plan 锚定已解决核心失忆）
_CHAR_PER_TOKEN = 2           # 中英混合估算：~2 chars/token


@dataclass
class PMResponse:
    """PM 返回给前端的结构化响应"""
    intent: str              # "create" | "patch" | "rollback" | "chat" | "scan" | "clarify" | "action"
    reply: str               # PM 的自然语言回复
    plan_md: Optional[str] = None     # plan.md（create 意图时返回）
    actions: Optional[list] = None    # 确定性按钮 [{"id": "confirm", ...}]
    is_executing: bool = False        # 是否已触发 Engine 执行


class PMAgent:
    """
    ASTrea 化身 — 用户的唯一对话窗口。

    Phase 2.5.1 双层架构：
    - Layer 1: 路由嗅探器 — 保守正则(仅命令前缀) + LLM 主力分类
    - Layer 2: 人格化身 — 高情商回复 + 上下文按需注入
    """

    def __init__(self, project_id: str):
        self.project_id = project_id
        self.model = os.getenv("MODEL_PM", "deepseek-v4-flash")
        _et, _re = default_llm.parse_thinking_config(os.getenv("THINKING_PM", "false"))
        self.enable_thinking = _et
        self._reasoning_effort = _re

        # 对话滑动窗口（内存态）
        self.conversation: List[dict] = []

        # 待确认的规划（等待用户 confirm/reject）
        self.pending_req: Optional[dict] = None
        self.pending_plan_md: Optional[str] = None
        self._plan_created_at_round: int = 0  # plan 产生时的 round_id（安全窗口用）
        self._plan_version: int = 0           # 当前 plan 版本号

        # patch / rollback 暂存
        self.pending_patch: Optional[str] = None          # 用户的修改意图描述
        self.pending_extend: Optional[str] = None         # 用户的新增模块意图描述
        self.pending_continue: Optional[str] = None       # 继续修复上一轮 QA 失败的确认上下文
        self.pending_rollback_commit: Optional[str] = None # 待回滚的 commit hash

        # 状态机（v3.0 透明化路由）
        self.state: str = "idle"
        # 有效状态: "idle" | "wait_confirm" | "wait_clarify"
        self.pending_mode: str = ""  # execute_project_task 的 mode: create/modify/continue/rollback/audit

        # Engine 执行时的 mode（create/patch/rollback）
        self.confirmed_mode: str = "auto"

        # 上一轮路由结果（供 Layer 2 使用）
        self._last_route: Optional[dict] = None

        # 轮次计数（用于 FTS5 round_id）
        self.round_id: int = 0

        # 决策备忘录（Phase 2 Letta-Lite Core Memory）
        self._memo: Dict[str, str] = {
            "tech_stack": "",      # 技术栈决策
            "features": "",        # 已确认功能列表
            "design": "",          # 设计偏好
            "pending": "",         # 待定事项
            "user_prefs": "",      # 用户性格/偏好
        }
        self._memo_log: List[str] = []  # 审计日志
        self._memo_lock = threading.Lock()
        self._archiving_event = threading.Event()
        self._archiving_event.set()  # 初始：无归档任务

        # v4.0: 执行账本（每轮 Engine 执行的结构化摘要）
        self.execution_ledger: List[dict] = []

        # v4.0: Phase 分步构建
        self.project_phases: List[dict] = []     # [{index, name, features, status}]
        self.current_phase_index: int = 0
        self._full_plan_md: Optional[str] = None  # 完整 plan（含所有 Phase）

        # FTS5 对话存储（延迟初始化，需要 project_dir）
        self._store = None

    def refresh_runtime_config(self):
        """从当前环境变量刷新 PM 运行时模型配置。"""
        self.model = os.getenv("MODEL_PM", "deepseek-v4-flash")
        _et, _re = default_llm.parse_thinking_config(os.getenv("THINKING_PM", "false"))
        self.enable_thinking = _et
        self._reasoning_effort = _re

    def _chat_completion(self, **kwargs):
        self.refresh_runtime_config()
        kwargs.setdefault("model", self.model)
        kwargs.setdefault("enable_thinking", self.enable_thinking)
        kwargs.setdefault("reasoning_effort", self._reasoning_effort)
        return default_llm.chat_completion(**kwargs)

    def _generate_reply(self, context: str, fallback: str) -> str:
        """
        轻量 LLM 调用：根据上下文动态生成 1-2 句自然语言回复。
        失败时降级到 fallback 静态文案。
        """
        try:
            resp = self._chat_completion(
                messages=[
                    {"role": "system", "content": (
                        "你是用户的 AI 项目经理助手。根据下面的上下文，用1-2句自然口语回复用户。"
                        "要求：简洁、有针对性、不重复上下文原文、不用敬语套话、"
                        "如果涉及执行操作，提醒用户关注左侧面板进度。"
                    )},
                    {"role": "user", "content": context},
                ],
                temperature=0.7,
            )
            reply = (resp.content or "").strip()
            return reply if reply else fallback
        except Exception as e:
            logger.warning(f"_generate_reply 降级: {e}")
            return fallback

    def _get_store(self):
        """延迟初始化 ConversationStore"""
        if self._store is None:
            try:
                from core.conversation_store import ConversationStore
                projects_dir = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "projects"
                )
                project_dir = os.path.join(projects_dir, self.project_id)
                if os.path.isdir(project_dir):
                    self._store = ConversationStore(project_dir)
            except Exception as e:
                logger.warning(f"ConversationStore 初始化失败: {e}")
        return self._store

    # ============================================================
    # 决策备忘录管理（Phase 2 Letta-Lite）
    # ============================================================

    _MEMO_FIELDS = {"tech_stack", "features", "design", "pending", "user_prefs"}

    def _execute_update_memo(self, field: str, action: str, value: str):
        """字段级备忘录更新执行器（代码层强制安全约束）"""
        if field not in self._MEMO_FIELDS:
            logger.warning(f"非法备忘录字段: {field}")
            return
        if not value or not value.strip():
            logger.warning(f"拒绝空值写入: {field}")
            return

        with self._memo_lock:
            old = self._memo.get(field, "")
            if action == "set":
                self._memo[field] = value.strip()
                self._memo_log.append(f"R{self.round_id} SET {field}: {old!r} → {value!r}")
            elif action == "append":
                self._memo[field] = f"{old}, {value.strip()}" if old else value.strip()
                self._memo_log.append(f"R{self.round_id} APPEND {field}: +{value!r}")
            else:
                logger.warning(f"非法备忘录操作: {action}")
                return
            logger.info(f"备忘录更新: [{action}] {field} = {self._memo[field]}")

    def _build_memo_text(self) -> str:
        """将备忘录序列化为注入 prompt 的文本"""
        labels = {
            "tech_stack": "技术栈",
            "features": "已确认功能",
            "design": "设计偏好",
            "pending": "待定事项",
            "user_prefs": "用户偏好",
        }
        with self._memo_lock:
            parts = []
            for key, label in labels.items():
                val = self._memo.get(key, "")
                if val:
                    parts.append(f"{label}: {val}")
            memo_text = "\n".join(parts) if parts else ""
        # v4.0: 注入执行账本摘要
        if self.execution_ledger:
            recent = self.execution_ledger[-3:]
            ledger_lines = []
            for e in recent:
                status = "✅" if e["success"] else "❌"
                features = ", ".join(e["built_features"][:3]) if e["built_features"] else "无"
                ledger_lines.append(f"  R{e['round']} {status} {e['mode']}: {features}")
            memo_text += "\n【执行历史】\n" + "\n".join(ledger_lines)
        return memo_text

    def _async_archive(self, user_msg: str, pm_reply: str):
        """后台线程：提取本轮决策变更并更新备忘录（用户无感知）"""
        try:
            current_memo = self._build_memo_text() or "（暂无记录）"
            resp = self._chat_completion(
                model=self.model,
                messages=[
                    {"role": "system", "content": Prompts.PM_MEMO_EXTRACT.format(
                        current_memo=current_memo
                    )},
                    {"role": "user", "content": f"用户: {user_msg}\nPM: {pm_reply}"},
                ],
                tools=Prompts.MEMO_UPDATE_TOOL,
                tool_choice="auto",
                temperature=0.0,
            )
            if resp.tool_calls:
                for tc in resp.tool_calls:
                    try:
                        args = json.loads(tc.function.arguments)
                        self._execute_update_memo(**args)
                    except (json.JSONDecodeError, TypeError, KeyError) as e:
                        logger.warning(f"备忘录 tool call 解析失败: {e}")
            else:
                logger.debug("本轮无决策变更，备忘录无更新")
        except Exception as e:
            logger.warning(f"异步归档失败（不影响主流程）: {e}")
        finally:
            self._archiving_event.set()  # 标记归档完成

    # ============================================================
    # 主入口
    # ============================================================

    def chat(self, user_message: str) -> PMResponse:
        """
        主入口：接收用户消息，返回结构化响应。

        流程：追加历史 → 状态机分发/意图分类 → 处理 → 截断窗口 → 持久化 → 返回
        """
        self.refresh_runtime_config()
        self.round_id += 1
        logger.info(f"PM 收到消息 (round={self.round_id}): {user_message[:80]}...")

        # Phase 2: 等待上一轮异步归档完成（最多 3s，前端显示"正在思考..."）
        if not self._archiving_event.wait(timeout=3.0):
            logger.warning("上一轮归档超时，跳过等待继续处理")

        # 追加用户消息到窗口
        self.conversation.append({"role": "user", "content": user_message})

        # v4.0: 统一意图引擎 — 无状态机分支，每轮都走 LLM 路由
        route_result = self._classify_intent(user_message)
        self._last_route = route_result
        logger.info(f"路由结果: {route_result}")
        response = self._dispatch_route(route_result, user_message)

        # 追加 PM 回复到窗口
        self.conversation.append({"role": "assistant", "content": response.reply})

        # 弹性 Token 截断滑动窗口
        self.conversation = self._trim_window_by_tokens(self.conversation)

        # 持久化到 FTS5
        store = self._get_store()
        if store:
            try:
                store.append("user", user_message, self.round_id)
                store.append("pm", response.reply, self.round_id)
            except Exception as e:
                logger.warning(f"FTS5 写入失败: {e}")

        # Phase 2: 异步归档 — 后台提取决策到备忘录（用户无感知）
        self._archiving_event.clear()  # 标记：归档中
        threading.Thread(
            target=self._async_archive,
            args=(user_message, response.reply),
            daemon=True,
        ).start()

        return response

    def _dispatch_route(self, route_result: dict, message: str) -> PMResponse:
        """根据路由结果分发到对应处理器（v2.6 Tool Calling 版）"""
        route = route_result.get("route", "reply_to_chat")
        args = route_result.get("args", {})

        if route == "execute_project_task":
            mode = args.get("mode", "modify")
            if mode == "create":
                # v4.0: 如果有待确认方案，"确认"意图直接触发执行
                if self.pending_plan_md:
                    return self._handle_confirm()
                # v5.1 兜底: 项目已存在时 create 强制降级为 modify
                # 防止 LLM 路由器将"功能变更"误判为"新建项目"导致全量覆盖
                if self._project_exists():
                    logger.warning(
                        "⚠️ [路由纠正] LLM 选了 create 但项目已存在 (%s)，"
                        "强制降级为 modify（_handle_patch）",
                        self.project_id,
                    )
                    return self._handle_patch(message)
                return self._handle_create(message)
            elif mode == "modify":
                # v4.0: 如果有待确认的 patch 方案，直接触发执行
                if self.pending_patch:
                    return self._handle_patch_execute(confirm_message=message)
                return self._handle_patch(message)
            elif mode == "continue":
                # v4.0: 如果有待确认的 extend（Phase 续期），直接触发
                if self.pending_extend:
                    return self._handle_extend_execute()
                # 如果有 pending_continue（上轮 QA 修复确认），直接执行
                if self.pending_continue:
                    return self._handle_continue_execute()
                # v4.1 兆底: 如果有 Phase 待执行，自动预装并走 extend
                if self.project_phases:
                    next_phase = next((p for p in self.project_phases if p["status"] == "pending"), None)
                    if next_phase and self._full_plan_md:
                        self.pending_extend = self._build_phase_plan(self._full_plan_md, next_phase["index"] - 1)
                        logger.info(f"⚡ Phase 兆底预装: Phase {next_phase['index']}「{next_phase['name']}」")
                        return self._handle_extend_execute()
                return self._handle_continue(message)
            elif mode == "rollback":
                return self._handle_rollback(message)
            elif mode == "audit":
                return self._handle_audit(message)
            else:
                return self._handle_patch(message)  # 未知 mode 降级为 modify
        elif route == "route_to_revise_plan":
            # v4.1: 修复残留的 self.state 判断，改用 pending_plan_md
            if self.pending_plan_md:
                return self._handle_plan_revision(message)
            else:
                return self._handle_create(message)
        elif route == "ask_for_clarification":
            return self._handle_clarify_v2(args)
        elif route == "run_project_test":
            return self._handle_test(args)
        elif route == "search_archive":
            return self._handle_archive_search(args)
        elif route == "reply_to_chat":
            return self._handle_chat(message)
        else:
            return self._handle_chat(message)

    # [已删除] handle_action — v4.0 取消按钮确认，纯对话驱动

    # ============================================================
    # 意图识别引擎（v4.0 — 纯 LLM，零正则）
    # ============================================================

    def _classify_intent(self, message: str) -> dict:
        """v4.0: 纯 LLM 意图识别，零正则。所有消息 100% 走 Tool Calling。"""
        return self._tool_call_route(message)

    def _tool_call_route(self, message: str) -> dict:
        """
        通过 LLM Tool Calling 实现路由（v2.6 替代旧 JSON 分类器）。
        LLM 从 5 个工具中选择一个，系统级结构化输出，无需 JSON 手动解析。
        """
        project_exists = self._project_exists()
        # Phase 2 PM A-1: 注入结构化项目状态（而非仅 bool）
        if project_exists:
            project_dir = self._get_project_dir()
            file_info = self._scan_project_files(project_dir)
            tech = self._detect_tech_stack(file_info['files'])
            desc = self._extract_project_description(project_dir)
            project_status = (
                f"项目 {self.project_id} — 已存在 ({file_info['total']} 个文件)\n"
                f"  技术栈: {tech or '未识别'} | 描述: {desc or '无'}"
            )
        else:
            project_status = f"项目 {self.project_id} — 不存在或为空项目"

        # v4.0: 路由器感知所有待确认状态
        if self.pending_plan_md:
            project_status += "\n⚠️ 有待确认的项目方案"
        if self.pending_patch:
            project_status += "\n⚠️ 有待确认的修改方案"
        if self.pending_extend:
            project_status += "\n⚠️ 有待确认的扩展方案（下一 Phase）"
        if self.pending_continue:
            project_status += "\n⚠️ 有待确认的修复方案"

        # Phase 2: 路由器感知历史决策
        memo_text = self._build_memo_text()
        if memo_text:
            project_status += f"\n【历史决策】{memo_text}"

        # v4.1: 活跃项目记忆 — plan.md + Phase 进度
        if self._full_plan_md:
            phase_summary = self._build_phase_status_text()
            project_status += f"\n【活跃项目方案】\n{phase_summary}"

        try:
            system_prompt = Prompts.PM_ROUTE_SYSTEM.format(
                project_status=project_status,
            )
            messages = [{"role": "system", "content": system_prompt}]
            # 带上近几轮对话（策略 B 蓄水池：让 LLM 自动从上下文中累积信息）
            messages.extend(self.conversation[-4:])

            resp = self._chat_completion(
                model=self.model,
                messages=messages,
                tools=Prompts.PM_ROUTE_TOOLS,
                tool_choice="auto",
                temperature=0.0,
            )

            # 解析 Tool Call
            if resp.tool_calls:
                tc = resp.tool_calls[0]
                func_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                # v5.0: 提取 CoT reasoning 用于审计追溯，不传入下游
                reasoning = args.pop("reasoning", "")
                if reasoning:
                    logger.info(f"路由 CoT reasoning: {reasoning}")
                else:
                    logger.warning(f"路由 {func_name} 未提供 reasoning（模型可能未遵循 schema）")
                logger.info(f"Tool Calling 路由: {func_name}({args})")
                # 根据路由类型自动推断 context_needs（兼容下游 _build_project_context）
                context_needs = []
                if func_name == "execute_project_task":
                    context_needs = ["file_list"]
                return {"route": func_name, "args": args, "context_needs": context_needs}

            # Fallback: 模型没选工具 → chat
            logger.info("Tool Calling 未选择工具，降级为 chat")
            return {"route": "reply_to_chat", "args": {}}

        except Exception as e:
            logger.warning(f"Tool Calling 路由失败: {e}，降级为 chat")
            return {"route": "reply_to_chat", "args": {}}

    def _project_exists(self) -> bool:
        """检查当前项目是否已有真实文件"""
        projects_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "projects"
        )
        project_dir = os.path.join(projects_dir, self.project_id)
        if not os.path.isdir(project_dir):
            return False
        # 排除隐藏目录和配置文件，检查是否有实际源代码
        for root, dirs, files in os.walk(project_dir):
            dirs[:] = [d for d in dirs if d not in
                       ('__pycache__', 'venv', '.venv', 'node_modules', '.sandbox', '.astrea', '.git')]
            real_files = [f for f in files if not f.startswith('.') and f != 'plan.md']
            if real_files:
                return True
        return False

    # ============================================================
    # Plan 生成（Phase 2: 替代 PlannerLite 管道）
    # ============================================================

    def _generate_plan(self) -> str:
        """
        直接从对话窗口生成 plan.md。
        PM 持有完整上下文（颜色/布局/功能讨论），一次 LLM 调用搞定。

        v2: 安全窗口内注入旧 plan 作为参考（防止 reject 后重建时丢失讨论细节）。
        """
        # 构建对话文本
        conv_text = "\n".join(
            f"{'用户' if m['role'] == 'user' else 'PM'}: {m['content']}"
            for m in self.conversation
        )

        # 安全窗口内注入旧 plan（<=3 轮）
        old_plan_hint = ""
        if self.pending_plan_md and self._plan_age_in_rounds() <= 3:
            old_plan_hint = f"\n\n【上一版方案（仅供参考，可全部推翻）】\n{self.pending_plan_md}"

        try:
            response = self._chat_completion(
                model=self.model,
                messages=[
                    {"role": "system", "content": Prompts.PM_GENERATE_PLAN},
                    {"role": "user", "content": f"以下是与用户的对话，请从中生成技术方案文档：\n\n{conv_text}{old_plan_hint}"},
                ],
                temperature=0.3,
            )
            plan_md = response.content.strip()

            # 清理可能的 Markdown 代码块包裹
            if plan_md.startswith("```markdown"):
                plan_md = plan_md[len("```markdown"):].strip()
            if plan_md.startswith("```"):
                plan_md = plan_md[3:].strip()
            if plan_md.endswith("```"):
                plan_md = plan_md[:-3].strip()

            logger.info(f"plan.md 生成完毕 ({len(plan_md)} 字符)")
            return plan_md

        except Exception as e:
            logger.error(f"Plan 生成失败: {e}")
            return "# 项目方案\n\n（方案生成失败，请重试）"

    def _revise_plan(self, revision_request: str) -> str:
        """
        增量修订 plan：基于旧 plan + 用户修改意见，只改用户指定的部分。
        Fix A 核心：替代旧的全量重新生成。
        """
        if not self.pending_plan_md:
            # 没有旧 plan，降级为全量生成
            logger.warning("_revise_plan 没有旧 plan，降级为 _generate_plan")
            return self._generate_plan()

        # 构建对话上下文（供 LLM 理解语境）
        conv_text = "\n".join(
            f"{'用户' if m['role'] == 'user' else 'PM'}: {m['content']}"
            for m in self.conversation[-6:]  # 只取最近 6 条，修订不需要远古历史
        )

        try:
            system_prompt = Prompts.PM_REVISE_PLAN.format(
                existing_plan=self.pending_plan_md,
            )
            response = self._chat_completion(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"用户的修改意见：{revision_request}\n\n对话上下文：\n{conv_text}"},
                ],
                temperature=0.2,
            )
            plan_md = response.content.strip()

            # 清理 Markdown 代码块包裹
            if plan_md.startswith("```markdown"):
                plan_md = plan_md[len("```markdown"):].strip()
            if plan_md.startswith("```"):
                plan_md = plan_md[3:].strip()
            if plan_md.endswith("```"):
                plan_md = plan_md[:-3].strip()

            logger.info(f"plan.md 增量修订完毕 ({len(plan_md)} 字符)")
            return plan_md

        except Exception as e:
            logger.error(f"Plan 增量修订失败: {e}，降级为全量生成")
            return self._generate_plan()

    def _plan_age_in_rounds(self) -> int:
        """计算当前 pending_plan_md 的年龄（距离产生时过了多少轮对话）"""
        if self._plan_created_at_round <= 0:
            return 999  # 没有记录 → 视为远古
        return self.round_id - self._plan_created_at_round

    def _save_plan_to_disk(self, plan_md: str, project_dir: str) -> int:
        """保存 plan.md，同时备份历史版本。返回版本号。"""
        try:
            astrea_dir = os.path.join(project_dir, ".astrea")
            os.makedirs(astrea_dir, exist_ok=True)

            # 确定版本号
            version = self._get_next_plan_version(astrea_dir)

            # 备份到 plan_v{N}.md
            versioned_path = os.path.join(astrea_dir, f"plan_v{version}.md")
            with open(versioned_path, "w", encoding="utf-8") as f:
                f.write(plan_md)

            # 同时写入 plan.md（始终是最新版，向下兼容）
            plan_path = os.path.join(astrea_dir, "plan.md")
            with open(plan_path, "w", encoding="utf-8") as f:
                f.write(plan_md)

            logger.info(f"plan.md v{version} 已保存: {versioned_path}")
            return version
        except Exception as e:
            logger.warning(f"plan.md 保存失败: {e}")
            return 0

    @staticmethod
    def _get_next_plan_version(astrea_dir: str) -> int:
        """扫描 plan_v*.md 确定下一个版本号"""
        import glob
        existing = glob.glob(os.path.join(astrea_dir, "plan_v*.md"))
        if not existing:
            return 1
        versions = []
        for p in existing:
            m = re.search(r'plan_v(\d+)\.md$', p)
            if m:
                versions.append(int(m.group(1)))
        return max(versions) + 1 if versions else 1

    @staticmethod
    def _extract_plan_title(plan_md: str) -> str:
        """从 plan.md 的 # 标题行提取项目名称"""
        for line in plan_md.split("\n"):
            line = line.strip()
            if line.startswith("# ") and not line.startswith("## "):
                return line[2:].strip()
        return "您的项目"

    @staticmethod
    def _extract_plan_defaults(plan_md: str) -> list:
        """从 plan.md 中提取标注了'（默认）'的技术栈字段"""
        defaults = []
        for line in plan_md.split("\n"):
            if "（默认）" in line or "(默认)" in line:
                # 提取 **字段名** 中的内容
                m = re.search(r'\*\*(.+?)\*\*', line)
                if m:
                    defaults.append(m.group(1))
        return defaults

    # ============================================================
    # [已废弃] 需求标准化 — Phase 2 后不再使用，保留以防回退
    # ============================================================

    # ============================================================
    # v4.0: Phase 拆分器
    # ============================================================

    @staticmethod
    def _infer_phase_scope(name: str, features: list) -> str:
        """基于 Phase 名称和功能列表推断 scope_type。
        返回: backend / frontend / fullstack
        """
        text = (name + " " + " ".join(features)).lower()
        backend_kw = {"后端", "api", "数据库", "backend", "server", "database", "db", "模型", "model",
                      "路由", "route", "接口", "endpoint", "orm", "migration", "鉴权", "auth"}
        frontend_kw = {"前端", "frontend", "页面", "ui", "界面", "组件", "component",
                       "样式", "css", "布局", "layout", "交互", "react", "vue", "模板", "template"}
        has_backend = any(kw in text for kw in backend_kw)
        has_frontend = any(kw in text for kw in frontend_kw)
        if has_backend and has_frontend:
            return "fullstack"
        if has_backend:
            return "backend"
        if has_frontend:
            return "frontend"
        return "fullstack"  # 无法判断时不做过滤

    def _extract_phases(self, plan_md: str) -> list:
        """从 plan.md 中解析 Phase 结构。支持英文标准格式 + 中文变体兜底。"""
        import re as _re
        phases = []

        # 主正则：匹配标准 `### Phase N: xxx` 格式
        pattern = r'### Phase (\d+):\s*(.+?)(?:\n|$)([\s\S]*?)(?=### Phase|\Z)'
        matches = list(_re.finditer(pattern, plan_md))

        if matches:
            # 标准解析路径
            for m in matches:
                phase_body = m.group(3).strip()
                features = [
                    line.strip('- ').strip()
                    for line in phase_body.split('\n')
                    if line.strip().startswith('- ')
                ]
                name = m.group(2).strip().rstrip('—').strip()
                scope_type = self._infer_phase_scope(name, features)
                phases.append({
                    "index": int(m.group(1)),
                    "name": name,
                    "features": features,
                    "scope_type": scope_type,
                    "status": "pending",
                })
            if phases:
                logger.info(f"Phase 解析: 共 {len(phases)} 个阶段 — "
                             + ", ".join(f"P{p['index']}:{p['name']}[{p['scope_type']}]({len(p['features'])})" for p in phases))
            return phases

        # ═══ 兜底：中文变体（第N步/阶段N/步骤N 等）═══
        # LLM 可能忽略 prompt 的格式约束，输出中文编号标题
        _CN_NUM = {'一': 1, '二': 2, '三': 3, '四': 4, '五': 5,
                   '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
        cn_pattern = (
            r'###\s*'
            r'(?:第([一二三四五六七八九十\d]+)(?:步|阶段|期)|'   # 第一步/第1步/第一阶段/第1阶段
            r'阶段([一二三四五六七八九十\d]+)|'              # 阶段一/阶段1
            r'步骤(\d+))'                                    # 步骤1
            r'[：:]\s*(.+?)(?:\n|$)'
            r'([\s\S]*?)(?=###\s*(?:第|阶段|步骤)|\Z)'
        )
        for m in _re.finditer(cn_pattern, plan_md):
            raw_idx = m.group(1) or m.group(2) or m.group(3) or "0"
            idx = _CN_NUM.get(raw_idx, None)
            if idx is None:
                try:
                    idx = int(raw_idx)
                except ValueError:
                    idx = len(phases) + 1
            name = m.group(4).strip().rstrip('—').strip()
            body = m.group(5).strip()
            features = [
                line.strip('- ').strip()
                for line in body.split('\n')
                if line.strip().startswith('- ')
            ]
            scope_type = self._infer_phase_scope(name, features)
            phases.append({
                "index": idx,
                "name": name,
                "features": features,
                "scope_type": scope_type,
                "status": "pending",
            })
        if phases:
            logger.warning(
                "Phase 解析（中文兜底）: LLM 未遵守 Phase 格式约束，已降级解析。"
                " 共 %d 个阶段 — %s",
                len(phases),
                ", ".join(f"P{p['index']}:{p['name']}[{p['scope_type']}]({len(p['features'])})" for p in phases),
            )
        return phases

    def _build_phase_plan(self, full_plan: str, phase_index: int) -> str:
        """从完整 plan 中提取指定 Phase 的子集 plan。
        按 Phase 范围裁剪技术栈和功能，注入显式范围约束。"""
        if phase_index >= len(self.project_phases):
            return full_plan

        phase = self.project_phases[phase_index]
        features = phase["features"]
        scope_type = phase.get("scope_type", "fullstack")

        # 提取技术栈章节（## 技术栈 到下一个 ## 之间）
        import re as _re
        tech_match = _re.search(r'(## 技术栈[\s\S]*?)(?=\n## |\Z)', full_plan)
        tech_section = tech_match.group(1).strip() if tech_match else ""

        # 提取设计风格章节（如有）
        design_match = _re.search(r'(## 设计风格[\s\S]*?)(?=\n## |\Z)', full_plan)
        design_section = design_match.group(1).strip() if design_match else ""

        # 构建 Phase 子集 plan
        title_match = _re.match(r'#\s+(.+)', full_plan)
        title = title_match.group(1) if title_match else "项目"

        phase_plan_parts = [f"# {title} — Phase {phase['index']}: {phase['name']}"]
        phase_plan_parts.append(f"\n**阶段范围**: {scope_type}")

        # 按 scope_type 裁剪技术栈：只保留属于本阶段的技术
        if tech_section and scope_type != "fullstack":
            filtered_tech_lines = []
            frontend_keywords = {"vue", "react", "angular", "tailwind", "css", "sass", "scss",
                                 "webpack", "vite", "next", "nuxt", "svelte", "前端", "frontend",
                                 "typescript", "jsx", "tsx", "element", "antd", "bootstrap"}
            backend_keywords = {"python", "flask", "fastapi", "django", "sqlite", "postgres",
                                "mysql", "redis", "sqlalchemy", "后端", "backend", "uvicorn",
                                "gunicorn", "celery", "数据库", "database", "api"}
            for line in tech_section.split("\n"):
                line_lower = line.lower()
                # 章节标题行总是保留
                if line.strip().startswith("## "):
                    filtered_tech_lines.append(line)
                    continue
                is_frontend = any(kw in line_lower for kw in frontend_keywords)
                is_backend = any(kw in line_lower for kw in backend_keywords)
                if scope_type == "backend" and is_frontend and not is_backend:
                    filtered_tech_lines.append(f"{line.rstrip()}（⏳ 将在后续前端阶段实现）")
                elif scope_type == "frontend" and is_backend and not is_frontend:
                    filtered_tech_lines.append(f"{line.rstrip()}（✅ 已在前序后端阶段完成）")
                else:
                    filtered_tech_lines.append(line)
            phase_plan_parts.append("\n" + "\n".join(filtered_tech_lines))
        elif tech_section:
            phase_plan_parts.append(f"\n{tech_section}")

        # 注入 Phase 范围约束
        scope_desc = {
            "backend": "本阶段只实现后端代码（Python 服务、数据库模型、API 路由、业务逻辑）。前端页面、样式、JavaScript 将在后续阶段实现。",
            "frontend": "本阶段只实现前端代码（页面、组件、样式、交互逻辑）。后端 API 已在前序阶段完成，直接调用即可。",
            "fullstack": "本阶段同时涉及前后端代码。",
        }
        phase_plan_parts.append(
            f"\n## ⚠️ Phase 范围约束\n"
            f"{scope_desc.get(scope_type, scope_desc['fullstack'])}\n"
            f"严禁规划或生成不属于本阶段范围的文件。"
        )

        phase_plan_parts.append("\n## 核心功能")
        for i, feat in enumerate(features, 1):
            phase_plan_parts.append(f"{i}. {feat}")
        if design_section:
            phase_plan_parts.append(f"\n{design_section}")

        # 注入上下文参考：告知后续/已完成阶段的技术栈（只读，不执行）
        other_phases = [p for p in self.project_phases if p["index"] != phase["index"]]
        if other_phases:
            ctx_lines = ["\n## 📋 项目全景参考（只读，不要为以下阶段创建文件）"]
            for p in other_phases:
                status = "✅ 已完成" if p["status"] == "done" else "⏳ 待实施"
                p_scope = p.get("scope_type", "fullstack")
                feat_preview = "、".join(p["features"][:3])
                if len(p["features"]) > 3:
                    feat_preview += f" 等{len(p['features'])}项"
                ctx_lines.append(f"- Phase {p['index']}: {p['name']}（{status}, {p_scope}）— {feat_preview}")

            # 针对 scope_type 生成设计约束提示
            if scope_type == "backend":
                future_frontend = [p for p in other_phases if p.get("scope_type") in ("frontend", "fullstack") and p["status"] != "done"]
                if future_frontend:
                    fe_techs = []
                    for p in future_frontend:
                        for feat in p["features"]:
                            fl = feat.lower()
                            if any(kw in fl for kw in ("vue", "react", "angular", "svelte")):
                                fe_techs.append(feat.split("，")[0].split("、")[0].strip())
                    if fe_techs:
                        ctx_lines.append(f"\n**设计约束**: 后续阶段有前端（{', '.join(fe_techs[:3])}）会调用本阶段 API，请确保：")
                        ctx_lines.append("  1. API 返回标准 JSON 格式")
                        ctx_lines.append("  2. 配置 CORS 中间件（允许跨域）")
                        ctx_lines.append("  3. 不要创建 Jinja2/HTML 模板文件")
                    else:
                        ctx_lines.append("\n**设计约束**: 后续阶段有前端会消费本阶段 API，请确保 API 返回 JSON 并配置 CORS。")
            elif scope_type == "frontend":
                done_backend = [p for p in other_phases if p.get("scope_type") == "backend" and p["status"] == "done"]
                if done_backend:
                    ctx_lines.append(f"\n**接口参考**: 后端 API 已在 {', '.join(p['name'] for p in done_backend)} 中实现，直接调用即可。")

            phase_plan_parts.extend(ctx_lines)

        return "\n".join(phase_plan_parts)

    def _build_phase_status_text(self) -> str:
        """构建 Phase 进度摘要，注入路由器 system prompt。
        解决 PM 在 Phase 续期时失忆的问题。"""
        if not self.project_phases:
            return ""

        # 项目标题
        title = self._extract_plan_title(self._full_plan_md) if self._full_plan_md else "当前项目"

        parts = [f"项目: {title}"]
        parts.append(f"共 {len(self.project_phases)} 个阶段:")

        for phase in self.project_phases:
            status_icon = {
                "done": "✅",
                "executing": "🔄",
                "failed": "❌",
                "pending": "⏳",
            }.get(phase["status"], "⏳")
            features_preview = ", ".join(phase["features"][:3])
            if len(phase["features"]) > 3:
                features_preview += f" 等{len(phase['features'])}项"
            parts.append(
                f"  {status_icon} Phase {phase['index']}: {phase['name']} [{phase['status']}] — {features_preview}"
            )

        # 当前阶段提示
        current = self.current_phase_index + 1
        next_phase = next((p for p in self.project_phases if p["status"] == "pending"), None)
        if next_phase:
            parts.append(f"\n下一待执行阶段: Phase {next_phase['index']}「{next_phase['name']}」")
            if self.pending_extend:
                parts.append("⚠️ 该阶段已预装为待确认扩展方案，用户说「继续」即可启动")

        return "\n".join(parts)

    def _standardize_requirement(self, user_message: str) -> dict:
        """
        将用户自然语言需求翻译为 structured_req JSON。
        LLM 只做翻译，不做创造。
        """
        # 构建对话历史上下文（使用完整窗口，不再截断）
        history_text = ""
        if self.conversation:
            history_text = "\n".join(
                f"{'用户' if m['role'] == 'user' else 'PM'}: {m['content']}"
                for m in self.conversation
            )

        user_prompt = f"对话历史：\n{history_text}\n\n当前用户需求：\n{user_message}"

        try:
            response = self._chat_completion(
                model=self.model,
                messages=[
                    {"role": "system", "content": Prompts.PM_STANDARDIZE_REQUIREMENT},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.1,
            )
            json_str = response.content.strip()
            # 清理 Markdown 代码块
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0].strip()
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0].strip()

            req = json.loads(json_str)
            logger.info(f"需求标准化完成: {req.get('summary', '未知')}")
            return req

        except (json.JSONDecodeError, Exception) as e:
            logger.error(f"需求标准化失败: {e}")
            # 降级：用简化 prompt 重试一次 LLM 提取
            try:
                fallback_resp = self._chat_completion(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": (
                            "从用户消息中提取需求摘要。返回纯 JSON（无 markdown 包裹）：\n"
                            '{"summary": "一句话摘要", "core_features": ["功能1", "功能2"], '
                            '"tech_preferences": {}, "defaults_applied": [], '
                            '"implied_requirements": []}'
                        )},
                        {"role": "user", "content": user_message},
                    ],
                    temperature=0.0,
                )
                fallback_json = fallback_resp.content.strip()
                if "```" in fallback_json:
                    fallback_json = fallback_json.split("```")[1].split("```")[0].strip()
                    if fallback_json.startswith("json"):
                        fallback_json = fallback_json[4:].strip()
                req = json.loads(fallback_json)
                logger.info(f"需求标准化降级成功: {req.get('summary', '未知')}")
                return req
            except Exception as retry_err:
                logger.error(f"需求标准化降级也失败: {retry_err}")
                # 最终兜底：仅传递原始文本，不做任何假设
                first_line = user_message.split('\n')[0].strip()[:100]
                return {
                    "summary": first_line,
                    "core_features": [first_line],
                    "implied_requirements": [],
                    "tech_preferences": {},
                    "defaults_applied": [],
                }

    # ============================================================
    # 意图处理器
    # ============================================================

    def _handle_test(self, args: dict) -> PMResponse:
        """用户主动要求测试当前项目 — 直接调 QA Agent，不修改代码。"""
        if not self._project_exists():
            return PMResponse(intent="chat", reply="当前项目还没有代码，无法执行测试。请先创建项目。")

        test_scope = args.get("test_scope", "全量测试")
        project_dir = self._get_project_dir()

        global_broadcaster.emit_sync("PM", "info", f"🧪 用户请求测试: {test_scope}")

        # 收集项目代码
        all_code = {}
        ignore_dirs = {'.sandbox', '.git', '__pycache__', '.venv', 'node_modules', '.idea', '.astrea'}
        for root, dirs, files in os.walk(project_dir):
            dirs[:] = [d for d in dirs if d not in ignore_dirs]
            for fname in files:
                if fname.startswith('.'):
                    continue
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, project_dir).replace("\\", "/")
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        all_code[rel] = f.read()
                except Exception:
                    pass

        if not all_code:
            return PMResponse(intent="chat", reply="项目目录中没有找到可测试的文件。")

        # 启动 QA
        try:
            from agents.qa_agent import QAAgent
            from tools.sandbox import sandbox_env

            venv_python = ""
            try:
                venv_python = sandbox_env.venv_manager.get_or_create_venv(self.project_id)
            except Exception:
                pass

            qa = QAAgent(self.project_id)
            result = qa.run_qa(
                project_spec="",
                all_code=all_code,
                sandbox_dir=project_dir,
                venv_python=venv_python,
            )

            # 构造回复
            passed = result.get("passed", False)
            feedback = result.get("feedback", "")
            endpoint_results = result.get("endpoint_results", [])

            if endpoint_results:
                ok_count = sum(1 for ep in endpoint_results if ep.get("ok"))
                total = len(endpoint_results)
                ep_lines = []
                for ep in endpoint_results:
                    icon = "✅" if ep.get("ok") else "❌"
                    detail = f" — {ep.get('detail', '')}" if ep.get("detail") else ""
                    ep_lines.append(f"{icon} {ep.get('method', '?')} {ep.get('url', '?')} → {ep.get('status_code', '?')}{detail}")
                ep_summary = "\n".join(ep_lines)
                status = "全部通过" if passed else f"{ok_count}/{total} 通过"
                reply = f"🧪 测试完成 — {status}\n\n{ep_summary}"
                if feedback and not passed:
                    reply += f"\n\n📋 {feedback}"
            else:
                status = "✅ 通过" if passed else "❌ 失败"
                reply = f"🧪 测试完成 — {status}\n\n{feedback}"

            if not passed:
                reply += "\n\n如果需要修复这些问题，请告诉我。"

            return PMResponse(intent="test", reply=reply)

        except Exception as e:
            logger.error(f"QA 测试执行失败: {e}")
            return PMResponse(intent="chat", reply=f"测试执行失败: {str(e)}")

    def _handle_archive_search(self, args: dict) -> PMResponse:
        """档案检索处理 — 由路由器 LLM 判定后触发"""
        query = args.get("query", "")
        if not query:
            return PMResponse(intent="chat", reply="请告诉我你想查找什么内容？")

        archive_result = self._search_archive(query)
        if archive_result:
            return PMResponse(intent="archive", reply=archive_result)
        return PMResponse(intent="chat", reply="没有找到相关的历史对话记录。")

    def _handle_chat(self, message: str) -> PMResponse:
        """闲聊处理 — 高情商 PM 直接回复"""

        context_needs = self._last_route.get("context_needs", []) if self._last_route else []
        project_context = self._build_project_context(context_needs)

        # Fix D: 在 wait_confirm 阶段注入完整 plan 作为锚定上下文（不截断）
        if self.state == "wait_confirm" and self.pending_plan_md:
            project_context += f"\n\n【当前待确认方案（完整内容）】\n{self.pending_plan_md}"

        # Phase 2: 注入决策备忘录（Letta-Lite Core Memory）
        memo_text = self._build_memo_text()
        if memo_text:
            project_context += f"\n\n【决策备忘录】\n{memo_text}"

        route_hint = ""  # chat 模式无特殊 hint
        system_prompt = Prompts.PM_SYSTEM.format(
            project_context=project_context,
            route_hint=route_hint,
        )

        try:
            messages = [{"role": "system", "content": system_prompt}]
            messages.extend(self.conversation[-6:])

            response = self._chat_completion(
                model=self.model,
                messages=messages,
                temperature=0.7,
            )
            return PMResponse(intent="chat", reply=response.content.strip())
        except Exception as e:
            logger.error(f"PM 闲聊失败: {e}")
            return PMResponse(intent="chat", reply="抱歉，我暂时无法回复，请稍后再试。")

    def _handle_create(self, message: str) -> PMResponse:
        """
        创建处理（v2 升级版）：
        PM 直接从完整对话上下文生成 plan.md，不再经过 structured_req JSON 中转。
        v2: 分隔符协议检测 + 条件按钮 + 版本管理。
        """
        global_broadcaster.emit_sync("PM", "info", "正在分析您的需求...")

        # 1. 直接从对话生成 plan.md
        raw_plan = self._generate_plan()

        # 2. 分隔符协议：提取 PM 的追问（Fix B）
        pm_questions = ""
        if "===PM_QUESTIONS===" in raw_plan:
            plan_md, pm_questions = raw_plan.split("===PM_QUESTIONS===", 1)
            plan_md = plan_md.strip()
            pm_questions = pm_questions.strip()
        else:
            plan_md = raw_plan

        # 3. 版本化保存（Fix C）
        project_dir = self._get_project_dir()
        version = self._save_plan_to_disk(plan_md, project_dir)
        self._plan_version = version

        # 4. v4.0: Phase 解析
        phases = self._extract_phases(plan_md)
        if phases:
            self.project_phases = phases
            self.current_phase_index = 0
            self._full_plan_md = plan_md  # 保留完整 plan
            # 仅将 Phase 1 作为待确认范围
            phase_plan = self._build_phase_plan(plan_md, 0)
            self.pending_plan_md = phase_plan
            self.pending_mode = "create"
        else:
            self.pending_plan_md = plan_md
            self.pending_mode = "create"

        self._plan_created_at_round = self.round_id

        # 5. v5.1: LLM 基于 plan.md 事实生成自然回复（替代硬编码拼接）
        reply = self._generate_plan_summary_reply(plan_md, pm_questions, version)

        global_broadcaster.emit_sync("PM", "plan_preview", "技术方案预览已就绪")

        # v4.0: 不再输出按钮，PM 通过引导式回复等待用户自然语言确认
        return PMResponse(
            intent="create",
            reply=reply,
            plan_md=plan_md,
        )

    def _generate_plan_summary_reply(
        self, plan_md: str, pm_questions: str, version: int
    ) -> str:
        """
        v5.1: 让 LLM 基于 plan.md 事实生成自然的方案确认回复。
        替代旧的硬编码 reply_parts 拼接。
        """
        # 构建上下文：plan + 追问 + 版本 + 近几轮对话
        user_input = f"【技术方案 v{version}】\n{plan_md}"
        if pm_questions:
            user_input += f"\n\n===PM_QUESTIONS===\n{pm_questions}"

        # 带上近几轮对话供 LLM 理解语境
        recent_conv = "\n".join(
            f"{'用户' if m['role'] == 'user' else 'PM'}: {m['content']}"
            for m in self.conversation[-4:]
        )
        if recent_conv:
            user_input += f"\n\n【近期对话】\n{recent_conv}"

        try:
            resp = self._chat_completion(
                model=self.model,
                messages=[
                    {"role": "system", "content": Prompts.PM_PLAN_SUMMARY},
                    {"role": "user", "content": user_input},
                ],
                temperature=0.5,
            )
            reply = resp.content.strip() if resp.content else ""
            if reply:
                return reply
        except Exception as e:
            logger.warning(f"PM 方案回复生成失败: {e}，使用降级回复")

        # 降级：极简回复
        title = self._extract_plan_title(plan_md)
        return f"「{title}」的方案（v{version}）已生成，您看看有没有要调整的。"

    def _handle_patch(self, message: str) -> PMResponse:
        """
        修改处理（v5.0 统一调查版）：
        - PM 委托 TechLead 做白盒调查（替代旧的 read_file 循环）
        - TechLead 的结构化 verdict 翻译为用户确认文本
        - diagnosis 缓存供 Engine 复用，避免二次调查
        """
        self.pending_patch = message
        self.state = "wait_patch_confirm"

        if self._project_exists():
            project_dir = self._get_project_dir()

            # === TechLead 委托调查（替代旧的 PM read_file 循环）===
            diagnosis = None
            try:
                from agents.tech_lead import TechLeadAgent
                from core.techlead_scope import resolve_target_scope

                task_context = (
                    f"【用户修改请求】\n{message}\n\n"
                    "请逐个读取可能受影响的文件，定位 bug 的根因和需要修改的具体行号。\n"
                    "重点关注：函数签名是否匹配、模板变量是否存在、路由路径是否正确。"
                )

                # 定向范围：从用户消息中提取，避免 TechLead 全项目盲扫超时
                pm_scope = resolve_target_scope(project_dir, message)
                if not pm_scope.is_resolved():
                    pm_scope = None  # 解析失败则不传，保持旧逻辑

                logger.info("🔍 [PM] 委托 TechLead 调查用户报告的问题...")
                global_broadcaster.emit_sync(
                    "TechLead", "patch_investigate_start",
                    "🔍 TechLead 正在白盒调查修改范围..."
                )
                tech_lead = TechLeadAgent()
                diagnosis = tech_lead.investigate(
                    project_dir=project_dir,
                    task_context=task_context,
                    target_scope=pm_scope,
                    # max_steps 不传，由 investigate() 内部按 scope_size 动态计算
                )
                if diagnosis:
                    confidence = diagnosis.get("confidence", 0.0)
                    logger.info(
                        "✅ [PM] TechLead 调查完成 (confidence=%.2f): %s",
                        confidence,
                        diagnosis.get("root_cause", "")[:120],
                    )
                else:
                    logger.warning("⚠️ [PM] TechLead 调查未产出判定")
            except Exception as e:
                logger.warning(f"⚠️ [PM] TechLead 调查异常: {e}")

            # 缓存 diagnosis 供 Engine 复用
            self._cached_tech_lead_diagnosis = diagnosis

            # === 将 TechLead verdict 翻译为用户友好的确认回复 ===
            if diagnosis and diagnosis.get("root_cause"):
                root_cause = diagnosis.get("root_cause", "")
                fix_instruction = diagnosis.get("fix_instruction", "")
                guilty_file = diagnosis.get("guilty_file", "")
                confidence = diagnosis.get("confidence", 0.0)

                # 基于骨架 + TechLead 结果生成用户确认文本
                reply_parts = ["我已经定位到了问题所在：\n"]
                if guilty_file:
                    reply_parts.append(f"📍 **问题文件**: `{guilty_file}`")
                reply_parts.append(f"🔍 **根因分析**: {root_cause}")
                if fix_instruction:
                    reply_parts.append(f"🔧 **修复方向**: {fix_instruction}")
                reply_parts.append(f"\n置信度: {confidence:.0%}。确认后我将立即修复。")
                reply = "\n".join(reply_parts)

                # manager_brief 用 TechLead 的结构化输出（比 PM 的猜测更精确）
                self._last_manager_brief = (
                    f"root_cause: {root_cause}\n"
                    f"guilty_file: {guilty_file}\n"
                    f"fix_instruction: {fix_instruction}\n"
                    f"confidence: {confidence}"
                )
            else:
                # TechLead 调查失败，降级为骨架推理
                context_needs = ["file_tree", "skeleton", "git_log"]
                project_context = self._build_project_context(context_needs)

                route_hint = (
                    "【当前场景：用户想修改已有项目】\n"
                    "基于骨架信息做粗粒度影响分析即可，确认后会有专业工程师深入调查。\n"
                    "回复简洁友好，告知用户点击确认按钮。"
                )
                system_prompt = Prompts.PM_SYSTEM.format(
                    project_context=project_context,
                    route_hint=route_hint,
                )
                messages = [{"role": "system", "content": system_prompt}]
                messages.extend(self.conversation[-6:])
                try:
                    resp = self._chat_completion(
                        model=self.model,
                        messages=messages,
                        temperature=0.5,
                    )
                    reply = resp.content.strip() if resp.content else "明白，我来帮您处理这个修改。"
                except Exception:
                    reply = "明白，我来帮您处理这个修改。"
                self._last_manager_brief = None

            return PMResponse(
                intent="patch",
                reply=reply,
                actions=[
                    {"id": "patch_confirm", "label": "✅ 确认修改", "style": "primary"},
                    {"id": "patch_cancel", "label": "取消", "style": "default"},
                ],
            )
        else:
            # 项目不存在，引导用户走 create
            self.state = "idle"
            self.pending_patch = None
            return PMResponse(
                intent="patch",
                reply="目前这个项目还没有文件呢，是不是想先创建一个新项目？直接告诉我您想做什么就行。",
            )

    def _handle_extend(self, message: str) -> PMResponse:
        """新增模块处理：先扫描老项目上下文，再进入二次确认。"""
        if not self._project_exists():
            self.state = "idle"
            self.pending_extend = None
            return PMResponse(
                intent="extend",
                reply="当前项目还不存在，不能直接新增模块。请先创建项目，或切换到一个已有项目。",
            )

        project_dir = self._get_project_dir()
        try:
            existing_context = scan_existing_project(project_dir)
        except Exception as e:
            logger.warning(f"Extend 上下文扫描失败: {e}")
            existing_context = {}

        file_count = len(existing_context.get("file_tree") or [])
        route_count = len(existing_context.get("existing_routes") or [])
        model_count = len(existing_context.get("existing_models") or [])
        tech_stack = ", ".join(existing_context.get("tech_stack") or []) or "未识别"
        entrypoint = (
            existing_context.get("architecture_contract", {}).get("entrypoint_file")
            or "未识别"
        )
        route_preview = []
        for item in (existing_context.get("existing_routes") or [])[:3]:
            method = str(item.get("method") or "?").upper()
            path = str(item.get("path") or "?")
            route_preview.append(f"{method} {path}")
        route_hint = f"已有路由示例：{'；'.join(route_preview)}。" if route_preview else "当前未识别到明确路由。"

        self.pending_extend = message
        self.pending_mode = "extend"
        self.state = "wait_confirm"

        return PMResponse(
            intent="extend",
            reply=(
                f"检测到当前项目已有 {file_count} 个文件、{route_count} 个路由、{model_count} 个模型。"
                f"技术栈：{tech_stack}；入口文件：{entrypoint}。"
                f"{route_hint}我会基于现有架构规划新增模块，并把老文件修改限制为焊接式局部编辑。是否开始？"
            ),
            actions=[
                {"id": "confirm", "label": "✅ 开始", "style": "primary"},
                {"id": "cancel", "label": "取消", "style": "default"},
            ],
        )

    def _handle_continue(self, message: str) -> PMResponse:
        """确认是否基于上一轮 QA 失败上下文继续修复。"""
        if not self._project_exists():
            return PMResponse(
                intent="continue",
                reply="当前项目不存在，无法继续修复。请先创建项目或切换到已有项目。",
            )

        project_dir = self._get_project_dir()
        state = BlackboardState.load_from_disk(project_dir)
        failure_context = state.failure_context if state else {}
        endpoint_results = failure_context.get("endpoint_results") or []
        failed_endpoints = [
            ep for ep in endpoint_results
            if isinstance(ep, dict) and not ep.get("ok")
        ]

        if not endpoint_results or not failed_endpoints:
            return PMResponse(
                intent="continue",
                reply="没有可用的上一轮 QA 失败端点上下文，不能进入继续修复。请先运行一次项目验证，或明确说明要修改的功能。",
            )

        repair_scope = failure_context.get("repair_scope") or failure_context.get("failed_files") or []
        if isinstance(repair_scope, str):
            repair_scope = [repair_scope]
        if not repair_scope:
            return PMResponse(
                intent="continue",
                reply="上一轮失败上下文缺少 repair_scope/failed_files，无法安全限定修复范围。请明确要修复的文件或功能。",
            )

        endpoint_lines = []
        for ep in failed_endpoints[:5]:
            endpoint_lines.append(
                f"{ep.get('method', '?')} {ep.get('url', '?')} -> {ep.get('status_code', '?')}"
            )
        endpoint_text = "；".join(endpoint_lines)
        scope_text = ", ".join(str(x) for x in repair_scope)

        self.pending_continue = message
        self.pending_mode = "continue"
        self.state = "wait_confirm"

        return PMResponse(
            intent="continue",
            reply=(
                f"检测到上一轮 QA 失败端点 {len(failed_endpoints)} 个：{endpoint_text}。"
                f"本次继续修复将严格限制在这些文件内：{scope_text}。是否开始？"
            ),
            actions=[
                {"id": "confirm", "label": "✅ 开始", "style": "primary"},
                {"id": "cancel", "label": "取消", "style": "default"},
            ],
        )

    def _handle_continue_execute(self) -> PMResponse:
        """用户确认继续修复后，启动 Engine continue mode。"""
        if not self.pending_continue:
            return PMResponse(intent="action", reply="当前没有需要继续修复的内容，请先发起一次构建或修改。")

        self.confirmed_requirement = self.pending_continue
        self.confirmed_mode = "continue"
        continue_desc = self.pending_continue[:60]
        self.pending_continue = None
        self.state = "idle"

        global_broadcaster.emit_sync("PM", "info", "用户已确认继续修复，正在启动 Continue 模式...")

        reply = self._generate_reply(
            f"用户确认继续修复。上一轮 QA 失败的上下文：{continue_desc}。"
            f"操作类型：基于上次失败结果继续修复。",
            fallback=f"收到，正在继续修复「{continue_desc}」，请留意面板进度。",
        )
        return PMResponse(intent="action", reply=reply, is_executing=True)

    def _handle_extend_execute(self) -> PMResponse:
        """用户确认新增模块后，启动 Engine extend mode。"""
        if not self.pending_extend:
            return PMResponse(intent="action", reply="当前没有待确认的新增模块，请先描述您想添加的功能。")

        self.confirmed_requirement = self.pending_extend
        self.confirmed_mode = "extend"
        # v5.1: 提取 plan 标题作为描述，避免截取 raw Markdown 泄漏源码
        extend_desc = self._extract_plan_title(self.pending_extend)
        self.pending_extend = None
        self.state = "idle"

        global_broadcaster.emit_sync("PM", "info", "用户已确认新增模块，正在启动 Extend 模式...")

        reply = self._generate_reply(
            f"用户确认新增模块。模块描述：{extend_desc}。"
            f"操作类型：在现有项目上新增功能模块。",
            fallback=f"收到，正在为「{extend_desc}」启动开发，请关注面板进度。",
        )
        return PMResponse(intent="action", reply=reply, is_executing=True)

    def _parse_dual_output(self, content: str) -> tuple:
        """解析 PM 的双输出：用户回复 + Manager Brief"""
        if "---" in content:
            parts = content.split("---", 1)
            reply = parts[0].strip()
            brief = parts[1].strip()
            return reply, brief
        return content, ""

    def _handle_patch_execute(self, confirm_message: str = "") -> PMResponse:
        """
        用户确认修改 → 启动 Engine patch mode。
        v5.0: 将 PM 阶段缓存的 TechLead diagnosis 传给 Engine，跳过二次调查。
        v5.2: 接收 confirm_message 参数，追加用户在确认时的补充/修正指令。
        """
        if not self.pending_patch:
            return PMResponse(intent="action", reply="当前没有待执行的修改，请先告诉我您想改什么。")

        # v5.2: 用户确认时的补充指令（如 "改为5001"）覆盖诊断中的具体参数
        user_amendment = ""
        if confirm_message and confirm_message.strip() != self.pending_patch.strip():
            user_amendment = confirm_message.strip()

        # v5.0: 优先使用 TechLead 的结构化 diagnosis 作为影响分析
        pm_analysis = ""
        cached_diagnosis = getattr(self, '_cached_tech_lead_diagnosis', None)
        manager_brief = getattr(self, '_last_manager_brief', '') or ''

        if manager_brief:
            pm_analysis = manager_brief
        elif not cached_diagnosis:
            # Fallback: 从对话窗口中提取 PM 最后一次非空回复作为分析
            for msg in reversed(self.conversation):
                if msg["role"] == "assistant" and len(msg["content"]) > 50:
                    pm_analysis = msg["content"]
                    break

        parts = [f"【用户需求】\n{self.pending_patch}"]
        if user_amendment:
            parts.append(f"【用户补充指令（优先级最高，必须覆盖诊断中的对应参数）】\n{user_amendment}")
        if pm_analysis:
            parts.append(f"【TechLead 诊断（已验证，包含精确的修改位置和方向）】\n{pm_analysis}")

        # v5.3: 注入用户偏好 memo → 让 Coder 感知已确认的风格/技术栈约束
        memo_constraints = []
        if self._memo.get("user_prefs"):
            memo_constraints.append(f"用户偏好: {self._memo['user_prefs']}")
        if self._memo.get("design"):
            memo_constraints.append(f"设计风格: {self._memo['design']}")
        if self._memo.get("tech_stack"):
            memo_constraints.append(f"技术栈: {self._memo['tech_stack']}")
        if memo_constraints:
            parts.append(f"【用户偏好约束（必须遵守）】\n" + "\n".join(memo_constraints))

        self.confirmed_requirement = "\n\n".join(parts)

        self.confirmed_mode = "patch"

        patch_desc = self.pending_patch[:60]
        self.pending_patch = None
        self._last_manager_brief = None
        self.state = "idle"

        global_broadcaster.emit_sync("PM", "info", f"用户已确认修改，正在启动 Patch 模式...")

        reply = self._generate_reply(
            f"用户确认修改。修改内容：{patch_desc}。"
            f"操作类型：对现有项目做局部修改（Patch 模式）。",
            fallback=f"收到，正在修改「{patch_desc}」，请关注面板进度。",
        )
        return PMResponse(intent="action", reply=reply, is_executing=True)

    def _handle_rollback(self, message: str) -> PMResponse:
        """
        回滚处理 — 查询 git log 定位 commit，请用户确认。
        本阶段降级实现：从 git log 中搜索关键词。
        """
        if not self._project_exists():
            return PMResponse(
                intent="rollback",
                reply="这个项目还没有文件，没有可以回滚的记录。",
            )

        # 查询 git log
        commits = self._query_git_log(message)
        if commits:
            # 找到匹配的 commit
            commit = commits[0]
            self.state = "wait_rollback_confirm"
            
            # 判断是否带有 Round 标识
            import re
            m = re.search(r"\[Round (\d+)\]", commit["message"])
            if m:
                round_id = m.group(1)
                self.pending_rollback_target = f"Round: {round_id}"
                reply = f"找到了该次修改所在的批次 (Round {round_id})。我们将撤销该批次所有的修改。\n\n最早的一笔任务是：\n> {commit['message']} ({commit['date']})\n\n要彻底恢复到这批次修改之前的状态吗？"
            else:
                self.pending_rollback_target = f"Commit: {commit['hash']}"
                reply = f"找到了一条相关的修改记录：\n\n> {commit['message']} ({commit['date']})\n\n要恢复到这次修改之前的状态吗？"

            return PMResponse(
                intent="rollback",
                reply=reply,
                actions=[
                    {"id": "rollback_confirm", "label": "确认回滚", "style": "danger"},
                    {"id": "rollback_cancel", "label": "取消", "style": "secondary"},
                ],
            )
        else:
            self.state = "idle"
            return PMResponse(
                intent="rollback",
                reply="没有找到相关的修改记录。您能告诉我更多细节吗？比如大概是什么时候改的、改了什么。",
            )

    def _handle_rollback_execute(self) -> PMResponse:
        """执行回滚"""
        self.state = "idle"
        target = getattr(self, "pending_rollback_target", None)
        self.pending_rollback_target = None
        self.pending_rollback_commit = None # 保留旧字段清空兼容

        if not target:
            return PMResponse(intent="action", reply="当前没有待回滚的记录，请先告诉我您想恢复哪次修改。")

        self.confirmed_requirement = f"Rollback {target}"
        self.confirmed_mode = "rollback"

        # 执行实际 git revert
        global_broadcaster.emit_sync("PM", "info", f"用户已确认回滚，正在启动 Rollback 模式...")
        
        reply = self._generate_reply(
            f"用户确认回滚。回滚目标：{target}。"
            f"操作类型：撤销近期修改批次，恢复到之前的状态。",
            fallback="收到，正在执行回滚，请留意面板进度。",
        )
        return PMResponse(intent="action", reply=reply, is_executing=True)

    def _handle_clarify(self, message: str) -> PMResponse:
        """[旧版兼容] 意图不明确时追问用户"""
        self.state = "wait_clarify"
        has_project = self._project_exists()
        context = (
            f"用户说了：「{message}」，但意图不明确。"
            f"{'当前已有项目，可能是想修改或新建。' if has_project else '当前没有项目，可能是想创建新项目。'}"
            f"需要追问用户具体想做什么。"
        )
        fallback = "您是想修改现有项目还是创建新项目？可以说得再具体些。" if has_project else "您是想创建一个新项目吗？告诉我您想做什么就行。"
        reply = self._generate_reply(context, fallback)
        return PMResponse(intent="clarify", reply=reply)

    def _handle_clarify_v2(self, args: dict) -> PMResponse:
        """Tool Calling 触发的澄清（策略 A 熔断 + 策略 C 引导式收束）"""
        self.state = "wait_clarify"
        question = args.get("question", "能告诉我更多细节吗？")
        options = args.get("options", [])

        if options:
            # 策略 C: 选项作为文本建议嵌入回复，用户自由回答
            opts_text = "\n".join([f"{i+1}. {opt}" for i, opt in enumerate(options)])
            reply = f"{question}\n\n{opts_text}\n\n您可以选择上述方向，也可以告诉我您自己的想法。"
        else:
            reply = question

        return PMResponse(intent="clarify", reply=reply)

    def _handle_plan_revision(self, message: str) -> PMResponse:
        """
        用户在 wait_confirm 状态下发送了修改意见 → 增量修订 plan（不再全量重写）。
        v2: 调用 _revise_plan 基于旧 plan 做增量编辑。
        """
        global_broadcaster.emit_sync("PM", "info", "正在根据您的反馈调整方案...")

        # 1. 增量修订（Fix A 核心）
        raw_plan = self._revise_plan(message)

        # 2. 分隔符协议（Fix B）
        pm_questions = ""
        if "===PM_QUESTIONS===" in raw_plan:
            plan_md, pm_questions = raw_plan.split("===PM_QUESTIONS===", 1)
            plan_md = plan_md.strip()
            pm_questions = pm_questions.strip()
        else:
            plan_md = raw_plan

        # 3. 版本化保存（Fix C）
        project_dir = self._get_project_dir()
        version = self._save_plan_to_disk(plan_md, project_dir)
        self._plan_version = version

        # 4. 更新待确认状态
        self.pending_plan_md = plan_md
        self._plan_created_at_round = self.round_id

        # 5. 用 LLM 生成有温度的确认回复
        try:
            resp = self._chat_completion(
                model=self.model,
                messages=[
                    {"role": "system", "content": "你是友好的项目经理。用一句话告知用户方案已根据反馈更新至新版本，请查看并确认。语气简洁自然，不要机械化。"},
                    {"role": "user", "content": f"用户反馈：{message[:100]}，当前版本：v{version}"},
                ],
                temperature=0.7,
            )
            reply = resp.content.strip()
        except Exception:
            reply = f"好的，方案已更新至第 {version} 版，我只改了您提到的部分，请在右侧面板查看~"

        # 6. 追问放在对话框
        if pm_questions:
            reply += f"\n\n另外还有个问题想确认：\n{pm_questions}"

        # 7. 条件按钮
        if pm_questions:
            actions = [
                {"id": "reject", "label": "我来回答", "style": "secondary"},
            ]
        else:
            actions = [
                {"id": "confirm", "label": "确认执行", "style": "primary"},
                {"id": "reject", "label": "继续调整", "style": "secondary"},
            ]

        return PMResponse(
            intent="create",
            reply=reply,
            plan_md=plan_md,
            actions=actions,
        )

    def _handle_scan(self, message: str) -> PMResponse:
        """扫描处理 — 调用已实现的逆向扫描"""
        try:
            from tools.project_scanner import ProjectScanner
            projects_dir = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                "projects"
            )
            project_dir = os.path.join(projects_dir, self.project_id)

            scanner = ProjectScanner()
            scan_result = scanner.scan(project_dir)

            if not scan_result.get("files_found"):
                return PMResponse(
                    intent="scan",
                    reply="这个项目目录下没有找到可分析的源代码文件。",
                )

            files_found = scan_result.get('files_found', 0)
            tech_stack = ', '.join(scan_result.get('tech_stack', ['未识别']))
            reply = f"扫描完成：发现 {files_found} 个文件，技术栈是 {tech_stack}。"

            if scan_result.get("entry_point"):
                ep = scan_result["entry_point"]
                reply += f"\n入口文件是 {ep.get('file', '未知')}，端口 {ep.get('port', '未知')}。"

            return PMResponse(intent="scan", reply=reply)
        except Exception as e:
            logger.error(f"扫描失败: {e}")
            return PMResponse(intent="scan", reply=f"扫描过程中出了点问题：{str(e)}")

    # ============================================================
    # 代码审查（TechLead A-1）
    # ============================================================

    def _handle_audit(self, message: str) -> PMResponse:
        """
        代码审查处理：TechLead 侦查 → PM 撰写报告。

        流程：
        1. 唤醒 TechLead ReAct 扫描项目 → 输出结构化发现列表 (JSON)
        2. PM 根据用户对话上下文 + 发现列表 → 撰写用户友好的 Markdown 报告
        3. 保存报告到项目目录 + 在对话中展示摘要
        """
        if not self._project_exists():
            return PMResponse(
                intent="audit",
                reply="这个项目还没有代码文件，没法审查。要不要先创建一个项目？",
            )

        project_dir = self._get_project_dir()
        target_scope = resolve_target_scope(project_dir, message)
        if not target_scope.is_resolved():
            return PMResponse(
                intent="audit",
                reply=target_scope.clarify_question,
            )

        global_broadcaster.emit_sync("PM", "info", "🔬 正在调度技术团队审查代码...")

        # Step 1: TechLead 侦查
        try:
            from agents.tech_lead import TechLeadAgent
            tech_lead = TechLeadAgent()
            findings = tech_lead.audit(project_dir, message, target_scope=target_scope)
        except Exception as e:
            logger.error(f"❗ TechLead 审查失败: {e}")
            return PMResponse(
                intent="audit",
                reply=f"审查过程中遇到了问题：{str(e)}。可以稍后再试。",
            )

        if not findings:
            report_md = self._write_audit_report(findings, message, target_scope)
            try:
                report_path = os.path.join(project_dir, "code_audit_report.md")
                with open(report_path, "w", encoding="utf-8") as f:
                    f.write(report_md)
                logger.info(f"审查报告已保存: {report_path}")
            except Exception as e:
                logger.warning(f"审查报告保存失败: {e}")
            return PMResponse(
                intent="audit",
                reply=(
                    "定向审查完成，在以下范围内没有发现明显问题："
                    f"{', '.join(target_scope.candidate_files)}。"
                    "已生成审查报告并保存至 code_audit_report.md。"
                ),
                plan_md=report_md,
            )

        # Step 2: PM 根据发现列表 + 对话上下文撰写报告
        report_md = self._write_audit_report(findings, message, target_scope)

        # Step 3: 保存报告
        try:
            report_path = os.path.join(project_dir, "code_audit_report.md")
            with open(report_path, "w", encoding="utf-8") as f:
                f.write(report_md)
            logger.info(f"审查报告已保存: {report_path}")
        except Exception as e:
            logger.warning(f"审查报告保存失败: {e}")

        # 摘要
        high_count = sum(1 for f in findings if f.get("severity") == "high")
        medium_count = sum(1 for f in findings if f.get("severity") == "medium")
        summary = f"审查完成！发现 {len(findings)} 个问题"
        if high_count:
            summary += f"（其中 {high_count} 个高危）"
        elif medium_count:
            summary += f"（其中 {medium_count} 个中危）"
        summary += (
            f"。本次为定向审查，范围：{', '.join(target_scope.candidate_files)}。"
            "报告已保存至 code_audit_report.md，详细内容在右侧方案面板中。"
        )

        return PMResponse(intent="audit", reply=summary, plan_md=report_md)

    # ============================================================
    # 确认/拒绝处理
    # ============================================================

    def _write_audit_report(self, findings: list, user_request: str,
                            target_scope: Optional[TargetScope] = None) -> str:
        """
        报告阶段直接基于已校验 findings 生成确定性 Markdown，避免二次幻觉。
        """
        scope_text = target_scope.summary_text() if target_scope else "未指定定向范围。"
        return render_audit_report_markdown(findings, user_request, scope_text)

    # ============================================================
    # v4.0: 执行回传环 + 引导式回复
    # ============================================================

    def on_execution_complete(self, success: bool, mode: str, round_id: str, summary: dict):
        """
        Engine 执行完成后的回调。
        写入执行账本 → 推进 Phase → LLM 生成引导性回复 → WebSocket 推送。
        """
        from datetime import datetime
        entry = {
            "round": round_id,
            "mode": mode,
            "success": success,
            "built_features": summary.get("completed_descriptions", []),
            "fused_tasks": summary.get("fused_tasks", []),
            "error_tasks": summary.get("error_tasks", []),
            "open_issues": summary.get("open_issues", []),
            "files_created": summary.get("files_created", 0),
            "files_modified": summary.get("files_modified", 0),
            "timestamp": datetime.now().isoformat(),
        }
        self.execution_ledger.append(entry)
        logger.info(f"📒 执行账本写入: R{round_id} {mode} success={success} "
                     f"done={len(entry['built_features'])} fused={len(entry['fused_tasks'])}")

        # Phase 推进
        self._advance_phase(entry)

        # LLM 生成引导性回复
        try:
            guided_reply = self._generate_post_execution_reply(entry)
        except Exception as e:
            logger.warning(f"引导回复生成失败: {e}")
            status = "成功" if success else "存在一些问题"
            guided_reply = f"本轮执行已完成（{status}）。告诉我接下来要做什么。"

        # 写入对话历史 + WebSocket 推送
        self.conversation.append({"role": "assistant", "content": guided_reply})
        global_broadcaster.emit_sync("PM", "execution_complete", guided_reply)

    def _generate_post_execution_reply(self, execution_entry: dict) -> str:
        """让 LLM 根据执行结果生成引导性回复（禁止硬编码模板）"""
        context_parts = []
        context_parts.append(f"执行模式: {execution_entry['mode']}, "
                             f"结果: {'成功' if execution_entry['success'] else '失败'}")

        if execution_entry["built_features"]:
            features = execution_entry["built_features"][:5]
            context_parts.append(f"本轮新增/修改的功能: {', '.join(features)}")

        if execution_entry["fused_tasks"]:
            context_parts.append(f"失败/跳过的任务: {len(execution_entry['fused_tasks'])} 个")

        if execution_entry["error_tasks"]:
            context_parts.append(f"出错任务: {len(execution_entry['error_tasks'])} 个")

        if execution_entry.get("open_issues"):
            issues = execution_entry["open_issues"]
            context_parts.append(f"待修复问题: {len(issues)} 个")
            for iss in issues[:3]:
                context_parts.append(f"  - [{iss['category']}] {iss['summary']}")

        context_parts.append(f"文件创建: {execution_entry.get('files_created', 0)}, "
                             f"文件修改: {execution_entry.get('files_modified', 0)}")

        # Phase 进度
        if self.project_phases:
            done = [p for p in self.project_phases if p["status"] == "done"]
            pending = [p for p in self.project_phases if p["status"] == "pending"]
            context_parts.append(f"Phase 进度: {len(done)}/{len(self.project_phases)} 完成")
            if pending:
                next_p = pending[0]
                context_parts.append(f"下一阶段: Phase {next_p['index']}「{next_p['name']}」"
                                     f"(功能: {', '.join(next_p['features'][:3])})")
            elif not pending and len(done) == len(self.project_phases):
                context_parts.append("所有阶段已全部完成")

        context = "\n".join(context_parts)

        # 安全替换（避免 context 含 {} 导致 format 爆炸）
        system_content = Prompts.PM_POST_EXECUTION_GUIDE.replace(
            "{execution_context}", context)
        resp = self._chat_completion(
            model=self.model,
            messages=[
                {"role": "system", "content": system_content},
                {"role": "user", "content": "请根据执行结果生成引导性回复"},
            ],
            temperature=0.5,
        )
        return resp.content.strip()

    def _advance_phase(self, execution_entry: dict):
        """Phase 完成后自动推进到下一个"""
        if not self.project_phases:
            return

        # 找到当前正在执行的 Phase
        current = None
        for p in self.project_phases:
            if p["status"] in ("pending", "executing"):
                current = p
                break

        if not current:
            return

        if execution_entry["success"]:
            current["status"] = "done"
            logger.info(f"Phase {current['index']}「{current['name']}」已完成")

            # 查找下一个待执行的 Phase
            next_phase = next((p for p in self.project_phases if p["status"] == "pending"), None)
            if next_phase:
                self.current_phase_index = next_phase["index"] - 1
                # 预装下一 Phase 为 pending_extend
                if self._full_plan_md:
                    self.pending_extend = self._build_phase_plan(self._full_plan_md, next_phase["index"] - 1)
                logger.info(f"Phase {next_phase['index']}「{next_phase['name']}」已预装为待确认")
            else:
                logger.info("所有 Phase 已全部完成")
        else:
            current["status"] = "failed"
            logger.warning(f"Phase {current['index']}「{current['name']}」执行失败")

    def _build_phase_status_text(self) -> str:
        """构建 Phase 进度文本，注入路由上下文（供 LLM 感知项目全貌）"""
        lines = []
        if self._full_plan_md:
            for line in self._full_plan_md.split('\n'):
                if line.startswith('# '):
                    lines.append(f"项目: {line[2:].strip()}")
                    break
        for p in (self.project_phases or []):
            icon = {"done": "✅", "pending": "⏳", "executing": "🔄", "failed": "❌"}.get(p["status"], "?")
            lines.append(f"  {icon} Phase {p['index']}「{p['name']}」{p['status']}")
        if self.pending_extend:
            lines.append("  ⚠️ 下一 Phase 已预装，等待用户确认继续")
        return "\n".join(lines)

    def _handle_confirm(self) -> PMResponse:
        """
        用户确认 → 将 plan.md 作为 confirmed_requirement 传给 Engine。
        Phase 2: 不再依赖 structured_req JSON，直接用 plan.md。
        """
        if not self.pending_plan_md:
            return PMResponse(intent="action", reply="当前没有待确认的方案，请先描述您想创建的项目。")

        title = self._extract_plan_title(self.pending_plan_md)

        # 直接把 plan.md 作为 Engine 的需求输入
        self.confirmed_requirement = self.pending_plan_md

        # v5.3: 注入用户偏好 memo（与 patch 路径一致）
        memo_constraints = []
        if self._memo.get("user_prefs"):
            memo_constraints.append(f"用户偏好: {self._memo['user_prefs']}")
        if self._memo.get("design"):
            memo_constraints.append(f"设计风格: {self._memo['design']}")
        if memo_constraints:
            self.confirmed_requirement += "\n\n【用户偏好约束（必须遵守）】\n" + "\n".join(memo_constraints)

        self.confirmed_mode = "create"

        self.pending_plan_md = None
        self.state = "idle"

        global_broadcaster.emit_sync("PM", "info", f"用户已确认方案，正在启动开发团队...")

        reply = self._generate_reply(
            f"用户确认创建项目。项目名称：{title}。"
            f"操作类型：启动全新项目构建。",
            fallback=f"收到，正在为「{title}」启动构建，请关注面板进度。",
        )
        return PMResponse(intent="action", reply=reply, is_executing=True)

    def _handle_reject(self) -> PMResponse:
        """用户拒绝 → PM 追问，保持 wait_confirm 状态"""
        # 构建拒绝上下文：告诉 LLM 用户拒绝了什么
        rejected_context = ""
        if self.pending_plan_md:
            rejected_title = self._extract_plan_title(self.pending_plan_md)
            rejected_context = f"用户拒绝了项目方案「{rejected_title}」"
        elif self.pending_patch:
            rejected_context = f"用户拒绝了修改方案「{self.pending_patch[:60]}」"
        else:
            rejected_context = "用户拒绝了当前方案"
        reply = self._generate_reply(
            f"{rejected_context}。需要追问用户想怎么调整（技术栈/功能增减/设计风格等）。",
            fallback="好的，您想怎么调整？直接说就行。",
        )
        return PMResponse(intent="action", reply=reply)

    # ============================================================
    # Layer 2: 项目感知上下文（Phase 2 PM A-1）
    # ============================================================

    def _get_project_dir(self) -> str:
        """获取当前项目的绝对路径"""
        projects_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "projects"
        )
        return os.path.join(projects_dir, self.project_id)

    _SKIP_DIRS = {'__pycache__', 'venv', '.venv', 'node_modules', '.sandbox', '.astrea', '.git'}

    def _scan_project_files(self, project_dir: str) -> dict:
        """遍历项目目录，收集文件元信息"""
        files = []
        for root, dirs, filenames in os.walk(project_dir):
            dirs[:] = [d for d in dirs if d not in self._SKIP_DIRS]
            for fname in filenames:
                if fname.startswith('.') or fname == 'plan.md':
                    continue
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, project_dir).replace('\\', '/')
                ext = os.path.splitext(fname)[1].lower()
                try:
                    with open(fpath, encoding='utf-8', errors='ignore') as fh:
                        line_count = sum(1 for _ in fh)
                except Exception:
                    line_count = 0
                files.append({"name": rel, "basename": fname, "ext": ext, "lines": line_count})
        return {"total": len(files), "files": files}

    def _detect_tech_stack(self, files: list) -> str:
        """根据文件后缀和内容关键词检测技术栈（确定性，零 LLM）"""
        names = {f["basename"] for f in files}
        exts = {f["ext"] for f in files}
        parts = []

        # 后端
        if any(f["basename"] in ("app.py", "routes.py") for f in files):
            # 尝试区分 Flask / FastAPI
            project_dir = self._get_project_dir()
            for f in files:
                if f["basename"] in ("app.py", "main.py", "routes.py"):
                    try:
                        with open(os.path.join(project_dir, f["name"]), encoding='utf-8', errors='ignore') as fh:
                            head = fh.read(500)
                        if "fastapi" in head.lower():
                            parts.append("FastAPI")
                            break
                        elif "flask" in head.lower():
                            parts.append("Flask")
                            break
                        elif "django" in head.lower():
                            parts.append("Django")
                            break
                    except Exception:
                        pass
            if not parts:
                parts.append("Python")
        elif "package.json" in names:
            # 检查是否是 Express
            project_dir = self._get_project_dir()
            pkg_path = os.path.join(project_dir, "package.json")
            if os.path.isfile(pkg_path):
                try:
                    with open(pkg_path, encoding='utf-8') as fh:
                        pkg = fh.read(300)
                    if "express" in pkg.lower():
                        parts.append("Express")
                    elif "next" in pkg.lower():
                        parts.append("Next.js")
                except Exception:
                    pass

        # 前端
        if ".vue" in exts:
            parts.append("Vue 3 Vite" if "vite.config" in ' '.join(names) else "Vue 3 CDN")
        elif ".jsx" in exts or ".tsx" in exts:
            parts.append("React Vite" if "vite.config" in ' '.join(names) else "React")
        elif ".html" in exts and "templates" in ' '.join(f["name"] for f in files):
            parts.append("Jinja2 SSR")
        elif ".html" in exts:
            parts.append("Vanilla JS")

        # 数据库
        for f in files:
            if f["basename"] in ("models.py", "database.py", "db.py"):
                project_dir = self._get_project_dir()
                try:
                    with open(os.path.join(project_dir, f["name"]), encoding='utf-8', errors='ignore') as fh:
                        head = fh.read(500)
                    if "sqlite" in head.lower() or "db.sqlite" in head.lower():
                        parts.append("SQLite")
                    elif "sqlalchemy" in head.lower():
                        parts.append("SQLAlchemy")
                    elif "sequelize" in head.lower():
                        parts.append("Sequelize")
                    break
                except Exception:
                    pass

        return " + ".join(parts) if parts else ""

    def _extract_project_description(self, project_dir: str) -> str:
        """从 .astrea/plan.md 提取项目描述（首行标题）"""
        plan_path = os.path.join(project_dir, ".astrea", "plan.md")
        if not os.path.isfile(plan_path):
            return ""
        try:
            with open(plan_path, encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("# "):
                        return line[2:].strip()
                    if line and not line.startswith("#"):
                        return line[:60]
        except Exception:
            pass
        return ""

    def _build_file_tree(self, files: list, project_dir: str) -> str:
        """生成缩进文件树（含行数）"""
        # 按路径排序，目录优先
        sorted_files = sorted(files, key=lambda f: f["name"])
        lines = []
        seen_dirs = set()
        for f in sorted_files:
            parts = f["name"].split("/")
            # 显示目录层级
            if len(parts) > 1:
                dir_path = "/".join(parts[:-1])
                if dir_path not in seen_dirs:
                    seen_dirs.add(dir_path)
                    lines.append(f"  {dir_path}/")
            indent = "    " if len(parts) > 1 else "  "
            lines.append(f"{indent}{parts[-1]} ({f['lines']}行)")
        return "\n".join(lines[:25])  # 最多 25 行

    def _build_skeleton_index(self, files: list, project_dir: str, max_files: int = 5) -> str:
        """为关键文件生成带行号+依赖标注的骨架索引（PM 的目录页）"""
        KEY_BASENAMES = {
            'app.py', 'main.py', 'routes.py', 'views.py', 'models.py',
            'index.html', 'style.css', 'app.js',
            'App.vue', 'App.jsx', 'page.js', 'page.jsx',
        }

        index_parts = []
        for f in files:
            if f["basename"] not in KEY_BASENAMES:
                continue
            if len(index_parts) >= max_files:
                break

            filepath = os.path.join(project_dir, f["name"])
            if not os.path.isfile(filepath):
                continue

            skeleton = self._extract_file_skeleton(filepath, f["ext"], files, project_dir)
            if skeleton:
                index_parts.append(f"{f['name']} ({f['lines']}行):\n{skeleton}")

        return "\n\n".join(index_parts)

    def _extract_file_skeleton(self, filepath: str, ext: str,
                                all_files: list = None, project_dir: str = None) -> str:
        """提取单个文件的骨架签名 + 行号范围 + 依赖标注"""
        try:
            with open(filepath, encoding='utf-8', errors='ignore') as fh:
                lines = fh.readlines()
        except Exception:
            return ""

        # 收集 import 的模块名（用于依赖标注）
        imported_modules = set()
        for line in lines[:30]:
            m = re.match(r'^\s*(?:from|import)\s+(\w+)', line)
            if m:
                imported_modules.add(m.group(1))

        entries = []

        if ext == '.py':
            i = 0
            while i < len(lines):
                stripped = lines[i].strip()
                line_no = i + 1

                # class 定义
                if stripped.startswith('class ') and '(' in stripped:
                    class_match = re.match(r'class\s+(\w+)', stripped)
                    if class_match:
                        end = self._find_block_end_py(lines, i)
                        entries.append(f"  class {class_match.group(1)}  [L{line_no}-{end + 1}]")
                        # 提取字段（如 SQLAlchemy Column 或简单赋值）
                        columns = []
                        for ci in range(i + 1, min(end + 1, len(lines))):
                            col_m = re.match(r'\s+(\w+)\s*=\s*(?:db\.Column|models\.\w+Field|Column)', lines[ci])
                            if col_m:
                                columns.append(col_m.group(1))
                        if columns:
                            entries.append(f"    columns: {', '.join(columns[:8])}")

                # def 定义
                elif stripped.startswith('def '):
                    func_match = re.match(r'def\s+(\w+)\s*\(([^)]*)\)', stripped)
                    if func_match:
                        end = self._find_block_end_py(lines, i)
                        sig = f"def {func_match.group(1)}({func_match.group(2)})"

                        # 提取路由装饰器
                        route = ""
                        if i > 0:
                            for di in range(max(0, i - 3), i):
                                route_m = re.search(r"@\w+\.(?:route|get|post|put|delete)\(['\"](.+?)['\"]", lines[di])
                                if route_m:
                                    method_m = re.search(r'\.(get|post|put|delete)\(', lines[di])
                                    method = method_m.group(1).upper() if method_m else ""
                                    route = f"  → {method} {route_m.group(1)}" if method else f"  → {route_m.group(1)}"
                                    break

                        entries.append(f"  {sig}  [L{line_no}-{end + 1}]{route}")

                        # 扫描函数体内的依赖关系
                        body_text = "".join(lines[i:end + 1])
                        deps = self._extract_deps_from_body(body_text, imported_modules)
                        for dep in deps:
                            entries.append(f"    ↳ {dep}")

                i += 1

        elif ext in ('.html', '.vue', '.jsx', '.tsx'):
            for i, line in enumerate(lines):
                stripped = line.strip()
                line_no = i + 1
                # 主要结构元素
                tag_m = re.match(r'<(main|section|form|table|nav|header|footer|ul|ol)\b', stripped)
                if tag_m:
                    # 提取 class/id
                    attr = ""
                    id_m = re.search(r'id=["\']([^"\']+)["\']', stripped)
                    cls_m = re.search(r'class=["\']([^"\']+)["\']', stripped)
                    if id_m:
                        attr = f' id="{id_m.group(1)}"'
                    elif cls_m:
                        attr = f' class="{cls_m.group(1)}"'
                    entries.append(f"  <{tag_m.group(1)}{attr}>  [L{line_no}]")

                # form action
                action_m = re.search(r'action=["\']([^"\']+)["\']', stripped)
                if action_m:
                    entries.append(f"    ↳ calls: {action_m.group(1)}")

                # fetch 调用
                fetch_m = re.search(r"fetch\(['\"]([^'\"]+)['\"]", stripped)
                if fetch_m:
                    entries.append(f"    ↳ calls: {fetch_m.group(1)}")

                # Jinja extends
                ext_m = re.search(r"\{%\s*extends\s+['\"](.+?)['\"]", stripped)
                if ext_m:
                    entries.append(f"  ↳ extends: {ext_m.group(1)}")

        elif ext == '.css':
            for i, line in enumerate(lines):
                stripped = line.strip()
                line_no = i + 1
                if stripped and not stripped.startswith(('/*', '*', '//', '}', '@')) and '{' in stripped:
                    selector = stripped.split('{')[0].strip()
                    if selector:
                        entries.append(f"  {selector}  [L{line_no}]")

        return "\n".join(entries[:15])  # 每个文件最多 15 个条目

    def _extract_deps_from_body(self, body_text: str, imported_modules: set) -> list:
        """从函数体中提取依赖标注"""
        deps = []
        seen = set()

        # render_template 依赖
        for m in re.finditer(r"render_template\(['\"](.+?)['\"]", body_text):
            dep = f"renders: templates/{m.group(1)}"
            if dep not in seen:
                deps.append(dep)
                seen.add(dep)

        # 模块调用依赖 (xxx.func())
        for m in re.finditer(r"(\w+)\.(\w+)\(", body_text):
            mod, func = m.group(1), m.group(2)
            if mod in imported_modules and mod not in ('self', 'os', 'json', 'logging', 'request', 'db', 'app', 're'):
                dep = f"calls: {mod}.{func}()"
                if dep not in seen:
                    deps.append(dep)
                    seen.add(dep)

        # redirect + url_for 依赖
        for m in re.finditer(r"redirect\(url_for\(['\"](.+?)['\"]", body_text):
            dep = f"redirects: {m.group(1)}"
            if dep not in seen:
                deps.append(dep)
                seen.add(dep)

        return deps[:5]  # 每个函数最多 5 个依赖

    @staticmethod
    def _find_block_end_py(lines: list, start_idx: int) -> int:
        """找到 Python 函数/类块的结束行（基于缩进）"""
        if start_idx >= len(lines):
            return start_idx
        # 获取定义行的缩进
        first_line = lines[start_idx]
        base_indent = len(first_line) - len(first_line.lstrip())

        end = start_idx
        for j in range(start_idx + 1, len(lines)):
            line = lines[j]
            if not line.strip():  # 空行跳过
                continue
            current_indent = len(line) - len(line.lstrip())
            if current_indent <= base_indent:
                break
            end = j
        return end

    def _get_recent_git_log(self, project_dir: str, max_count: int = 3) -> str:
        """格式化最近 N 条 git log"""
        git_dir = os.path.join(project_dir, ".git")
        if not os.path.isdir(git_dir):
            return ""
        try:
            import subprocess as sp
            result = sp.run(
                ["git", "log", f"--max-count={max_count}", "--format=%s|%ar"],
                cwd=project_dir,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
            )
            if result.returncode != 0 or not result.stdout.strip():
                return ""
            lines = []
            for line in result.stdout.strip().split('\n'):
                parts = line.split('|', 1)
                if len(parts) == 2:
                    lines.append(f"  {parts[0].strip()} ({parts[1].strip()})")
            return "\n".join(lines)
        except Exception:
            return ""

    @staticmethod
    def _trim_window_by_tokens(conversation: list) -> list:
        """
        弹性 Token 截断：按 token 预算裁剪对话窗口。

        规则：
        - 从最新消息往回计算，累计 token 不超过 _WINDOW_TOKEN_BUDGET
        - 弹性设计：不在对话对（user+assistant）中间截断
          → 如果加入某条消息后超预算，仍然保留该消息及其配对
        - 最坏情况：窗口只剩最后一轮对话
        """
        if not conversation:
            return conversation

        budget = _WINDOW_TOKEN_BUDGET
        total_tokens = 0
        cut_index = 0  # 从这个 index 开始保留

        # 从后往前扫描
        for i in range(len(conversation) - 1, -1, -1):
            msg_tokens = len(conversation[i].get("content", "")) / _CHAR_PER_TOKEN
            total_tokens += msg_tokens

            if total_tokens > budget:
                # 超预算了，但要保证弹性：不在对话对中间截断
                # 确保 cut_index 落在 user 消息的位置（偶数 index 或第一条）
                cut_index = i + 1
                # 如果 cut_index 指向 assistant 消息，再往前推一条（保留完整对）
                if cut_index < len(conversation) and conversation[cut_index]["role"] == "assistant":
                    cut_index = max(0, cut_index - 1)
                break

        result = conversation[cut_index:]
        if len(result) < len(conversation):
            logger.debug(
                f"滑动窗口截断: {len(conversation)} → {len(result)} 条消息, "
                f"≈{int(total_tokens)} tokens"
            )
        return result

    def _build_project_context(self, context_needs: list = None) -> str:
        """
        分级构建项目上下文快照（Phase 2 PM A-1）。

        Level 0（所有模式）: 项目名 + 文件数 + 技术栈 + 描述       ~200 chars
        Level 1（Patch/Chat）: 文件树 + 骨架索引 + git log          ~600 chars
        """
        if context_needs is None:
            context_needs = []

        project_dir = self._get_project_dir()
        if not os.path.isdir(project_dir):
            return "项目: 新项目（尚无文件）"

        file_info = self._scan_project_files(project_dir)
        if file_info['total'] == 0:
            return "项目: 空项目（尚无源代码）"

        parts = []

        # === L0: 基础元信息（所有模式）===
        tech_stack = self._detect_tech_stack(file_info['files'])
        description = self._extract_project_description(project_dir)
        parts.append(f"项目: {self.project_id}")
        parts.append(f"状态: 已有 {file_info['total']} 个文件")
        if tech_stack:
            parts.append(f"技术栈: {tech_stack}")
        if description:
            parts.append(f"描述: {description}")

        # === L1: 文件树 + 骨架索引（Patch/Rollback/Chat）===
        if any(n in context_needs for n in ("file_tree", "file_list", "skeleton")):
            tree = self._build_file_tree(file_info['files'], project_dir)
            if tree:
                parts.append(f"\n📁 文件结构:\n{tree}")

        if "skeleton" in context_needs:
            skeleton = self._build_skeleton_index(file_info['files'], project_dir)
            if skeleton:
                parts.append(f"\n📄 关键文件骨架（↳ 标注了跨文件依赖关系）:\n{skeleton}")

        if "git_log" in context_needs:
            log = self._get_recent_git_log(project_dir, max_count=3)
            if log:
                parts.append(f"\n📝 最近修改:\n{log}")

        return "\n".join(parts)

    # ============================================================
    # 辅助方法
    # ============================================================

    @staticmethod
    def _structured_req_to_prompt(req: dict) -> str:
        """将 structured_req 转为自然语言 prompt（给 Manager 用）"""
        parts = [f"项目：{req.get('summary', '用户项目')}"]

        features = req.get("core_features", [])
        if features:
            parts.append("核心功能：" + "、".join(features))

        implied = req.get("implied_requirements", [])
        if implied:
            parts.append("隐含需求：" + "、".join(implied))

        tech = req.get("tech_preferences", {})
        if tech:
            tech_items = [f"{k}={v}" for k, v in tech.items()]
            parts.append("技术栈偏好：" + ", ".join(tech_items))

        return "\n".join(parts)

    def get_user_requirement(self) -> Optional[str]:
        """获取用户已确认的需求文本（供 Engine 使用）"""
        if self.pending_plan_md:
            return self.pending_plan_md
        return None

    def _search_archive(self, query: str) -> Optional[str]:
        """搜索对话档案（FTS5）"""
        store = self._get_store()
        if not store:
            return None

        clean_query = query.strip()
        if not clean_query:
            clean_query = query

        try:
            results = store.search(clean_query, limit=5)
            if not results:
                return "没有找到相关的历史对话记录。"

            lines = ["找到以下相关历史记录：", ""]
            for r in results:
                role_label = "用户" if r["role"] == "user" else "PM"
                lines.append(f"**{role_label}** (轮次 {r['round_id']}):")
                lines.append(f"> {r['content'][:200]}")
                lines.append("")

            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"档案检索失败: {e}")
            return None

    def _query_git_log(self, message: str, max_count: int = 10) -> list:
        """从 git log 中搜索与用户消息相关的 commit（降级版 Ledger 查询）"""
        projects_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "projects"
        )
        project_dir = os.path.join(projects_dir, self.project_id)
        git_dir = os.path.join(project_dir, ".git")

        if not os.path.isdir(git_dir):
            return []

        try:
            result = subprocess.run(
                ["git", "log", f"--max-count={max_count}", "--format=%H|%s|%ai"],
                cwd=project_dir,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=5,
            )
            if result.returncode != 0 or not result.stdout:
                return []

            commits = []
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                parts = line.split('|', 2)
                if len(parts) == 3:
                    commits.append({
                        "hash": parts[0].strip(),
                        "message": parts[1].strip(),
                        "date": parts[2].strip()[:10],
                    })

            # 优先级 1: 用户直接指定了 Round 编号（如 "撤销 Round 2" / "回滚第2轮"）
            import re
            round_direct = re.search(r"(?:round|轮)\s*(\d+)", message, re.IGNORECASE)
            if round_direct:
                target_round = round_direct.group(1)
                round_commits = [c for c in commits if f"[Round {target_round}]" in c["message"]]
                if round_commits:
                    return [round_commits[-1]]  # 返回该轮最早的 commit

            # 优先级 2: 关键词匹配
            msg_lower = message.lower()
            matched = [c for c in commits if any(
                kw in c["message"].lower() for kw in msg_lower.split() if len(kw) > 1
            )]

            if matched:
                first_match = matched[0]
                m = re.search(r"\[Round (\d+)\]", first_match["message"])
                if m:
                    round_id = m.group(1)
                    # 抓取与之属于同一个 Round 的所有连续 commits
                    round_commits = [c for c in commits if f"[Round {round_id}]" in c["message"]]
                    # git log 按时间倒序，最后一个就是该轮次最早生成的 commit
                    if round_commits:
                        return [round_commits[-1]]
                return matched

            # 优先级 3: 没匹配到, 返回最近一个有 Round 标记的 commit 所在轮次
            for c in commits:
                m = re.search(r"\[Round (\d+)\]", c["message"])
                if m:
                    round_id = m.group(1)
                    round_commits = [cc for cc in commits if f"[Round {round_id}]" in cc["message"]]
                    if round_commits:
                        return [round_commits[-1]]

            return commits[:3]  # 最终兜底

        except Exception as e:
            logger.warning(f"git log 查询失败: {e}")
            return []
