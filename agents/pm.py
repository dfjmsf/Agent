"""
PM Agent — 用户与 ASTrea 系统的唯一对话入口

Phase 2.1 核心组件：
- 二层意图路由（硬路由正则 + 软路由 LLM）
- 需求标准化（自然语言 → structured_req JSON）
- 规划组协调（调用 PlannerLite 生成 plan.md）
- 确认/拒绝流（确定性按钮，零 LLM 分类成本）
- 对话滑动窗口（5 轮 = 10 条消息）
- FTS5 对话档案持久化
"""
import os
import re
import json
import logging
import threading
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
    intent: str              # "chat" | "plan" | "code" | "scan" | "action"
    reply: str               # PM 的自然语言回复
    plan_md: Optional[str] = None     # plan.md（plan 意图时返回）
    actions: Optional[list] = None    # 确定性按钮 [{"id": "confirm", ...}]
    is_executing: bool = False        # 是否已触发 Engine 执行


class PMAgent:
    """
    项目经理 Agent — 用户的唯一对话窗口。

    职责：意图路由 + 需求标准化 + 规划组协调 + 闲聊
    不做：写代码、审查代码、直接操控 Engine
    """

    def __init__(self, project_id: str):
        self.project_id = project_id
        self.model = os.getenv("MODEL_PM", "deepseek-chat")

        # 对话滑动窗口（内存态，MVP 不持久化窗口本身）
        self.conversation: List[dict] = []

        # 待确认的规划（等待用户 confirm/reject）
        self.pending_req: Optional[dict] = None
        self.pending_plan_md: Optional[str] = None

        # 状态机
        self.state: str = "idle"  # "idle" | "wait_confirm"

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
                logger.warning(f"⚠️ ConversationStore 初始化失败: {e}")
        return self._store

    # ============================================================
    # 主入口
    # ============================================================

    def chat(self, user_message: str) -> PMResponse:
        """
        主入口：接收用户消息，返回结构化响应。

        流程：追加历史 → 意图分类 → 分发处理 → 截断窗口 → 持久化 → 返回
        """
        self.round_id += 1
        logger.info(f"💬 PM 收到消息 (round={self.round_id}): {user_message[:80]}...")

        # 追加用户消息到窗口
        self.conversation.append({"role": "user", "content": user_message})

        # 如果在等待确认状态，用户的自由文本视为修改需求
        if self.state == "wait_confirm":
            response = self._handle_plan_revision(user_message)
        else:
            # 意图分类
            intent = self._classify_intent(user_message)
            logger.info(f"🔍 意图分类结果: {intent}")

            # 分发处理
            if intent == "chat":
                response = self._handle_chat(user_message)
            elif intent == "plan":
                response = self._handle_plan(user_message)
            elif intent == "code":
                response = self._handle_code(user_message)
            elif intent == "scan":
                response = self._handle_scan(user_message)
            else:
                response = self._handle_chat(user_message)

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
                logger.warning(f"⚠️ FTS5 写入失败: {e}")

        return response

    def handle_action(self, action: str) -> PMResponse:
        """
        处理确定性按钮动作（零 LLM 分类成本）。

        Args:
            action: "confirm" | "reject"
        """
        logger.info(f"🔘 PM 收到按钮动作: {action}")

        if action == "confirm":
            return self._handle_confirm()
        elif action == "reject":
            return self._handle_reject()
        else:
            return PMResponse(
                intent="action",
                reply=f"未知操作: {action}"
            )

    # ============================================================
    # 意图分类（二层路由）
    # ============================================================

    def _classify_intent(self, message: str) -> str:
        """
        二层路由：硬路由（正则匹配，零 LLM）→ 软路由（LLM 分类，~100 tokens）
        """
        msg = message.strip().lower()

        # --- 层 1: 硬路由（正则匹配）---
        chat_patterns = [r'^/chat\b', r'^你好', r'^hi\b', r'^hello\b', r'^聊聊']
        plan_patterns = [r'^/plan\b', r'帮我做', r'帮我设计', r'帮我规划',
                         r'帮我开发', r'做一个', r'开发一个', r'创建一个', r'写一个']
        code_patterns = [r'^/code\b', r'^开始写', r'^执行', r'^开始编码']
        scan_patterns = [r'^/scan\b', r'扫描', r'分析项目', r'逆向', r'接手']

        # 检查历史上下文中是否有追问（如"之前"、"上次"）
        archive_patterns = [r'之前', r'上次', r'历史', r'说过什么']

        for p in chat_patterns:
            if re.search(p, msg):
                return "chat"
        for p in plan_patterns:
            if re.search(p, msg):
                return "plan"
        for p in code_patterns:
            if re.search(p, msg):
                return "code"
        for p in scan_patterns:
            if re.search(p, msg):
                return "scan"

        # 检查是否是档案检索请求
        for p in archive_patterns:
            if re.search(p, msg):
                return "chat"  # 通过 chat 处理，内部会触发档案搜索

        # --- 层 2: 软路由（LLM 分类）---
        try:
            prompt = Prompts.PM_INTENT_CLASSIFIER.format(message=message)
            response = default_llm.chat_completion(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.0,
            )
            result = response.content.strip().lower()
            if result in ("chat", "plan", "code", "scan"):
                return result
            logger.warning(f"⚠️ LLM 意图分类返回非法值: {result}，降级为 chat")
            return "chat"
        except Exception as e:
            logger.warning(f"⚠️ LLM 意图分类失败: {e}，降级为 chat")
            return "chat"

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
            logger.info(f"✅ 需求标准化完成: {req.get('summary', '未知')}")
            return req

        except (json.JSONDecodeError, Exception) as e:
            logger.error(f"❌ 需求标准化失败: {e}")
            # 降级：从原始消息提取关键信息
            first_line = user_message.split('\n')[0].strip()[:100]
            # 尝试从编号列表中提取功能点
            features = []
            for line in user_message.split('\n'):
                line = line.strip()
                if re.match(r'^[\d]+[.、]', line):
                    features.append(line[:80])
            if not features:
                features = [first_line]

            # 检测用户消息中是否提及技术栈
            tech = {"database": "sqlite", "frontend": "jinja2_ssr", "backend": "flask"}
            defaults = []
            msg_lower = user_message.lower()
            if "sqlalchemy" in msg_lower or "sqlite" in msg_lower:
                tech["database"] = "sqlite"
            else:
                defaults.append({"field": "数据库", "value": "SQLite", "reason": "用户未指定"})
            if "flask" in msg_lower:
                tech["backend"] = "flask"
            else:
                defaults.append({"field": "后端", "value": "Flask", "reason": "用户未指定"})
            if "jinja" in msg_lower:
                tech["frontend"] = "jinja2_ssr"
            else:
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
        """闲聊处理 — PM 直接回复（注入项目上下文）"""
        # 检查是否是档案检索请求
        archive_keywords = ["之前", "上次", "历史", "说过什么", "说过"]
        if any(kw in message for kw in archive_keywords):
            archive_result = self._search_archive(message)
            if archive_result:
                return PMResponse(intent="chat", reply=archive_result)

        project_context = self._build_project_context()
        system_prompt = Prompts.PM_SYSTEM.format(project_context=project_context)

        try:
            messages = [{"role": "system", "content": system_prompt}]
            # 注入滑动窗口历史
            messages.extend(self.conversation[-6:])

            response = default_llm.chat_completion(
                model=self.model,
                messages=messages,
                temperature=0.7,
            )
            return PMResponse(intent="chat", reply=response.content.strip())
        except Exception as e:
            logger.error(f"❌ PM 闲聊失败: {e}")
            return PMResponse(intent="chat", reply="抱歉，我暂时无法回复，请稍后再试。")

    def _handle_plan(self, message: str) -> PMResponse:
        """规划处理 — 标准化需求 → 规划组生成 plan.md → 返回给用户确认"""
        global_broadcaster.emit_sync("PM", "info", "📋 正在分析您的需求...")

        # 1. 标准化需求
        structured_req = self._standardize_requirement(message)

        # 2. 调用规划组生成 plan.md
        from agents.planner_lite import PlannerLiteAgent
        planner = PlannerLiteAgent()
        plan_md = planner.generate_plan(structured_req)

        # 3. 保存待确认状态
        self.pending_req = structured_req
        self.pending_plan_md = plan_md
        self.state = "wait_confirm"

        # 4. 构建回复（包含默认值告知）
        defaults = structured_req.get("defaults_applied", [])
        defaults_notice = ""
        if defaults:
            notices = [f"- {d['field']}：{d['value']}（{d['reason']}）" for d in defaults]
            defaults_notice = "\n\n⚠️ 以下选项使用了默认值，如需调整请告诉我：\n" + "\n".join(notices)

        reply = f"我为您设计了以下技术方案，请确认是否开始开发：{defaults_notice}"

        global_broadcaster.emit_sync("PM", "plan_preview", "📋 技术方案预览已就绪")

        return PMResponse(
            intent="plan",
            reply=reply,
            plan_md=plan_md,
            actions=[
                {"id": "confirm", "label": "✅ 确认执行", "style": "primary"},
                {"id": "reject", "label": "❌ 修改需求", "style": "secondary"},
            ]
        )

    def _handle_plan_revision(self, message: str) -> PMResponse:
        """用户在 wait_confirm 状态下发送了修改意见 → 重新标准化 + 重新生成 plan"""
        global_broadcaster.emit_sync("PM", "info", "📝 正在根据您的反馈调整方案...")

        # 基于上一轮的 structured_req 做增量修改
        structured_req = self._standardize_requirement(message)

        # 重新调用规划组
        from agents.planner_lite import PlannerLiteAgent
        planner = PlannerLiteAgent()
        plan_md = planner.generate_plan(structured_req)

        # 更新待确认状态
        self.pending_req = structured_req
        self.pending_plan_md = plan_md

        defaults = structured_req.get("defaults_applied", [])
        defaults_notice = ""
        if defaults:
            notices = [f"- {d['field']}：{d['value']}（{d['reason']}）" for d in defaults]
            defaults_notice = "\n\n⚠️ 默认值：\n" + "\n".join(notices)

        reply = f"已根据您的反馈更新了方案，请再次确认：{defaults_notice}"

        return PMResponse(
            intent="plan",
            reply=reply,
            plan_md=plan_md,
            actions=[
                {"id": "confirm", "label": "✅ 确认执行", "style": "primary"},
                {"id": "reject", "label": "❌ 继续修改", "style": "secondary"},
            ]
        )

    def _handle_code(self, message: str) -> PMResponse:
        """编码处理 — 检查有无已确认 plan，有则触发 Engine"""
        if self.pending_req and self.state == "wait_confirm":
            # 用户直接说"执行"/"开始写" → 等同于 confirm
            return self._handle_confirm()

        # 没有 pending plan → 提示先走 plan
        return PMResponse(
            intent="code",
            reply="还没有待执行的方案哦。请先告诉我您想做什么项目，我来帮您规划。"
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
                    reply="该项目目录下没有找到可分析的源代码文件。"
                )

            # 格式化扫描结果摘要
            summary_parts = [
                f"📂 扫描完成：发现 {scan_result.get('files_found', 0)} 个文件",
                f"🔧 技术栈：{', '.join(scan_result.get('tech_stack', ['未识别']))}",
            ]
            if scan_result.get("entry_point"):
                ep = scan_result["entry_point"]
                summary_parts.append(f"🚀 入口：{ep.get('file', '未知')} (端口 {ep.get('port', '未知')})")

            return PMResponse(
                intent="scan",
                reply="\n".join(summary_parts),
            )
        except Exception as e:
            logger.error(f"❌ 扫描失败: {e}")
            return PMResponse(intent="scan", reply=f"扫描过程中发生错误：{str(e)}")

    # ============================================================
    # 确认/拒绝处理
    # ============================================================

    def _handle_confirm(self) -> PMResponse:
        """用户确认 → 保存需求文本 → 通知 server.py 启动 Engine"""
        if not self.pending_req:
            return PMResponse(intent="action", reply="没有待确认的方案。")

        summary = self.pending_req.get("summary", "用户项目")

        # 保存需求文本（server.py 会读取这个值启动 Engine）
        self.confirmed_requirement = self._structured_req_to_prompt(self.pending_req)

        # 清空待确认状态
        self.pending_req = None
        self.pending_plan_md = None
        self.state = "idle"

        global_broadcaster.emit_sync("PM", "info", f"✅ 用户已确认方案，正在启动开发团队...")

        return PMResponse(
            intent="action",
            reply=f"收到！正在为「{summary}」启动开发团队，请在左侧面板关注进度。",
            is_executing=True,
        )

    def _handle_reject(self) -> PMResponse:
        """用户拒绝 → PM 追问，保持 wait_confirm 状态"""
        # 状态不变，仍在 wait_confirm，等待用户发修改意见
        return PMResponse(
            intent="action",
            reply="了解，您希望怎么调整？比如换技术栈、增减功能、改文件结构？"
        )

    # ============================================================
    # 辅助方法
    # ============================================================

    def _build_project_context(self) -> str:
        """构建当前项目的上下文信息"""
        projects_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "projects"
        )
        project_dir = os.path.join(projects_dir, self.project_id)

        parts = [f"项目 ID: {self.project_id}"]

        if os.path.isdir(project_dir):
            # 简单统计文件数
            file_count = 0
            for root, dirs, files in os.walk(project_dir):
                dirs[:] = [d for d in dirs if d not in
                           ('__pycache__', 'venv', '.venv', 'node_modules', '.sandbox', '.astrea', '.git')]
                file_count += len([f for f in files if not f.startswith('.')])
            parts.append(f"文件数: {file_count}")
        else:
            parts.append("状态: 新项目（尚无文件）")

        return " | ".join(parts)

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

        # 提取搜索关键词（去掉"之前"、"上次"等前缀词）
        clean_query = query
        for prefix in ["之前说过", "之前", "上次", "历史", "说过什么", "说过"]:
            clean_query = clean_query.replace(prefix, "").strip()

        if not clean_query:
            clean_query = query  # fallback 用原文

        try:
            results = store.search(clean_query, limit=5)
            if not results:
                return "没有找到相关的历史对话记录。"

            lines = ["📜 找到以下相关历史记录：", ""]
            for r in results:
                role_label = "👤 用户" if r["role"] == "user" else "🤖 PM"
                lines.append(f"**{role_label}** (轮次 {r['round_id']}):")
                lines.append(f"> {r['content'][:200]}")
                lines.append("")

            return "\n".join(lines)
        except Exception as e:
            logger.warning(f"⚠️ 档案检索失败: {e}")
            return None
