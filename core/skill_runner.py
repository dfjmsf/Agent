"""
SkillRunner — QA Agent 的 Skill 执行引擎（兼容桥）

本模块现为兼容层，内部委托 core.skills.* 的具体 Skill 实现。
外部接口（构造函数、get_tool_schemas、execute、cleanup）保持 100% 不变，
确保 QA Agent 的 `from core.skill_runner import SkillRunner` 零改动。

实际 Skill 实现已拆分至：
- core/skills/sandbox_terminal.py  (run_terminal)
- core/skills/sandbox_http.py      (http_request)
- core/skills/file_reader.py       (read_file)
- core/skills/port_checker.py      (check_port)
"""
import json
import logging

from core.skills.sandbox_terminal import SandboxTerminalSkill
from core.skills.sandbox_http import SandboxHttpSkill
from core.skills.file_reader import FileReaderSkill
from core.skills.port_checker import PortCheckerSkill
from core.skills.check_ui_visuals import CheckUIVisualsSkill

logger = logging.getLogger("SkillRunner")


class SkillRunner:
    """Skill 执行引擎 — QA Agent 专属（兼容桥）"""

    def __init__(self, sandbox_dir: str, project_id: str, venv_python: str = ""):
        """
        Args:
            sandbox_dir: 沙盒目录（QA 的全部操作边界）
            project_id: 项目 ID
            venv_python: sandbox venv 的 python 可执行文件路径
        """
        self.sandbox_dir = sandbox_dir
        self.project_id = project_id
        self.venv_python = venv_python or "python"

        # 组装 Skill 实例
        self._terminal = SandboxTerminalSkill(sandbox_dir, self.venv_python)
        self._http = SandboxHttpSkill()
        self._file_reader = FileReaderSkill(sandbox_dir)
        self._port_checker = PortCheckerSkill()
        self._ui_visuals = CheckUIVisualsSkill(sandbox_dir)

        # 名称 → Skill 实例映射
        self._skills = {
            "run_terminal": self._terminal,
            "http_request": self._http,
            "read_file": self._file_reader,
            "check_port": self._port_checker,
            "check_ui_visuals": self._ui_visuals,
        }

    # ============================================================
    # Tool Schemas — 喂给 LLM 的函数签名（接口不变）
    # ============================================================

    def get_tool_schemas(self) -> list:
        """返回 QA Agent 可用的所有 Skill 的 JSON Schema"""
        schemas = [skill.schema() for skill in self._skills.values()]
        # report_result 是控制流信号，不属于任何 Skill，直接内联
        schemas.append({
            "type": "function",
            "function": {
                "name": "report_result",
                "description": (
                    "提交最终测试判定。调用此工具将终止测试循环。"
                    "必须在充分测试后才调用。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "passed": {
                            "type": "boolean",
                            "description": "测试是否通过"
                        },
                        "feedback": {
                            "type": "string",
                            "description": "测试结果的详细描述（通过时写测试摘要，失败时写具体错误和修复建议）"
                        },
                        "failed_files": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "失败时需要修复的文件列表（如 ['app.py', 'models.py']）"
                        },
                        "error_type": {
                            "type": "string",
                            "description": "可选。结构化错误类型，如 IMPORT_SYMBOL_MISSING、APP_BOOT_SYNTAX_ERROR"
                        },
                        "importer_file": {
                            "type": "string",
                            "description": "可选。发起错误导入的文件"
                        },
                        "provider_file": {
                            "type": "string",
                            "description": "可选。被导入的本地模块文件"
                        },
                        "missing_symbols": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "可选。启动失败时识别出的缺失符号列表"
                        },
                        "repair_scope": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "可选。建议一起修复的文件范围"
                        },
                        "endpoint_results": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "method": {"type": "string", "description": "HTTP 方法 (GET/POST/PUT/DELETE)"},
                                    "url": {"type": "string", "description": "完整测试 URL"},
                                    "status_code": {"type": "integer", "description": "HTTP 响应状态码"},
                                    "ok": {"type": "boolean", "description": "该端点是否通过 (2xx = true)"},
                                    "detail": {"type": "string", "description": "失败时的简要原因"},
                                },
                                "required": ["method", "url", "status_code", "ok"],
                            },
                            "description": "每个被测端点的逐条结果。即使部分失败也要列出所有已测端点。"
                        },
                    },
                    "required": ["passed", "feedback"],
                },
            },
        })
        return schemas

    # ============================================================
    # 统一执行入口（接口不变）
    # ============================================================

    def execute(self, tool_name: str, arguments: dict) -> str:
        """
        执行一个 Skill，返回文本结果。

        Args:
            tool_name: Skill 名称
            arguments: LLM 传入的参数字典

        Returns:
            执行结果的文本描述
        """
        # report_result 由 QA Agent _react_loop 硬拦截，不会到这里
        # 但保留兜底以防万一
        if tool_name == "report_result":
            return json.dumps({
                "passed": arguments.get("passed", False),
                "feedback": arguments.get("feedback", ""),
                "failed_files": arguments.get("failed_files", []),
                "error_type": arguments.get("error_type", ""),
                "importer_file": arguments.get("importer_file", ""),
                "provider_file": arguments.get("provider_file", ""),
                "missing_symbols": arguments.get("missing_symbols", []),
                "repair_scope": arguments.get("repair_scope", []),
                "endpoint_results": arguments.get("endpoint_results", []),
            })

        skill = self._skills.get(tool_name)
        if not skill:
            return f"错误: 未知的 Skill '{tool_name}'"

        try:
            return skill.execute(**arguments)
        except TypeError as e:
            return f"错误: 参数不匹配 — {e}"
        except Exception as e:
            logger.error(f"Skill '{tool_name}' 执行异常: {e}")
            return f"错误: {type(e).__name__}: {e}"

    # ============================================================
    # 资源清理（接口不变）
    # ============================================================

    def cleanup(self):
        """QA Agent 结束时调用的总清理"""
        self._terminal.cleanup_server()


if __name__ == "__main__":
    """简单自测"""
    import os
    import tempfile

    print("=== SkillRunner 自测（兼容桥模式）===\n")

    with tempfile.TemporaryDirectory() as td:
        runner = SkillRunner(sandbox_dir=td, project_id="test", venv_python="python")

        # Test 1: get_tool_schemas
        schemas = runner.get_tool_schemas()
        assert len(schemas) == 6
        names = {s["function"]["name"] for s in schemas}
        assert names == {"run_terminal", "read_file", "http_request", "check_port", "check_ui_visuals", "report_result"}
        print(f"✅ Test 1: 6 个 Skill Schema 完整")

        # Test 2: run_terminal
        result = runner.execute("run_terminal", {"command": "echo hello_world"})
        assert "hello_world" in result
        print(f"✅ Test 2: run_terminal echo → {result.strip()[:50]}")

        # Test 3: read_file (存在的文件)
        with open(os.path.join(td, "test.txt"), "w") as f:
            f.write("hello from file")
        result = runner.execute("read_file", {"file_path": "test.txt"})
        assert "hello from file" in result
        print(f"✅ Test 3: read_file → '{result.strip()[:30]}'")

        # Test 4: read_file (越狱防护)
        result = runner.execute("read_file", {"file_path": "../../etc/passwd"})
        assert "禁止" in result or "错误" in result
        print(f"✅ Test 4: 越狱防护 → '{result.strip()[:50]}'")

        # Test 5: check_port (未监听的端口)
        result = runner.execute("check_port", {"port": 59999})
        assert "未监听" in result
        print(f"✅ Test 5: check_port 59999 → '{result.strip()}'")

        # Test 6: http_request (localhost 校验)
        result = runner.execute("http_request", {"method": "GET", "url": "http://evil.com/api"})
        assert "错误" in result
        print(f"✅ Test 6: 外部 URL 拦截 → '{result.strip()[:50]}'")

        # Test 7: report_result
        result = runner.execute("report_result", {"passed": True, "feedback": "all good"})
        parsed = json.loads(result)
        assert parsed["passed"] is True
        print(f"✅ Test 7: report_result → {parsed}")

        # Test 8: 未知 Skill
        result = runner.execute("edit_file", {"path": "x"})
        assert "未知" in result
        print(f"✅ Test 8: 未知 Skill 拦截 → '{result.strip()[:50]}'")

        runner.cleanup()

    print("\n🎉 SkillRunner 兼容桥模式 — 全部自测通过！")
