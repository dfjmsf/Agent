"""
TechLead Agent — 白盒排障特工 + 代码审查侦探 (A-1)

Phase 3 升级：从 117 行被动仲裁者 → ~300 行主动 ReAct 调查特工。

两大模式：
1. investigate() — TDD 排障：Coder 反复失败时主动查代码/日志/grep 定位根因
2. audit()       — 代码审查：PM 直接调度，扫描项目输出结构化发现列表

工具箱（ReAct Tool Calling）：
- read_file: 读取任意项目文件
- list_files: 项目文件树
- grep_project: 关键词搜索
- read_sandbox_log: 沙盒日志
- emit_verdict: 终止调查，输出判定

设计原则：
- TechLead 是「侦探」，只输出结构化发现/指令
- 审查报告的撰写交给 PM（PM 持有用户上下文）
- 排障指令直接点对点传递给 Coder
"""
import os
import json
import logging
from typing import Optional, Dict, List

from core.audit_guard import validate_audit_findings
from core.llm_client import default_llm
from core.techlead_scope import TargetScope
from core.ws_broadcaster import global_broadcaster

logger = logging.getLogger("TechLead")

MAX_INVESTIGATE_STEPS = 14  # 排障模式硬顶（动态计算时的上界）
MAX_AUDIT_STEPS = 25        # 审查模式最大 ReAct 步数（加入防雪球后安全上调至25）


class TechLeadAgent:
    """白盒排障特工 — ReAct 驱动的主动调查者"""

    def __init__(self):
        self.model = os.getenv("MODEL_TECH_LEAD", "qwen3-max")
        _et, _re = default_llm.parse_thinking_config(os.getenv("THINKING_TECH_LEAD", "false"))
        self.enable_thinking = _et
        self._reasoning_effort = _re

    # ============================================================
    # 模式 1: 排障调查 (TDD 循环中被 TaskRunner 唤醒)
    # ============================================================

    def investigate(self, project_dir: str, task_context: str,
                    sandbox_dir: str = None, max_steps: int = None,
                    target_scope: Optional[TargetScope] = None,
                    signal: Optional[dict] = None) -> dict:
        """
        白盒排障 ReAct 循环。

        Args:
            project_dir: 项目所在目录
            task_context: Reviewer feedback / 错误信息
            sandbox_dir: 沙盒目录（可选）
            max_steps: 最大步数

        Returns:
            {
                "root_cause": str,
                "fix_instruction": str,
                "guilty_file": str,
            }
            失败时返回 None
        """
        if max_steps is None:
            # 动态步数：候选文件数 + 5 步基础动作 (list_files + grep + read_log + verdict + 余量)
            # 下限 8（简单单文件 bug 足够），上限 MAX_INVESTIGATE_STEPS=14（防沉迷）
            scope_size = len(target_scope.candidate_files) if target_scope and target_scope.candidate_files else 0
            max_steps = min(max(scope_size + 5, 8), MAX_INVESTIGATE_STEPS)

        logger.info(f"🔍 TechLead 排障调查启动 (max_steps={max_steps})")
        global_broadcaster.emit_sync("TechLead", "investigate_start",
                                     "🔍 TechLead 白盒排障启动...")

        scope_text = target_scope.summary_text() if target_scope else "未启用定向范围限制。"
        signal_text = ""
        if signal:
            signal_text = (
                f"\n【结构化跨文件信号】\n"
                f"- provider_file: {signal.get('provider_file', '')}\n"
                f"- importer_file: {signal.get('importer_file', '')}\n"
                f"- missing_symbol: {signal.get('missing_symbol', '')}\n"
                f"- stage: {signal.get('stage', '')}\n"
            )

        system_prompt = (
            "你是 ASTrea 系统的技术骨干（TechLead），一位资深全栈工程师。\n"
            "你的职责是：深入调查代码 Bug 的根因，给出精确的修复指令。\n\n"
            "你拥有以下工具来主动查探：\n"
            "- read_file: 完整读取任意项目文件\n"
            "- grep_project: 在项目中搜索关键词/正则\n"
            "- list_files: 查看项目文件结构\n"
            "- read_sandbox_log: 查看运行时日志\n"
            "- emit_verdict: 提交最终判定（必须在调查充分后调用）\n\n"
            "调查策略：\n"
            "1. 先用 list_files 了解项目结构\n"
            "2. 根据错误信息用 grep_project 定位可疑代码\n"
            "3. 用 read_file 深入阅读相关文件\n"
            "4. 如有运行时错误，用 read_sandbox_log 查看日志\n"
            "5. 确认根因后调用 emit_verdict 提交\n\n"
            "emit_verdict 时必须包含：\n"
            "- root_cause: 根因分析\n"
            "- root_cause_type: 只能是 missing_export / naming_mismatch / signature_mismatch / architecture_drift / wrong_target\n"
            "- fix_instruction: 给 Coder 的具体修复指令（要精确到改哪个文件、哪行、怎么改）\n"
            "- guilty_file: 需要修改的文件路径\n"
            "- recommended_target_files: 推荐继续检查/修改的文件列表\n"
            "- qa_plan: 可选，Patch 后最小浏览器验证计划。UI 交互问题优先给出，例如 "
            "[{\"action\":\"click\",\"selector\":\"#create-btn\",\"assert\":\"visible\",\"target\":\"#editor-panel\"}]\n"
            "- confidence: 0 到 1 的置信度\n\n"
            "【定向边界】\n"
            f"{scope_text}\n"
            "禁止越过上述文件范围做全项目泛审。若当前 importer 才是根因，root_cause_type 应输出 wrong_target。\n"
        )

        user_prompt = (
            f"以下是当前的错误/问题信息，请展开调查：\n\n"
            f"{task_context}"
            f"{signal_text}"
        )

        return self._react_loop(
            system_prompt, user_prompt, project_dir, sandbox_dir,
            max_steps=max_steps, mode="investigate",
            allowed_files=(target_scope.candidate_files if target_scope else None),
        )

    # ============================================================
    # 模式 2: 代码审查 (PM 直接调度)
    # ============================================================

    def audit(self, project_dir: str, user_request: str,
              max_steps: int = None, target_scope: Optional[TargetScope] = None) -> list:
        """
        代码审查模式：扫描项目，输出结构化发现列表。

        Args:
            project_dir: 项目目录
            user_request: 用户的审查要求

        Returns:
            findings 列表 (list of dict)
        """
        logger.info(f"🔬 TechLead 代码审查启动: {user_request[:60]}")
        global_broadcaster.emit_sync("TechLead", "audit_start",
                                     f"🔬 TechLead 代码审查启动: {user_request[:40]}...")

        global_findings = []

        scope_text = target_scope.summary_text() if target_scope else "未指定定向范围，允许全项目审查。"

        system_prompt = (
            "你是代码审计专家。审查项目代码，重点关注 **运行时正确性** 和功能完整性。\n\n"
            "【审查优先级（从高到低）】\n"
            "1. 🔴 运行时崩溃（最高优先！）：\n"
            "   - models.py 返回 tuple 但模板/路由用 .attribute 访问 → 必崩\n"
            "   - render_template() 传入的变量名 vs 模板 {{ }} 中使用的变量名不一致 → 必崩\n"
            "   - request.form['xxx'] 中的 key 与 HTML <input name> 不匹配 → 必崩\n"
            "   - 缺少 init_db() 调用 → 'no such table' 崩溃\n"
            "   - sqlite3 查询不设 row_factory → tuple 崩溃\n"
            "2. 🟡 逻辑缺陷：数据流断裂、条件判断遗漏、接口返回格式不一致\n"
            "3. 🟢 安全问题（最低优先）：硬编码密钥、CSRF 等（这些不影响功能，降级处理）\n\n"
            "工具：\n"
            "- read_file(file_path): 读取文件完整内容\n"
            "- grep_project(pattern): 搜索关键词\n"
            "- record_finding(...): 发现问题后立即记录（支持批量，传 findings 参数）\n"
            "- emit_verdict(findings): 结束审查并提交报告\n\n"
            "规则：\n"
            "1. 只审查 2~4 个核心源代码文件，忽略文档和配置\n"
            "2. 每个文件只需 read_file 一次（返回完整内容）\n"
            "3. 发现问题立即 record_finding 记录\n"
            "4. 核心文件审查完毕后立即 emit_verdict 结案\n"
            "5. 如果代码质量良好无明显问题，直接 emit_verdict 提交空报告\n"
            "6. 【铁律】运行时崩溃问题（severity=high）必须优先于安全问题报告！\n"
            "7. 如果给出了定向范围，禁止跳出范围做全项目泛审。\n"
            f"\n【本次审查范围】\n{scope_text}\n"
        )

        system_prompt += (
            "\n附加约束：\n"
            "1. 所有 high finding 必须附带 evidence_text，且 evidence_text 必须来自已读文件。\n"
            "2. 所有包含“缺少/不存在/未设置/没有”的结论，若没有直接证据或无法确定，就不要报。\n"
            "3. 禁止同一问题重复 record_finding。\n"
        )
        if target_scope and target_scope.candidate_files:
            tree_text = "定向候选文件:\n" + "\n".join(f"- {path}" for path in target_scope.candidate_files)
            src_count = len(target_scope.candidate_files)
        else:
            from core.skills.tech_lead_skills import ListFilesSkill
            tree_text = ListFilesSkill(project_dir).execute(max_depth=3)
            src_exts = ('.py', '.js', '.ts', '.jsx', '.tsx', '.html', '.vue', '.css')
            src_count = sum(1 for line in tree_text.split('\n')
                            if any(line.strip().endswith(ext) for ext in src_exts))

        if max_steps is None:
            max_steps = min(max(src_count * 2 + 3, 8), MAX_AUDIT_STEPS)
        logger.info(f"📊 [TechLead] 检测到 {src_count} 个源文件，max_steps={max_steps}")

        user_prompt = (
            f"审查要求：{user_request}\n\n"
            f"项目文件树：\n{tree_text}\n\n"
            f"请根据文件树挑选 2~3 个核心源代码文件开始审查。"
        )

        result = self._react_loop(
            system_prompt, user_prompt, project_dir, sandbox_dir=None,
            max_steps=max_steps, mode="audit", agent_findings=global_findings,
            allowed_files=(target_scope.candidate_files if target_scope else None),
        )

        # 组合独立发现和最后阶段的额外发现
        final_list = list(global_findings)
        if result and result.get("findings"):
            final_list.extend(result["findings"])
        validated_findings, dropped_findings = validate_audit_findings(
            project_dir,
            final_list,
            allowed_files=(target_scope.candidate_files if target_scope else None),
        )
        if dropped_findings:
            logger.warning("audit guard dropped %s findings", len(dropped_findings))
        return validated_findings

    # ============================================================
    # 向后兼容: arbitrate() (旧版跨文件仲裁接口)
    # ============================================================

    def arbitrate(self, current_file: str, current_code: str,
                  conflict_file: str, conflict_code: str,
                  l06_error: str, user_requirement: str) -> Optional[Dict]:
        """
        [向后兼容] 跨文件冲突仲裁。
        内部委托 investigate()，不再独立实现。
        """
        # 构造任务上下文
        task_context = (
            f"【跨文件冲突】\n"
            f"当前文件: {current_file}\n"
            f"冲突文件: {conflict_file}\n"
            f"L0.6 错误: {l06_error}\n"
            f"用户需求: {user_requirement[:300]}\n\n"
            f"--- {current_file} (部分) ---\n{current_code[:2000]}\n\n"
            f"--- {conflict_file} (部分) ---\n{conflict_code[:2000]}"
        )

        # 推断项目目录
        project_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        projects_base = os.path.join(project_dir, "projects")

        result = self.investigate(projects_base, task_context, max_steps=5)
        if result:
            return {
                "guilty_file": result.get("guilty_file", current_file),
                "fix_instruction": result.get("fix_instruction", ""),
                "reasoning": result.get("root_cause", ""),
            }
        return None

    # ============================================================
    # ReAct 核心循环
    # ============================================================

    def _react_loop(self, system_prompt: str, user_prompt: str,
                    project_dir: str, sandbox_dir: str = None,
                    max_steps: int = 8, mode: str = "investigate",
                    agent_findings: list = None, allowed_files: list = None) -> Optional[dict]:
        """
        通用 ReAct 循环引擎。

        Args:
            mode: "investigate" 或 "audit"
        """
        from core.skills.tech_lead_skills import build_tech_lead_skills

        # 构建 Skill 集合
        skills = build_tech_lead_skills(project_dir, sandbox_dir, agent_findings, allowed_files=allowed_files)
        tool_schemas = [skill.schema() for skill in skills.values()]

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        verdict_result = None
        file_read_counts = {}  # 记录单个文件的读取次数
        action_counts = {}     # 记录特定工具调用次数

        for step in range(max_steps):
            if verdict_result:
                break

            remaining = max_steps - step
            logger.info(f"🔍 [TechLead] Step {step + 1}/{max_steps}")
            
            # === 滑动折叠历史 (Sliding Trimmer) ===
            # 放宽至近 6-8 轮的记忆全保留，超出后才折叠，防止其过度失忆而跑回去重看
            if len(messages) > 18:
                for idx in range(2, len(messages) - 12):
                    msg = messages[idx]
                    if getattr(msg, "get", None) and msg.get("role") == "tool":
                        content = msg.get("content", "")
                        if len(content) > 1000 and not content.startswith("[长代码安全折叠"):
                            messages[idx]["content"] = f"[长代码安全折叠：前期文件已脱水。核心逻辑已存于你的脑海，严禁为了细枝末节反复切片重读源文件！]\n{content[:200]}..."

            if remaining <= 2:
                urgency_msg = (
                    f"【系统警告】还有最后 {remaining} 步强制结束！你必须立即调用 `emit_verdict` 结案。"
                    "如果你没有找到任何严重问题或觉得无需修改代码，"
                    "请直接调用 `emit_verdict` 提交空的调查/审查结果，切勿继续无目的地搜索！"
                )
                messages.append({"role": "user", "content": urgency_msg})
                logger.warning(f"⚡ [TechLead] 触发末路警告 (remaining={remaining})")

            try:
                response = default_llm.chat_completion(
                    model=self.model,
                    messages=messages,
                    tools=tool_schemas,
                    temperature=0.1,
                    enable_thinking=self.enable_thinking,
                    reasoning_effort=self._reasoning_effort,
                )
            except Exception as e:
                logger.error(f"❌ TechLead LLM 调用失败: {e}", exc_info=True)
                break

            # 没有 tool_calls → LLM 自行结束（不应该，但要处理）
            if not getattr(response, "tool_calls", None):
                content = getattr(response, "content", "") or ""
                logger.warning(f"⚠️ TechLead 未调用工具，直接输出: {content[:100]}")
                messages.append({"role": "assistant", "content": content})
                # 尝试从纯文本中提取信息
                if mode == "investigate":
                    verdict_result = {
                        "root_cause": content[:500],
                        "root_cause_type": "wrong_target",
                        "fix_instruction": content[:500],
                        "guilty_file": "",
                        "recommended_target_files": [],
                        "confidence": 0.0,
                    }
                break

            # 追加 assistant 消息（含 tool_calls）
            messages.append(response)

            # 推送 LLM 思考文本（透明化）
            thinking = getattr(response, "content", "") or ""
            if thinking.strip():
                global_broadcaster.emit_sync("TechLead", "thinking",
                    f"💭 {thinking[:200]}")

            # 执行每个 tool_call
            for tc in response.tool_calls:
                func_name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}

                # 记录动作类别
                action_counts[func_name] = action_counts.get(func_name, 0) + 1

                # === 物理级防死扣拦截器 ===
                if func_name == "read_file":
                    file_path = args.get("file_path", "unknown")
                    file_read_counts[file_path] = file_read_counts.get(file_path, 0) + 1
                    # 审计模式：同文件只允许读 1 次
                    # 排障模式：允许 2 次（全量 + 一次精读），第 3 次起拦截
                    max_reads = 1 if mode == "audit" else 2
                    if file_read_counts[file_path] > max_reads:
                        logger.warning(f"🛡️ [TechLead] 拦截重复读取 {file_path} (第 {file_read_counts[file_path]} 次)")
                        intercept_msg = (
                            f"该文件已完整读取过 {max_reads} 次，内容在上下文中。"
                            "请直接从记忆中分析，不要反复重读同一文件！"
                            "如果已有足够信息请立即调用 emit_verdict 结案。"
                        )
                        messages.append({"role": "tool", "tool_call_id": tc.id,
                                         "name": func_name, "content": intercept_msg})
                        global_broadcaster.emit_sync("TechLead", "intercepted",
                            f"🛡️ 拦截: {file_path} 已达查阅上限")
                        continue

                if mode == "audit":
                    if func_name == "grep_project" and action_counts.get("grep_project", 0) > 3:
                        logger.warning(f"🛡️ [TechLead] 拦截 grep_project (第 {action_counts['grep_project']} 次)")
                        intercept_msg = "搜索次数已达上限。请根据已有信息推理，并调用 emit_verdict 结案。"
                        messages.append({"role": "tool", "tool_call_id": tc.id,
                                         "name": func_name, "content": intercept_msg})
                        global_broadcaster.emit_sync("TechLead", "intercepted",
                            f"🛡️ 拦截: grep_project 已达配额")
                        continue

                logger.info(f"🔧 [TechLead] tool_call: {func_name}({', '.join(f'{k}={str(v)[:50]}' for k,v in args.items())})")
                global_broadcaster.emit_sync("TechLead", "tool_call",
                    f"🔧 {func_name}({', '.join(f'{k}={str(v)[:30]}' for k,v in args.items())})")

                # emit_verdict → 终止循环
                if func_name == "emit_verdict":
                    verdict_result = self._parse_verdict(args, mode)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": func_name,
                        "content": "VERDICT_ACCEPTED",
                    })
                    break

                # 执行普通 Skill
                skill = skills.get(func_name)
                if skill:
                    try:
                        result_text = skill.execute(**args)
                    except Exception as e:
                        result_text = f"工具执行异常: {e}"
                else:
                    result_text = f"未知工具: {func_name}"

                # 截断过长的结果（1M 上下文窗口足够承载，仅做极端防护）
                max_result_len = 20000
                if len(result_text) > max_result_len:
                    result_text = result_text[:max_result_len] + "\n... (超过 20000 字符已截断)"

                # 动态进度贴片 + 已读文件列表（防骑驴找驴）
                header = f"[进度: 第 {step+1}步/总{max_steps}步]\n"
                if file_read_counts:
                    read_list = ", ".join(file_read_counts.keys())
                    header += f"[已完整读取: {read_list}。内容在上下文中，无需 grep 验证]\n"
                header += "[如无事可做请调 emit_verdict 终止]\n"
                result_text = header + result_text

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": func_name,
                    "content": result_text,
                })

        # 循环结束
        if verdict_result:
            if mode == "investigate":
                logger.info(
                    f"✅ [TechLead] 排障完成: root_cause={verdict_result.get('root_cause', '')[:80]}"
                )
                global_broadcaster.emit_sync("TechLead", "investigate_done",
                    f"✅ TechLead 排障完成: {verdict_result.get('root_cause', '')[:60]}")
            else:
                findings_count = len(verdict_result.get("findings", []))
                logger.info(f"✅ [TechLead] 审查完成: {findings_count} 个发现")
                global_broadcaster.emit_sync("TechLead", "audit_done",
                    f"✅ TechLead 审查完成: {findings_count} 个发现")
        else:
            logger.warning(f"⚠️ [TechLead] ReAct 循环结束但未产出判定 (mode={mode})")
            global_broadcaster.emit_sync("TechLead", "timeout",
                "⚠️ TechLead 调查超时，尝试合成降级判定...")

            # === 降级兜底：从历史上下文中合成低置信度判定 ===
            if mode == "investigate":
                # 收集线索：已读文件 + 最后一条 assistant 思考
                read_files = list(file_read_counts.keys())
                last_thinking = ""
                for msg in reversed(messages):
                    role = msg.get("role") if isinstance(msg, dict) else getattr(msg, "role", "")
                    content = msg.get("content", "") if isinstance(msg, dict) else getattr(msg, "content", "")
                    if role == "assistant" and content and len(content) > 20:
                        last_thinking = content[:500]
                        break

                if read_files or last_thinking:
                    verdict_result = {
                        "root_cause": f"TechLead 调查超时未能明确收敛。已检查文件: {', '.join(read_files[:5])}。最后思考: {last_thinking[:300]}",
                        "root_cause_type": "wrong_target",
                        "fix_instruction": f"请 Coder 重点检查以下文件中与用户报告问题相关的逻辑: {', '.join(read_files[:3])}",
                        "guilty_file": read_files[0] if read_files else "",
                        "recommended_target_files": read_files[1:4],
                        "confidence": 0.2,
                    }
                    logger.info(f"🔄 [TechLead] 降级判定已合成 (guilty={verdict_result['guilty_file']})")
                    global_broadcaster.emit_sync("TechLead", "fallback_verdict",
                        f"🔄 TechLead 降级判定: {verdict_result['guilty_file'] or '无明确嫌疑文件'}")

        return verdict_result

    @staticmethod
    def _parse_verdict(args: dict, mode: str) -> dict:
        """解析 emit_verdict 的参数"""
        if mode == "investigate":
            recommended = args.get("recommended_target_files", [])
            if isinstance(recommended, str):
                try:
                    recommended = json.loads(recommended)
                except (json.JSONDecodeError, TypeError):
                    recommended = [item.strip() for item in recommended.split(",") if item.strip()]
            if not isinstance(recommended, list):
                recommended = []
            qa_plan = args.get("qa_plan", [])
            if isinstance(qa_plan, str):
                try:
                    qa_plan = json.loads(qa_plan)
                except (json.JSONDecodeError, TypeError):
                    qa_plan = []
            if isinstance(qa_plan, dict):
                qa_plan = [qa_plan]
            if not isinstance(qa_plan, list):
                qa_plan = []
            return {
                "root_cause": args.get("root_cause", "未知"),
                "root_cause_type": args.get("root_cause_type", "wrong_target"),
                "fix_instruction": args.get("fix_instruction", ""),
                "guilty_file": args.get("guilty_file", ""),
                "recommended_target_files": recommended,
                "qa_plan": qa_plan,
                "confidence": float(args.get("confidence", 0.0) or 0.0),
            }
        else:
            # audit 模式：解析 findings，兼容 list / str / 空值
            findings_raw = args.get("findings", [])
            if isinstance(findings_raw, list):
                findings = findings_raw
            elif isinstance(findings_raw, str):
                try:
                    findings = json.loads(findings_raw)
                    if not isinstance(findings, list):
                        findings = [findings]
                except (json.JSONDecodeError, TypeError):
                    findings = [{
                        "file": "unknown", "line": 0, "severity": "info",
                        "category": "未分类", "issue": str(findings_raw)[:500],
                        "suggestion": ""
                    }]
            else:
                findings = []
            normalized = []
            for item in findings:
                if not isinstance(item, dict):
                    continue
                normalized.append({
                    "file": item.get("file", ""),
                    "line": item.get("line", 0),
                    "severity": item.get("severity", "info"),
                    "category": item.get("category", "质量"),
                    "issue": item.get("issue", ""),
                    "suggestion": item.get("suggestion", ""),
                    "evidence_text": item.get("evidence_text", item.get("evidence_excerpt", "")),
                    "confidence": item.get("confidence"),
                })
            return {"findings": normalized}
