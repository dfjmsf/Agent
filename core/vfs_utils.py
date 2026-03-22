"""
VfsUtils — 物理文件系统隔离器

v1.3 核心基建：
- 真理区 (truth_dir): 只有 Engine 在任务 PASSED 后才能写入
- 沙盒区 (.sandbox): CodePatcher 缝合后写入，Reviewer 在此运行测试
- 即使大模型写了破坏性代码，也只在沙盒里执行，真理区毫发无损
"""
import os
import shutil
import logging
from typing import Dict, Optional

logger = logging.getLogger("VfsUtils")


class VfsUtils:
    """
    物理文件系统隔离器。

    职责：
    - commit_to_truth: Engine 专用，将已验证代码写入真理区
    - write_to_sandbox: 将缝合后的完整代码写入沙盒测试区
    - read_truth: 读取真理区文件（Observer 底层调用）
    - sync_truth_to_sandbox: 将真理区完整同步到沙盒（测试前）
    """

    def __init__(self, project_root: str):
        self.truth_dir = os.path.abspath(project_root)
        self.sandbox_dir = os.path.join(self.truth_dir, ".sandbox")
        os.makedirs(self.truth_dir, exist_ok=True)

    def commit_to_truth(self, file_path: str, content: str):
        """
        Engine 专用：将已验证的代码写入真理区。

        Args:
            file_path: 相对路径 (如 "src/main.py")
            content: 文件内容
        """
        abs_path = os.path.join(self.truth_dir, file_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
        logger.info(f"💾 [Truth] 已 commit: {file_path} ({len(content)} bytes)")

    def read_truth(self, file_path: str) -> Optional[str]:
        """读取真理区的文件内容"""
        abs_path = os.path.join(self.truth_dir, file_path)
        if not os.path.isfile(abs_path):
            return None
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception as e:
            logger.error(f"❌ 真理区读取失败: {file_path} — {e}")
            return None

    def write_to_sandbox(self, files: Dict[str, str]):
        """
        将缝合后的文件写入沙盒测试区。

        先将真理区完整同步，再覆盖需要测试的文件。
        """
        # 同步真理区到沙盒
        self.sync_truth_to_sandbox()

        # 覆盖目标文件
        for file_path, content in files.items():
            abs_path = os.path.join(self.sandbox_dir, file_path)
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
            logger.info(f"📦 [Sandbox] 已写入: {file_path} ({len(content)} bytes)")

    def sync_truth_to_sandbox(self):
        """将真理区完整同步到沙盒区"""
        if os.path.exists(self.sandbox_dir):
            shutil.rmtree(self.sandbox_dir, ignore_errors=True)

        # 复制真理区到沙盒区（排除 .sandbox 自身和 .git）
        def _ignore(directory, files):
            ignored = set()
            for f in files:
                if f in ('.sandbox', '.git', '__pycache__', '.venv', 'node_modules'):
                    ignored.add(f)
            return ignored

        if os.path.exists(self.truth_dir):
            shutil.copytree(self.truth_dir, self.sandbox_dir, ignore=_ignore, dirs_exist_ok=True)
            logger.info(f"🔄 [Sandbox] 真理区已同步到沙盒")

    def clean_sandbox(self):
        """清理沙盒区"""
        if os.path.exists(self.sandbox_dir):
            shutil.rmtree(self.sandbox_dir, ignore_errors=True)
            logger.info(f"🧹 [Sandbox] 已清理")

    def list_truth_files(self) -> Dict[str, int]:
        """列出真理区所有文件及其大小"""
        result = {}
        for root, dirs, files in os.walk(self.truth_dir):
            dirs[:] = [d for d in dirs if d not in ('.sandbox', '.git', '__pycache__', '.venv', 'node_modules')]
            for f in files:
                abs_path = os.path.join(root, f)
                rel_path = os.path.relpath(abs_path, self.truth_dir).replace("\\", "/")
                result[rel_path] = os.path.getsize(abs_path)
        return result
