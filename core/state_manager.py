import os
import logging
from typing import Dict, Optional

logger = logging.getLogger("StateManager")

class ProjectState:
    """
    全项目状态管理器 (Virtual File System & Retry Tracker)
    
    作用：
    1. 在 Reviewer 还没有通过审查前，Coder 写的所有代码都只存在于这个内存 VFS 中，
       防止污染真实开发树。
    2. 维护 Retry（重试）次数，触发熔断机制。
    """
    
    def __init__(self):
        # 虚拟文件系统: {相对文件路径: 代码字符串}
        self.vfs: Dict[str, str] = {}
        # 任务重试记录: {任务ID/文件名: 失败重试次数}
        self.retries: Dict[str, int] = {}

    def save_draft(self, filepath: str, content: str) -> None:
        """保存代码草稿到内存中"""
        self.vfs[filepath] = content
        logger.debug(f"已暂存草稿: {filepath}")

    def get_draft(self, filepath: str) -> Optional[str]:
        """获取代码草稿"""
        return self.vfs.get(filepath)

    def get_all_vfs(self) -> Dict[str, str]:
        """获取当前整个项目的虚拟代码状态"""
        return self.vfs

    def increment_retry(self, task_id: str) -> int:
        """
        增加特定任务的重试计数。
        用于后续的 MAX_RETRIES 熔断判定。
        """
        if task_id not in self.retries:
            self.retries[task_id] = 0
        self.retries[task_id] += 1
        return self.retries[task_id]

    def reset_retry(self, task_id: str) -> None:
        """任务成功后重置计数器"""
        if task_id in self.retries:
            self.retries[task_id] = 0

    def get_retry_count(self, task_id: str) -> int:
        return self.retries.get(task_id, 0)

    def commit_to_disk(self, target_dir: str) -> None:
        """
        [危险动作] 将所有审查通过的内存代码，物理刷入最终磁盘。
        通常在整个 Project 最终交付时，或者 Manager 确认完毕时调用。
        """
        logger.info(f"💾 正在将项目状态刷入物理磁盘: {target_dir}")
        self._write_vfs_to_dir(target_dir)

    def clear_state(self) -> None:
        """
        清空当前内存中的所有项目状体和重试计数。
        在执行新的独立项目前必须调用，防止代码串扰。
        """
        self.vfs.clear()
        self.retries.clear()
        logger.debug("🗑️ 虚拟文件系统 (VFS) 与重试记录已被清空。")

    def sync_to_sandbox(self, sandbox_dir: str) -> None:
        """
        [核心功能] 每次 Reviewer 跑测试脚本前，必须将现在的内存 VFS
        写进沙盒目录，否则测试脚本会找不到模块 import 报错！
        """
        logger.debug(f"🔄 正在同步 VFS 状态至沙盒工作区: {sandbox_dir}")
        self._write_vfs_to_dir(sandbox_dir)

    def _write_vfs_to_dir(self, dest_dir: str) -> None:
        for rel_path, content in self.vfs.items():
            # 安全拼接路径，防止跨目录跳跃 (如 ../../../etc/passwd)
            clean_rel_path = rel_path.lstrip("/").lstrip("\\")
            abs_path = os.path.abspath(os.path.join(dest_dir, clean_rel_path))
            
            # 确保生成的绝对路径依然在边界内
            if not abs_path.startswith(os.path.abspath(dest_dir)):
                logger.error(f"检测到非法路径跳跃，已拦截: {clean_rel_path}")
                continue
                
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)

# 单例模式
global_state = ProjectState()
