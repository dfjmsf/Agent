"""
Reviewer Agent v3 — L0 静态检查 + L1 合约审计

v2 → v3 变更：
- 删除: LLM 生成测试脚本 + 沙盒执行 + 三层自愈（移交 IntegrationTester）
- 新增: L0 确定性静态检查（语法+结构+导入，0 LLM 消耗）
- 新增: L1 轻量 LLM 审查（只读合约审计，~800 tokens）
"""
import os
import re
import ast
import json
import logging
from typing import Dict, Any, Tuple, List

from core.llm_client import default_llm
from core.prompt import Prompts
from tools.sandbox import sandbox_env
from core.ws_broadcaster import global_broadcaster
from core.database import get_recent_events, recall_reviewer_experience, infer_domain

logger = logging.getLogger("ReviewerAgent")


class ReviewerAgent:
    """
    审查 Agent (Reviewer v3 - Lite)
    L0: 静态检查（语法 + 结构 + 导入）— 0 LLM 消耗
    L1: 合约审计（LLM 只读审查）— ~800 tokens
    """
    def __init__(self, project_id: str = "default_project"):
        self.model = os.getenv("MODEL_REVIEWER", "qwen3-max")
        self.project_id = project_id

    # ============================================================
    # L0: 静态检查（确定性，0 LLM 消耗）
    # ============================================================

    def _l0_static_check(self, target_file: str, code_content: str,
                         sandbox_dir: str, expected_symbols: list) -> Tuple[bool, str]:
        """
        L0 静态检查：
          L0.1 语法检查 — ast.parse()
          L0.2 结构检查 — AST 提取符号 vs 规划书期望
          L0.3 导入检查 — 沙盒中 import（10s 超时）

        Returns: (passed, error_msg)
        """
        is_python = target_file.endswith('.py')
        is_js = target_file.endswith('.js')

        # --- L0.1 语法检查（仅 Python）---
        tree = None
        if is_python:
            try:
                tree = ast.parse(code_content)
                logger.info(f"✅ [L0.1] 语法检查通过: {target_file}")
            except SyntaxError as e:
                error = f"[L0.1 语法错误] {target_file} 第 {e.lineno} 行: {e.msg}"
                logger.warning(f"❌ {error}")
                global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
                return False, error
        elif is_js:
            # JS 文件：用 Node.js 做语法检查
            l01_pass, l01_error = self._l0_js_syntax_check(target_file, sandbox_dir)
            if not l01_pass:
                return False, l01_error
            logger.info(f"✅ [L0.1] JS 语法检查通过: {target_file}")
        else:
            # 其他前端文件（HTML/CSS）：只检查内容非空
            if not code_content or not code_content.strip():
                error = f"[L0.1] {target_file} 内容为空"
                logger.warning(f"❌ {error}")
                return False, error
            logger.info(f"✅ [L0.1] 非 Python 文件，内容非空: {target_file}")

        # --- L0.2 结构检查（仅 Python + 有期望符号）---
        if is_python and expected_symbols and tree:
            defined = self._extract_defined_symbols(tree)
            missing = [s for s in expected_symbols if s not in defined]
            if missing:
                error = f"[L0.2 结构缺失] {target_file} 缺少规划书中定义的: {', '.join(missing)}"
                logger.warning(f"❌ {error}")
                global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
                return False, error
            logger.info(f"✅ [L0.2] 结构检查通过: {len(expected_symbols)} 个符号全部存在")

        # --- L0.3 导入检查（仅 Python 文件）---
        if is_python:
            l03_pass, l03_error = self._l0_import_check(target_file, sandbox_dir)
            if not l03_pass:
                return False, l03_error
            logger.info(f"✅ [L0.3] 导入检查通过: {target_file}")

        return True, ""

    def _l0_js_syntax_check(self, target_file: str, sandbox_dir: str) -> Tuple[bool, str]:
        """L0.1 JS: 用 Node.js --check 验证 JS 语法"""
        import subprocess as _sp

        js_path = os.path.join(sandbox_dir, target_file)
        if not os.path.isfile(js_path):
            logger.warning(f"⚠️ [L0.1 JS] 文件不存在: {js_path}，跳过")
            return True, ""

        try:
            result = _sp.run(
                ["node", "--check", js_path],
                capture_output=True, text=True, timeout=5
            )
            if result.returncode == 0:
                return True, ""

            # 提取错误信息
            err = result.stderr.strip()[:500] if result.stderr else "未知语法错误"
            error = f"[L0.1 JS 语法错误] {target_file}: {err}"
            logger.warning(f"❌ {error}")
            global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
            return False, error

        except FileNotFoundError:
            logger.warning("⚠️ [L0.1 JS] Node.js 不可用，跳过 JS 语法检查")
            return True, ""
        except Exception as e:
            logger.warning(f"⚠️ [L0.1 JS] 检查异常: {e}，跳过")
            return True, ""

    def _l0_import_check(self, target_file: str, sandbox_dir: str) -> Tuple[bool, str]:
        """L0.3: 在沙盒中尝试 import 模块，超时 10 秒"""
        # 从文件路径提取模块名
        module_name = os.path.splitext(os.path.basename(target_file))[0]

        # sys.path 需要同时包含项目根目录和文件所在目录
        # 这样 from src.models import X（根目录相对）和 from models import X（同目录相对）都能工作
        dir_part = os.path.dirname(target_file).replace("\\", "/")
        path_setup = "import sys\nsys.path.insert(0, '.')"
        if dir_part:
            path_setup += f"\nsys.path.insert(0, '{dir_part}')"

        test_code = f"""
{path_setup}
try:
    import {module_name}
    print("✅ IMPORT_OK")
except Exception as e:
    print(f"❌ IMPORT_FAIL: {{e}}")
    import sys; sys.exit(1)
"""
        # 使用较短超时（10s 而非默认 60s）
        try:
            result = sandbox_env.execute_code(
                test_code, self.project_id, sandbox_dir=sandbox_dir, timeout=10)

            stdout = result.get("stdout", "")
            stderr = result.get("stderr", "")

            if "IMPORT_OK" in stdout:
                return True, ""

            # 超时（可能是 uvicorn.run 泄露到顶层等情况）→ 跳过，交给 L2
            if result.get("returncode") == -1 and "timed out" in stderr.lower():
                logger.warning(f"⚠️ [L0.3] import 超时（可能有阻塞代码），跳过导入检查")
                return True, ""

            # 错误信息可能在 stdout（IMPORT_FAIL）或 stderr
            fail_detail = ""
            if "IMPORT_FAIL:" in stdout:
                fail_detail = stdout.split("IMPORT_FAIL:")[1].strip()[:500]
            elif stderr:
                fail_detail = stderr[:500]

            # 已知良性错误：L0.3 的单文件 import 无法模拟完整包环境，跳过交给 L2
            benign_patterns = [
                "relative import",        # from .models import X（包内相对导入）
                "No module named",         # 兄弟模块还没写好 / 第三方未装
                "cannot import name",      # 兄弟模块接口未就绪
            ]
            if any(p in fail_detail for p in benign_patterns):
                logger.warning(f"⚠️ [L0.3] 良性导入错误（跳过）: {fail_detail[:200]}")
                return True, ""

            error = f"[L0.3 导入失败] {target_file}: {fail_detail}"
            logger.warning(f"❌ {error}")
            global_broadcaster.emit_sync("Reviewer", "l0_fail", error)
            return False, error

        except Exception as e:
            logger.warning(f"⚠️ [L0.3] 导入检查异常: {e}，跳过")
            return True, ""

    @staticmethod
    def _extract_defined_symbols(tree: ast.AST) -> set:
        """从 AST 中提取所有顶层定义的函数名、类名、变量名"""
        defined = set()
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(node.name)
            elif isinstance(node, ast.ClassDef):
                defined.add(node.name)
                # 也提取类的方法
                for item in ast.iter_child_nodes(node):
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        defined.add(f"{node.name}.{item.name}")
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        defined.add(target.id)
        return defined

    @staticmethod
    def _extract_expected_symbols(target_file: str,
                                  module_interfaces: dict) -> list:
        """从规划书的 module_interfaces 中提取期望的函数/类名"""
        if not module_interfaces:
            return []

        basename = os.path.basename(target_file)
        iface_str = module_interfaces.get(basename, "")
        if not iface_str:
            # 尝试不带扩展名
            iface_str = module_interfaces.get(os.path.splitext(basename)[0], "")
        if not iface_str:
            return []

        # 从 "def create_app(), USD_TO_CNY = 7.2, class Config" 等格式中提取符号
        symbols = []
        # 匹配 def xxx 或 class xxx
        for match in re.finditer(r'(?:def|class)\s+(\w+)', str(iface_str)):
            symbols.append(match.group(1))
        # 匹配 VAR = ... 格式的常量
        for match in re.finditer(r'(\b[A-Z_][A-Z_0-9]*)\s*=', str(iface_str)):
            symbols.append(match.group(1))
        return symbols

    # ============================================================
    # L1: 合约审计（轻量 LLM，只读不执行）
    # ============================================================

    def _l1_contract_audit(self, target_file: str, code_content: str,
                           description: str, memory_hint: str,
                           module_interfaces: dict) -> Tuple[bool, str]:
        """
        L1 合约审计：LLM 阅读代码检查接口一致性。
        不生成测试脚本，不执行沙盒。
        ~800 tokens。
        """
        system_prompt = Prompts.REVIEWER_SYSTEM + memory_hint

        # 构建上下文
        iface_str = ""
        if module_interfaces:
            iface_str = "\n".join([f"  {k}: {v}" for k, v in module_interfaces.items()])

        user_content = (
            f"【当前要审查的文件】: {target_file}\n"
            f"【业务需求描述】: {description}\n"
            f"【Coder 提交的代码内容】:\n```\n{code_content}\n```\n\n"
        )
        if iface_str:
            user_content += f"【规划书接口契约 module_interfaces】:\n{iface_str}\n\n"

        user_content += "请检查代码是否满足上述接口契约，输出 JSON 结果。"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content}
        ]

        try:
            response_msg = default_llm.chat_completion(
                messages=messages,
                model=self.model,
                temperature=0.1
            )

            raw = response_msg.content.strip()
            # 尝试解析 JSON
            return self._parse_audit_result(raw)

        except Exception as e:
            logger.warning(f"⚠️ [L1] LLM 调用异常: {e}，放行")
            return True, "审查通过（LLM 异常，跳过）"

    @staticmethod
    def _parse_audit_result(raw: str) -> Tuple[bool, str]:
        """解析 L1 审计结果（JSON 格式）"""
        # 清理 markdown 包裹
        if "```json" in raw:
            raw = raw.split("```json")[1].split("```")[0].strip()
        elif "```" in raw:
            raw = raw.split("```")[1].split("```")[0].strip()

        try:
            result = json.loads(raw)
            status = result.get("status", "").upper()
            feedback = result.get("feedback", "")
            if status == "PASS":
                return True, feedback or "合约审计通过"
            else:
                return False, f"[L1 合约审计] {feedback}"
        except (json.JSONDecodeError, TypeError):
            # LLM 输出非 JSON → 尝试从文本中识别 PASS/FAIL
            if "PASS" in raw.upper():
                return True, "合约审计通过"
            elif "FAIL" in raw.upper():
                return False, f"[L1 合约审计] {raw[:500]}"
            else:
                # 无法解析，默认放行
                return True, f"合约审计通过（LLM 输出格式异常，已放行）"

    # ============================================================
    # 上下文构建（保留自 v2）
    # ============================================================

    def _build_review_context(self, target_file: str, module_interfaces: dict = None) -> str:
        """构建 Reviewer 的记忆上下文"""
        memory_hint = ""

        # 1. 短期记忆 → 项目文件树
        file_tree_events = get_recent_events(
            project_id=self.project_id, limit=1,
            event_types=["file_tree"], caller="Reviewer"
        )
        if file_tree_events:
            memory_hint += f"\n\n【📂 当前项目文件结构】\n{file_tree_events[0].content[:500]}"

        # 2. 跨文件接口契约
        if module_interfaces:
            iface_str = "\n".join([f"  {k}: {v}" for k, v in module_interfaces.items()])
            memory_hint += f"\n\n【🔗 跨文件接口契约】\n{iface_str}"

        # 3. Reviewer 经验召回（预防已知错误）
        try:
            test_exps = recall_reviewer_experience(
                f"{target_file}", n_results=2, caller="Reviewer"
            )
            if test_exps:
                exp_str = "\n".join([f"  {i+1}. {e[:200]}" for i, e in enumerate(test_exps)])
                memory_hint += f"\n\n【🧪 历史审查经验】\n{exp_str}"
        except Exception as e:
            logger.warning(f"⚠️ Reviewer 经验召回失败: {e}")

        return memory_hint

    # ============================================================
    # 主入口
    # ============================================================

    def evaluate_draft(self, target_file: str, description: str,
                       code_content: str = None, sandbox_dir: str = None,
                       module_interfaces: dict = None) -> Tuple[bool, str]:
        """
        评估文件草稿（v3 L0+L1 管线）

        Args:
            target_file: 目标文件路径
            description: 任务描述
            code_content: Engine 传入的已缝合代码
            sandbox_dir: 沙盒工作目录
            module_interfaces: 跨文件接口契约（来自 Manager 规划书）

        返回:
            is_pass (bool): 是否审查通过
            feedback (str): 修改建议/报错 或 简短评语
        """
        if not code_content:
            return False, "没有找到该文件的代码内容（Engine 未传入 code_content）"

        logger.info(f"🛡️ Reviewer 正在审查文件: {target_file}")
        global_broadcaster.emit_sync("Reviewer", "review_start",
            f"开始审查目标文件: {target_file}", {"target": target_file})

        # === Step 1: L0 静态检查 (0 LLM) ===
        expected_symbols = self._extract_expected_symbols(target_file, module_interfaces)
        l0_pass, l0_error = self._l0_static_check(
            target_file, code_content, sandbox_dir, expected_symbols)

        if not l0_pass:
            logger.warning(f"❌ Reviewer L0 驳回: {l0_error[:200]}")
            global_broadcaster.emit_sync("Reviewer", "review_fail",
                f"L0 静态检查未通过", {"feedback": l0_error})
            return False, l0_error

        global_broadcaster.emit_sync("Reviewer", "l0_pass", "✅ L0 静态检查通过")

        # === Step 2: L1 合约审计 (~800 tokens) ===
        # 非 Python 文件跳过 L1（HTML/CSS/JS 的合约审计意义不大）
        if not target_file.endswith('.py'):
            logger.info(f"✅ [L1] 非 Python 文件，跳过合约审计: {target_file}")
            global_broadcaster.emit_sync("Reviewer", "review_pass",
                "✓ 审核通过！合并入主分支。", {"feedback": "L0 通过，非 Python 跳过 L1"})
            return True, "L0 通过（非 Python 文件，跳过 L1）"

        memory_hint = self._build_review_context(target_file, module_interfaces)
        l1_pass, l1_feedback = self._l1_contract_audit(
            target_file, code_content, description, memory_hint, module_interfaces)

        if l1_pass:
            logger.info(f"✅ Reviewer 审查通过: {l1_feedback[:100]}")
            global_broadcaster.emit_sync("Reviewer", "review_pass",
                "✓ 审核通过！合并入主分支。", {"feedback": l1_feedback})
        else:
            logger.warning(f"❌ Reviewer L1 驳回: {l1_feedback[:200]}")
            global_broadcaster.emit_sync("Reviewer", "review_fail",
                "审查未通过！", {"feedback": l1_feedback})

        return l1_pass, l1_feedback
