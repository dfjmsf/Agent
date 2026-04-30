import os
import re
import json
import copy
import time
import logging
import threading
from typing import List, Dict, Any, Optional, Tuple
from core.llm_client import default_llm
from core.prompt import Prompts
from core.ws_broadcaster import global_broadcaster
from agents.coder import CoderAgent
from agents.reviewer import ReviewerAgent
from core.spec_compiler import compile_spec
from core.spec_validator import (
    normalize_spec, validate_spec, format_warnings_for_llm, has_blocking_warnings
)
from core.database import (
    append_event, get_recent_events, rename_project_events,
    recall, upsert_file_tree, delete_events_by_type,
    create_project_meta, update_project_status, rename_project_meta,
    insert_trajectory, finalize_trajectory,
    get_recalled_memory_union, settle_memory_scores,
    get_global_round, tick_global_round,
)
from agents.synthesizer import SynthesizerAgent
from agents.auditor import AuditorAgent

logger = logging.getLogger("ManagerAgent")

MAX_RETRIES = int(os.getenv("MAX_RETRIES", 5))

class ManagerAgent:
    """
    主控 Agent (Manager)
    专职：需求拆解、任务派发、循环调度、熔断控制。
    """
    def __init__(self, project_id: str = "default_project"):
        self.model = os.getenv("MODEL_PLANNER", "qwen3-max")
        _et, _re = default_llm.parse_thinking_config(os.getenv("THINKING_PLANNER", "false"))
        self.enable_thinking = _et
        self._reasoning_effort = _re
        self.llm_client = default_llm
        self.project_id = project_id
        self.coder = CoderAgent(project_id)
        self.reviewer = ReviewerAgent(project_id)
        self.last_raw_spec: Optional[dict] = None
        self.last_compiled_spec: Optional[dict] = None
        self.last_spec_warnings: List[Any] = []
        self.last_spec_parse_error: Optional[str] = None
        self.last_spec_raw_response: str = ""

    def _reset_spec_generation_state(self):
        self.last_raw_spec = None
        self.last_compiled_spec = None
        self.last_spec_warnings = []
        self.last_spec_parse_error = None
        self.last_spec_raw_response = ""

    def has_spec_parse_failure(self) -> bool:
        return bool(self.last_spec_parse_error)

    @staticmethod
    def _route_module_contract_prompt() -> str:
        return (
            "\n\n【路由模块实现契约】\n"
            "对每个 routes.py / routes/*.py 模块，必须显式规划一种实现范式：\n"
            "1. `direct_blueprint`: 顶层直接声明 blueprint 与 `@bp.route(...)` handler。\n"
            "2. `init_function`: 文件内存在 `init_*_routes(...)` / `register_*_routes(...)` 作为挂载 helper。\n"
            "禁止混用两种范式。\n"
            "如果 app 侧会通过 `url_prefix` 挂载 blueprint，则 route 文件中的 local path 必须是相对路径，"
            "禁止重复写完整 `/api/...` 前缀。\n"
            "允许额外输出 `route_module_contracts` 字段，按模块声明 `mode/blueprints/helper_functions/url_prefix_hint`。\n"
            "不允许只输出 `init_*_routes(...)` 而不提供对应的实际 route_contracts。\n"
        )

    @staticmethod
    def _architecture_contract_prompt() -> str:
        return (
            "\n\n銆愬悗绔崟涓€鏋舵瀯濂戠害銆慭n"
            "蹇呴』鏄惧紡杈撳嚭 `architecture_contract` 瀛楁锛屽苟鍙兘閫夋嫨涓€濂楀悗绔寖寮忥紝"
            "绂佹娣风敤 FastAPI / Flask 鍙婂叾瀵瑰簲鐨?db / auth / router 璇箟銆俓n"
            "`architecture_contract` 至少包含锛歕n"
            "1. `backend_framework`: `fastapi` 鎴?`flask`\n"
            "2. `orm_mode`: `sqlalchemy_session` 鎴?`flask_sqlalchemy`\n"
            "3. `auth_mode`: `jwt_header` / `flask_login_session` / `none`\n"
            "4. `router_mode`: `fastapi_apirouter` 鎴?`flask_blueprint`\n"
            "5. `entrypoint_mode`: `uvicorn_app` 鎴?`flask_app_factory`\n"
            "6. `package_layout`: `flat_modules` 鎴?`package_src`\n"
            "7. `import_style`: `sibling_import` 鎴?`package_import`\n"
            "濡傛灉閫夋嫨 `fastapi`锛屽垯涓嶅緱鍑虹幇 `Blueprint`銆乣Flask-Login`銆乣Flask-SQLAlchemy` 绛夋绱犮€俓n"
            "濡傛灉閫夋嫨 `flask`锛屽垯涓嶅緱鍑虹幇 `APIRouter`銆乣Depends(get_db)`銆乣FastAPI` 绛夋绱犮€俓n"
            "濡傛灉 `package_layout=flat_modules`锛屽垯涓嶅緱浣跨敤 `from src.xxx import ...` 杩欑被鍖呭紡瀵煎叆銆俓n"
        )

    def _generate_project_spec(self, user_requirement: str, plan_md: str = None,
                               playbook_hint: str = "") -> dict:
        """
        步骤 1: 生成或增量更新项目规划书 (Project Spec)。
        - 新项目: 全量生成
        - 已有规划书: 注入旧 spec，让 LLM 判断是否需要修改（允许原样输出）
        - plan_md: 用户确认过的 plan.md，作为 P0.5 合同约束
        - playbook_hint: Playbook 铁律摘要，防止规划出被禁止的技术栈
        """
        logger.info("📋 Manager 正在生成/更新项目规划书...")
        self._reset_spec_generation_state()
        
        # 检查是否已有规划书
        existing_spec_events = get_recent_events(
            project_id=self.project_id, limit=1,
            event_types=["project_spec"], caller="Manager/Spec"
        )
        
        if existing_spec_events:
            # 已有规划书 → 增量更新（全量覆写模式，prompt 提供"可以不改"选项）
            old_spec_content = existing_spec_events[0].content
            system_prompt = Prompts.MANAGER_SPEC_UPDATE_SYSTEM.format(existing_spec=old_spec_content)
            logger.info("📋 检测到已有规划书，进入增量更新模式")
        else:
            # 新项目 → 全量生成
            system_prompt = Prompts.MANAGER_SPEC_SYSTEM
            logger.info("📋 新项目，全量生成规划书")
        
        # P2: Playbook 铁律注入（防止规划出被禁止的技术栈）
        if playbook_hint:
            system_prompt += (
                "\n\n═══ P2 — 编码铁律（规划时必须遵守，违反将导致后续构建熔断）═══\n"
                f"{playbook_hint}\n"
                "【重要】你在规划 module_interfaces 时，不得使用上述被禁止的包/类/函数！\n"
            )
            logger.info(f"📜 Playbook 铁律已注入 Manager Spec Prompt")

        # P0.5 合同注入：plan.md 约束高于一切
        if plan_md:
            contract_clause = (
                "\n\n═══ P0.5 — 用户确认的项目方案（合同级约束，高于一切 P1/P2 规则）═══\n"
                "以下是用户审核并确认过的项目方案（plan.md），你必须严格遵循：\n\n"
                f"{plan_md}\n\n"
                "【铁律】\n"
                "1. 技术栈必须与 plan.md 完全一致，不得擅自替换\n"
                "2. 核心功能必须与 plan.md 完全一致，不得擅自增减\n"
                "3. 你可以在此基础上补充工程细节（如 module_interfaces、api_contracts），但不得违反上述约束\n"
            )
            system_prompt = contract_clause + "\n" + system_prompt
            logger.info("📜 plan.md 合同已注入 Manager Spec Prompt")
        
        user_prompt = f"主人的开发需求：\n{user_requirement}\n请输出项目规划书 JSON。"
        
        system_prompt += self._route_module_contract_prompt()
        system_prompt += self._architecture_contract_prompt()

        try:
            raw_response = self.llm_client.chat_completion(
                enable_thinking=self.enable_thinking,
                reasoning_effort=self._reasoning_effort,
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]
            )
            json_str = raw_response.content
            self.last_spec_raw_response = json_str
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0].strip()
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0].strip()
            
            spec = json.loads(json_str)
            self.last_raw_spec = copy.deepcopy(spec)

            # ===== M-1e: 合同自检 =====
            spec = self._validate_and_fix_spec(spec, system_prompt)

            spec_text = json.dumps(spec, ensure_ascii=False, indent=2)
            
            # upsert 语义：删旧写新（通过 database API，不直接操作 ORM）
            delete_events_by_type(self.project_id, "project_spec")
            
            append_event("manager", "project_spec", spec_text, project_id=self.project_id)
            
            logger.info(f"✅ 项目规划书生成完毕 ({len(spec_text)} bytes)")
            global_broadcaster.emit_sync("Manager", "project_spec_ready", "项目规划书已就绪", {"spec": spec})
            
            return spec
            
        except json.JSONDecodeError as e:
            logger.error(f"规划书 JSON 解析失败: {e}")
            self.last_spec_parse_error = str(e)
            return {}
        except Exception as e:
            import traceback
            logger.error(f"规划书生成异常: {e}\n{traceback.format_exc()}")
            self.last_spec_parse_error = str(e)
            return {}

    def _generate_spec_from_scan(self, scan_result: dict) -> dict:
        """
        Phase 1.3: 从 ProjectScanner 的确定性扫描结果生成 project_spec。
        LLM 只负责填空 10%：module_graph, naming_conventions, key_decisions。
        """
        logger.info("📋 Manager 正在从扫描结果合成项目规划书...")
        self._reset_spec_generation_state()
        global_broadcaster.emit_sync("Manager", "spec_from_scan_start",
            "🔍 正在从扫描结果合成项目规划书...")

        # 构造扫描摘要（控制 Token，不注入全部骨架）
        scan_summary_parts = []
        scan_summary_parts.append(f"技术栈: {', '.join(scan_result.get('tech_stack', []))}")
        scan_summary_parts.append(f"文件列表: {', '.join(scan_result.get('files', []))}")

        entry = scan_result.get("entry", {})
        if entry.get("file"):
            scan_summary_parts.append(
                f"入口文件: {entry['file']} (端口: {entry.get('port', '未知')}, "
                f"框架: {entry.get('framework', '未知')})"
            )

        # 路由
        routes = scan_result.get("routes", [])
        if routes:
            routes_text = "\n".join(
                f"  {r['method']} {r['path']} → {r.get('function', '?')} ({r.get('file', '?')})"
                for r in routes
            )
            scan_summary_parts.append(f"路由:\n{routes_text}")
        else:
            scan_summary_parts.append("路由: 未通过装饰器检测到（可能使用 add_url_rule 或其他模式，请从骨架推断）")

        # 模型
        models = scan_result.get("models", [])
        if models:
            models_text = "\n".join(
                f"  {m['name']} ({m.get('file', '?')}): {', '.join(m.get('fields', []))}"
                + (f" [表: {m['table']}]" if m.get('table') else "")
                for m in models
            )
            scan_summary_parts.append(f"数据模型:\n{models_text}")

        # 骨架（只注入关键骨架，控制 Token）
        skeletons = scan_result.get("skeletons", {})
        if skeletons:
            skeleton_text = "\n\n".join(
                f"--- {path} ---\n{skel[:800]}" + ("..." if len(skel) > 800 else "")
                for path, skel in list(skeletons.items())[:10]  # 最多 10 个文件
            )
            scan_summary_parts.append(f"代码骨架:\n{skeleton_text}")

        scan_summary = "\n\n".join(scan_summary_parts)

        # 关键文件全文
        key_files = scan_result.get("key_files_code", {})
        key_files_text = "\n\n".join(
            f"=== {path} ===\n{code}"
            for path, code in key_files.items()
        ) if key_files else "无关键文件全文"

        system_prompt = Prompts.MANAGER_SPEC_FROM_SCAN_SYSTEM.format(
            scan_summary=scan_summary,
            key_files_code=key_files_text,
        )
        system_prompt += self._route_module_contract_prompt()
        system_prompt += self._architecture_contract_prompt()
        user_prompt = "请根据扫描结果生成项目规划书 JSON。"

        try:
            raw_response = self.llm_client.chat_completion(
                enable_thinking=self.enable_thinking,
                reasoning_effort=self._reasoning_effort,
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]
            )
            json_str = raw_response.content
            self.last_spec_raw_response = json_str
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0].strip()
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0].strip()

            spec = json.loads(json_str)
            self.last_raw_spec = copy.deepcopy(spec)

            # ===== M-1e: 合同自检（逆向规划书同样校验） =====
            spec = self._validate_and_fix_spec(spec, system_prompt)

            logger.info(f"✅ 逆向规划书合成完毕: {spec.get('project_name', '?')}")
            global_broadcaster.emit_sync("Manager", "spec_from_scan_done",
                f"✅ 逆向规划书合成完毕: {spec.get('project_name', '?')}", {"spec": spec})
            return spec

        except json.JSONDecodeError as e:
            logger.error(f"逆向规划书 JSON 解析失败: {e}")
            self.last_spec_parse_error = str(e)
            return {}
        except Exception as e:
            logger.error(f"逆向规划书合成异常: {e}")
            self.last_spec_parse_error = str(e)
            return {}

    def _validate_and_fix_spec(self, spec: dict, original_system_prompt: str = "") -> dict:
        """
        M-1e: 合同自检 + 可选的单轮 LLM 修正。

        流程：
        1. 运行确定性校验器 validate_spec()
        2. 如果有 warning/error → 通知前端 → LLM 尝试修正一轮
        3. 如果无问题 → 直接返回原 spec
        """
        try:
            spec = normalize_spec(spec)
            spec = compile_spec(spec)
        except Exception as e:
            import traceback
            logger.error(f"⚠️ compile_spec 异常（降级跳过编译）: {e}\n{traceback.format_exc()}")
            # 降级：跳过编译，使用原始 spec 继续

        try:
            warnings = validate_spec(spec)
        except Exception as e:
            import traceback
            logger.error(f"⚠️ validate_spec 异常（降级跳过校验）: {e}\n{traceback.format_exc()}")
            warnings = []

        self.last_compiled_spec = copy.deepcopy(spec)
        self.last_spec_warnings = list(warnings)

        if not warnings:
            global_broadcaster.emit_sync("Manager", "spec_validated",
                "✅ 规划书合同自检通过，零矛盾")
            return spec

        # 通知前端有矛盾
        logger.warning(f"⚠️ [SpecValidator] 检出 {len(warnings)} 条合同矛盾，已记录供后续规避（跳过 LLM 修正）")
        global_broadcaster.emit_sync("Manager", "spec_validation_warning",
            f"⚠️ 规划书检出 {len(warnings)} 条合同矛盾，已记录供后续规避",
            {"warnings": [repr(w) for w in warnings]})

        return spec
    def has_blocking_spec_validation(self) -> bool:
        return has_blocking_warnings(self.last_spec_warnings)

    def plan_tasks(self, user_requirement: str, project_spec: dict = None,
                   manager_playbook: str = "", complex_files_hint: str = "",
                   plan_md: str = None, phase_constraint: dict = None) -> dict:
        """
        步骤 2: 基于规划书拆解任务列表。
        plan_md: 用户确认过的 plan.md，作为 P0.5 合同约束
        phase_constraint: Phase 模式下的范围约束 {index, name, features, scope_type, ...}
        """
        logger.info("🧠 Manager 正在基于规划书拆解任务...")
        
        # 1. 查询短期记忆 — 只读 prompt 类事件（精简：不注入 TDD 噪音）
        recent_events = get_recent_events(
            project_id=self.project_id, limit=5,
            event_types=["prompt"], caller="Manager"
        )
        history_str = "\n".join([f"[用户需求]: {e.content[:150]}..." for e in recent_events]) if recent_events else "无近期对话。"
        
        # 2. 查询长期经验记忆 (RAG)
        past_experience = recall(user_requirement, n_results=3, project_id=self.project_id, caller="Manager")
        experience_str = "\n".join([e["content"] for e in past_experience]) if past_experience else "无相关历史经验。"

        # 3. 构造环境变量声明
        is_new_project = "新建项目" in self.project_id or "new_project" in self.project_id or self.project_id == "default_project"
        if is_new_project:
            env_context = "【项目环境】\n这是全新项目。项目名称已在规划书中确定。请专注于拆解任务列表，JSON 中的 `project_name` 使用规划书中的名称即可。"
        else:
            # v1.3: 直接从项目目录读取文件列表（不依赖 StateManager VFS）
            existing_files = []
            base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "projects", self.project_id))
            if os.path.isdir(base_dir):
                ignore = {'.sandbox', '.git', '__pycache__', '.venv', 'node_modules', '.idea'}
                for root, dirs, files in os.walk(base_dir):
                    dirs[:] = [d for d in dirs if d not in ignore]
                    for f in files:
                        rel = os.path.relpath(os.path.join(root, f), base_dir).replace("\\", "/")
                        existing_files.append(rel)
            file_tree = "\n".join([f"- {f}" for f in existing_files]) if existing_files else "目录暂空。"
            env_context = (
                f"【项目环境】\n当前项目宇宙已永久命名并固化为: `{self.project_id}`\n"
                f"你 MUST 且只能将 JSON 里的 `project_name` 固定为 `{self.project_id}`，绝对不允许修改项目名！！！\n"
                f"【已有文件架构】:\n{file_tree}\n"
                f"这是当前项目内存里的所有文件，你可以在此基础上规划对旧文件的修改（覆盖），或者指派增加全新的子模块文件。"
            )

        # 4. 注入规划书（如果有）
        spec_context = ""
        if project_spec:
            spec_str = json.dumps(project_spec, ensure_ascii=False, indent=2)
            spec_context = f"\n\n═══ P1 — 项目契约（覆盖一切 P2 指南，冲突时以此为准）═══\n【项目规划书 — 你必须基于此架构拆解任务，禁止 Playbook 模板覆盖规划书的文件结构】\n{spec_str}"

        # 4.5 注入 plan.md 合同（P0.5，高于 P1）
        plan_md_context = ""
        if plan_md:
            plan_md_context = (
                "\n\n═══ P0.5 — 用户确认的项目方案（合同级约束，高于一切 P1/P2 规则）═══\n"
                f"{plan_md}\n\n"
                "【铁律】技术栈和核心功能必须与上述 plan.md 完全一致，不得擅自替换或增减。\n"
            )
            logger.info("📜 plan.md 合同已注入 Manager Plan Prompt")

        # 4.6 构建 Phase 约束文本（P0 级，最高优先）
        phase_constraint_text = ""
        if phase_constraint:
            p_name = phase_constraint.get("name", "")
            p_scope = phase_constraint.get("scope_type", "fullstack")
            p_features = phase_constraint.get("features", [])
            p_completed = phase_constraint.get("completed_phases", [])
            scope_file_hint = {
                "backend": "只允许创建后端相关文件（.py 服务/模型/路由 + .html 模板文件如 Jinja2）。严禁创建纯前端文件（.jsx/.tsx/.vue/.svelte/.css/.js 等独立前端组件）。",
                "frontend": "只允许创建前端文件（.html/.js/.css/.jsx/.tsx/.vue 等）。后端 .py 文件已在前序阶段完成，只允许 weld 修改入口文件以挂载前端路由。",
                "fullstack": "本阶段同时涉及前后端，无文件类型限制。",
            }
            phase_constraint_text = (
                f"\n\n═══ P0 — Phase 范围引导（高优先级）═══\n"
                f"当前正在执行 Phase {phase_constraint.get('index', '?')}: {p_name}\n"
                f"阶段范围: {p_scope}\n"
                f"{scope_file_hint.get(p_scope, scope_file_hint['fullstack'])}\n\n"
                f"本阶段只允许规划以下功能范围内的文件：\n"
                + "\n".join(f"  - {feat}" for feat in p_features)
                + f"\n\n⚠️ 范围提示：\n"
                f"1. 优先规划属于「{p_name}」范围的文件\n"
                f"2. 其他阶段的独立功能可暂不规划，但共用的配置文件和模板可以包含\n"
                f"3. 确保输出的 tasks 能让本阶段的功能独立运行\n"
            )
            if p_completed:
                phase_constraint_text += (
                    f"\n已完成的阶段: {', '.join(p_completed)}\n"
                    f"这些阶段的产出文件已存在，如需引用可在 dependencies 中声明\n"
                )
            logger.info(f"📋 Phase P0 约束已注入 Prompt: {p_name}[{p_scope}]")

        # 5. 组装 system_prompt（按优先级排列：P0 Phase > P0.5 plan.md > 规则 > P1 规划书 > 环境 > P2 经验）
        manager_system = Prompts.MANAGER_SYSTEM.format(
            manager_playbook=manager_playbook,
            complex_files_hint=complex_files_hint or ""
        )
        system_prompt = (
            phase_constraint_text  # P0: Phase 硬约束最高优先
            + plan_md_context  # P0.5: plan.md 合同
            + manager_system
            + spec_context  # P1: 规划书紧跟规则之后
            + f"\n\n{env_context}"  # 环境信息
            + f"\n\n═══ P2 — 参考信息（仅供参考，与 P1 规划书冲突时必须服从 P1）═══"
            + f"\n【近期用户需求历史】\n{history_str}"
            + f"\n\n【RAG 检索到的过往经验】\n{experience_str}"
        )
        user_prompt = f"主人的开发需求：\n{user_requirement}\n请严格按照 JSON Schema 输出。"
        
        try:
            # Planner 不使用任何工具，只输出纯文本 JSON
            raw_response = self.llm_client.chat_completion(
                enable_thinking=self.enable_thinking,
                reasoning_effort=self._reasoning_effort,
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]
            )
            # 极简清洗，提取 JSON (防止大模型带了 Markdown 代码块)
            json_str = raw_response.content
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0].strip()
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0].strip()
                
            plan = json.loads(json_str)
            
            # 确保默认存在的健壮性
            if "project_name" not in plan:
                plan["project_name"] = "AutoGen_Project"
            if "tasks" not in plan:
                plan["tasks"] = []
            
            # 去重：防止 LLM 将同一文件拆成多个 task
            seen_files = set()
            deduped_tasks = []
            for task in plan["tasks"]:
                tf = task.get("target_file", "")
                if tf not in seen_files:
                    seen_files.add(tf)
                    deduped_tasks.append(task)
                else:
                    logger.warning(f"⚠️ 去重: 跳过重复文件 task: {tf}")
            if len(deduped_tasks) < len(plan["tasks"]):
                logger.warning(f"⚠️ Manager 去重: {len(plan['tasks'])} → {len(deduped_tasks)} 个 task")
            plan["tasks"] = deduped_tasks

            # Phase 后置过滤：确定性剔除不属于当前阶段的文件
            if phase_constraint:
                plan["tasks"] = self._filter_tasks_by_phase(plan["tasks"], phase_constraint)

            logger.info(f"✅ 任务拆解完成: {plan.get('project_name')}")
            global_broadcaster.emit_sync("Manager", "plan_ready", f"成功拆解任务: {plan.get('project_name')}", {"plan": plan})
            
            return plan
            
        except json.JSONDecodeError as e:
            logger.error(f"Manager 返回的并不是标准的 JSON 格式：\n{raw_response}")
            return {"project_name": "Error_Project", "architecture_summary": "解析失败", "tasks": []}
        except Exception as e:
            logger.error(f"Manager 拆解任务时发生异常: {e}")
            return {"project_name": "Error_Project", "architecture_summary": "API异常", "tasks": []}


    # ============================================================
    # Phase 感知：确定性任务过滤
    # ============================================================

    def _filter_tasks_by_phase(self, tasks: list, phase_constraint: dict) -> list:
        """确定性过滤：根据 Phase 的 scope_type 剔除越界 task。

        规则：
        - backend: 剔除前端文件（.html/.js/.css/.jsx/.tsx/.vue/.svelte）
        - frontend: 剔除纯后端文件（.py），但保留入口文件的 weld 修改
        - fullstack: 不过滤
        """
        scope_type = phase_constraint.get('scope_type', 'fullstack')
        if scope_type == 'fullstack':
            return tasks

        backend_exts = {'.py'}
        # .html 不在此列表：Flask/Jinja2 SSR 中模板属于后端
        frontend_exts = {'.css', '.js', '.jsx', '.ts', '.tsx', '.vue', '.svelte', '.scss', '.less'}
        config_exts = {'.txt', '.toml', '.cfg', '.ini', '.json', '.yaml', '.yml', '.env', '.md'}

        filtered = []
        for task in tasks:
            target = task.get('target_file', '')
            ext = os.path.splitext(target)[1].lower()

            # 配置文件永远放行
            if ext in config_exts or not ext:
                filtered.append(task)
                continue

            if scope_type == 'backend' and ext in frontend_exts:
                logger.warning(f'\u26a0\ufe0f [Phase 过滤] 剔除前端文件: {target}')
                global_broadcaster.emit_sync(
                    'Manager', 'phase_filter',
                    f'\u26a0\ufe0f Phase 过滤: 剔除非本阶段文件 {target}'
                )
                continue
            if scope_type == 'frontend' and ext in backend_exts:
                # 前端阶段允许 weld 入口文件（挂载前端路由）
                if task.get('task_type') == 'weld':
                    filtered.append(task)
                    continue
                logger.warning(f'\u26a0\ufe0f [Phase 过滤] 剔除后端文件: {target}')
                global_broadcaster.emit_sync(
                    'Manager', 'phase_filter',
                    f'\u26a0\ufe0f Phase 过滤: 剔除非本阶段文件 {target}'
                )
                continue
            filtered.append(task)

        if len(filtered) < len(tasks):
            logger.info(f'\U0001f50d [Phase 过滤] {len(tasks)} \u2192 {len(filtered)} 个 task (scope={scope_type})')
            global_broadcaster.emit_sync(
                'Manager', 'phase_filter_summary',
                f'\U0001f50d Phase 过滤: {len(tasks)} \u2192 {len(filtered)} 个任务 (scope={scope_type})'
            )
        return filtered

    # ============================================================
    # 两阶段规划（Phase 0.1: 20+ 文件大项目）
    # ============================================================

    def plan_module_groups(self, user_requirement: str, project_spec: dict) -> list:
        """
        Stage 1: 将大项目拆分为 3-5 个模块组。
        返回: [{group_id, name, description, files, dependencies}]
        """
        logger.info("🧩 [两阶段] Stage 1: 模块分组规划...")
        global_broadcaster.emit_sync("Manager", "module_group_start",
            "🧩 大型项目: 启动两阶段规划 — Stage 1 模块分组")

        spec_str = json.dumps(project_spec, ensure_ascii=False, indent=2) if project_spec else "无规划书"

        system_prompt = (
            Prompts.MANAGER_MODULE_GROUP_SYSTEM
            + f"\n\n【项目规划书】\n{spec_str}"
        )
        user_prompt = f"主人的开发需求：\n{user_requirement}\n请将项目拆分为模块组，严格按照 JSON Schema 输出。"

        try:
            raw_response = self.llm_client.chat_completion(
                enable_thinking=self.enable_thinking,
                reasoning_effort=self._reasoning_effort,
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]
            )
            json_str = raw_response.content
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0].strip()
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0].strip()

            result = json.loads(json_str)
            groups = result.get("module_groups", [])
            logger.info(f"✅ [两阶段] Stage 1 完成: {len(groups)} 个模块组")
            for g in groups:
                logger.info(f"  📦 {g.get('group_id')}: {g.get('name')} ({len(g.get('files', []))} 文件) deps={g.get('dependencies', [])}")
            return groups

        except Exception as e:
            logger.error(f"❌ [两阶段] Stage 1 失败: {e}，降级为单阶段")
            return []

    def plan_group_tasks(self, user_requirement: str, project_spec: dict,
                        module_group: dict, manager_playbook: str = "",
                        complex_files_hint: str = "") -> list:
        """
        Stage 2: 对单个模块组规划任务列表。
        返回: [task_dict] （不含 project_name，由调用者合并）
        """
        group_id = module_group.get("group_id", "unknown")
        group_name = module_group.get("name", "")
        group_files = module_group.get("files", [])

        logger.info(f"🧩 [两阶段] Stage 2: 规划模块组 [{group_id}: {group_name}] ({len(group_files)} 文件)")
        global_broadcaster.emit_sync("Manager", "group_plan_start",
            f"🧩 Stage 2: 规划模块组 {group_name}")

        spec_str = json.dumps(project_spec, ensure_ascii=False, indent=2) if project_spec else "无规划书"
        group_str = json.dumps(module_group, ensure_ascii=False, indent=2)

        manager_system = Prompts.MANAGER_SYSTEM.format(
            manager_playbook=manager_playbook,
            complex_files_hint=complex_files_hint or ""
        )
        system_prompt = (
            manager_system
            + f"\n\n═══ P1 — 项目契约 ═══\n{spec_str}"
            + f"\n\n【当前模块组信息 — 你只需要规划这个模块组内的文件】\n{group_str}"
            + f"\n\n⚠️ 重要：你只需要为以下文件创建 tasks: {group_files}"
            + "\n⚠️ dependencies 字段只允许引用当前模块组内的 task_id 或 target_file。"
            + "\n禁止在 task.dependencies 中写入 group_1/group_2 等模块组 ID，跨组顺序由 module_group.dependencies 表达。"
            + f"\n不要为其他模块组的文件创建 task！"
        )
        user_prompt = (
            f"主人的开发需求：\n{user_requirement}\n"
            f"请只为模块组 [{group_name}] 中的文件规划 tasks，严格按照 JSON Schema 输出。"
        )

        try:
            raw_response = self.llm_client.chat_completion(
                enable_thinking=self.enable_thinking,
                reasoning_effort=self._reasoning_effort,
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ]
            )
            json_str = raw_response.content
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0].strip()
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0].strip()

            plan = json.loads(json_str)
            tasks = plan.get("tasks", [])

            # 去重
            seen_files = set()
            deduped = []
            for t in tasks:
                tf = t.get("target_file", "")
                if tf not in seen_files:
                    seen_files.add(tf)
                    deduped.append(t)

            logger.info(f"✅ [两阶段] Stage 2 [{group_id}] 完成: {len(deduped)} 个 tasks")
            return deduped

        except Exception as e:
            logger.error(f"❌ [两阶段] Stage 2 [{group_id}] 失败: {e}")
            return []

    @staticmethod
    def _clean_tech_lead_filename(raw: str) -> str:
        """清理 TechLead 输出中的中文/英文括号注释。
        例如 'index.html（缺失文件，需新建）' → 'index.html'
        例如 'index.html (missing)' → 'index.html'
        """
        import re
        # 中文全角括号
        cleaned = re.sub(r'\uff08[^\uff09]*\uff09', '', raw)
        # 英文括号（仅清理末尾的注释性括号，不清理路径中的合法括号）
        cleaned = re.sub(r'\s*\([^)]*\)\s*$', '', cleaned)
        return cleaned.strip()

    def _normalize_patch_target_file(self, base_dir: str, raw_file: Any,
                                     existing_files: set,
                                     allow_new: bool = False) -> str:
        """将 TechLead verdict 中的文件路径归一化为项目内相对路径。
        
        Args:
            allow_new: 若为 True，允许返回不存在于 existing_files 中的文件路径
                      （用于 Patch Mode 创建缺失文件场景）
        """
        if not raw_file:
            return ""

        text = str(raw_file).strip().strip("`\"'")
        # 清理 TechLead 输出中的中文括号注释
        text = self._clean_tech_lead_filename(text)
        if not text:
            return ""

        base_abs = os.path.abspath(base_dir)
        text = text.replace("\\", "/")

        if os.path.isabs(text):
            abs_path = os.path.abspath(text)
            try:
                if os.path.commonpath([base_abs, abs_path]) != base_abs:
                    return ""
            except ValueError:
                return ""
            rel = os.path.relpath(abs_path, base_abs).replace("\\", "/")
        else:
            rel = text.lstrip("./")
            marker = f"/projects/{self.project_id}/"
            if marker in rel:
                rel = rel.split(marker, 1)[1]
            prefix = f"{self.project_id}/"
            if rel.startswith(prefix):
                rel = rel[len(prefix):]

            abs_path = os.path.abspath(os.path.join(base_abs, rel))
            try:
                if os.path.commonpath([base_abs, abs_path]) != base_abs:
                    return ""
            except ValueError:
                return ""

        rel = rel.replace("\\", "/")
        if rel.startswith("../") or rel == "..":
            return ""
        if existing_files and rel not in existing_files:
            lower_map = {path.lower(): path for path in existing_files}
            matched = lower_map.get(rel.lower(), "")
            if matched:
                return matched
            # 文件不存在但 allow_new=True → 允许创建新文件
            if allow_new:
                return rel
            return ""
        elif not existing_files and not os.path.isfile(os.path.join(base_abs, rel)):
            if allow_new:
                return rel
            return ""
        return rel

    def _build_tech_lead_patch_plan(self, user_requirement: str,
                                    tech_lead_diagnosis: dict,
                                    base_dir: str,
                                    existing_files: List[str]) -> Optional[dict]:
        """把 TechLead 结构化 verdict 直接适配成 Patch task，跳过二次 LLM 规划。"""
        if not tech_lead_diagnosis or not tech_lead_diagnosis.get("fix_instruction"):
            return None

        existing_set = set(existing_files or [])
        root_cause = str(tech_lead_diagnosis.get("root_cause", "")).strip()
        fix_instruction = str(tech_lead_diagnosis.get("fix_instruction", "")).strip()
        guilty_file = tech_lead_diagnosis.get("guilty_file", "")
        confidence = tech_lead_diagnosis.get("confidence", 0.0)
        recommended = tech_lead_diagnosis.get("recommended_target_files", []) or []

        # 构建写目标列表：guilty_file 为主目标，
        # recommended_target_files 中被 fix_instruction 明确引用的文件也升级为写目标。
        # 防扩散逻辑：只有文件名出现在 fix_instruction 中才升级，避免无关文件被误修改。
        if guilty_file:
            raw_targets = [guilty_file]
            # 检查 recommended 中是否有文件被 fix_instruction 明确引用
            if recommended and fix_instruction:
                fix_text_lower = fix_instruction.lower()
                recommended_list = [recommended] if isinstance(recommended, str) else list(recommended)
                for rec_file in recommended_list:
                    if not rec_file or rec_file == guilty_file:
                        continue
                    # 文件名（含路径或仅 basename）出现在修复指令中 → 需要修改
                    rec_basename = os.path.basename(str(rec_file))
                    if (str(rec_file).lower() in fix_text_lower
                            or rec_basename.lower() in fix_text_lower):
                        raw_targets.append(rec_file)
                if len(raw_targets) > 1:
                    logger.info(
                        "[Patch Mode] TechLead 快车道扩展写目标: %s → %s (fix_instruction 引用)",
                        guilty_file, raw_targets,
                    )
        elif isinstance(recommended, str):
            raw_targets = [recommended]
        else:
            raw_targets = list(recommended)

        # 判断是否可能需要创建新文件
        root_cause_type = str(tech_lead_diagnosis.get("root_cause_type", "")).strip()
        is_missing_file = root_cause_type in ("missing_export", "missing_file", "missing_module")

        targets = []
        new_file_targets = set()  # 记录需要新建的文件
        seen = set()
        for raw_target in raw_targets:
            # 先尝试严格匹配已有文件
            target = self._normalize_patch_target_file(base_dir, raw_target, existing_set)
            if not target and is_missing_file:
                # 文件不存在但属于 missing_file 类根因 → 允许创建
                target = self._normalize_patch_target_file(
                    base_dir, raw_target, existing_set, allow_new=True
                )
                if target:
                    new_file_targets.add(target)
                    logger.info(
                        "[Patch Mode] TechLead 快车道: 允许创建缺失文件 %s (root_cause_type=%s)",
                        target, root_cause_type,
                    )
            if target and target not in seen:
                seen.add(target)
                targets.append(target)

        if not targets:
            logger.warning(
                "[Patch Mode] TechLead 快车道未找到有效目标文件: guilty=%s recommended=%s",
                guilty_file, recommended,
            )
            return None

        # targets_set 用于判断哪些 recommended 文件是写目标、哪些是只读上下文
        targets_set = set(targets)

        tasks = []
        for idx, target_file in enumerate(targets, start=1):
            readonly_context = ""
            if recommended:
                recommended_list = [recommended] if isinstance(recommended, str) else list(recommended)
                context_files = [
                    item for item in recommended_list
                    if item
                    and self._normalize_patch_target_file(base_dir, item, existing_set) not in targets_set
                ]
                if context_files:
                    readonly_context = (
                        "\n\n【只读参考文件】\n"
                        f"{', '.join(context_files)}\n"
                        "这些文件不得作为本轮写目标。"
                    )

            is_new = target_file in new_file_targets
            if is_new:
                description = (
                    "【Patch Mode TechLead 快车道 — 新建缺失文件】\n"
                    f"用户需求:\n{user_requirement}\n\n"
                    f"TechLead 置信度: {confidence:.0%}\n\n"
                    f"【根因】\n{root_cause}\n\n"
                    f"【精确创建指令】\n{fix_instruction}\n\n"
                    f"{readonly_context}"
                    f"请创建文件 `{target_file}`，内容必须严格遵循上述指令。"
                )
            else:
                description = (
                    "【Patch Mode TechLead 快车道】\n"
                    f"用户需求:\n{user_requirement}\n\n"
                    f"TechLead 置信度: {confidence:.0%}\n"
                    f"有罪文件: {guilty_file}\n\n"
                    f"【根因】\n{root_cause}\n\n"
                    f"【精确修复指令】\n{fix_instruction}\n\n"
                    f"{readonly_context}"
                    f"只允许修改 `{target_file}`。必须优先使用 Editor 的 "
                    "`start_line/end_line` 行号定位完成局部修复；禁止全量重建项目。"
                )
            tasks.append({
                "task_id": f"patch_tl_{idx}",
                "target_file": target_file,
                "description": description,
                "dependencies": [],
                "task_type": "new_file" if is_new else "weld",
                "draft_action": "create" if is_new else "modify",
                "write_targets": [target_file],
                "tech_lead_invoked": True,
            })

        plan = {
            "project_name": self.project_id,
            "architecture_summary": "TechLead 白盒诊断定向修复",
            "tasks": tasks,
            "patch_context": {
                "source": "tech_lead_fast_lane",
                "confidence": confidence,
                "guilty_file": guilty_file,
            },
        }
        logger.info("[Patch Mode] TechLead 快车道生成 %d 个任务: %s", len(tasks), targets)
        return plan

    def plan_patch(self, user_requirement: str, project_spec: str = "",
                   playbook_hint: str = "", pm_analysis: str = "",
                   tech_lead_diagnosis: dict = None) -> dict:
        """
        Patch Mode 精简规划：读取项目文件树 + Observer 骨架，
        只规划需要修改的文件（跳过 Spec 生成）。

        Args:
            user_requirement: 修改需求描述
            project_spec: 项目规划书文本（可选，提供架构上下文）
            playbook_hint: Playbook 核心铁律摘要（可选，防止修复方案与 Reviewer 冲突）
            pm_analysis: PM 的详细影响分析（可选，包含精确的文件/行号/修改映射）
            tech_lead_diagnosis: TechLead 前置调查结果（可选，来自 Engine）
        """
        has_tech_lead = bool(tech_lead_diagnosis and tech_lead_diagnosis.get("fix_instruction"))
        diag_source = "TechLead 白盒调查" if has_tech_lead else "PM 分析"
        logger.info(f"⚡ [Patch Mode] Manager 精简规划启动... (诊断源: {diag_source})")
        global_broadcaster.emit_sync("Manager", "patch_plan_start",
            f"Patch Mode: 分析需修改的文件...（{diag_source}）")

        # 1. 读取项目文件树
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "projects", self.project_id))
        existing_files = []
        if os.path.isdir(base_dir):
            ignore = {'.sandbox', '.git', '__pycache__', '.venv', 'node_modules', '.idea'}
            for root, dirs, files in os.walk(base_dir):
                dirs[:] = [d for d in dirs if d not in ignore]
                for f in files:
                    rel = os.path.relpath(os.path.join(root, f), base_dir).replace("\\", "/")
                    existing_files.append(rel)
        file_tree = "\n".join([f"- {f}" for f in existing_files]) if existing_files else "目录暂空。"

        if has_tech_lead:
            fast_plan = self._build_tech_lead_patch_plan(
                user_requirement=user_requirement,
                tech_lead_diagnosis=tech_lead_diagnosis,
                base_dir=base_dir,
                existing_files=existing_files,
            )
            if fast_plan:
                global_broadcaster.emit_sync(
                    "Manager",
                    "patch_plan_ready",
                    f"Patch Mode: TechLead 快车道生成 {len(fast_plan['tasks'])} 个文件任务",
                    {"plan": fast_plan},
                )
                return fast_plan

        # 2. 提取所有源文件的 Observer 骨架
        skeleton_parts = []
        try:
            from tools.observer import Observer
            obs = Observer(base_dir)
            source_exts = {'.py', '.js', '.jsx', '.ts', '.tsx', '.html', '.css', '.vue'}
            for f in existing_files:
                ext = os.path.splitext(f)[1].lower()
                if ext in source_exts:
                    skeleton = obs.get_skeleton(f)
                    if skeleton and "Error" not in skeleton:
                        skeleton_parts.append(skeleton)
        except Exception as e:
            logger.warning(f"⚠️ [Patch Mode] Observer 骨架提取异常: {e}")

        file_skeletons = "\n\n".join(skeleton_parts) if skeleton_parts else "无骨架信息。"

        # 3. 构建 prompt（注入规划书 + Playbook 铁律）
        system_prompt = Prompts.MANAGER_PATCH_SYSTEM.format(
            project_id=self.project_id,
            file_tree=file_tree,
            file_skeletons=file_skeletons,
        )
        # P0 增强：注入项目规划书，让 Manager 了解架构
        if project_spec:
            system_prompt += f"\n\n【项目规划书（架构参考）】\n{project_spec[:3000]}"
        # P0 增强：注入 Playbook 铁律，防止修复方案与 Reviewer 冲突
        if playbook_hint:
            system_prompt += f"\n\n【编码铁律（Reviewer 审查依据，修复方案不得违反）】\n{playbook_hint}"

        # Phase 2.7: 注入 PM 影响分析作为 P0 约束
        if pm_analysis:
            system_prompt += (
                f"\n\n═══ P0 — PM 影响分析（最高优先级，必须严格采纳！）═══\n"
                f"以下是 PM 经过多次 tool-use 读取源代码后给出的精确修改指令。\n"
                f"你必须将这些精确修改指令**逐条写入每个 task 的 description 中**，\n"
                f"让 Coder 明确知道具体改哪一行、改什么值。禁止用模糊描述（如'改为深色主题'）！\n\n"
                f"{pm_analysis[:4000]}"
            )
            logger.info(f"📋 [Patch Mode] PM 影响分析已注入 ({len(pm_analysis)} 字符)")

        # v4.4: TechLead 白盒调查结果（最高优先级，覆盖 PM 分析）
        if has_tech_lead:
            tl_fix = tech_lead_diagnosis.get("fix_instruction", "")
            tl_root_cause = tech_lead_diagnosis.get("root_cause", "")
            tl_guilty = tech_lead_diagnosis.get("guilty_file", "")
            tl_confidence = tech_lead_diagnosis.get("confidence", 0.0)
            tl_recommended = tech_lead_diagnosis.get("recommended_target_files", [])
            system_prompt += (
                f"\n\n═══ P0+ — TechLead 白盒调查结果（最高优先级，必须采纳！）═══\n"
                f"TechLead 已通过 read_file/grep_project 实际读取代码，输出以下行级精确诊断：\n"
                f"【置信度】{tl_confidence:.0%}\n"
                f"【根因】{tl_root_cause}\n"
                f"【修复指令】{tl_fix}\n"
                f"【有罪文件】{tl_guilty}\n"
            )
            if tl_recommended:
                system_prompt += f"【建议修改文件】{', '.join(tl_recommended)}\n"
            system_prompt += (
                "\n你必须将以上 TechLead 的精确修复指令原文写入对应 task 的 description 中，"
                "让 Coder 能够直接按行号修改。"
            )
            logger.info(
                "📋 [Patch Mode] TechLead 诊断已注入: guilty=%s, confidence=%.2f",
                tl_guilty, tl_confidence,
            )

        user_prompt = f"主人的修改需求：\n{user_requirement}\n请严格按照 JSON Schema 输出。"

        # 4. 调用 LLM（max_tokens 保底，防止 system prompt 过长时输出被截断）
        try:
            raw_response = self.llm_client.chat_completion(
                enable_thinking=self.enable_thinking,
                reasoning_effort=self._reasoning_effort,
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                max_tokens=4096,
            )

            json_str = raw_response.content
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0].strip()
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0].strip()

            try:
                plan = json.loads(json_str)
            except json.JSONDecodeError:
                # v4.4: 截断 JSON 自动修复 — 补齐缺失的闭合括号
                logger.warning("[Patch Mode] JSON 解析失败，尝试截断修复...")
                repaired = self._repair_truncated_json(json_str)
                if repaired:
                    plan = repaired
                    logger.info("[Patch Mode] 截断 JSON 修复成功")
                else:
                    logger.error(f"[Patch Mode] Manager 返回非 JSON: {raw_response.content[:300]}")
                    return {"project_name": self.project_id, "tasks": []}

            plan["project_name"] = self.project_id  # 强制锁定
            if "tasks" not in plan:
                plan["tasks"] = []

            # 去重
            seen_files = set()
            deduped = []
            for task in plan["tasks"]:
                tf = task.get("target_file", "")
                if tf not in seen_files:
                    seen_files.add(tf)
                    deduped.append(task)
            plan["tasks"] = deduped

            logger.info(f"⚡ [Patch Mode] 精简规划完成: {len(plan['tasks'])} 个文件需修改")
            global_broadcaster.emit_sync("Manager", "patch_plan_ready",
                f"Patch Mode: {len(plan['tasks'])} 个文件需修改", {"plan": plan})
            return plan

        except json.JSONDecodeError:
            logger.error(f"[Patch Mode] Manager 返回非 JSON: {raw_response.content[:300]}")
            return {"project_name": self.project_id, "tasks": []}
        except Exception as e:
            logger.error(f"[Patch Mode] Manager 规划异常: {e}")
            return {"project_name": self.project_id, "tasks": []}

    @staticmethod
    def _repair_truncated_json(json_str: str) -> dict | None:
        """
        v4.4: 尝试修复被截断的 JSON（LLM 因 max_tokens 截断时常见）。
        策略：回退到最后一个有效的值边界，然后按 bracket 栈补齐闭合括号。
        """
        if not json_str or not json_str.strip():
            return None

        s = json_str.strip()

        # 确保以 { 开头
        brace_idx = s.find("{")
        if brace_idx < 0:
            return None
        s = s[brace_idx:]

        # 策略 1: 回退到最后一个完整属性边界
        # 找最后一个不在未闭合字符串内的 ", }, ], 数字
        # 如果字符串被截断（奇数个引号），回退到上一个完整的键值对
        quote_count = s.count('"')
        if quote_count % 2 != 0:
            # 奇数引号 = 字符串被截断，回退到最后一个完整的键值对
            last_complete = -1
            for marker in ['",', '"}', '"]', '": "', '": [', '": {']:
                # 找最后一个这样的 marker 之后，如果还有完整的值
                pass
            # 简单策略：回退到最后一个 `",` 或 `"}` 或 `"]`
            for cut in ['",' , '"}', '"]']:
                idx = s.rfind(cut)
                if idx > 0:
                    candidate = s[:idx + len(cut)]
                    if candidate.count('"') % 2 == 0:
                        s = candidate
                        break
            else:
                # 彻底截断：去掉最后一个未闭合的键值对
                last_comma = s.rfind(',')
                if last_comma > 0:
                    s = s[:last_comma]

        # 回退尾部非有效 JSON 字符
        while s and s[-1] not in ('"', '}', ']', '0', '1', '2', '3', '4', '5',
                                   '6', '7', '8', '9', 'e', 'l', 'u'):
            s = s[:-1]

        if not s:
            return None

        # 按 bracket 栈补齐闭合
        stack = []
        in_string = False
        escape = False
        for ch in s:
            if escape:
                escape = False
                continue
            if ch == '\\':
                escape = True
                continue
            if ch == '"' and not escape:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch in ('{', '['):
                stack.append(ch)
            elif ch == '}':
                if stack and stack[-1] == '{':
                    stack.pop()
            elif ch == ']':
                if stack and stack[-1] == '[':
                    stack.pop()

        # 补齐
        closing = []
        for bracket in reversed(stack):
            if bracket == '{':
                closing.append('}')
            elif bracket == '[':
                closing.append(']')

        repaired_str = s + ''.join(closing)

        try:
            return json.loads(repaired_str)
        except json.JSONDecodeError:
            # 二次尝试：截断到最后一个完整的对象/数组元素
            # 找最后一个 }, 或 ], 然后重新补齐
            for cut_marker in ['},', '],', '}', ']']:
                last_idx = repaired_str.rfind(cut_marker)
                if last_idx > 0:
                    truncated = repaired_str[:last_idx + len(cut_marker)]
                    # 重新计算 bracket 栈
                    stack2 = []
                    in_str2 = False
                    esc2 = False
                    for ch in truncated:
                        if esc2:
                            esc2 = False
                            continue
                        if ch == '\\':
                            esc2 = True
                            continue
                        if ch == '"' and not esc2:
                            in_str2 = not in_str2
                            continue
                        if in_str2:
                            continue
                        if ch in ('{', '['):
                            stack2.append(ch)
                        elif ch == '}' and stack2 and stack2[-1] == '{':
                            stack2.pop()
                        elif ch == ']' and stack2 and stack2[-1] == '[':
                            stack2.pop()
                    closing2 = []
                    for b in reversed(stack2):
                        closing2.append('}' if b == '{' else ']')
                    try:
                        return json.loads(truncated + ''.join(closing2))
                    except json.JSONDecodeError:
                        continue
            return None

    def plan_continue(self, failure_context: Dict[str, Any], open_issues_text: str = "",
                       tech_lead_diagnosis: dict = None) -> dict:
        """
        Continue Mode：基于上一轮 QA 失败上下文做定向修复。

        v4.4: TechLead 前置调查 — 当 TechLead 已通过 ReAct 工具调用完成白盒
        排障时，直接消费其行级精确修复指令，跳过 Manager 自己的 LLM 诊断。
        TechLead 调查失败时降级为 v4.3 LLM 诊断。

        Args:
            failure_context: 上一轮 QA 失败上下文
            open_issues_text: Phase 5.5 烂账账本的格式化文本（含回归检测标记）
            tech_lead_diagnosis: TechLead 前置调查结果（可选，来自 Engine）
        """
        has_tech_lead = bool(tech_lead_diagnosis and tech_lead_diagnosis.get("fix_instruction"))
        diag_source = "TechLead 白盒调查" if has_tech_lead else "LLM 诊断"
        logger.info(f"[Continue Mode] Manager 定向修复规划启动（v4.4 {diag_source}）")
        global_broadcaster.emit_sync(
            "Manager",
            "continue_plan_start",
            f"Continue Mode: 正在读取代码并诊断 bug 根因...（{diag_source}）",
        )

        if not failure_context:
            return {"project_name": self.project_id, "architecture_summary": "缺少失败上下文", "tasks": []}

        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "projects", self.project_id))
        existing_files = set()
        if os.path.isdir(base_dir):
            ignore = {'.sandbox', '.git', '__pycache__', '.venv', 'node_modules', '.idea', '.astrea'}
            for root, dirs, files in os.walk(base_dir):
                dirs[:] = [d for d in dirs if d not in ignore]
                for name in files:
                    rel = os.path.relpath(os.path.join(root, name), base_dir).replace("\\", "/")
                    existing_files.add(rel)

        def _as_files(value: Any) -> List[str]:
            files: List[str] = []
            if isinstance(value, str):
                files.append(value)
            elif isinstance(value, dict):
                for key in ("target_file", "file", "path", "provider_file", "importer_file"):
                    if value.get(key):
                        files.append(str(value[key]))
            elif isinstance(value, list):
                for item in value:
                    files.extend(_as_files(item))
            return files

        raw_files: List[str] = []
        raw_files.extend(_as_files(failure_context.get("repair_scope")))
        raw_files.extend(_as_files(failure_context.get("failed_files")))

        allowed_files: List[str] = []
        seen = set()
        for file_path in raw_files:
            rel = file_path.replace("\\", "/").lstrip("/")
            if not rel or rel in seen:
                continue
            if existing_files and rel not in existing_files:
                continue
            seen.add(rel)
            allowed_files.append(rel)

        # 提取失败/通过端点
        endpoint_results = failure_context.get("endpoint_results") or []
        failed_endpoints = [
            ep for ep in endpoint_results
            if isinstance(ep, dict) and not ep.get("ok")
        ]
        passed_endpoints = [
            ep for ep in endpoint_results
            if isinstance(ep, dict) and ep.get("ok")
        ]
        endpoint_summary = "; ".join(
            f"{ep.get('method', '?')} {ep.get('url', '?')} -> {ep.get('status_code', '?')} {ep.get('detail', '')}".strip()
            for ep in failed_endpoints[:6]
        ) or "上一轮 QA 未提供具体失败端点"
        passed_summary = "\n".join(
            f"  ✅ {ep.get('method', '?')} {ep.get('url', '?')} -> {ep.get('status_code', '?')}"
            for ep in passed_endpoints[:8]
        )
        feedback = str(failure_context.get("feedback") or failure_context.get("error_message") or "")[:1500]
        error_type = str(failure_context.get("error_type") or "integration_failure")

        # 读取修复范围内的文件源码
        file_codes: Dict[str, str] = {}
        for target_file in allowed_files:
            fpath = os.path.join(base_dir, target_file)
            if os.path.isfile(fpath):
                try:
                    with open(fpath, "r", encoding="utf-8") as f:
                        file_codes[target_file] = f.read()[:4000]
                except Exception:
                    pass

        # ============================================================
        # v4.4: 诊断阶段 — TechLead 优先，降级为 LLM
        # ============================================================
        diagnosis_results: Dict[str, str] = {}  # file -> 诊断结果

        if has_tech_lead:
            # === TechLead 已完成白盒调查，直接消费其精确修复指令 ===
            tl_fix = tech_lead_diagnosis.get("fix_instruction", "")
            tl_root_cause = tech_lead_diagnosis.get("root_cause", "")
            tl_guilty = tech_lead_diagnosis.get("guilty_file", "")
            tl_confidence = tech_lead_diagnosis.get("confidence", 0.0)
            tl_recommended = tech_lead_diagnosis.get("recommended_target_files", [])

            # 将 TechLead 诊断结果映射到 diagnosis_results
            # guilty_file 优先，其次 recommended_target_files，最后 allowed_files
            target_files_for_diag = []
            if tl_guilty:
                target_files_for_diag.append(tl_guilty)
            for rf in tl_recommended:
                if rf and rf not in target_files_for_diag:
                    target_files_for_diag.append(rf)
            # 确保 allowed_files 中的文件也能获得诊断
            for af in allowed_files:
                if af not in target_files_for_diag:
                    target_files_for_diag.append(af)

            for fname in target_files_for_diag:
                if fname in allowed_files or fname == tl_guilty:
                    diagnosis_results[fname] = (
                        f"【TechLead 白盒调查结果 (置信度: {tl_confidence:.0%})】\n"
                        f"【根因】{tl_root_cause}\n"
                        f"【修复指令】{tl_fix}\n"
                        f"【有罪文件】{tl_guilty}"
                    )

            logger.info(
                "[Continue Mode] 使用 TechLead 诊断: guilty=%s, confidence=%.2f, targets=%s",
                tl_guilty, tl_confidence, list(diagnosis_results.keys()),
            )

        elif file_codes and failed_endpoints:
            # === 降级: Manager LLM 单次诊断（v4.3 原有逻辑） ===
            code_sections = "\n\n".join(
                f"=== {fname} ===\n```\n{code}\n```"
                for fname, code in file_codes.items()
            )
            diag_prompt = (
                "你是一个顶级 Debug 专家。以下代码存在导致 QA 测试失败的 bug，请诊断根因。\n\n"
                f"【失败端点】\n{endpoint_summary}\n\n"
                f"【QA 反馈】\n{feedback}\n\n"
                f"【相关源码】\n{code_sections}\n\n"
            )
            if passed_summary:
                diag_prompt += f"【已通过端点 — 修复时不得破坏】\n{passed_summary}\n\n"
            if open_issues_text:
                diag_prompt += f"【历史问题台账】\n{open_issues_text}\n\n"

            diag_prompt += (
                "请对每个失败端点逐一分析根因，并为每个需要修复的文件输出精确的修复指令。\n"
                "输出格式（严格 JSON，不要 Markdown）：\n"
                "{\n"
                '  "diagnosis": [\n'
                '    {\n'
                '      "file": "routes.py",\n'
                '      "root_cause": "第 109 行 delete_expense 视图函数名与第 7 行从 models 导入的 delete_expense 同名，导致第 123 行递归调用自身而非 models.delete_expense",\n'
                '      "fix_instruction": "将视图函数 delete_expense 重命名为 handle_delete_expense，或在第 123 行改为 from models import delete_expense as model_delete_expense 并调用 model_delete_expense",\n'
                '      "affected_endpoints": ["POST /delete/<id>"]\n'
                '    }\n'
                '  ]\n'
                "}\n"
            )

            try:
                logger.info("[Continue Mode] TechLead 未提供诊断，降级为 Manager LLM 诊断...")
                global_broadcaster.emit_sync("Manager", "diagnosing",
                    "🔍 Manager 正在分析代码，诊断 bug 根因...（降级模式）")
                diag_response = self.llm_client.chat_completion(
                    enable_thinking=self.enable_thinking,
                    reasoning_effort=self._reasoning_effort,
                    model=self.model,
                    messages=[
                        {"role": "system", "content": "你是顶级 Debug 专家。只输出 JSON 诊断结果，不要解释。"},
                        {"role": "user", "content": diag_prompt},
                    ],
                )
                diag_text = diag_response.content
                # 清洗 JSON
                if "```json" in diag_text:
                    diag_text = diag_text.split("```json")[1].split("```")[0].strip()
                elif "```" in diag_text:
                    diag_text = diag_text.split("```")[1].split("```")[0].strip()

                diag_data = json.loads(diag_text)
                for item in diag_data.get("diagnosis", []):
                    fname = item.get("file", "")
                    root_cause = item.get("root_cause", "")
                    fix_instruction = item.get("fix_instruction", "")
                    affected = ", ".join(item.get("affected_endpoints", []))
                    if fname:
                        diagnosis_results[fname] = (
                            f"【根因诊断】{root_cause}\n"
                            f"【修复指令】{fix_instruction}\n"
                            f"【影响端点】{affected}"
                        )
                logger.info("[Continue Mode] LLM 诊断完成: %d 个文件有诊断结果", len(diagnosis_results))
                for fname, diag in diagnosis_results.items():
                    logger.info("  📋 %s: %s", fname, diag[:120])

            except json.JSONDecodeError as e:
                logger.warning(f"[Continue Mode] LLM 诊断结果 JSON 解析失败: {e}，降级为无诊断模式")
            except Exception as e:
                logger.warning(f"[Continue Mode] LLM 诊断调用异常: {e}，降级为无诊断模式")

        # 烂账账本
        issues_hint = ""
        if open_issues_text:
            issues_hint = f"\n\n【历史问题台账】\n{open_issues_text}"
            logger.info("[Continue Mode] 烂账账本已注入任务描述 (%d 字符)", len(open_issues_text))

        # 构建任务
        tasks = []
        for idx, target_file in enumerate(allowed_files, start=1):
            desc_parts = [
                f"【修复任务】修复 {target_file} 中导致 QA 测试失败的 bug。",
                f"\n错误类型: {error_type}",
                f"\n失败端点: {endpoint_summary}",
            ]

            # v4.4: 注入诊断结果（TechLead 白盒调查 或 LLM 诊断）
            diag = diagnosis_results.get(target_file, "")
            if diag:
                diag_label = "TechLead 白盒调查" if has_tech_lead else "Manager 诊断"
                desc_parts.append(f"\n\n═══ {diag_label}结果（必须严格按照此指令修复！）═══\n{diag}")

            # 注入 QA 反馈
            if feedback:
                desc_parts.append(f"\n\n【QA 错误反馈】\n{feedback}")

            # 注入当前源码
            code = file_codes.get(target_file, "")
            if code:
                desc_parts.append(f"\n\n【{target_file} 当前源码】\n```\n{code}\n```")

            # 已通过端点保护
            if passed_summary:
                desc_parts.append(f"\n\n【已通过端点 — 修复时不得破坏】\n{passed_summary}")

            desc_parts.append(f"\n\n本任务只允许修改 {target_file}，禁止扩展到 repair_scope/failed_files 之外。")

            if issues_hint:
                desc_parts.append(issues_hint)

            tasks.append({
                "task_id": f"continue_fix_{idx}",
                "target_file": target_file,
                "description": "".join(desc_parts),
                "dependencies": [],
                "write_targets": [target_file],
            })

        plan = {
            "project_name": self.project_id,
            "architecture_summary": "Continue Mode 定向修复（LLM 诊断）",
            "tasks": tasks,
            "continue_context": {
                "error_type": error_type,
                "failed_count": failure_context.get("failed_count", len(failed_endpoints)),
                "allowed_files": allowed_files,
                "has_diagnosis": bool(diagnosis_results),
                "diagnosis_source": "tech_lead" if has_tech_lead else "llm",
            },
        }
        logger.info("[Continue Mode] 定向规划完成: %s 个任务, files=%s, 诊断=%s",
                     len(tasks), allowed_files, bool(diagnosis_results))
        global_broadcaster.emit_sync(
            "Manager",
            "continue_plan_ready",
            f"Continue Mode: {len(tasks)} 个定向修复任务（已完成 LLM 诊断）",
            {"plan": plan},
        )
        return plan



    def plan_extend(
        self,
        new_module_requirement: str,
        existing_context: dict,
        manager_playbook: str = "",
        replan_feedback: dict | None = None,
        open_issues_text: str = "",
    ) -> dict:
        """
        Extend Mode：在已有项目基础上规划新增模块。
        代码层负责再次归一化 LLM 输出，强制落成 new_file / weld 的物理约束。

        Args:
            open_issues_text: Phase 5.5 烂账账本（含回归标记），注入 Prompt 防止新模块踩旧坑
        """
        logger.info("[Extend Mode] Manager 增量规划启动")
        global_broadcaster.emit_sync(
            "Manager",
            "extend_plan_start",
            "Extend Mode: 正在基于已有项目上下文规划新增模块...",
        )

        existing_context = existing_context or {}
        base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "projects", self.project_id))
        file_tree = [
            str(path).replace("\\", "/").lstrip("/")
            for path in (existing_context.get("file_tree") or [])
            if path
        ]
        existing_files = set(file_tree)
        route_blacklist = sorted({
            str(item.get("path", "")).strip()
            for item in (existing_context.get("existing_routes") or [])
            if isinstance(item, dict) and str(item.get("path", "")).strip()
        })

        skeleton_targets: List[str] = []
        entrypoint_file = str(
            (existing_context.get("architecture_contract") or {}).get("entrypoint_file") or ""
        ).replace("\\", "/").lstrip("/")
        if entrypoint_file:
            skeleton_targets.append(entrypoint_file)
        for item in (existing_context.get("existing_routes") or []) + (existing_context.get("existing_models") or []):
            if isinstance(item, dict) and item.get("file"):
                skeleton_targets.append(str(item["file"]).replace("\\", "/").lstrip("/"))
        for path in file_tree:
            if any(token in path.lower() for token in ("route", "model", "app.py", "main.py", "server.py", "template")):
                skeleton_targets.append(path)

        deduped_targets: List[str] = []
        seen_targets = set()
        for path in skeleton_targets:
            if not path or path in seen_targets or path not in existing_files:
                continue
            seen_targets.add(path)
            deduped_targets.append(path)
            if len(deduped_targets) >= 10:
                break

        file_skeletons = []
        if os.path.isdir(base_dir):
            from tools.observer import Observer

            observer = Observer(base_dir)
            for path in deduped_targets:
                try:
                    skeleton = observer.get_skeleton(path)
                except Exception:
                    skeleton = ""
                if skeleton:
                    file_skeletons.append(f"## {path}\n{skeleton[:2500]}")

        file_tree_text = "\n".join(file_tree[:200])
        if len(file_tree) > 200:
            file_tree_text += f"\n... 其余 {len(file_tree) - 200} 个文件省略"

        system_prompt = Prompts.MANAGER_EXTEND_SYSTEM.format(
            project_id=self.project_id,
            architecture_contract=json.dumps(
                existing_context.get("architecture_contract") or {},
                ensure_ascii=False,
                indent=2,
            ),
            file_tree=file_tree_text or "无",
            file_skeletons="\n\n".join(file_skeletons) or "无",
            route_blacklist=json.dumps(route_blacklist, ensure_ascii=False, indent=2),
            existing_routes=json.dumps(
                (existing_context.get("existing_routes") or [])[:60],
                ensure_ascii=False,
                indent=2,
            ),
            existing_models=json.dumps(
                (existing_context.get("existing_models") or [])[:60],
                ensure_ascii=False,
                indent=2,
            ),
            entrypoint_imports="\n".join(existing_context.get("entrypoint_imports") or []) or "无",
            manager_playbook=manager_playbook or "无",
            replan_feedback=json.dumps(replan_feedback or {}, ensure_ascii=False, indent=2),
        )
        # Phase 5.5: 注入烂账账本（回归 + 历史问题）
        if open_issues_text:
            system_prompt += (
                f"\n\n═══ 历史问题台账（烂账账本）═══\n"
                f"以下是此项目历史轮次中尚未闭环的问题，规划新模块时必须避开这些已知坑点：\n"
                f"{open_issues_text}\n"
            )
            logger.info("[Extend Mode] 烂账账本已注入 Prompt (%d 字符)", len(open_issues_text))

        user_prompt = (
            f"主人的新增需求：\n{new_module_requirement}\n"
            "请严格按 JSON Schema 输出，不要 Markdown。"
        )

        try:
            raw_response = self.llm_client.chat_completion(
                enable_thinking=self.enable_thinking,
                reasoning_effort=self._reasoning_effort,
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            )

            json_str = raw_response.content
            # 记录 LLM 原始输出前 500 字符（事后诊断用）
            logger.info("[Extend Mode] LLM 原始输出预览: %s", json_str[:500])
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0].strip()
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0].strip()

            try:
                plan = json.loads(json_str)
            except json.JSONDecodeError:
                # 截断 JSON 自动修复（复用 Patch Mode 的修复链路）
                logger.warning("[Extend Mode] JSON 解析失败，尝试截断修复...")
                repaired = self._repair_truncated_json(json_str)
                if repaired:
                    plan = repaired
                    logger.info("[Extend Mode] 截断 JSON 修复成功")
                else:
                    logger.error(
                        "[Extend Mode] Manager 返回非法 JSON，截断修复也失败。原始输出:\n%s",
                        json_str[:1000],
                    )
                    return {"project_name": self.project_id, "architecture_summary": "新增模块规划失败（JSON 解析失败）", "tasks": []}
        except Exception as e:
            logger.error(f"[Extend Mode] Manager 规划异常: {e}")
            return {"project_name": self.project_id, "architecture_summary": "新增模块规划失败", "tasks": []}

        # 方案 A: 归一化职责完全交给 extend.py 的 _normalize_extend_plan
        # manager 只设置 project_name 和 route_blacklist，不修改 tasks/dependencies/task_id
        plan["project_name"] = self.project_id
        extend_context = dict(plan.get("extend_context") or {})
        extend_context["route_blacklist"] = route_blacklist
        plan["extend_context"] = extend_context

        raw_tasks = plan.get("tasks") or []
        logger.info(
            "[Extend Mode] LLM 原始规划: %s 个任务",
            len(raw_tasks),
        )
        global_broadcaster.emit_sync(
            "Manager",
            "extend_plan_ready",
            f"Extend Mode: {len(raw_tasks)} 个原始任务（待 extend.py 归一化）",
            {"plan": plan},
        )
        return plan

    def execute_tdd_loop(self, task: Dict[str, Any], final_dir: str = None, task_meta: dict = None) -> Tuple[bool, Dict[str, str]]:
        """
        核心的 TDD (Test-Driven Development) 开发与审查闭环
        返回: (success: bool, milestones: dict)
        milestones: {"a": 初始代码, "b": 报错摘要流, "c": 最终代码}
        """
        target_file = task.get("target_file", "")
        description = task.get("description", "")
        task_id = task.get("task_id", target_file)
        
        logger.info(f"\n🚀 开始执行任务 [{task_id}]: {target_file}")
        global_broadcaster.emit_sync("Manager", "task_start", f"开始分发执行任务: {target_file}", {"task": task})

        vfs = global_state_manager.get_vfs(self.project_id)
        
        # 如果 VFS 中没有该文件，检查磁盘是否已有（跨请求修改场景）
        if vfs.get_draft(target_file) is None and final_dir:
            disk_path = os.path.join(final_dir, target_file)
            if os.path.isfile(disk_path):
                try:
                    with open(disk_path, "r", encoding="utf-8") as f:
                        existing_code = f.read()
                    vfs.save_draft(target_file, existing_code)
                    logger.info(f"📂 从磁盘预加载已有文件到 VFS: {target_file} ({len(existing_code)} bytes)")
                except Exception as e:
                    logger.warning(f"⚠️ 磁盘文件预加载失败: {target_file} - {e}")
        
        # 里程碑收集器
        milestones = {"a": "", "b": "", "c": ""}
        error_trail = []  # 报错摘要流
        
        feedback = None
        vfs.reset_retry(task_id)
        
        while True:
            current_retry = vfs.get_retry_count(task_id)
            
            # 1. 熔断机制判定
            if current_retry >= MAX_RETRIES:
                logger.error(f"🚨 [熔断触发] 任务 {task_id} 连续失败 {MAX_RETRIES} 次，陷入死循环！停止执行。")
                global_broadcaster.emit_sync("Manager", "task_abort", f"任务 {task_id} 发生熔断！", {})
                # 熔断时也收集里程碑 B
                milestones["b"] = "\n".join(error_trail) if error_trail else "无报错记录"
                return False, milestones
                
            if current_retry > 2 and feedback:
                # 第二级熔断警告：连续失败3次及以上，向 Coder 施压
                logger.warning(f"⚠️ 任务 {task_id} 已失败 {current_retry} 次，正在下发强制思路转换指令！")
                feedback += "\n\n【系统级绝密警告】你已经在这个问题上失败重试了3次以上！请立刻放弃你现在的思路或引用的第三方库，采用最基础、最简单或原生的写法来实现，切勿执迷不悟！"

            # 2. Coder 生成代码 (写进内存 VFS草稿区)
            if current_retry > 0:
                logger.info(f"🔄 第 {current_retry} 次重试修复 [{task_id}]...")
                global_broadcaster.emit_sync("Manager", "task_retry", f"子任务 {task_id} 正在进行第 {current_retry} 次重试", {"attempt": current_retry})

            # 🔧 关键修复：每次循环更新 existing_code，确保修复模式能看到真正的代码
            #   否则 CODER_FIX_SYSTEM prompt 中 current_code 为空，LLM 凭记忆编 search → 永远不匹配
            latest_code = vfs.get_draft(target_file) or ""
            if not latest_code and final_dir:
                disk_path = os.path.join(final_dir, target_file)
                if os.path.isfile(disk_path):
                    try:
                        with open(disk_path, "r", encoding="utf-8") as f:
                            latest_code = f.read()
                    except Exception:
                        pass
            if task_meta is not None:
                task_meta["existing_code"] = latest_code
                task_meta["retry_count"] = current_retry

            self.coder.generate_code(target_file, description, feedback, task_meta=task_meta)
            recalled_ids = getattr(self.coder, '_last_recalled_ids', [])
            global_broadcaster.emit_sync("Manager", "vfs_update", f"VFS 文件树更新暂存目标: {target_file}", {"vfs": vfs.get_all_vfs()})

            # 获取 Coder 输出的代码
            code_draft = vfs.get_draft(target_file) or ""
            
            # 里程碑 A：记录第一次的代码（“初始错误直觉”）
            if current_retry == 0:
                milestones["a"] = code_draft
            
            # 更新 File Tree
            file_list = list(vfs.get_all_vfs().keys())
            upsert_file_tree(self.project_id, file_list)
            
            # 3. Reviewer 测试与审查沙盒执行
            is_pass, reviewer_feedback = self.reviewer.evaluate_draft(target_file, description)
            
            # 4. 统一写入 tdd_round 事件
            verdict = "pass" if is_pass else "fail"
            if is_pass:
                # round_pass 精简：不含代码（与 Synthesizer 经验重复），只记摘要
                event_content = f"[PASS] 任务 {task_id} | 文件: {target_file} | 重试: {current_retry} | 审查通过"
            else:
                # round_fail 保留完整代码和报错（Coder 需要用来修复）
                event_content = (
                    f"[FAIL] 任务 {task_id} | 文件: {target_file} | 重试: {current_retry}\n"
                    f"--- 代码片段 ---\n{code_draft[:1500]}\n"
                    f"--- 审查结果 ---\n{reviewer_feedback[:500]}"
                )
            append_event("tdd", f"round_{verdict}", event_content, project_id=self.project_id,
                         metadata={"task_id": task_id, "target_file": target_file,
                                   "retry": current_retry, "verdict": verdict})

            if is_pass:
                # 里程碑 C：最终通过的代码（"通关密码"）
                milestones["c"] = code_draft
                milestones["b"] = "\n".join(error_trail) if error_trail else "（一次通过，无报错记录）"
                # 轨迹表：写入成功轨迹（含 recalled IDs）+ 回填最终代码
                insert_trajectory(
                    project_id=self.project_id, task_id=task_id,
                    attempt_round=current_retry, error_summary=None,
                    failed_code=None, recalled_memory_ids=recalled_ids,
                )
                finalize_trajectory(self.project_id, task_id, code_draft)
                logger.info(f"🎉 任务 [{task_id}] 审查通过！recalled_ids={recalled_ids}")
                return True, milestones
            else:
                # 里程碑 B：累积报错摘要
                error_trail.append(f"尝试{current_retry + 1}: {reviewer_feedback[:200]}")
                # 轨迹表：写入本轮失败快照 + 实际 recalled IDs
                insert_trajectory(
                    project_id=self.project_id, task_id=task_id,
                    attempt_round=current_retry,
                    error_summary=reviewer_feedback[:2000],
                    failed_code=code_draft,
                    recalled_memory_ids=recalled_ids,
                )
                feedback = reviewer_feedback
                vfs.increment_retry(task_id)
                logger.warning(f"🔨 任务 [{task_id}] 审查未通过，退回重写 (Current Retries: {current_retry + 1}/{MAX_RETRIES})")
        
        return False

    @staticmethod
    def _resolve_output_dir(project_name: str, out_dir: Optional[str] = None) -> str:
        """
        统一计算项目输出目录的逻辑。
        如果指定了 out_dir 则使用；否则自动在 projects/ 下按时间戳创建。
        """
        if out_dir:
            return os.path.abspath(out_dir)

        safe_proj_name = "".join(
            [c for c in project_name if c.isalnum() or c in ('_', '-')]
        ).strip()
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        auto_dir_name = f"{timestamp}_{safe_proj_name}"

        base_projects_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "projects"
        )
        return os.path.join(base_projects_dir, auto_dir_name)

    def run_project(self, user_requirement: str, out_dir: Optional[str] = None) -> Tuple[bool, str]:
        """
        项目生成统一主入口（CLI 和 Web 共用）。

        参数:
            user_requirement: 用户的需求文本
            out_dir: 可选的指定输出目录

        返回:
            (success: bool, final_dir: str) — 是否全部成功 + 最终落盘路径
        """
        # 1. 记录用户需求到事件流
        append_event("user", "prompt", user_requirement, project_id=self.project_id)
        create_project_meta(self.project_id)
        
        # 2. 清空上一轮残留状态
        vfs = global_state_manager.get_vfs(self.project_id)
        vfs.clear_state()
        global_broadcaster.emit_sync("System", "start_project", "系统重置并启动新项目生成...")

        # 3. 生成规划书 → 任务拆解
        project_spec = self._generate_project_spec(user_requirement)
        plan = self.plan_tasks(user_requirement, project_spec=project_spec)
        
        # 将 spec 存入 plan 供后续传递
        plan["project_spec"] = project_spec
        spec_text = json.dumps(project_spec, ensure_ascii=False, indent=2) if project_spec else "无规划书"
        append_event("manager", "plan", json.dumps(plan, ensure_ascii=False), project_id=self.project_id)

        # 4. 解析输出目录并执行动态重命名
        project_name = plan.get('project_name', 'Unnamed_Project').replace(" ", "_")
        
        if "新建项目" in self.project_id or "new_project" in self.project_id or "default_project" == self.project_id:
            old_project_id = self.project_id
            parts = old_project_id.split("_", 2)
            timestamp = f"{parts[0]}_{parts[1]}" if len(parts) >= 2 else time.strftime("%Y%m%d_%H%M%S")
            safe_proj_name = re.sub(r'[^\w\-\u4e00-\u9fa5]', '_', project_name)
            new_project_id = f"{timestamp}_{safe_proj_name}"
            
            base_projects_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "projects"))
            old_dir = os.path.join(base_projects_dir, old_project_id)
            new_dir = os.path.join(base_projects_dir, new_project_id)
            
            if os.path.exists(old_dir) and old_dir != new_dir:
                try:
                    os.rename(old_dir, new_dir)
                    # 更新所有作用域的 project_id
                    self.project_id = new_project_id
                    self.coder.project_id = new_project_id
                    self.reviewer.project_id = new_project_id
                    global_state_manager.rename_vfs(old_project_id, new_project_id)
                    
                    rename_project_events(old_project_id, new_project_id)
                    rename_project_meta(old_project_id, new_project_id, safe_proj_name)
                    
                    global_broadcaster.emit_sync("System", "project_renamed", f"项目正式主题生成，宇宙已重命名为: {safe_proj_name}", {
                        "old_id": old_project_id,
                        "new_id": new_project_id
                    })
                except Exception as e:
                    logger.error(f"动态重命名项目时出错: {e}")
                    new_project_id = old_project_id
            else:
                new_project_id = old_project_id
        else:
            new_project_id = self.project_id

        # 5. 重命名完成后异步预热 sandbox（用最终 project_id）
        from tools.sandbox import sandbox_env
        tech_stacks = project_spec.get("tech_stack", []) if project_spec else []
        if tech_stacks:
            final_project_id = new_project_id
            def _bg_warmup():
                sandbox_env.warm_up(final_project_id, tech_stacks)
            threading.Thread(target=_bg_warmup, daemon=True).start()

        if out_dir:
            final_dir = os.path.abspath(out_dir)
        else:
            final_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "projects", new_project_id))
            
        os.makedirs(final_dir, exist_ok=True)
        logger.info(f"📌 项目代码最终将落盘至: {final_dir}")

        # 4. TDD 循环
        tasks = plan.get("tasks", [])
        if not tasks:
            logger.warning("任务列表为空！")
            global_broadcaster.emit_sync("System", "error", "任务拆解为空，中止进程。")
            return False, final_dir

        logger.info(f"🔥 项目开始启动！项目名: {plan.get('project_name', 'Unnamed')}")

        all_milestones = []  # 收集所有任务的里程碑
        synthesizer = SynthesizerAgent(project_id=self.project_id)

        # 软删除该 project 的旧轨迹记录（标记 is_synthesized=True），确保本次审计范围干净
        from core.database import TaskTrajectory
        from core.database import ScopedSession as _SS
        _session = _SS()
        try:
            staled = _session.query(TaskTrajectory).filter(
                TaskTrajectory.project_id == self.project_id,
                TaskTrajectory.is_synthesized == False,
            ).update({"is_synthesized": True})
            _session.commit()
            if staled:
                logger.info(f"🧹 旧轨迹已归档: {staled} 条 (project={self.project_id})")
        except Exception:
            _session.rollback()
        finally:
            _SS.remove()

        for idx, task in enumerate(tasks):
            logger.info(f"\n[{idx+1}/{len(tasks)}] ========================")
            # 构建 task_meta 传递给 Coder：规划书 + 依赖列表
            task_meta = {
                "project_spec": spec_text,
                "dependencies": task.get("dependencies", []),
                "all_tasks": tasks,  # 用于按 dependencies 查找依赖文件
            }
            success, milestones = self.execute_tdd_loop(task, final_dir=final_dir, task_meta=task_meta)
            all_milestones.append({"task": task, "milestones": milestones, "success": success})

            if not success:
                logger.critical(f"💥 核心任务 {task.get('task_id')} 彻底失败。")
                global_broadcaster.emit_sync("System", "error", f"💥 核心任务 {task.get('task_id')} 连续熔断。项目腰斩！")
                append_event("system", "circuit_break", f"任务 {task.get('task_id')} 熔断", project_id=self.project_id)
                update_project_status(self.project_id, "failed")
                
                # 熔断时也调用 Synthesizer 提炼 Anti-pattern
                global_broadcaster.emit_sync("System", "info", "🧠 熔断了！正在提炼失败教训...")
                def _bg_failure():
                    synthesizer.synthesize_failure(milestones, user_requirement, plan)
                threading.Thread(target=_bg_failure, daemon=True).start()
                
                return False, final_dir

        # 6. 全部通过，调用 Synthesizer 提炼 Contrastive Pair
        logger.info("\n🏆 所有任务均已通过 Reviewer 测试！触发 Synthesizer 知识提炼...")
        
        vfs.commit_to_disk(final_dir)
        global_state_manager.remove_vfs(self.project_id)
        update_project_status(self.project_id, "success")
        logger.info(f"✨ 项目交付完成: {final_dir}")
        global_broadcaster.emit_sync("System", "success", f"✨ 项目完美生成！{final_dir}", {"final_path": final_dir})

        # 后台异步: Synthesizer 蒸馏 + Auditor 按 task 粒度审计 + AMC 延迟结算
        global_broadcaster.emit_sync("System", "info", "🧠 所有测试通过！正在执行经验提炼与 AMC 结算...")
        project_id = self.project_id
        def _bg_settlement():
            try:
                for item in all_milestones:
                    if item["success"]:
                        synthesizer.synthesize_success(item["milestones"], user_requirement, plan)
                logger.info("✨ [后台] Synthesizer 知识提炼完毕")

                # 按 task 粒度审计：每个 task 的 recalled IDs 只对该 task 的 final_code 审计
                all_used_ids, all_ignored_ids = set(), set()
                auditor = AuditorAgent()
                from core.database import ScopedSession, Memory

                for item in all_milestones:
                    if not item["success"]:
                        continue
                    tid = item["task"].get("task_id", "")
                    task_memory_ids = get_recalled_memory_union(project_id, tid)
                    if not task_memory_ids:
                        continue

                    task_final_code = item["milestones"].get("c", "")
                    if not task_final_code:
                        continue

                    # 填充记忆内容
                    memories_to_audit = [{"id": mid, "content": ""} for mid in task_memory_ids]
                    session = ScopedSession()
                    try:
                        for m in memories_to_audit:
                            if m["id"] > 0:
                                row = session.query(Memory).filter(Memory.id == m["id"]).first()
                                if row:
                                    m["content"] = row.content[:300]
                    finally:
                        ScopedSession.remove()

                    logger.info(f"📋 [后台] Auditor 审计 [{tid}]: {len(memories_to_audit)} 条记忆 vs {len(task_final_code)} bytes 代码")
                    audit_result = auditor.audit(task_final_code, memories_to_audit)

                    for r in audit_result.get("results", []):
                        mid = r.get("memory_id", -1)
                        if mid > 0:
                            (all_used_ids if r.get("adopted") else all_ignored_ids).add(mid)

                if all_used_ids or all_ignored_ids:
                    settle_memory_scores(all_used_ids, all_ignored_ids, get_global_round())
                    logger.info(f"✨ [后台] AMC 延迟结算完成: 功臣{len(all_used_ids)} 陪跑{len(all_ignored_ids)}")
                else:
                    logger.info("📋 [后台] 无召回记忆需要结算，跳过")
                tick_global_round()
            except Exception as e:
                logger.error(f"❌ [后台] 结算流程异常: {e}")
        threading.Thread(target=_bg_settlement, daemon=True).start()

        return True, final_dir
