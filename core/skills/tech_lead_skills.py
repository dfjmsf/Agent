"""
TechLead 专属 Skill 集合 — 白盒排障工具箱

包含：
- GrepProjectSkill: 在项目中搜索关键词/正则
- ReadSandboxLogSkill: 读取沙盒运行日志
- EmitVerdictSkill: 输出最终判定（终止 ReAct 循环）

复用说明：
- read_file → 复用 core/skills/file_reader.py 的 FileReaderSkill
- list_files → 复用 tools/observer.py 的 Observer.get_tree()
"""
import os
import re
import json
import logging
from core.skills.base import BaseSkill

logger = logging.getLogger("TechLeadSkills")

# 搜索结果上限（防止 Token 爆炸）
_MAX_GREP_RESULTS = 30
_MAX_LINE_LEN = 200


def _normalize_allowed_files(allowed_files) -> set[str]:
    return {
        str(path).replace("\\", "/").strip("/")
        for path in (allowed_files or [])
        if str(path).strip()
    }


class GrepProjectSkill(BaseSkill):
    """在项目目录中搜索关键词或正则表达式"""

    def __init__(self, project_dir: str, allowed_files=None):
        self.project_dir = project_dir
        self.allowed_files = _normalize_allowed_files(allowed_files)

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "grep_project",
                "description": (
                    "在项目文件中搜索关键词或正则表达式。"
                    "返回匹配的文件名、行号和行内容。"
                    "用于快速定位变量/函数/关键词在哪里被使用。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {
                            "type": "string",
                            "description": "搜索的关键词或正则表达式"
                        },
                        "file_glob": {
                            "type": "string",
                            "description": "文件过滤(例如 '*.py' 或 '*.js')，默认搜索所有源码文件",
                            "default": "*"
                        },
                    },
                    "required": ["pattern"]
                }
            }
        }

    def execute(self, **kwargs) -> str:
        pattern = kwargs.get("pattern", "")
        file_glob = kwargs.get("file_glob", "*")

        if not pattern:
            return "❌ pattern 不能为空"

        # 编译正则（如果失败则退化为普通文本搜索）
        try:
            regex = re.compile(pattern, re.IGNORECASE)
            use_regex = True
        except re.error:
            use_regex = False

        # 源码文件后缀白名单
        SOURCE_EXTS = {'.py', '.js', '.jsx', '.ts', '.tsx', '.html', '.css',
                       '.vue', '.svelte', '.json', '.md', '.yaml', '.yml',
                       '.toml', '.cfg', '.ini', '.txt', '.sql'}
        SKIP_DIRS = {'__pycache__', 'node_modules', '.git', '.venv', 'venv',
                     '.sandbox', '.astrea', 'dist', 'build'}

        # 解析文件 glob
        glob_ext = None
        if file_glob and file_glob != "*":
            if file_glob.startswith("*."):
                glob_ext = file_glob[1:]  # e.g., ".py"

        results = []
        candidate_files = []
        if self.allowed_files:
            candidate_files = sorted(self.allowed_files)
        else:
            for root, dirs, files in os.walk(self.project_dir):
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
                for fname in files:
                    rel_path = os.path.relpath(os.path.join(root, fname), self.project_dir).replace("\\", "/")
                    candidate_files.append(rel_path)

        for rel_path in candidate_files:
            ext = os.path.splitext(rel_path)[1].lower()
            if ext not in SOURCE_EXTS:
                continue
            if glob_ext and ext != glob_ext:
                continue

            fpath = os.path.join(self.project_dir, rel_path)
            if not os.path.isfile(fpath):
                continue

            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                    for line_no, line in enumerate(f, 1):
                        matched = bool(regex.search(line)) if use_regex else pattern.lower() in line.lower()
                        if matched:
                            line_text = line.rstrip()[:_MAX_LINE_LEN]
                            results.append(f"{rel_path}:{line_no}: {line_text}")

                            if len(results) >= _MAX_GREP_RESULTS:
                                results.append(f"... (超过 {_MAX_GREP_RESULTS} 条，已截断)")
                                return "\n".join(results)
            except Exception:
                continue

        if not results:
            return f"未找到匹配 '{pattern}' 的内容"

        return "\n".join(results)


class ListFilesSkill(BaseSkill):
    """列出项目文件结构树"""

    def __init__(self, project_dir: str, allowed_files=None):
        self.project_dir = project_dir
        self.allowed_files = _normalize_allowed_files(allowed_files)

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "list_files",
                "description": "列出项目的文件结构树，理解项目整体架构。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "max_depth": {
                            "type": "integer",
                            "description": "最大遍历深度，默认 3",
                            "default": 3
                        }
                    }
                }
            }
        }

    def execute(self, **kwargs) -> str:
        max_depth = kwargs.get("max_depth", 3)
        if self.allowed_files:
            lines = ["📁 targeted_scope/"]
            for rel_path in sorted(self.allowed_files):
                depth = rel_path.count("/")
                if depth >= max_depth:
                    continue
                prefix = "    " * depth
                lines.append(f"{prefix}└── 📄 {rel_path.split('/')[-1]}  ({rel_path})")
            return "\n".join(lines)
        try:
            from tools.observer import Observer
            obs = Observer(self.project_dir)
            tree = obs.get_tree(max_depth=max_depth)
            if tree:
                return tree
            return "（项目目录为空）"
        except Exception as e:
            return f"文件树获取失败: {e}"


class AuditFileReaderSkill(BaseSkill):
    """审计专用文件读取——直接返回完整内容，不暴露切片参数，防止 LLM 反复回头"""

    def __init__(self, project_dir: str, allowed_files=None):
        self.project_dir = os.path.abspath(project_dir)
        self.allowed_files = _normalize_allowed_files(allowed_files)

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "read_file",
                "description": "读取项目中指定文件的完整代码内容。",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file_path": {
                            "type": "string",
                            "description": "相对于项目根目录的文件路径（如 'app.py'、'routes.py'）"
                        }
                    },
                    "required": ["file_path"]
                }
            }
        }

    def execute(self, **kwargs) -> str:
        file_path = kwargs.get("file_path", "")
        normalized = os.path.normpath(file_path)
        if normalized.startswith("..") or os.path.isabs(normalized):
            return f"错误: 禁止访问项目目录之外的路径 '{file_path}'"
        normalized_rel = normalized.replace("\\", "/").strip("/")
        if self.allowed_files and normalized_rel not in self.allowed_files:
            return f"错误: 当前定向范围禁止读取 '{file_path}'"
        full_path = os.path.join(self.project_dir, normalized)
        if not os.path.abspath(full_path).startswith(self.project_dir):
            return f"错误: 路径越界 '{file_path}'"
        if not os.path.isfile(full_path):
            return f"文件不存在: {file_path}"
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                content = f.read()
            if len(content) > 12000:
                content = content[:12000] + "\n... (文件过大，已截断至 12000 字符)"
            return content
        except Exception as e:
            return f"读取失败: {e}"


class ReadSandboxLogSkill(BaseSkill):
    """读取沙盒运行日志（排障场景专用）"""

    def __init__(self, sandbox_dir: str = None):
        self.sandbox_dir = sandbox_dir

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "read_sandbox_log",
                "description": (
                    "读取沙盒/终端的最近运行日志。"
                    "用于查看运行时错误（traceback、stderr 等）。"
                    "仅在排障模式下有效。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "last_n": {
                            "type": "integer",
                            "description": "读取最近 N 行日志，默认 50",
                            "default": 50
                        }
                    }
                }
            }
        }

    def execute(self, **kwargs) -> str:
        last_n = kwargs.get("last_n", 50)

        if not self.sandbox_dir:
            return "（当前场景无沙盒日志）"

        # 尝试读取多种日志文件
        log_candidates = [
            os.path.join(self.sandbox_dir, ".sandbox", "run.log"),
            os.path.join(self.sandbox_dir, ".sandbox", "stderr.log"),
            os.path.join(self.sandbox_dir, "nohup.out"),
        ]

        for log_path in log_candidates:
            if os.path.isfile(log_path):
                try:
                    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                        lines = f.readlines()
                    tail = lines[-last_n:] if len(lines) > last_n else lines
                    return f"[{os.path.basename(log_path)}] 最近 {len(tail)} 行:\n" + "".join(tail)
                except Exception as e:
                    return f"日志读取失败: {e}"

        return "（未找到沙盒日志文件）"


class EmitVerdictSkill(BaseSkill):
    """
    输出最终判定 — 调用此 Skill 将终止 ReAct 循环。

    排障场景：输出 root_cause + fix_instruction + guilty_file
    审查场景：输出 findings JSON 列表
    """

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "emit_verdict",
                "description": (
                    "提交最终判定结果。调用此工具将终止调查循环。"
                    "排障场景：必须包含 root_cause 和 fix_instruction。"
                    "审查场景：提交审查完成。此时可以附加 findings 列表，或若之前已通过 `record_finding` 记录过问题，也可传入空列表 `[]`。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "root_cause": {
                            "type": "string",
                            "description": "根因分析（排障场景必填）"
                        },
                        "fix_instruction": {
                            "type": "string",
                            "description": "给 Coder 的修复指令（排障场景必填）"
                        },
                        "guilty_file": {
                            "type": "string",
                            "description": "需要修改的文件路径（排障场景）"
                        },
                        "root_cause_type": {
                            "type": "string",
                            "description": "根因类型：missing_export / naming_mismatch / signature_mismatch / architecture_drift / wrong_target"
                        },
                        "recommended_target_files": {
                            "type": "string",
                            "description": "推荐继续检查或修改的文件列表，JSON 数组字符串或逗号分隔字符串"
                        },
                        "confidence": {
                            "type": "number",
                            "description": "当前结论置信度，0 到 1"
                        },
                        "qa_plan": {
                            "type": "string",
                            "description": (
                                "可选。Patch 后最小浏览器 QA 计划，JSON 数组字符串。"
                                "当前支持: [{\"action\":\"click\",\"selector\":\"#create-btn\","
                                "\"assert\":\"visible\",\"target\":\"#editor-panel\"}]"
                            )
                        },
                        "findings": {
                            "type": "string",
                            "description": (
                                "JSON 格式的审查发现列表（审查场景可选）。"
                                "每项包含: file, line, severity(high/medium/low/info), "
                                "category(安全/性能/质量/架构), issue, suggestion"
                            )
                        },
                    }
                }
            }
        }

    def execute(self, **kwargs) -> str:
        # EmitVerdictSkill 的 execute 不会被 SkillRunner 直接调用
        # 它的参数由 TechLead ReAct 循环直接解析
        return "VERDICT_EMITTED"


class RecordFindingSkill(BaseSkill):
    """记录代码审查中发现的问题。支持单条和批量两种模式。"""

    def __init__(self, findings_pool: list):
        self.findings_pool = findings_pool

    def schema(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": "record_finding",
                "description": (
                    "记录代码审查中发现的问题。支持两种模式：\n"
                    "1. 单条模式：传入 file, issue, severity, category 等字段\n"
                    "2. 批量模式：传入 findings 字段（JSON 数组字符串），一次记录多条"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "description": "被审查文件路径"},
                        "line": {"type": "integer", "description": "出问题的行号"},
                        "severity": {"type": "string", "description": "严重度: high/medium/low/info"},
                        "category": {"type": "string", "description": "分类: 安全/性能/质量/架构"},
                        "issue": {"type": "string", "description": "精简描述具体问题"},
                        "suggestion": {"type": "string", "description": "修复建议"},
                        "findings": {"type": "string", "description": "JSON 数组字符串，一次提交多条发现。与单条参数互斥。"}
                    },
                    "required": []
                }
            }
        }

    def execute(self, **kwargs) -> str:
        from core.ws_broadcaster import global_broadcaster

        # 批量模式
        findings_str = kwargs.get("findings")
        if findings_str:
            try:
                if isinstance(findings_str, list):
                    items = findings_str
                else:
                    items = json.loads(findings_str)
                if not isinstance(items, list):
                    items = [items]
            except (json.JSONDecodeError, TypeError):
                items = [{"file": "unknown", "severity": "info", "category": "未分类",
                          "issue": str(findings_str)[:500], "suggestion": ""}]
            for item in items:
                finding = {
                    "file": item.get("file", ""), "line": item.get("line", 0),
                    "severity": item.get("severity", "info"), "category": item.get("category", "质量"),
                    "issue": item.get("issue", ""), "suggestion": item.get("suggestion", "")
                }
                self.findings_pool.append(finding)
                global_broadcaster.emit_sync("TechLead", "finding",
                    f"📋 发现: [{finding['severity']}] {finding['file']} — {finding['issue'][:80]}")
            return f"✅ 批量记录成功！本次新增 {len(items)} 条，累计 {len(self.findings_pool)} 条。"

        # 单条模式
        finding = {
            "file": kwargs.get("file", ""), "line": kwargs.get("line", 0),
            "severity": kwargs.get("severity", "info"), "category": kwargs.get("category", "质量"),
            "issue": kwargs.get("issue", ""), "suggestion": kwargs.get("suggestion", "")
        }
        self.findings_pool.append(finding)
        global_broadcaster.emit_sync("TechLead", "finding",
            f"📋 发现: [{finding['severity']}] {finding['file']} — {finding['issue'][:80]}")
        return f"✅ 记录成功！累计 {len(self.findings_pool)} 条。"


class ScopedReadFileSkill(BaseSkill):
    """为 TechLead 包装现有 FileReaderSkill，加一层 allowlist 约束。"""

    def __init__(self, reader: BaseSkill, allowed_files=None):
        self.reader = reader
        self.allowed_files = _normalize_allowed_files(allowed_files)

    def schema(self) -> dict:
        return self.reader.schema()

    def execute(self, **kwargs) -> str:
        file_path = str(kwargs.get("file_path", "")).replace("\\", "/").strip("/")
        if self.allowed_files and file_path not in self.allowed_files:
            return f"错误: 当前定向范围禁止读取 '{kwargs.get('file_path', '')}'"
        return self.reader.execute(**kwargs)


def build_tech_lead_skills(project_dir: str, sandbox_dir: str = None,
                           agent_findings: list = None, allowed_files=None) -> dict:
    """
    构建 TechLead 的完整 Skill 集合。

    agent_findings 不为 None 时视为审计模式，使用 AuditFileReaderSkill（完整读取）。
    否则为排障模式，使用 FileReaderSkill（支持精准切片）。
    """
    from core.skills.file_reader import FileReaderSkill

    # 审计模式：完整读取，不暴露切片参数
    # 排障模式：优先从 sandbox 读（sandbox = truth 全量快照 + 当前草稿）
    #           这修复了 TechLead 看不到 Coder 刚写完但未通过 Reviewer 的文件的致命盲区
    normalized_allowed = _normalize_allowed_files(allowed_files)
    if agent_findings is not None:
        reader = AuditFileReaderSkill(project_dir, normalized_allowed)
    else:
        read_dir = sandbox_dir if sandbox_dir and os.path.isdir(sandbox_dir) else project_dir
        reader = ScopedReadFileSkill(FileReaderSkill(read_dir), normalized_allowed)

    skills = {
        "read_file": reader,
        "grep_project": GrepProjectSkill(project_dir, normalized_allowed),
        "read_sandbox_log": ReadSandboxLogSkill(sandbox_dir),
        "emit_verdict": EmitVerdictSkill(),
    }
    if agent_findings is not None:
        # 审计模式：文件树已预注入 prompt，不需要 list_files
        skills["record_finding"] = RecordFindingSkill(agent_findings)
        if normalized_allowed:
            skills["list_files"] = ListFilesSkill(project_dir, normalized_allowed)
    else:
        # 排障模式：保留 list_files 能力
        skills["list_files"] = ListFilesSkill(project_dir, normalized_allowed)

    return skills
