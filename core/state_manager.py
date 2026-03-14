import os
import logging
import threading
from typing import Dict, Optional
import time

logger = logging.getLogger("StateManager")


class VirtualFileSystem:
    def __init__(self, project_id: str, mode: str = "create", base_path: str = None):
        self.project_id = project_id
        self.mode = mode              # "create"(从零生成) | "edit"(已有项目)
        self.base_path = base_path    # "edit"模式下的磁盘根路径
        self._lock = threading.Lock()
        self.vfs: Dict[str, str] = {}
        self.retries: Dict[str, int] = {}

        self.is_dirty: bool = False
        self.last_accessed: float = time.time()

    def _update_access(self):
        self.last_accessed = time.time()

    def save_draft(self, filepath: str, content: str) -> None:
        """保存代码草稿到内存中，并置为脏状态"""
        with self._lock:
            self.vfs[filepath] = content
            self.is_dirty = True
            self._update_access()
        logger.debug(f"[{self.project_id}] 已暂存草稿: {filepath}")

    def get_draft(self, filepath: str) -> Optional[str]:
        """获取代码草稿。edit 模式下优先从 VFS 内存取，fallback 从磁盘读取。"""
        with self._lock:
            self._update_access()
            content = self.vfs.get(filepath)
            if content is not None:
                return content
            # edit 模式下，如果内存中没有，尝试从磁盘读取
            if self.mode == "edit" and self.base_path:
                abs_path = os.path.join(self.base_path, filepath)
                if os.path.isfile(abs_path):
                    try:
                        with open(abs_path, "r", encoding="utf-8") as f:
                            return f.read()
                    except Exception:
                        pass
            return None

    def get_all_vfs(self) -> Dict[str, str]:
        """获取当前整个项目的虚拟代码状态 (返回浅拷贝防止外部并发修改)"""
        with self._lock:
            self._update_access()
            return dict(self.vfs)

    def increment_retry(self, task_id: str) -> int:
        """增加特定任务的重试计数。用于后续的 MAX_RETRIES 熔断判定。"""
        with self._lock:
            self._update_access()
            if task_id not in self.retries:
                self.retries[task_id] = 0
            self.retries[task_id] += 1
            return self.retries[task_id]

    def reset_retry(self, task_id: str) -> None:
        """任务成功后重置计数器"""
        with self._lock:
            self._update_access()
            if task_id in self.retries:
                self.retries[task_id] = 0

    def get_retry_count(self, task_id: str) -> int:
        with self._lock:
            self._update_access()
            return self.retries.get(task_id, 0)

    def apply_edits(self, filepath: str, edits: list) -> tuple:
        """
        差量编辑：对指定文件执行 search/replace 操作。
        
        Args:
            filepath: 相对文件路径
            edits: [{"search": "原始片段", "replace": "替换内容"}, ...]
        
        Returns:
            (success: bool, message: str)
        """
        with self._lock:
            self._update_access()
            content = self.vfs.get(filepath)
            
            # 如果 VFS 中没有，尝试从磁盘读取（edit 模式）
            if content is None and self.mode == "edit" and self.base_path:
                abs_path = os.path.join(self.base_path, filepath)
                if os.path.isfile(abs_path):
                    try:
                        with open(abs_path, "r", encoding="utf-8") as f:
                            content = f.read()
                    except Exception as e:
                        return False, f"磁盘读取失败: {e}"
            
            if content is None:
                return False, f"文件不存在: {filepath}"
            
            applied = 0
            failed = []
            for i, edit in enumerate(edits):
                search = edit.get("search", "")
                replace = edit.get("replace", "")
                if not search:
                    failed.append(f"edit[{i}]: search 为空")
                    continue
                if search in content:
                    content = content.replace(search, replace, 1)
                    applied += 1
                else:
                    failed.append(f"edit[{i}]: 未找到匹配 '{search[:50]}...'")
            
            if applied > 0:
                self.vfs[filepath] = content
                self.is_dirty = True
                logger.info(f"🔧 [{self.project_id}] apply_edits({filepath}): {applied} 成功, {len(failed)} 失败")
            
            if failed:
                return applied > 0, f"应用 {applied} 处编辑, {len(failed)} 处失败: {'; '.join(failed)}"
            return True, f"全部 {applied} 处编辑应用成功"

    def commit_to_disk(self, target_dir: str) -> None:
        """
        [危险动作] 将所有审查通过的内存代码，物理刷入最终磁盘。
        """
        with self._lock:
            self._update_access()
            logger.info(f"💾 正在将项目 [{self.project_id}] 状态刷入物理磁盘: {target_dir}")
            self._write_vfs_to_dir(target_dir)
            self.is_dirty = False

    def clear_state(self) -> None:
        """
        清空当前内存中的所有项目状态和重试计数。
        """
        with self._lock:
            self.vfs.clear()
            self.retries.clear()
            self.is_dirty = False
            self._update_access()
        logger.debug(f"🗑️ [{self.project_id}] 虚拟文件系统 (VFS) 与重试记录已被清空。")

    def sync_to_sandbox(self, sandbox_dir: str) -> None:
        """
        每次 Reviewer 跑测试脚本前，必须将现在的内存 VFS 写进沙盒目录。
        """
        with self._lock:
            self._update_access()
            logger.debug(f"🔄 正在同步 [{self.project_id}] VFS 状态至沙盒工作区: {sandbox_dir}")
            self._write_vfs_to_dir(sandbox_dir)

    def _write_vfs_to_dir(self, dest_dir: str) -> None:
        """内部方法，调用方必须已持有 self._lock"""
        for rel_path, content in self.vfs.items():
            clean_rel_path = rel_path.lstrip("/").lstrip("\\")
            abs_path = os.path.abspath(os.path.join(dest_dir, clean_rel_path))

            if not abs_path.startswith(os.path.abspath(dest_dir)):
                logger.error(f"检测到非法路径跳跃，已拦截: {clean_rel_path}")
                continue

            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write(content)


class StateManager:
    """
    负责维护全系统的 VFS 缓存池，实现基于多例的物理+逻辑隔离。
    支持 LRU 缓存淘汰与脏数据自动落盘机制，防止内存 OOM。
    """
    def __init__(self, max_projects=5):
        self._pool_lock = threading.Lock()  # 保护 vfs_pool 字典的并发访问
        self.vfs_pool: Dict[str, VirtualFileSystem] = {}
        self.max_projects = max_projects
        self.projects_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "projects"))
        if not os.path.exists(self.projects_root):
            os.makedirs(self.projects_root)

    def get_vfs(self, project_id: str, mode: str = "create", base_path: str = None) -> VirtualFileSystem:
        """提取指定事务(项目)的轻量草稿箱。防串扰核心。"""
        with self._pool_lock:
            if project_id in self.vfs_pool:
                return self.vfs_pool[project_id]

            if len(self.vfs_pool) >= self.max_projects:
                self._evict_lru()

            new_vfs = VirtualFileSystem(project_id, mode=mode, base_path=base_path)
            self.vfs_pool[project_id] = new_vfs
            logger.info(f"✨ 放入池中: 项目 ({project_id}) mode={mode}。当前活跃VFS数: {len(self.vfs_pool)}")
            return new_vfs

    def _evict_lru(self):
        """淘汰最久未通信的死寂草稿实例 (调用方须已持有 _pool_lock)"""
        oldest_id = None
        oldest_time = float('inf')
        for pid, vfs in self.vfs_pool.items():
            if vfs.last_accessed < oldest_time:
                oldest_time = vfs.last_accessed
                oldest_id = pid

        if oldest_id:
            old_vfs = self.vfs_pool[oldest_id]
            logger.info(f"🧹 LRU 触发: 正在清退闲置 VFS ({oldest_id}) ...")
            if old_vfs.is_dirty:
                target_dir = os.path.join(self.projects_root, oldest_id)
                old_vfs.commit_to_disk(target_dir)
            del self.vfs_pool[oldest_id]

    def rename_vfs(self, old_id: str, new_id: str):
        """动态重命名项目时，同步迁移内存池中的草稿"""
        with self._pool_lock:
            if old_id in self.vfs_pool:
                vfs = self.vfs_pool.pop(old_id)
                self.vfs_pool[new_id] = vfs
                logger.info(f"🔄 VFS 缓存键迁移: {old_id} -> {new_id}")

    def remove_vfs(self, project_id: str):
        """手动拔除并强制保存某项 VFS"""
        with self._pool_lock:
            if project_id in self.vfs_pool:
                vfs = self.vfs_pool[project_id]
                if vfs.is_dirty:
                    vfs.commit_to_disk(os.path.join(self.projects_root, project_id))
                del self.vfs_pool[project_id]


# 替代曾经的 GlobalState 单例指针
global_state_manager = StateManager()
