import os
import re
import ast
import json
from typing import Dict, Any, List, Optional

from core.ws_broadcaster import global_broadcaster
from core.blackboard import Blackboard, TaskStatus
from core.route_topology import analyze_route_topology
import logging

logger = logging.getLogger("IntegrationManager")


class IntegrationManager:
    """
    负责执行项目级别的端到端集成测试，
    并在测试失败后，调用 ManagerAgent 进行精确的 mini re-plan 任务重置。
    """

    def __init__(self, blackboard: Blackboard, vfs, project_id: str):
        self.blackboard = blackboard
        self.vfs = vfs
        self.project_id = project_id
        self._last_result: Dict[str, Any] = {}
        self._last_failure_context: Dict[str, Any] = {}

    def needs_integration_test(self, phase_mode: bool = False, is_final_phase: bool = False) -> bool:
        """判断是否需要完整集成测试"""
        # v4.1: Phase 中间阶段跳过完整集成测试（改走 run_startup_check）
        if phase_mode and not is_final_phase:
            logger.info("📦 Phase 中间阶段，跳过完整集成测试（将执行启动验证）")
            return False

        files = [t.target_file for t in self.blackboard.state.tasks]
        has_backend = any(f.endswith('.py') for f in files)
        has_frontend = any(f.endswith(('.html', '.js', '.jsx', '.ts', '.tsx', '.vue')) for f in files)
        # 也检查规划书中是否有 API 关键词
        spec = self.blackboard.state.spec_text or ""
        has_api = any(kw in spec.lower() for kw in ['api', 'flask', 'fastapi', 'uvicorn', 'express', 'http'])

        # 场景 1: 有后端 + 有前端或 API → 需要完整集成测试
        if has_backend and (has_frontend or has_api):
            return True

        # 场景 2: 纯前端 npm 构建项目（有 package.json）→ 需要前端冒烟测试
        has_package_json = any(
            os.path.basename(f) == 'package.json' for f in files
        )
        if has_package_json and has_frontend:
            logger.info("📦 检测到纯前端 npm 项目，启用前端冒烟测试")
            return True

        return False

    def run_startup_check(self) -> bool:
        """v4.1: Phase 中间阶段的轻量级验证 — 只检查进程能否启动"""
        logger.info("🚦 [Phase 轻量验证] 启动检查...")
        global_broadcaster.emit_sync("System", "integration_test",
            "🚦 Phase 中间阶段: 执行启动验证")

        truth_dir = self.blackboard.state.out_dir
        if not truth_dir or not os.path.isdir(truth_dir):
            logger.warning("⚠️ 启动验证跳过: 无项目目录")
            return True

        # 检测入口文件
        entry_file = self._detect_entry_file(truth_dir)
        if not entry_file:
            logger.info("📦 未检测到可启动的入口文件，跳过启动验证")
            return True

        # 尝试启动进程
        try:
            from tools.sandbox import sandbox_env
            venv_python = sandbox_env.venv_manager.get_or_create_venv(self.project_id)
        except Exception:
            venv_python = ""

        python_cmd = venv_python or "python"
        import subprocess
        import time

        entry_path = os.path.join(truth_dir, entry_file)
        env = os.environ.copy()
        env["FLASK_APP"] = entry_file
        env["FLASK_ENV"] = "testing"

        try:
            proc = subprocess.Popen(
                [python_cmd, entry_path],
                cwd=truth_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            time.sleep(3)  # 等待启动

            if proc.poll() is not None:
                # 进程已退出 — 启动失败
                stderr = proc.stderr.read().decode("utf-8", errors="replace")[:500]
                logger.warning(f"❌ 启动验证失败: 进程退出 (rc={proc.returncode})")
                logger.warning(f"   stderr: {stderr[:200]}")
                global_broadcaster.emit_sync("System", "integration_failed",
                    f"❌ 启动验证失败: {stderr[:100]}")

                # 写入 failure context 供后续参考
                self._last_failure_context = {
                    "error_type": "STARTUP_CRASH",
                    "feedback": f"进程启动后立即崩溃: {stderr}",
                }
                return False
            else:
                # 进程还活着 — 启动成功
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                logger.info("✅ 启动验证通过: 进程正常运行")
                global_broadcaster.emit_sync("System", "integration_passed",
                    "✅ Phase 启动验证通过")
                return True
        except Exception as e:
            logger.warning(f"⚠️ 启动验证异常: {e}")
            return True  # 异常时不阻断交付

    @staticmethod
    def _detect_entry_file(project_dir: str) -> str:
        """检测项目入口文件"""
        candidates = ["app.py", "main.py", "run.py", "server.py", "manage.py", "wsgi.py"]
        for c in candidates:
            if os.path.isfile(os.path.join(project_dir, c)):
                return c
        return ""

    def get_last_error(self) -> str:
        """获取上一次验证的错误信息"""
        ctx = self._last_failure_context or {}
        return ctx.get("feedback", "")

    @staticmethod
    def extract_guilty_file_from_stderr(stderr: str, project_dir: str = "") -> str:
        """从 stderr/traceback 中提取最可能的出错文件（相对路径）。
        优先取最后一个项目内文件（离崩溃点最近）。"""
        import re as _re
        # 匹配 Python traceback 中的 File "xxx.py", line N
        file_matches = _re.findall(r'File "([^"]+\.py)"', stderr)
        if not file_matches:
            return ""

        # 过滤掉标准库和 site-packages
        project_files = []
        for fpath in file_matches:
            normalized = fpath.replace("\\", "/")
            if "site-packages" in normalized or "lib/python" in normalized:
                continue
            if project_dir:
                proj_norm = project_dir.replace("\\", "/").rstrip("/")
                if normalized.startswith(proj_norm):
                    rel = normalized[len(proj_norm):].lstrip("/")
                    project_files.append(rel)
                    continue
            # 没有 project_dir 前缀匹配时，取basename
            basename = os.path.basename(normalized)
            if basename not in ("__init__.py",):
                project_files.append(basename)

        # 返回最后一个（离崩溃点最近）
        return project_files[-1] if project_files else ""

    def run_integration_test(self, focus_endpoints: list = None) -> bool:
        """Phase 2.5: 端到端集成测试，返回是否通过

        v4.4: focus_endpoints 按需测试 — 仅测受影响端点 + 回归冒烟。
        """
        qa_mode = os.getenv("QA_MODE", "react")  # react | legacy

        mode_hint = f"按需 {len(focus_endpoints)} 端点" if focus_endpoints else "全量"
        logger.info(f"🧪 [Phase 2.5] 集成测试启动... (模式: {qa_mode}, 范围: {mode_hint})")
        global_broadcaster.emit_sync("System", "integration_test",
            f"🧪 Phase 2.5: 启动集成测试 ({qa_mode} 模式, {mode_hint})")

        # 收集项目所有代码（从真理区，非仅 task 列表）
        all_code = {}
        if self.vfs:
            truth_dir = self.blackboard.state.out_dir
            if truth_dir and os.path.isdir(truth_dir):
                for root, dirs, files in os.walk(truth_dir):
                    dirs[:] = [d for d in dirs if d not in
                               {'__pycache__', '.git', '.astrea', '.sandbox',
                                'venv', '.venv', 'node_modules'}]
                    for f in files:
                        if f.startswith('.'):
                            continue
                        fpath = os.path.join(root, f)
                        rel = os.path.relpath(fpath, truth_dir).replace('\\', '/')
                        try:
                            with open(fpath, "r", encoding="utf-8") as fh:
                                all_code[rel] = fh.read()
                        except Exception:
                            pass
        if not all_code:
            # Fallback: 从 tasks 收集
            for task in self.blackboard.state.tasks:
                code = ""
                if self.vfs:
                    try:
                        truth_path = os.path.join(self.blackboard.state.out_dir, task.target_file)
                        if os.path.isfile(truth_path):
                            with open(truth_path, "r", encoding="utf-8") as f:
                                code = f.read()
                    except Exception:
                        pass
                all_code[task.target_file] = code

        sandbox_dir = self.vfs.sandbox_dir if self.vfs else ""
        venv_python = ""
        try:
            from tools.sandbox import sandbox_env
            venv_python = sandbox_env.venv_manager.get_or_create_venv(self.project_id)
        except Exception:
            pass

        if qa_mode == "react":
            from agents.qa_agent import QAAgent
            qa = QAAgent(self.project_id)
            result = qa.run_qa(
                project_spec=self.blackboard.state.spec_text,
                all_code=all_code,
                sandbox_dir=sandbox_dir,
                venv_python=venv_python,
                focus_endpoints=focus_endpoints,
            )
        else:
            from agents.integration_tester import IntegrationTester
            tester = IntegrationTester(self.project_id)
            result = tester.run_integration_test(
                project_spec=self.blackboard.state.spec_text,
                all_code=all_code,
                sandbox_dir=sandbox_dir,
            )

        self._last_result = result or {}

        # 输出 per-endpoint 清单摘要（如果 QA 返回了 endpoint_results）
        endpoint_results = (result or {}).get("endpoint_results", [])
        if endpoint_results:
            ok_count = sum(1 for ep in endpoint_results if ep.get("ok"))
            total_count = len(endpoint_results)
            ep_summary = f"{ok_count}/{total_count} 端点通过"
            for ep in endpoint_results:
                status_icon = "✅" if ep.get("ok") else "❌"
                detail = f" — {ep.get('detail', '')}" if ep.get("detail") else ""
                logger.info(
                    f"  {status_icon} {ep.get('method', '?')} {ep.get('url', '?')} → {ep.get('status_code', '?')}{detail}"
                )
            global_broadcaster.emit_sync("System", "endpoint_summary",
                f"📊 端点测试清单: {ep_summary}")

        # Phase 5.5: 烂账账本同步 — 将 endpoint_results 同步到 open_issues
        if endpoint_results:
            current_round = len(self.blackboard.state.round_history) + 1
            for ep in endpoint_results:
                method = ep.get("method", "?")
                url = ep.get("url", "?")
                endpoint_key = f"{method} {url}"
                if ep.get("ok"):
                    # 端点通过 → 闭环对应的 open issue
                    self.blackboard.resolve_issues_by_endpoint(endpoint_key, current_round)
                else:
                    # 端点失败 → upsert 到烂账台账
                    detail = ep.get("detail", "")
                    status_code = ep.get("status_code", "?")
                    summary = f"{endpoint_key} → {status_code}"
                    if detail:
                        summary += f" ({detail[:100]})"
                    self.blackboard.upsert_issue(
                        category="qa_failure",
                        summary=summary,
                        related_files=result.get("failed_files", []) or [],
                        related_endpoint=endpoint_key,
                        current_round=current_round,
                    )

        if result["passed"]:
            self._last_failure_context = {}
            if result.get("warning"):
                logger.warning("⚠️ [Phase 2.5] 集成测试未能执行（脚本问题），项目仍交付但标注警告")
                global_broadcaster.emit_sync("System", "integration_warning",
                    "⚠️ 集成测试未能执行，项目已交付但未经端到端验证")
            else:
                logger.info("✅ [Phase 2.5] 集成测试通过！")
                global_broadcaster.emit_sync("System", "integration_passed", "✅ 集成测试通过！")
            return True
        else:
            failure_context = self._build_failure_context(result, all_code)
            self._last_failure_context = failure_context

            # 将 endpoint_results 写入 failure_context 供下游 Manager 消费
            if endpoint_results:
                failure_context["endpoint_results"] = endpoint_results
            failed_endpoint_results = [
                ep for ep in endpoint_results
                if not ep.get("ok")
            ]
            failure_context.setdefault("endpoint_results", endpoint_results)
            failure_context.setdefault("failed_files", result.get("failed_files", []) or [])
            failure_context.setdefault("repair_scope", failure_context.get("failed_files", []))
            failure_context.setdefault("error_type", result.get("error_type") or "integration_failure")
            failure_context["feedback"] = result.get("feedback", "")
            failure_context["passed_count"] = sum(1 for ep in endpoint_results if ep.get("ok"))
            failure_context["failed_count"] = len(failed_endpoint_results)
            failure_context["raw_result"] = {
                "passed": result.get("passed"),
                "warning": result.get("warning"),
            }
            self._last_failure_context = failure_context
            self.blackboard.record_failure_context(
                "integration_warning",
                result.get("feedback", ""),
                extra_context=failure_context,
            )

            affected_files = self._collect_affected_files(
                result.get("failed_files", []), failure_context
            )

            # 将失败信息写入对应 task
            error_msg = f"[集成测试] {result['feedback'][:500]}"
            for tf in affected_files:
                for task in self.blackboard.state.tasks:
                    if task.target_file == tf:
                        self.blackboard.mark_task_error(task.task_id, error_msg)
                        break

            if not affected_files:
                for task in self.blackboard.state.tasks:
                    if task.status == TaskStatus.DONE:
                        self.blackboard.mark_task_error(task.task_id, error_msg)

            if failure_context:
                logger.warning(
                    "🩺 [Phase 2.5] 结构化诊断: type=%s importer=%s provider=%s missing=%s",
                    failure_context.get("error_type", ""),
                    failure_context.get("importer_file", ""),
                    failure_context.get("provider_file", ""),
                    failure_context.get("missing_symbols", []),
                )

            logger.warning(f"❌ [Phase 2.5] 集成测试失败: {result['feedback'][:200]}")
            global_broadcaster.emit_sync("System", "integration_failed",
                f"❌ 集成测试失败: {result['feedback'][:100]}")
            return False

    def retry_with_replan(self) -> bool:
        """
        集成测试失败后的精确回退：
        回 Manager 做 mini re-plan（全局分诊），重置 Manager 指定文件任务。
        返回 True 表示成功进行了 re-plan 并有待处理的修正任务。
        """
        logger.info("🔄 [Phase 2.5] 集成测试失败，回 Manager 做精确分诊...")
        global_broadcaster.emit_sync("System", "integration_retry",
            "🔄 集成测试失败，Manager 正在分析需修复的文件...")

        feedback_parts = []
        for task in self.blackboard.state.tasks:
            if task.error_logs:
                last_err = task.error_logs[-1] if isinstance(task.error_logs, list) else str(task.error_logs)
                if "[集成测试]" in str(last_err):
                    feedback_parts.append(f"{task.target_file}: {last_err}")
        feedback = "\n".join(feedback_parts) if feedback_parts else "集成测试失败（无详细信息）"

        try:
            from core.database import append_event
            append_event(
                "tdd", "round_fail",
                f"[集成测试失败] {feedback[:800]}",
                project_id=self.project_id,
                metadata={"source": "integration_test"}
            )
            logger.info("📝 集成测试报错已写入短期记忆（供 Coder 修复参考）")
        except Exception as e:
            logger.warning(f"⚠️ 写入集成测试短期记忆失败: {e}")

        try:
            from agents.manager import ManagerAgent
            manager = ManagerAgent(project_id=self.project_id)

            patch_requirement = self._build_patch_requirement(feedback)

            spec_text = self.blackboard.state.spec_text or ""
            playbook_hint = ""
            try:
                from core.playbook_loader import PlaybookLoader
                _pb = PlaybookLoader()
                _tech = (self.blackboard.state.project_spec or {}).get("tech_stack", [])
                full_pb = _pb.load_for_coder(_tech, "app.py")
                if full_pb:
                    iron_rules = [line for line in full_pb.split("\n")
                                  if any(k in line for k in ("禁止", "严禁", "铁律", "绝对不", "MUST NOT"))]
                    if iron_rules:
                        playbook_hint = "\n".join(iron_rules[:20])
                        logger.info(f"📜 [Mini Re-plan] 注入 {len(iron_rules)} 条 Playbook 铁律")
            except Exception as e:
                logger.warning(f"⚠️ Playbook 铁律提取失败: {e}")

            patch_plan = manager.plan_patch(
                patch_requirement,
                project_spec=spec_text,
                playbook_hint=playbook_hint,
            )

            tasks_to_fix = self._merge_patch_tasks_with_failure_context(
                patch_plan.get("tasks", []) or [],
                self._last_failure_context,
            )

            if not tasks_to_fix:
                logger.warning("⚠️ Manager 未识别出需要修复的文件，使用原始回退逻辑 (重试现有 ERROR/TODO)")
                return True

            reset_count = 0
            for patch_task in tasks_to_fix:
                target = patch_task.get("target_file", "")
                fix_desc = patch_task.get("description", "")
                for task in self.blackboard.state.tasks:
                    if task.target_file == target:
                        fix_round = len([log for log in (task.error_logs or [])
                                        if "[集成测试]" in str(log)]) + 1
                        original_desc = task.description
                        new_desc = (
                            f"[FIX_{fix_round}] {fix_desc}\n"
                            f"--- 原始任务 ---\n{original_desc}"
                        )
                        fix_feedback = (
                            f"【集成测试失败 — QA 原始报错】\n{feedback}\n\n"
                            f"【Manager 修复指令】\n{fix_desc}"
                        )
                        self.blackboard.reset_task_for_fix(
                            task.task_id, new_desc, fix_feedback,
                            reset_retry=True,
                        )
                        reset_count += 1
                        logger.info(f"🎯 [Mini Re-plan] {target}: [FIX_{fix_round}] {fix_desc[:80]}")
                        break

            if reset_count == 0:
                logger.warning("⚠️ Manager 指定的文件不在任务列表中，使用原始回退逻辑")
                return True

            logger.info(f"🔄 [Mini Re-plan] Manager 分诊完成: {reset_count} 个文件需修复")
            global_broadcaster.emit_sync("System", "integration_replan",
                f"🔄 Manager 精确分诊: {reset_count} 个文件需修复")
            return True

        except Exception as e:
            logger.error(f"❌ [Mini Re-plan] Manager 调用异常: {e}，使用原始回退逻辑")
            return True

    def _build_failure_context(self, result: Dict[str, Any], all_code: Dict[str, str]) -> Dict[str, Any]:
        """从 QA 结果中提取结构化失败上下文。"""
        structured_keys = (
            "error_type", "importer_file", "provider_file",
            "missing_symbols", "repair_scope",
            "double_prefixed_blueprints", "unregistered_handlers", "missing_effective_routes",
        )
        if any((result or {}).get(key) for key in structured_keys):
            return {
                "error_type": (result or {}).get("error_type", ""),
                "missing_symbol": ((result or {}).get("missing_symbols", []) or [""])[0],
                "missing_symbols": (result or {}).get("missing_symbols", []) or [],
                "import_module": os.path.splitext(os.path.basename((result or {}).get("provider_file", "")))[0],
                "importer_file": (result or {}).get("importer_file", ""),
                "provider_file": (result or {}).get("provider_file", ""),
                "repair_scope": (result or {}).get("repair_scope", []) or [],
                "double_prefixed_blueprints": (result or {}).get("double_prefixed_blueprints", []) or [],
                "unregistered_handlers": (result or {}).get("unregistered_handlers", []) or [],
                "missing_effective_routes": (result or {}).get("missing_effective_routes", []) or [],
            }

        feedback = (result or {}).get("feedback", "") or ""
        failed_files = (result or {}).get("failed_files", []) or []

        import_diag = self._diagnose_import_symbol_mismatch(
            feedback=feedback,
            failed_files=failed_files,
            all_code=all_code,
        )
        if import_diag:
            return import_diag

        route_diag = self._diagnose_route_topology_mismatch(
            feedback=feedback,
            all_code=all_code,
        )
        if route_diag:
            return route_diag

        return {}

    def _build_patch_requirement(self, feedback: str) -> str:
        """构造传给 Manager 的 mini re-plan 文本，附加结构化诊断结果。"""
        parts = [
            "[集成测试失败，需要修复]",
            feedback,
        ]

        ctx = self._last_failure_context or {}
        if ctx:
            parts.extend([
                "",
                "【结构化诊断】",
                f"- error_type: {ctx.get('error_type', '')}",
                f"- importer_file: {ctx.get('importer_file', '')}",
                f"- provider_file: {ctx.get('provider_file', '')}",
                f"- import_module: {ctx.get('import_module', '')}",
                f"- first_missing_symbol: {ctx.get('missing_symbol', '')}",
                f"- missing_symbols: {', '.join(ctx.get('missing_symbols', []))}",
                f"- repair_scope: {', '.join(ctx.get('repair_scope', []))}",
            ])
            if ctx.get("error_type") == "IMPORT_SYMBOL_MISSING" and ctx.get("missing_symbols"):
                parts.extend([
                    "",
                    "【强约束】",
                    "这不是只缺一个符号的问题。",
                    "你必须一次性补齐 missing_symbols 中的全部符号，禁止只修第一个报错后就结束。",
                ])
            if ctx.get("error_type") == "ROUTE_TOPOLOGY_MISMATCH":
                parts.extend([
                    f"- double_prefixed_blueprints: {json.dumps(ctx.get('double_prefixed_blueprints', []), ensure_ascii=False)}",
                    f"- unregistered_handlers: {json.dumps(ctx.get('unregistered_handlers', []), ensure_ascii=False)}",
                    f"- missing_effective_routes: {', '.join(ctx.get('missing_effective_routes', []))}",
                    "",
                    "【强约束】",
                    "优先修复路由挂载拓扑，不要只对单个 404 端点打补丁。",
                    "若 blueprint 已在 app.py 挂载 url_prefix，则 route 文件内只能保留相对路径。",
                    "若 handler 已声明但未注册为 decorator/add_url_rule，必须补齐端点注册。",
                ])

            # 注入 per-endpoint 清单（如果有）
            ep_results = ctx.get("endpoint_results", [])
            if ep_results:
                parts.extend(["", "【端点测试清单】"])
                for ep in ep_results:
                    icon = "✅" if ep.get("ok") else "❌"
                    detail = f" — {ep.get('detail', '')}" if ep.get("detail") else ""
                    parts.append(
                        f"  {icon} {ep.get('method', '?')} {ep.get('url', '?')} → {ep.get('status_code', '?')}{detail}"
                    )
                ok_count = sum(1 for ep in ep_results if ep.get("ok"))
                parts.append(f"  合计: {ok_count}/{len(ep_results)} 通过")
                if ok_count > 0:
                    parts.extend([
                        "",
                        "【重要】以上标记 ✅ 的端点当前工作正常，修复时不要破坏它们。",
                        "只修复标记 ❌ 的端点涉及的代码路径。",
                    ])

        parts.extend([
            "",
            "请根据以上错误信息，精确判断哪些文件需要修改、如何修改。",
        ])
        return "\n".join(parts)

    def _merge_patch_tasks_with_failure_context(
        self,
        tasks_to_fix: List[Dict[str, Any]],
        failure_context: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        """将结构化失败上下文并入 patch tasks，避免 Manager 只修第一个缺口。"""
        merged: Dict[str, Dict[str, Any]] = {}
        for task in tasks_to_fix:
            target = task.get("target_file", "")
            if target and target not in merged:
                merged[target] = dict(task)

        error_type = failure_context.get("error_type")
        if error_type == "ROUTE_TOPOLOGY_MISMATCH":
            repair_scope = failure_context.get("repair_scope", []) or []
            double_prefixed = failure_context.get("double_prefixed_blueprints", []) or []
            unregistered = failure_context.get("unregistered_handlers", []) or []

            double_prefixed_modules = {
                item.get("module", ""): item for item in double_prefixed
                if isinstance(item, dict) and item.get("module")
            }
            unregistered_modules = {
                item.get("module", ""): item for item in unregistered
                if isinstance(item, dict) and item.get("module")
            }

            for path in repair_scope:
                if not path or path in merged:
                    continue
                if path in double_prefixed_modules:
                    issue = double_prefixed_modules[path]
                    merged[path] = {
                        "target_file": path,
                        "description": (
                            f"修复 {path} 的路由局部路径，去掉与 app url_prefix 重复的前缀。"
                            f"问题 blueprint={issue.get('blueprint', '')}，local_paths={issue.get('local_paths', [])}。"
                        ),
                    }
                    continue
                if path in unregistered_modules:
                    issue = unregistered_modules[path]
                    merged[path] = {
                        "target_file": path,
                        "description": (
                            f"为 {path} 中未注册的 handler 补齐 HTTP 端点注册："
                            f"{', '.join(issue.get('handlers', []))}。"
                        ),
                    }
                    continue
                merged[path] = {
                    "target_file": path,
                    "description": (
                        f"修复 {path} 的路由挂载拓扑，使其与 app.py 的 blueprint 注册方式一致，"
                        f"并覆盖缺失端点: {', '.join(failure_context.get('missing_effective_routes', []))}。"
                    ),
                }
            return list(merged.values())

        if error_type != "IMPORT_SYMBOL_MISSING":
            return list(merged.values())

        importer_file = failure_context.get("importer_file", "")
        provider_file = failure_context.get("provider_file", "")
        import_module = failure_context.get("import_module", "")
        missing_symbols = failure_context.get("missing_symbols", []) or []

        if provider_file and provider_file not in merged:
            merged[provider_file] = {
                "target_file": provider_file,
                "description": (
                    f"在 {provider_file} 中一次性补齐以下被 {importer_file or import_module} 依赖的缺失符号: "
                    f"{', '.join(missing_symbols)}。确保 `from {import_module} import ...` 可以成功导入。"
                ),
            }

        if importer_file and importer_file not in merged:
            merged[importer_file] = {
                "target_file": importer_file,
                "description": (
                    f"校验 {importer_file} 对 {provider_file or import_module} 的导入与调用。"
                    f"如导入符号集合与实际实现不一致，统一修正为与 provider 一致。"
                ),
            }

        return list(merged.values())

    @staticmethod
    def _collect_affected_files(
        failed_files: List[str],
        failure_context: Dict[str, Any],
    ) -> List[str]:
        """整合 QA failed_files 与结构化诊断的 repair scope。"""
        affected: List[str] = []
        seen = set()
        for path in (failed_files or []) + list(failure_context.get("repair_scope", []) or []):
            if path and path not in seen:
                affected.append(path)
                seen.add(path)
        return affected

    def _diagnose_import_symbol_mismatch(
        self,
        feedback: str,
        failed_files: List[str],
        all_code: Dict[str, str],
    ) -> Dict[str, Any]:
        """诊断本地模块 import 缺符号，批量收集同组缺失接口。"""
        match = re.search(
            r"cannot import name ['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]? from ['\"]?([A-Za-z_][A-Za-z0-9_\.]*)['\"]?",
            feedback or "",
        )
        if not match:
            return {}

        missing_symbol = match.group(1).strip()
        import_module = match.group(2).strip()

        importer_file = self._find_importer_file(all_code, import_module, missing_symbol, failed_files)
        provider_file = self._resolve_provider_file(importer_file, import_module, all_code)

        if not provider_file:
            return {}

        imported_symbols = self._extract_imported_symbols(
            all_code.get(importer_file, ""),
            import_module,
        ) if importer_file else []
        provider_symbols = self._extract_defined_symbols_from_code(
            all_code.get(provider_file, "")
        )

        missing_symbols = [
            symbol for symbol in imported_symbols
            if symbol not in provider_symbols
        ]
        if missing_symbol and missing_symbol not in missing_symbols:
            missing_symbols.insert(0, missing_symbol)
        if not missing_symbols:
            missing_symbols = [missing_symbol]

        repair_scope = [path for path in [provider_file, importer_file] if path]

        return {
            "error_type": "IMPORT_SYMBOL_MISSING",
            "missing_symbol": missing_symbol,
            "missing_symbols": missing_symbols,
            "import_module": import_module,
            "importer_file": importer_file,
            "provider_file": provider_file,
            "repair_scope": repair_scope,
        }

    def _diagnose_route_topology_mismatch(
        self,
        feedback: str,
        all_code: Dict[str, str],
    ) -> Dict[str, Any]:
        """诊断 404/路由注册类错误，输出结构化路由拓扑问题。"""
        lowered = (feedback or "").lower()
        if "404" not in lowered and "not found" not in lowered and "路由" not in (feedback or ""):
            return {}

        topology = analyze_route_topology(
            all_code=all_code,
            project_spec=self.blackboard.state.project_spec or {},
            feedback=feedback,
        )
        if not topology:
            return {}
        return topology

    def _find_importer_file(
        self,
        all_code: Dict[str, str],
        import_module: str,
        missing_symbol: str,
        failed_files: List[str],
    ) -> str:
        """定位发起导入的源文件。优先 failed_files，再退化到全项目扫描。"""
        ordered_files: List[str] = []
        seen = set()
        for path in (failed_files or []) + list(all_code.keys()):
            if path.endswith(".py") and path not in seen:
                ordered_files.append(path)
                seen.add(path)

        for path in ordered_files:
            symbols = self._extract_imported_symbols(all_code.get(path, ""), import_module)
            if missing_symbol in symbols:
                return path

        return ordered_files[0] if ordered_files else ""

    def _resolve_provider_file(
        self,
        importer_file: str,
        import_module: str,
        all_code: Dict[str, str],
    ) -> str:
        """根据 import module 名称解析 provider 文件路径。"""
        module_rel = import_module.replace(".", "/").strip("/")
        if not module_rel:
            return ""

        candidates = [
            f"{module_rel}.py",
            f"{module_rel}/__init__.py",
        ]

        if importer_file:
            importer_dir = os.path.dirname(importer_file).replace("\\", "/").strip("/")
            if importer_dir:
                candidates.extend([
                    f"{importer_dir}/{module_rel}.py",
                    f"{importer_dir}/{module_rel}/__init__.py",
                ])

        seen = set()
        for candidate in candidates:
            normalized = os.path.normpath(candidate).replace("\\", "/")
            if normalized in seen or normalized.startswith("../"):
                continue
            seen.add(normalized)
            if normalized in all_code:
                return normalized

        return ""

    @staticmethod
    def _extract_imported_symbols(importer_code: str, import_module: str) -> List[str]:
        """提取某个 importer 文件从指定模块导入的符号列表。"""
        if not importer_code:
            return []

        try:
            tree = ast.parse(importer_code)
        except SyntaxError:
            return []

        symbols: List[str] = []
        module_suffix = f".{import_module}"
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module:
                continue
            if node.module != import_module and not node.module.endswith(module_suffix):
                continue
            for alias in node.names:
                if alias.name != "*" and alias.name not in symbols:
                    symbols.append(alias.name)

        return symbols

    @staticmethod
    def _extract_defined_symbols_from_code(provider_code: str) -> set:
        """提取 provider 文件中已定义的顶层函数、类和变量名。"""
        if not provider_code:
            return set()

        try:
            tree = ast.parse(provider_code)
        except SyntaxError:
            return set()

        symbols = set()
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                symbols.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        symbols.add(target.id)
        return symbols
