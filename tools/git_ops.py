"""
Git 操作工具模块 — 基于 subprocess 调用系统 git，零 Python 依赖。
"""
import os
import subprocess
import logging
from typing import List, Dict, Optional

logger = logging.getLogger("GitOps")

# 项目级 .gitignore 模板
_GITIGNORE_TEMPLATE = """\
__pycache__/
*.pyc
*.pyo
.venv/
venv/
*.db
*.sqlite3
*.db-wal
*.db-shm
.astrea/
.sandbox/
node_modules/
.idea/
.vscode/
*.log
"""


def _run_git(project_dir: str, args: list, check: bool = False) -> subprocess.CompletedProcess:
    """执行 git 命令，统一错误处理"""
    cmd = ["git"] + args
    try:
        result = subprocess.run(
            cmd, cwd=project_dir, capture_output=True, text=True, timeout=15,
            encoding="utf-8", errors="replace",
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"}
        )
        # 防御：确保 stdout/stderr 不是 None
        if result.stdout is None:
            result = subprocess.CompletedProcess(cmd, result.returncode, stdout="", stderr=result.stderr or "")
        if check and result.returncode != 0:
            logger.warning(f"Git 命令失败: {' '.join(cmd)}\nstderr: {(result.stderr or '').strip()}")
        return result
    except FileNotFoundError:
        logger.error("系统未安装 git，跳过 git 操作")
        return subprocess.CompletedProcess(cmd, returncode=-1, stdout="", stderr="git not found")
    except subprocess.TimeoutExpired:
        logger.warning(f"Git 命令超时: {' '.join(cmd)}")
        return subprocess.CompletedProcess(cmd, returncode=-1, stdout="", stderr="timeout")
    except Exception as e:
        logger.warning(f"Git 命令异常: {e}")
        return subprocess.CompletedProcess(cmd, returncode=-1, stdout="", stderr=str(e))


def is_git_repo(project_dir: str) -> bool:
    """检查目录是否已是 git 仓库"""
    return os.path.isdir(os.path.join(project_dir, ".git"))


def git_init(project_dir: str) -> bool:
    """初始化 git 仓库 + 创建 .gitignore"""
    if is_git_repo(project_dir):
        return True

    result = _run_git(project_dir, ["init"])
    if result.returncode != 0:
        return False

    # 写入 .gitignore
    gitignore_path = os.path.join(project_dir, ".gitignore")
    if not os.path.exists(gitignore_path):
        try:
            with open(gitignore_path, "w", encoding="utf-8") as f:
                f.write(_GITIGNORE_TEMPLATE)
        except Exception as e:
            logger.warning(f"写入 .gitignore 失败: {e}")

    # 配置用户信息（仅本仓库范围，不影响全局）
    _run_git(project_dir, ["config", "user.name", "ASTrea Agent"])
    _run_git(project_dir, ["config", "user.email", "astrea@agent.local"])

    logger.info(f"✅ Git 仓库已初始化: {project_dir}")
    return True


def git_commit(project_dir: str, message: str) -> bool:
    """执行 git add . && git commit"""
    if not is_git_repo(project_dir):
        if not git_init(project_dir):
            return False

    # Stage all
    add_result = _run_git(project_dir, ["add", "-A"])
    if add_result.returncode != 0:
        return False

    # 检查是否有变更需要 commit
    status = _run_git(project_dir, ["status", "--porcelain"])
    if not status.stdout.strip():
        logger.info("Git: 无变更需要 commit，跳过")
        return True

    # Commit
    result = _run_git(project_dir, ["commit", "-m", message])
    if result.returncode == 0:
        short_hash = _run_git(project_dir, ["rev-parse", "--short", "HEAD"]).stdout.strip()
        logger.info(f"✅ Git commit 成功: {short_hash} - {message}")
        return True

    logger.warning(f"Git commit 失败: {result.stderr.strip()}")
    return False


def get_head_hash(project_dir: str) -> Optional[str]:
    """获取当前 HEAD 的完整 commit hash（Phase 2.1 Task Ledger 用）"""
    result = _run_git(project_dir, ["rev-parse", "HEAD"])
    if result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    return None


def git_log(project_dir: str, max_count: int = 30) -> List[Dict]:
    """获取 commit 历史"""
    if not is_git_repo(project_dir):
        return []

    # 使用分隔符格式化输出，方便解析
    sep = "|||"
    fmt = f"%H{sep}%h{sep}%s{sep}%an{sep}%ai{sep}"
    result = _run_git(project_dir, [
        "log", f"--max-count={max_count}",
        f"--pretty=format:{fmt}",
        "--shortstat"
    ])

    if result.returncode != 0:
        return []

    commits = []
    lines = result.stdout.strip().split("\n")
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        if sep in line:
            parts = line.split(sep)
            if len(parts) >= 5:
                commit = {
                    "hash": parts[0],
                    "short_hash": parts[1],
                    "message": parts[2],
                    "author": parts[3],
                    "date": parts[4],
                    "stats": ""
                }
                # 下一行可能是 shortstat（如果存在）
                if i + 1 < len(lines) and sep not in lines[i + 1] and lines[i + 1].strip():
                    commit["stats"] = lines[i + 1].strip()
                    i += 1
                commits.append(commit)
        i += 1

    return commits


def git_diff(project_dir: str, commit_hash: str) -> str:
    """获取指定 commit 的 diff"""
    if not is_git_repo(project_dir):
        return ""

    # 对比该 commit 与其父 commit
    result = _run_git(project_dir, ["diff", f"{commit_hash}~1..{commit_hash}", "--stat"])
    stat = result.stdout if result.returncode == 0 else ""

    result2 = _run_git(project_dir, ["diff", f"{commit_hash}~1..{commit_hash}"])
    diff = result2.stdout if result2.returncode == 0 else ""

    if not diff:
        # 可能是初始 commit（没有父 commit）
        result3 = _run_git(project_dir, ["show", commit_hash, "--format="])
        diff = result3.stdout if result3.returncode == 0 else "无法获取 diff"

    return diff


def git_status(project_dir: str) -> Dict:
    """获取 git 仓库状态概要"""
    if not is_git_repo(project_dir):
        return {"initialized": False}

    # 最新 commit
    head = _run_git(project_dir, ["log", "-1", "--pretty=format:%h - %s (%ai)"])
    head_info = (head.stdout or "").strip() if head.returncode == 0 else "无 commit"

    # commit 总数
    count = _run_git(project_dir, ["rev-list", "--count", "HEAD"])
    total_str = (count.stdout or "").strip()
    total = int(total_str) if count.returncode == 0 and total_str.isdigit() else 0

    return {
        "initialized": True,
        "latest_commit": head_info,
        "total_commits": total,
    }


if __name__ == "__main__":
    # 简单测试
    import sys
    test_dir = sys.argv[1] if len(sys.argv) > 1 else "."
    print(f"is_git_repo: {is_git_repo(test_dir)}")
    print(f"status: {git_status(test_dir)}")
    for c in git_log(test_dir, 5):
        print(f"  {c['short_hash']} | {c['message']} | {c['date']}")
