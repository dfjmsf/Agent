"""
PM Agent — ASTrea 化身，用户的唯一对话窗口

Phase 2.5.1 双层架构：
  Layer 1: 路由嗅探器（保守正则 + LLM 主力分类）
  Layer 2: 人格化身（高情商回复 + 上下文按需注入）

路由类型：create / patch / rollback / chat / scan / clarify
状态机：idle / wait_confirm / wait_patch_confirm / wait_rollback_confirm / wait_clarify
"""
import os
import re
import json
import logging
import subprocess
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any

from core.llm_client import default_llm
from core.prompt import Prompts
from core.ws_broadcaster import global_broadcaster

logger = logging.getLogger("PMAgent")

# 滑动窗口大小（5 轮 = 10 条消息：5 user + 5 assistant）
_SLIDING_WINDOW_SIZE = 10


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
        self.model = os.getenv("MODEL_PM", "deepseek-chat")

        # 对话滑动窗口（内存态）
        self.conversation: List[dict] = []

        # 待确认的规划（等待用户 confirm/reject）
        self.pending_req: Optional[dict] = None
        self.pending_plan_md: Optional[str] = None

        # patch / rollback 暂存
        self.pending_patch: Optional[str] = None          # 用户的修改意图描述
        self.pending_rollback_commit: Optional[str] = None # 待回滚的 commit hash

        # 状态机
        self.state: str = "idle"
        # 有效状态: "idle" | "wait_confirm" | "wait_patch_confirm"
        #           | "wait_rollback_confirm" | "wait_clarify"

        # Engine 执行时的 mode（create/patch/rollback）
        self.confirmed_mode: str = "auto"

        # 上一轮路由结果（供 Layer 2 使用）
        self._last_route: Optional[dict] = None

        # 轮次计数（用于 FTS5 round_id）
        self.round_id: int = 0

        # FTS5 对话存储（延迟初始化，需要 project_dir）
        self._store = None

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
    # 主入口
    # ============================================================

    def chat(self, user_message: str) -> PMResponse:
        """
        主入口：接收用户消息，返回结构化响应。

        流程：追加历史 → 状态机分发/意图分类 → 处理 → 截断窗口 → 持久化 → 返回
        """
        self.round_id += 1
        logger.info(f"PM 收到消息 (round={self.round_id}): {user_message[:80]}...")

        # 追加用户消息到窗口
        self.conversation.append({"role": "user", "content": user_message})

        # ---- 状态机优先分发 ----
        if self.state == "wait_confirm":
            response = self._handle_plan_revision(user_message)
        elif self.state == "wait_patch_confirm":
            # 用户在修改确认阶段的回复，暂时走 chat 处理
            response = self._handle_chat(user_message)
        elif self.state == "wait_rollback_confirm":
            response = self._handle_chat(user_message)
        elif self.state == "wait_clarify":
            # 用户澄清后重新路由
            self.state = "idle"
            route_result = self._classify_intent(user_message)
            self._last_route = route_result
            response = self._dispatch_route(route_result, user_message)
        else:
            # idle 状态 → Layer 1 路由
            route_result = self._classify_intent(user_message)
            self._last_route = route_result
            logger.info(f"路由结果: {route_result}")
            response = self._dispatch_route(route_result, user_message)

        # 追加 PM 回复到窗口
        self.conversation.append({"role": "assistant", "content": response.reply})

        # 截断滑动窗口
        if len(self.conversation) > _SLIDING_WINDOW_SIZE:
            self.conversation = self.conversation[-_SLIDING_WINDOW_SIZE:]

        # 持久化到 FTS5
        store = self._get_store()
        if store:
            try:
                store.append("user", user_message, self.round_id)
                store.append("pm", response.reply, self.round_id)
            except Exception as e:
                logger.warning(f"FTS5 写入失败: {e}")

        return response

    def _dispatch_route(self, route_result: dict, message: str) -> PMResponse:
        """根据路由结果分发到对应处理器"""
        route = route_result.get("route", "chat")

        if route == "create":
            return self._handle_create(message)
        elif route == "patch":
            return self._handle_patch(message)
        elif route == "rollback":
            return self._handle_rollback(message)
        elif route == "scan":
            return self._handle_scan(message)
        elif route == "clarify":
            return self._handle_clarify(message)
        else:
            return self._handle_chat(message)

    def handle_action(self, action: str) -> PMResponse:
        """
        处理确定性按钮动作（零 LLM 分类成本）。

        Args:
            action: "confirm" | "reject" | "rollback_confirm" | "rollback_cancel"
        """
        logger.info(f"PM 收到按钮动作: {action}")

        if action == "confirm":
            return self._handle_confirm()
        elif action == "reject":
            return self._handle_reject()
        elif action == "patch_confirm":
            return self._handle_patch_execute()
        elif action == "patch_cancel":
            self.state = "idle"
            self.pending_patch = None
            return PMResponse(intent="action", reply="好的，取消修改。")
        elif action == "rollback_confirm":
            return self._handle_rollback_execute()
        elif action == "rollback_cancel":
            self.state = "idle"
            self.pending_rollback_commit = None
            return PMResponse(intent="action", reply="好的，取消回滚操作。")
        else:
            return PMResponse(intent="action", reply=f"未知操作: {action}")

    # ============================================================
    # Layer 1: 路由嗅探器（保守正则 + LLM 主力）
    # ============================================================

    def _classify_intent(self, message: str) -> dict:
        """
        双层路由：
        - 层 1: 保守正则 — 仅匹配显式命令前缀（100% 确定性）
        - 层 2: LLM 分类 — 所有自然语言一律交给 LLM（~200 tokens）
        - 安全网: 置信度 < 0.7 → clarify
        """
        msg = message.strip()

        # === 层 1: 显式命令前缀（100% 确定性，零 LLM）===
        if msg.startswith('/plan') or msg.startswith('/create'):
            return {"route": "create", "confidence": 1.0, "context_needs": []}
        if msg.startswith('/scan'):
            return {"route": "scan", "confidence": 1.0, "context_needs": []}
        if msg.startswith('/rollback'):
            return {"route": "rollback", "confidence": 1.0, "context_needs": ["file_list"]}
        if msg.startswith('/patch'):
            return {"route": "patch", "confidence": 1.0, "context_needs": ["file_list"]}

        # === 层 2: LLM 分类（所有自然语言）===
        result = self._llm_classify(message)

        # === 安全网：置信度不够就追问 ===
        if result.get("confidence", 0) < 0.7:
            logger.info(f"路由置信度过低 ({result.get('confidence', 0)})，触发 clarify")
            return {"route": "clarify", "confidence": result.get("confidence", 0), "context_needs": []}

        return result

    def _llm_classify(self, message: str) -> dict:
        """调用 LLM 做五分类意图识别，输出结构化 dict"""
        project_exists = self._project_exists()
        project_status = f"项目 {self.project_id} — {'已存在，包含源代码文件' if project_exists else '不存在或为空项目'}"

        try:
            prompt = Prompts.PM_INTENT_CLASSIFIER_V2.format(
                project_status=project_status,
                message=message,
            )
            response = default_llm.chat_completion(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
            )
            raw = response.content.strip()

            # 清理 Markdown 代码块包裹
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0].strip()
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0].strip()

            result = json.loads(raw)

            # 验证必要字段
            route = result.get("route", "chat")
            valid_routes = ("create", "patch", "rollback", "chat", "scan")
            if route not in valid_routes:
                logger.warning(f"LLM 返回非法路由 '{route}'，降级为 chat")
                route = "chat"

            return {
                "route": route,
                "confidence": float(result.get("confidence", 0.8)),
                "context_needs": result.get("context_needs", []),
            }
        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"LLM 意图分类失败: {e}，降级为 chat")
            return {"route": "chat", "confidence": 0.5, "context_needs": []}

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
    # 需求标准化
    # ============================================================

    def _standardize_requirement(self, user_message: str) -> dict:
        """
        将用户自然语言需求翻译为 structured_req JSON。
        LLM 只做翻译，不做创造。
        """
        # 构建对话历史上下文
        history_text = ""
        if self.conversation:
            recent = self.conversation[-6:]  # 最近 3 轮
            history_text = "\n".join(
                f"{'用户' if m['role'] == 'user' else 'PM'}: {m['content']}"
                for m in recent
            )

        user_prompt = f"对话历史：\n{history_text}\n\n当前用户需求：\n{user_message}"

        try:
            response = default_llm.chat_completion(
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
            # 降级：从原始消息提取关键信息
            first_line = user_message.split('\n')[0].strip()[:100]
            features = []
            for line in user_message.split('\n'):
                line = line.strip()
                if re.match(r'^[\d]+[.、]', line):
                    features.append(line[:80])
            if not features:
                features = [first_line]

            tech = {"database": "sqlite", "frontend": "jinja2_ssr", "backend": "flask"}
            defaults = []
            msg_lower = user_message.lower()
            if "sqlalchemy" not in msg_lower and "sqlite" not in msg_lower:
                defaults.append({"field": "数据库", "value": "SQLite", "reason": "用户未指定"})
            if "flask" not in msg_lower:
                defaults.append({"field": "后端", "value": "Flask", "reason": "用户未指定"})
            if "jinja" not in msg_lower:
                defaults.append({"field": "前端", "value": "Jinja2 SSR", "reason": "用户未指定"})

            return {
                "summary": first_line,
                "core_features": features,
                "implied_requirements": ["数据持久化"],
                "tech_preferences": tech,
                "defaults_applied": defaults,
            }

    # ============================================================
    # 意图处理器
    # ============================================================

    def _handle_chat(self, message: str) -> PMResponse:
        """闲聊处理 — 高情商 PM 直接回复"""
        # 检查是否是档案检索请求
        archive_keywords = ["之前", "上次", "历史", "说过什么", "说过"]
        if any(kw in message for kw in archive_keywords):
            archive_result = self._search_archive(message)
            if archive_result:
                return PMResponse(intent="chat", reply=archive_result)

        context_needs = self._last_route.get("context_needs", []) if self._last_route else []
        project_context = self._build_project_context(context_needs)
        route_hint = ""  # chat 模式无特殊 hint
        system_prompt = Prompts.PM_SYSTEM.format(
            project_context=project_context,
            route_hint=route_hint,
        )

        try:
            messages = [{"role": "system", "content": system_prompt}]
            messages.extend(self.conversation[-6:])

            response = default_llm.chat_completion(
                model=self.model,
                messages=messages,
                temperature=0.7,
            )
            return PMResponse(intent="chat", reply=response.content.strip())
        except Exception as e:
            logger.error(f"PM 闲聊失败: {e}")
            return PMResponse(intent="chat", reply="抱歉，我暂时无法回复，请稍后再试。")

    def _handle_create(self, message: str) -> PMResponse:
        """创建处理 — 标准化需求 → 规划组生成 plan.md → 返回给用户确认"""
        global_broadcaster.emit_sync("PM", "info", "正在分析您的需求...")

        # 1. 标准化需求
        structured_req = self._standardize_requirement(message)

        # 2. 调用规划组生成 plan.md（自动保存到 .astrea/plan.md）
        from agents.planner_lite import PlannerLiteAgent
        planner = PlannerLiteAgent()
        projects_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "projects"
        )
        project_dir = os.path.join(projects_dir, self.project_id)
        plan_md = planner.generate_plan(structured_req, project_dir=project_dir)

        # 3. 保存待确认状态
        self.pending_req = structured_req
        self.pending_plan_md = plan_md
        self.state = "wait_confirm"

        # 4. PM 口语化归纳
        summary = structured_req.get("summary", "您的项目")
        defaults = structured_req.get("defaults_applied", [])
        features = structured_req.get("core_features", [])

        reply_parts = [f"我帮您梳理了一下「{summary}」的方案"]
        if features:
            feature_str = "、".join(f[:20] for f in features[:3])
            reply_parts.append(f"，核心功能包括{feature_str}")
        reply_parts.append("。详细的技术方案在右侧方案面板里，您看看有没有要调整的。")

        if defaults:
            notices = [f"{d['field']}用的是{d['value']}" for d in defaults]
            reply_parts.append(f"\n\n另外，{', '.join(notices)}，这些是默认选择，需要换的话随时告诉我。")

        reply = "".join(reply_parts)

        global_broadcaster.emit_sync("PM", "plan_preview", "技术方案预览已就绪")

        return PMResponse(
            intent="create",
            reply=reply,
            plan_md=plan_md,
            actions=[
                {"id": "confirm", "label": "确认执行", "style": "primary"},
                {"id": "reject", "label": "我要调整", "style": "secondary"},
            ]
        )

    def _handle_patch(self, message: str) -> PMResponse:
        """
        修改处理（二步确认）：
        Step 1: PM 确认理解了修改意图，展示确认按钮
        Step 2: 用户点击确认 → _handle_patch_execute() 启动 Engine patch mode
        """
        self.pending_patch = message
        self.state = "wait_patch_confirm"

        if self._project_exists():
            # 项目存在，PM 确认修改意图
            context_needs = self._last_route.get("context_needs", []) if self._last_route else []
            project_context = self._build_project_context(context_needs)

            # 用 LLM 生成一个有温度的确认回复
            route_hint = "【当前场景：用户想修改已有项目，请简洁确认理解了修改需求，告知用户点击确认按钮即可启动修改】"
            system_prompt = Prompts.PM_SYSTEM.format(
                project_context=project_context,
                route_hint=route_hint,
            )
            try:
                messages = [{"role": "system", "content": system_prompt}]
                messages.extend(self.conversation[-6:])
                resp = default_llm.chat_completion(
                    model=self.model,
                    messages=messages,
                    temperature=0.5,
                )
                reply = resp.content.strip()
            except Exception:
                reply = "明白，我来帮您处理这个修改。"

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

    def _handle_patch_execute(self) -> PMResponse:
        """
        用户确认修改 → 启动 Engine patch mode。
        """
        if not self.pending_patch:
            return PMResponse(intent="action", reply="没有待执行的修改。")

        # 保存修改需求作为 Engine prompt
        self.confirmed_requirement = self.pending_patch
        self.confirmed_mode = "patch"

        patch_desc = self.pending_patch[:60]
        self.pending_patch = None
        self.state = "idle"

        global_broadcaster.emit_sync("PM", "info", f"用户已确认修改，正在启动 Patch 模式...")

        return PMResponse(
            intent="action",
            reply=f"收到！正在以 Patch 模式修改「{patch_desc}」，请在左侧面板关注进度。",
            is_executing=True,
        )

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
            return PMResponse(intent="action", reply="没有待回滚的记录。")

        self.confirmed_requirement = f"Rollback {target}"
        self.confirmed_mode = "rollback"

        # 执行实际 git revert
        global_broadcaster.emit_sync("PM", "info", f"用户已确认回滚，正在启动 Rollback 模式...")
        
        return PMResponse(
            intent="action",
            reply=f"收到！正在为您执行回滚操作，将撤销最近的修改批次，请留意左侧面板进度。",
            is_executing=True,
        )

    def _handle_clarify(self, message: str) -> PMResponse:
        """意图不明确时追问用户"""
        self.state = "wait_clarify"

        # 根据上下文生成有温度的追问
        if self._project_exists():
            reply = "我不太确定您的意思——您是想对现有项目做一些修改，还是想创建一个全新的项目？"
        else:
            reply = "我想先确认一下，您是想创建一个新项目吗？如果是的话，可以跟我说说您想做什么。"

        return PMResponse(intent="clarify", reply=reply)

    def _handle_plan_revision(self, message: str) -> PMResponse:
        """用户在 wait_confirm 状态下发送了修改意见 → 重新标准化 + 重新生成 plan"""
        global_broadcaster.emit_sync("PM", "info", "正在根据您的反馈调整方案...")

        structured_req = self._standardize_requirement(message)

        from agents.planner_lite import PlannerLiteAgent
        planner = PlannerLiteAgent()
        plan_md = planner.generate_plan(structured_req)

        self.pending_req = structured_req
        self.pending_plan_md = plan_md

        reply = "已经根据您的反馈更新了方案，请在方案面板查看最新版本。"

        return PMResponse(
            intent="create",
            reply=reply,
            plan_md=plan_md,
            actions=[
                {"id": "confirm", "label": "确认执行", "style": "primary"},
                {"id": "reject", "label": "继续调整", "style": "secondary"},
            ]
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
    # 确认/拒绝处理
    # ============================================================

    def _handle_confirm(self) -> PMResponse:
        """用户确认 → 保存需求文本 → 通知 server.py 启动 Engine"""
        if not self.pending_req:
            return PMResponse(intent="action", reply="没有待确认的方案。")

        summary = self.pending_req.get("summary", "用户项目")

        self.confirmed_requirement = self._structured_req_to_prompt(self.pending_req)
        self.confirmed_mode = "create"

        self.pending_req = None
        self.pending_plan_md = None
        self.state = "idle"

        global_broadcaster.emit_sync("PM", "info", f"用户已确认方案，正在启动开发团队...")

        return PMResponse(
            intent="action",
            reply=f"收到！正在为「{summary}」启动开发团队，请在左侧面板关注进度。",
            is_executing=True,
        )

    def _handle_reject(self) -> PMResponse:
        """用户拒绝 → PM 追问，保持 wait_confirm 状态"""
        return PMResponse(
            intent="action",
            reply="好的，您想怎么调整？比如换个技术栈、加减功能什么的，直接说就行。"
        )

    # ============================================================
    # Layer 2: 上下文按需注入
    # ============================================================

    def _build_project_context(self, context_needs: list = None) -> str:
        """根据 context_needs 按需构建项目上下文，避免全量灌入"""
        if context_needs is None:
            context_needs = []

        projects_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "projects"
        )
        project_dir = os.path.join(projects_dir, self.project_id)

        parts = [f"项目: {self.project_id}"]

        project_exists = os.path.isdir(project_dir)

        if not project_exists:
            parts.append("状态: 新项目（尚无文件）")
            return " | ".join(parts)

        # 基础信息：总是包含文件数
        file_count = 0
        file_names = []
        for root, dirs, files in os.walk(project_dir):
            dirs[:] = [d for d in dirs if d not in
                       ('__pycache__', 'venv', '.venv', 'node_modules', '.sandbox', '.astrea', '.git')]
            for f in files:
                if not f.startswith('.'):
                    file_count += 1
                    file_names.append(os.path.relpath(os.path.join(root, f), project_dir))
        parts.append(f"文件数: {file_count}")

        # 按需注入详细上下文
        if "file_list" in context_needs and file_names:
            parts.append(f"\n项目文件: {', '.join(file_names[:20])}")

        if "tech_stack" in context_needs:
            # 尝试从 plan.md 提取技术栈
            plan_path = os.path.join(project_dir, ".astrea", "plan.md")
            if os.path.isfile(plan_path):
                try:
                    with open(plan_path, "r", encoding="utf-8") as f:
                        plan_content = f.read(500)
                    parts.append(f"\n技术方案摘要: {plan_content[:200]}")
                except Exception:
                    pass

        if "frontend_skeleton" in context_needs:
            # 列出前端相关文件
            frontend_files = [f for f in file_names if any(
                f.endswith(ext) for ext in ('.html', '.js', '.jsx', '.vue', '.css', '.ts', '.tsx')
            )]
            if frontend_files:
                parts.append(f"\n前端文件: {', '.join(frontend_files[:10])}")

        if "project_progress" in context_needs:
            parts.append(f"\n进度: 项目包含 {file_count} 个文件")

        return " | ".join(parts[:2]) + "".join(parts[2:])

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
        if self.pending_req:
            return self._structured_req_to_prompt(self.pending_req)
        return None

    def _search_archive(self, query: str) -> Optional[str]:
        """搜索对话档案（FTS5）"""
        store = self._get_store()
        if not store:
            return None

        clean_query = query
        for prefix in ["之前说过", "之前", "上次", "历史", "说过什么", "说过"]:
            clean_query = clean_query.replace(prefix, "").strip()

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
