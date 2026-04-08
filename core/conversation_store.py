"""
ConversationStore — 项目级对话持久化存储

基于 SQLite + FTS5 unicode61 全文检索。
每个项目独立一个 conversation.db，实现物理隔离（记忆与项目同生死共存亡）。

存储位置：projects/<project_id>/.astrea/conversation.db
"""
import os
import sqlite3
import logging
from datetime import datetime, timezone
from typing import List, Dict

logger = logging.getLogger("ConversationStore")


class ConversationStore:
    """项目级对话持久化存储（SQLite + FTS5 unicode61）"""

    def __init__(self, project_dir: str):
        """
        初始化对话存储。

        Args:
            project_dir: 项目根目录（如 projects/20260402_xxx_MyApp/）
        """
        astrea_dir = os.path.join(project_dir, ".astrea")
        os.makedirs(astrea_dir, exist_ok=True)

        db_path = os.path.join(astrea_dir, "conversation.db")
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")  # 并发安全
        self._init_tables()
        logger.info(f"💬 ConversationStore 已初始化: {db_path}")

    def _init_tables(self):
        """创建 FTS5 虚拟表 + 普通表"""
        # 普通表：存储完整对话记录（FTS5 不支持 rowid 以外的主键）
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                round_id INTEGER NOT NULL,
                timestamp TEXT NOT NULL
            )
        """)

        # FTS5 虚拟表：全文检索索引
        # content=conversations 表示 FTS5 使用 conversations 表作为内容源
        try:
            self.conn.execute("""
                CREATE VIRTUAL TABLE IF NOT EXISTS conversation_fts
                USING fts5(
                    role,
                    content,
                    round_id UNINDEXED,
                    timestamp UNINDEXED,
                    content=conversations,
                    content_rowid=id,
                    tokenize='unicode61'
                )
            """)
        except sqlite3.OperationalError:
            # FTS5 不可用时降级（极少见，Windows Python 默认含 FTS5）
            logger.warning("⚠️ FTS5 不可用，对话检索将使用 LIKE 降级查询")

        # 触发器：自动同步 FTS5 索引
        self.conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS conversations_ai AFTER INSERT ON conversations BEGIN
                INSERT INTO conversation_fts(rowid, role, content, round_id, timestamp)
                VALUES (new.id, new.role, new.content, new.round_id, new.timestamp);
            END;
            CREATE TRIGGER IF NOT EXISTS conversations_ad AFTER DELETE ON conversations BEGIN
                INSERT INTO conversation_fts(conversation_fts, rowid, role, content, round_id, timestamp)
                VALUES ('delete', old.id, old.role, old.content, old.round_id, old.timestamp);
            END;
        """)
        self.conn.commit()

    def append(self, role: str, content: str, round_id: int):
        """
        追加一条对话记录。

        Args:
            role: "user" | "pm" | "system"
            content: 消息内容
            round_id: 轮次 ID
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT INTO conversations (role, content, round_id, timestamp) VALUES (?, ?, ?, ?)",
            (role, content, round_id, timestamp)
        )
        self.conn.commit()

    def search(self, query: str, limit: int = 10) -> List[Dict]:
        """
        全文检索对话记录。

        Args:
            query: 搜索关键词
            limit: 最大返回条数

        Returns:
            [{role, content, round_id, timestamp}, ...]
        """
        if not query.strip():
            return []

        try:
            # 尝试 FTS5 MATCH 查询
            cursor = self.conn.execute("""
                SELECT c.role, c.content, c.round_id, c.timestamp,
                       rank
                FROM conversation_fts f
                JOIN conversations c ON c.id = f.rowid
                WHERE conversation_fts MATCH ?
                ORDER BY rank
                LIMIT ?
            """, (query, limit))

            results = []
            for row in cursor:
                results.append({
                    "role": row[0],
                    "content": row[1],
                    "round_id": row[2],
                    "timestamp": row[3],
                })
            return results

        except sqlite3.OperationalError:
            # FTS5 查询失败时降级为 LIKE
            logger.warning("⚠️ FTS5 查询失败，降级为 LIKE 查询")
            cursor = self.conn.execute("""
                SELECT role, content, round_id, timestamp
                FROM conversations
                WHERE content LIKE ?
                ORDER BY id DESC
                LIMIT ?
            """, (f"%{query}%", limit))

            return [
                {"role": row[0], "content": row[1], "round_id": row[2], "timestamp": row[3]}
                for row in cursor
            ]

    def delete_after_round(self, round_id: int):
        """
        删除某轮之后的所有记录（用于回滚清理）。

        Args:
            round_id: 保留此轮及之前的记录，删除之后的
        """
        self.conn.execute(
            "DELETE FROM conversations WHERE round_id > ?",
            (round_id,)
        )
        self.conn.commit()
        logger.info(f"🗑️ 已删除 round_id > {round_id} 的所有对话记录")

    def get_recent(self, limit: int = 20) -> List[Dict]:
        """获取最近的对话记录"""
        cursor = self.conn.execute("""
            SELECT role, content, round_id, timestamp
            FROM conversations
            ORDER BY id DESC
            LIMIT ?
        """, (limit,))

        results = [
            {"role": row[0], "content": row[1], "round_id": row[2], "timestamp": row[3]}
            for row in cursor
        ]
        results.reverse()  # 按时间正序
        return results

    def close(self):
        """关闭数据库连接"""
        if self.conn:
            self.conn.close()
