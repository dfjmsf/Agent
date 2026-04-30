"""
engine/modes/rollback.py — Rollback 模式：从 git log 中定位 commit，执行 git revert
"""
import os
import re
import subprocess
import logging
from typing import Tuple

from core.database import update_project_status
from core.ws_broadcaster import global_broadcaster

logger = logging.getLogger("AstreaEngine")

_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_PROJECTS_DIR = os.path.join(_ROOT_DIR, "projects")


def run_rollback_mode(engine, user_requirement: str) -> Tuple[bool, str]:
    """
    Rollback 模式：从 git log 中定位 commit，执行 git revert。
    """
    logger.info(f"⏪ Rollback Mode 启动: {engine.project_id}")
    global_broadcaster.emit_sync("System", "start_project", f"⏪ Rollback Mode: {engine.project_id}")

    base_dir = os.path.join(_PROJECTS_DIR, engine.project_id)
    git_dir = os.path.join(base_dir, ".git")

    if not os.path.isdir(git_dir):
        logger.error("💥 [Rollback] 项目没有 Git 仓库，无法回滚")
        global_broadcaster.emit_sync("System", "error", "💥 项目没有 Git 仓库，无法回滚")
        return False, base_dir

    try:
        result = subprocess.run(
            ["git", "log", "--max-count=20", "--format=%H|%s|%ai"],
            cwd=base_dir, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=5,
        )
        if result.returncode != 0 or not result.stdout:
            logger.error(f"💥 [Rollback] git log 失败: {result.stderr}")
            return False, base_dir

        commits = []
        for line in result.stdout.strip().split('\n'):
            if not line.strip():
                continue
            parts = line.split('|', 2)
            if len(parts) == 3:
                commits.append({
                    "hash": parts[0].strip(),
                    "message": parts[1].strip(),
                    "date": parts[2].strip(),
                })

        if not commits:
            logger.warning("⚠️ [Rollback] 没有可回滚的 commit")
            global_broadcaster.emit_sync("System", "error", "没有可回滚的 commit 记录")
            return False, base_dir

        req_lower = user_requirement.lower()
        if req_lower.startswith("rollback round:"):
            round_id = user_requirement.split(":", 1)[1].strip()
            logger.info(f"⏪ [Rollback] 收到批次回退请求: Round {round_id}")

            round_commits = [c for c in commits if f"[Round {round_id}]" in c["message"]]
            if not round_commits:
                logger.warning(f"⚠️ [Rollback] 找不到包含 [Round {round_id}] 的 commit")
                global_broadcaster.emit_sync("System", "error", f"找不到批次 {round_id} 的提交记录")
                return False, base_dir

            global_broadcaster.emit_sync("System", "info",
                f"⏪ 正在级联回退批次: Round {round_id} (共 {len(round_commits)} 条记录)...")
            for c in round_commits:
                commit_hash = c["hash"]
                logger.info(f"⏪ [Rollback] revert {commit_hash[:8]}: {c['message']}")
                res = subprocess.run(
                    ["git", "revert", "--no-commit", commit_hash],
                    cwd=base_dir, capture_output=True, text=True,
                    encoding="utf-8", errors="replace", timeout=15,
                )
                if res.returncode != 0:
                    logger.error(f"❌ [Rollback] git revert 冲突: {res.stderr}")
                    subprocess.run(["git", "revert", "--abort"],
                                   cwd=base_dir, capture_output=True, timeout=5)
                    global_broadcaster.emit_sync("System", "error", "❌ 级联回退时发生冲突，已中止。")
                    return False, base_dir

            res = subprocess.run(
                ["git", "commit", "-m", f"⏪ Rollback [Round {round_id}]"],
                cwd=base_dir, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=10,
            )
            if res.returncode == 0:
                logger.info(f"✅ [Rollback] 批次回退成功: Round {round_id}")
                global_broadcaster.emit_sync("System", "success",
                    f"✅ 已成功将批次 Round {round_id} 的全部修改连根拔除")
                update_project_status(engine.project_id, "success")
                return True, base_dir
            else:
                logger.error(f"❌ [Rollback] 提交回退记录失败: {res.stderr}")
                return False, base_dir

        else:
            target_commit = None
            if req_lower.startswith("rollback commit:"):
                commit_hash = user_requirement.split(":", 1)[1].strip()
                for c in commits:
                    if c["hash"].startswith(commit_hash):
                        target_commit = c
                        break
            else:
                for c in commits:
                    if any(kw in c["message"].lower() for kw in req_lower.split() if len(kw) > 1):
                        target_commit = c
                        break

            if not target_commit:
                target_commit = commits[0]
                logger.info(f"⏪ [Rollback] 未匹配到关键词，回退最近的 commit: {target_commit['hash'][:8]}")

            commit_hash = target_commit["hash"]
            logger.info(f"⏪ [Rollback] 执行 git revert {commit_hash[:8]}: {target_commit['message']}")
            global_broadcaster.emit_sync("System", "info",
                f"⏪ 正在回退: {target_commit['message']} ({target_commit['date'][:10]})")

            revert_result = subprocess.run(
                ["git", "revert", "--no-edit", commit_hash],
                cwd=base_dir, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=30,
            )

            if revert_result.returncode == 0:
                logger.info(f"✅ [Rollback] 回退成功: {commit_hash[:8]}")
                global_broadcaster.emit_sync("System", "success",
                    f"✅ 已成功回退到 {target_commit['message']} 之前的状态")
                update_project_status(engine.project_id, "success")
                return True, base_dir
            else:
                logger.error(f"❌ [Rollback] git revert 冲突: {revert_result.stderr}")
                subprocess.run(["git", "revert", "--abort"],
                               cwd=base_dir, capture_output=True, timeout=5)
                global_broadcaster.emit_sync("System", "error",
                    "❌ 回退时发生冲突，已自动中止。可能需要手动处理。")
                return False, base_dir

    except Exception as e:
        logger.error(f"❌ [Rollback] 异常: {e}")
        global_broadcaster.emit_sync("System", "error", f"❌ 回退异常: {str(e)}")
        return False, base_dir
