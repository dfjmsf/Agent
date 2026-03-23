"""
统一数据层 — PostgreSQL + pgvector
替代原 db.py (SQLite) + memory.py (ChromaDB)，实现短期/长期记忆一体化。
"""
import os
import time
import logging
from datetime import datetime
from typing import List, Optional, Dict, Any

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Boolean, event, Float
from sqlalchemy.orm import declarative_base, sessionmaker, scoped_session
from sqlalchemy.dialects.postgresql import JSONB, ARRAY as PG_ARRAY
from pgvector.sqlalchemy import Vector
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger("Database")

# ============================================================
# 1. 数据库连接
# ============================================================

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:astrea123@localhost:5432/astrea")

engine = create_engine(
    DATABASE_URL,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,  # 自动检测断连
    echo=False,
)

# scoped_session 保证线程隔离
SessionFactory = sessionmaker(bind=engine, autocommit=False, autoflush=False)
ScopedSession = scoped_session(SessionFactory)

Base = declarative_base()

# ============================================================
# 2. ORM 模型
# ============================================================

EMBEDDING_DIM = 1024  # text-embedding-v4 输出维度


class SessionEvent(Base):
    """短期记忆：事件流 (替代原 session_history)"""
    __tablename__ = "session_events"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(String(200), index=True, nullable=False)
    role = Column(String(50))         # user / manager / coder / reviewer / sandbox / system
    event_type = Column(String(50))   # prompt / plan / code / test_pass / test_fail / circuit_break / reflection
    content = Column(Text)
    embedding = Column(Vector(EMBEDDING_DIM))  # 项目经验向量化 (B1 轻量 RAG)
    metadata_ = Column("metadata", JSONB, default={})
    created_at = Column(DateTime, default=datetime.utcnow)


class Memory(Base):
    """长期记忆：向量化经验 (替代原 ChromaDB 双 collection)"""
    __tablename__ = "memories"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(String(200), index=True)
    scope = Column(String(20), default="project")  # 'global' | 'project'
    content = Column(Text, nullable=False)
    embedding = Column(Vector(EMBEDDING_DIM))
    metadata_ = Column("metadata", JSONB, default={})
    created_at = Column(DateTime, default=datetime.utcnow)
    # --- 结构化字段 (v1.2.2) ---
    tech_stacks = Column(PG_ARRAY(String), default=[])     # L0 硬过滤, GIN 索引
    exp_type = Column(String(50), default="general")        # contrastive_pair / anti_pattern / general
    scenario = Column(Text)                                 # 场景描述
    # --- 动态追踪字段 (AMC 底座) ---
    success_count = Column(Integer, default=0)              # S
    usage_count = Column(Integer, default=0)                # U
    last_used_round = Column(Integer, default=0)            # R_last


class ProjectMeta(Base):
    """项目元数据 (激活原死代码)"""
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(String(200), unique=True, nullable=False, index=True)
    name = Column(String(200))
    status = Column(String(50), default="in_progress")  # in_progress / success / failed
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class TaskTrajectory(Base):
    """TDD 试错轨迹表 — 记录每次打回的代码快照和报错"""
    __tablename__ = "astrea_task_trajectories"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(String(200), nullable=False, index=True)
    task_id = Column(String(100), nullable=False)
    attempt_round = Column(Integer, default=0)
    error_summary = Column(Text)
    failed_code = Column(Text)
    final_code = Column(Text, default=None)
    recalled_memory_ids = Column(PG_ARRAY(Integer), default=[])
    is_synthesized = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


class BlackboardCheckpoint(Base):
    """Blackboard 断点续传快照 (v1.3)"""
    __tablename__ = "blackboard_checkpoints"

    project_id = Column(String(200), primary_key=True)
    state_json = Column(Text, nullable=False)         # BlackboardState.model_dump_json()
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ============================================================
# 3. 初始化
# ============================================================

def init_db():
    """创建所有表 + pgvector 扩展 + 列迁移 + GIN 索引 (全幂等)"""
    from sqlalchemy import text
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    Base.metadata.create_all(bind=engine)
    
    # v1.2.2 迁移: 为已有 memories 表添加新列 (幂等，列存在则跳过)
    migrations = [
        "ALTER TABLE memories ADD COLUMN IF NOT EXISTS tech_stacks VARCHAR[] DEFAULT '{}'",
        "ALTER TABLE memories ADD COLUMN IF NOT EXISTS exp_type VARCHAR(50) DEFAULT 'general'",
        "ALTER TABLE memories ADD COLUMN IF NOT EXISTS scenario TEXT",
        "ALTER TABLE memories ADD COLUMN IF NOT EXISTS success_count INTEGER DEFAULT 0",
        "ALTER TABLE memories ADD COLUMN IF NOT EXISTS usage_count INTEGER DEFAULT 0",
        "ALTER TABLE memories ADD COLUMN IF NOT EXISTS last_used_round INTEGER DEFAULT 0",
        # session_events 轻量 RAG
        f"ALTER TABLE session_events ADD COLUMN IF NOT EXISTS embedding vector({EMBEDDING_DIM})",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
            except Exception:
                pass  # 列已存在，静默跳过
        conn.commit()
    
    # GIN 索引: 支撑 tech_stacks 数组过滤
    with engine.connect() as conn:
        conn.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_mem_tech_stacks 
            ON memories USING GIN(tech_stacks)
        """))
        conn.commit()
    logger.info("✅ 数据库初始化完成 (含 v1.3 迁移 + GIN 索引)")


def check_health() -> bool:
    """检查 PG 连接是否可用"""
    try:
        from sqlalchemy import text
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception as e:
        logger.error(f"❌ PostgreSQL 连接失败: {e}")
        return False


# ============================================================
# 4. Embedding 函数 (复用 default_llm 单例)
# ============================================================

def get_embedding(text: str) -> Optional[List[float]]:
    """调用 Qwen text-embedding-v4 API 获取向量，复用 LLMClient 单例。"""
    if not text or not text.strip():
        return None
    t0 = time.time()
    try:
        from core.llm_client import default_llm
        response = default_llm.client.embeddings.create(
            model="text-embedding-v4",
            input=[text]
        )
        elapsed = (time.time() - t0) * 1000
        logger.info(f"🔢 Embedding 完成 ({elapsed:.0f}ms, dim={len(response.data[0].embedding)})")
        return response.data[0].embedding
    except Exception as e:
        logger.error(f"❌ Embedding 请求失败: {e}")
        return None


# ============================================================
# 5. 短期记忆操作 (替代 db.py)
# ============================================================

def append_event(
    role: str,
    event_type: str,
    content: str,
    project_id: str = "default",
    metadata: dict = None,
    embedding: list = None,
):
    """追加一条事件到短期记忆流。embedding 可选（项目经验写入时带向量）。"""
    logger.info(f"📝 短期记忆写入中... [{role}/{event_type}] project={project_id}")
    t0 = time.time()
    session = ScopedSession()
    try:
        record = SessionEvent(
            project_id=project_id,
            role=role,
            event_type=event_type,
            content=content,
            embedding=embedding,
            metadata_=metadata or {},
        )
        session.add(record)
        session.commit()
        elapsed = (time.time() - t0) * 1000
        vec_hint = " +vec" if embedding else ""
        logger.info(f"✅ 短期记忆写入完成 [{role}/{event_type}]{vec_hint} ({elapsed:.0f}ms)")
    except Exception as e:
        session.rollback()
        logger.error(f"写入 session_events 失败: {e}")
    finally:
        ScopedSession.remove()


def recall_project_experience(
    query: str,
    project_id: str,
    limit: int = 3,
    similarity_threshold: float = 0.5,
    caller: str = "Unknown",
) -> List[str]:
    """
    轻量 RAG: 对项目级经验 (experience_project) 做向量语义检索。
    仅检索有 embedding 的事件，按相似度排序，过滤低分项。
    返回: List[str] 经验文本内容
    """
    logger.info(f"🔍 [{caller}] 项目经验 RAG 召回中... project={project_id}")
    t0 = time.time()
    
    query_embedding = get_embedding(query)
    if not query_embedding:
        logger.warning(f"🔍 [{caller}] Embedding 失败，降级为时间序读取")
        # 降级: 回退到时间序
        events = get_recent_events(project_id=project_id, limit=limit,
                                   event_types=["experience_project"], caller=caller)
        return [e.content[:200] for e in events]
    
    session = ScopedSession()
    try:
        from sqlalchemy import text as sql_text
        sql = sql_text("""
            SELECT content, 1 - (embedding <=> CAST(:qvec AS vector)) AS similarity
            FROM session_events
            WHERE project_id = :pid
              AND event_type = 'experience_project'
              AND embedding IS NOT NULL
              AND 1 - (embedding <=> CAST(:qvec AS vector)) >= :threshold
            ORDER BY embedding <=> CAST(:qvec AS vector)
            LIMIT :n
        """)
        result = session.execute(sql, {
            "qvec": str(query_embedding),
            "pid": project_id,
            "threshold": similarity_threshold,
            "n": limit,
        })
        rows = list(result)
        elapsed = (time.time() - t0) * 1000
        
        if rows:
            sims = ', '.join([f"{float(r[1]):.3f}" for r in rows])
            logger.info(f"🔍 [{caller}] 项目经验 RAG 命中 {len(rows)} 条 ({elapsed:.0f}ms) sims=[{sims}]")
        else:
            logger.info(f"🔍 [{caller}] 项目经验 RAG 命中 0 条 ({elapsed:.0f}ms)")
        
        return [row[0] for row in rows]
    except Exception as e:
        logger.warning(f"⚠️ 项目经验 RAG 失败: {e}，降级为时间序")
        events = get_recent_events(project_id=project_id, limit=limit,
                                   event_types=["experience_project"], caller=caller)
        return [e.content[:200] for e in events]
    finally:
        ScopedSession.remove()


def get_recent_events(
    project_id: str = "default",
    limit: int = 10,
    event_types: Optional[List[str]] = None,
    caller: str = "Unknown"
) -> List[SessionEvent]:
    """滑动窗口：获取最近 N 条事件，支持按类型过滤。"""
    filter_hint = f" types={event_types}" if event_types else ""
    logger.info(f"📖 [{caller}] 短期记忆读取中... project={project_id} limit={limit}{filter_hint}")
    t0 = time.time()
    session = ScopedSession()
    try:
        query = session.query(SessionEvent).filter(SessionEvent.project_id == project_id)
        if event_types:
            query = query.filter(SessionEvent.event_type.in_(event_types))
        records = query.order_by(SessionEvent.created_at.desc()).limit(limit).all()
        result = list(reversed(records))
        elapsed = (time.time() - t0) * 1000
        types_summary = ", ".join([f"{r.role}/{r.event_type}" for r in result[-3:]]) if result else "空"
        logger.info(f"📖 [{caller}] 短期记忆命中 {len(result)} 条 ({elapsed:.0f}ms) 最近: [{types_summary}]")
        return result
    finally:
        ScopedSession.remove()


def rename_project_events(old_id: str, new_id: str):
    """项目重命名时同步迁移所有事件记录。"""
    session = ScopedSession()
    try:
        session.query(SessionEvent).filter(
            SessionEvent.project_id == old_id
        ).update({"project_id": new_id})
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"重命名事件记录失败: {e}")
    finally:
        ScopedSession.remove()


def upsert_file_tree(project_id: str, file_list: list):
    """覆盖式写入项目文件树到短期记忆（每个项目只保留最新的一条）。"""
    import json as _json
    session = ScopedSession()
    try:
        # 删除旧的 file_tree 事件
        session.query(SessionEvent).filter(
            SessionEvent.project_id == project_id,
            SessionEvent.event_type == "file_tree"
        ).delete()
        
        # 写入新的
        tree_content = _json.dumps({"files": file_list}, ensure_ascii=False)
        record = SessionEvent(
            project_id=project_id,
            role="system",
            event_type="file_tree",
            content=tree_content,
            metadata_={"file_count": len(file_list)},
        )
        session.add(record)
        session.commit()
        logger.info(f"📂 File Tree 已更新: {project_id} ({len(file_list)} 文件)")
    except Exception as e:
        session.rollback()
        logger.error(f"File Tree 写入失败: {e}")
    finally:
        ScopedSession.remove()


# ============================================================
# 5.5 轨迹表操作 (TDD 试错记录)
# ============================================================

def insert_trajectory(
    project_id: str,
    task_id: str,
    attempt_round: int,
    error_summary: str,
    failed_code: str,
    recalled_memory_ids: List[int] = None,
):
    """TDD 打回时写入一条试错轨迹（0 Token，纯静默 INSERT）。"""
    logger.info(f"📝 轨迹记录写入中... [{project_id}/{task_id}] round={attempt_round}")
    t0 = time.time()
    session = ScopedSession()
    try:
        record = TaskTrajectory(
            project_id=project_id,
            task_id=task_id,
            attempt_round=attempt_round,
            error_summary=error_summary[:2000] if error_summary else None,
            failed_code=failed_code[:5000] if failed_code else None,
            recalled_memory_ids=recalled_memory_ids or [],
        )
        session.add(record)
        session.commit()
        elapsed = (time.time() - t0) * 1000
        logger.info(f"✅ 轨迹记录写入完成 [{task_id}/round{attempt_round}] ({elapsed:.0f}ms)")
    except Exception as e:
        session.rollback()
        logger.error(f"轨迹记录写入失败: {e}")
    finally:
        ScopedSession.remove()


def finalize_trajectory(project_id: str, task_id: str, final_code: str):
    """TDD 通过时，将最终成功代码回填到该 task 的轨迹中。"""
    session = ScopedSession()
    try:
        session.query(TaskTrajectory).filter(
            TaskTrajectory.project_id == project_id,
            TaskTrajectory.task_id == task_id,
        ).update({"final_code": final_code[:5000] if final_code else None})
        session.commit()
        logger.info(f"✅ 轨迹最终代码回填完成 [{task_id}]")
    except Exception as e:
        session.rollback()
        logger.error(f"轨迹回填失败: {e}")
    finally:
        ScopedSession.remove()


def get_recalled_memory_union(project_id: str, task_id: str) -> set:
    """从轨迹表中捞出该 task 本轮召回的所有记忆 ID（排除已归档），去重并集。"""
    session = ScopedSession()
    try:
        from sqlalchemy import text as sql_text
        sql = sql_text("""
            SELECT DISTINCT UNNEST(recalled_memory_ids)
            FROM astrea_task_trajectories
            WHERE project_id = :pid AND task_id = :tid
              AND recalled_memory_ids IS NOT NULL
              AND is_synthesized = false
        """)
        result = session.execute(sql, {"pid": project_id, "tid": task_id})
        ids = {row[0] for row in result}
        logger.info(f"🔗 轨迹记忆并集: [{task_id}] → {len(ids)} 条去重 IDs: {ids}")
        return ids
    except Exception as e:
        logger.error(f"轨迹记忆并集查询失败: {e}")
        return set()
    finally:
        ScopedSession.remove()

# ============================================================
# 5.6 AMC 评分 (ASTrea Memory Consolidation)
# ============================================================

import math

def amc_score(s: int, u: int, delta_r: int, C: int = 5, k: float = 0.0005) -> float:
    """
    AMC 评分公式: ((s+C)/(u+2*C)) * log10(s+10) * exp(-k * delta_r)
    - C: 系统容忍度(惯性)，防止首次评分过高/过低
    - k: 时间衰减系数
    - delta_r: 当前全局轮次 - 该记忆的 last_used_round
    """
    return ((s + C) / (u + 2 * C)) * math.log10(s + 10) * math.exp(-k * delta_r)


# ============================================================
# 5.7 全局逻辑时钟
# ============================================================

class GlobalRound(Base):
    """全局逻辑时钟 — 仅 task 成功通过时 +1"""
    __tablename__ = "astrea_global_round"
    id = Column(Integer, primary_key=True, default=1)
    current_round = Column(Integer, default=0)


def get_global_round() -> int:
    """获取当前全局轮次 R。"""
    session = ScopedSession()
    try:
        row = session.query(GlobalRound).first()
        if not row:
            row = GlobalRound(id=1, current_round=0)
            session.add(row)
            session.commit()
        return row.current_round
    finally:
        ScopedSession.remove()


def tick_global_round() -> int:
    """任务成功通过时推进全局轮次 R += 1。返回新的 R。"""
    session = ScopedSession()
    try:
        row = session.query(GlobalRound).first()
        if not row:
            row = GlobalRound(id=1, current_round=0)
            session.add(row)
            session.flush()
        row.current_round += 1
        session.commit()
        new_r = row.current_round
        logger.info(f"⏰ 全局逻辑时钟推进: R = {new_r}")
        return new_r
    except Exception as e:
        session.rollback()
        logger.error(f"全局时钟推进失败: {e}")
        return 0
    finally:
        ScopedSession.remove()


# ============================================================
# 5.8 Score 结算 (延迟结算模式)
# ============================================================

def settle_memory_scores(used_ids: set, ignored_ids: set, global_round: int):
    """
    终局大清算 — 原子更新所有涉及记忆的 S/U/R。
    - 功臣(used): S+1, U+1, last_used_round=R
    - 陪跑(ignored): U+1, last_used_round=R
    """
    if not used_ids and not ignored_ids:
        return
    
    session = ScopedSession()
    try:
        from sqlalchemy import text as sql_text
        
        # 功臣: S+1, U+1
        if used_ids:
            ids_list = list(used_ids)
            session.execute(sql_text("""
                UPDATE memories 
                SET success_count = success_count + 1,
                    usage_count = usage_count + 1,
                    last_used_round = :r
                WHERE id = ANY(:ids)
            """), {"r": global_round, "ids": ids_list})
            logger.info(f"🏅 功臣结算: {ids_list} → S+1, U+1, R={global_round}")
        
        # 陪跑: U+1 only
        if ignored_ids:
            ids_list = list(ignored_ids)
            session.execute(sql_text("""
                UPDATE memories 
                SET usage_count = usage_count + 1,
                    last_used_round = :r
                WHERE id = ANY(:ids)
            """), {"r": global_round, "ids": ids_list})
            logger.info(f"🚶 陪跑结算: {ids_list} → U+1, R={global_round}")
        
        session.commit()
        logger.info(f"✅ AMC 结算完成: 功臣{len(used_ids)} + 陪跑{len(ignored_ids)}")
    except Exception as e:
        session.rollback()
        logger.error(f"AMC 结算失败: {e}")
    finally:
        ScopedSession.remove()


# ============================================================
# 6. 长期记忆操作 (替代 memory.py)
# ============================================================

def memorize(
    text: str,
    scope: str = "project",
    project_id: str = "global",
    metadata: dict = None,
    tech_stacks: list = None,
    exp_type: str = "general",
    scenario: str = "",
):
    """
    向量化一段经验并存入长期记忆。
    Embedding 提纯策略：仅对 scenario + content 做向量化，tech_stacks 标签不参与。
    """
    if not text or not text.strip():
        return

    # Embedding 提纯：scenario + content，排除技术栈标签
    embed_text = f"{scenario}\n{text}" if scenario else text
    embedding = get_embedding(embed_text)
    if not embedding:
        return

    session = ScopedSession()
    try:
        record = Memory(
            project_id=project_id if scope == "project" else None,
            scope=scope,
            content=text,
            embedding=embedding,
            metadata_=metadata or {},
            tech_stacks=tech_stacks or [],
            exp_type=exp_type,
            scenario=scenario or None,
        )
        session.add(record)
        session.commit()
        logger.info(f"✅ 长期记忆写入 ({scope}/{project_id}): '{text[:30]}...' stacks={tech_stacks or []}")
    except Exception as e:
        session.rollback()
        logger.error(f"长期记忆写入失败: {e}")
    finally:
        ScopedSession.remove()


def _rerank(query: str, documents: List[str], top_n: int = 5) -> List[dict]:
    """
    调用 DashScope Rerank API 对候选文档精排。
    
    返回: [{"content": str, "score": float}, ...] 按相关性降序
    """
    import httpx
    
    api_key = os.getenv("QWEN_API_KEY", "")
    if not api_key:
        logger.warning("⚠️ Rerank: QWEN_API_KEY 未配置，跳过精排")
        return [{"content": doc, "score": 0.0} for doc in documents[:top_n]]
    
    t0 = time.time()
    try:
        resp = httpx.post(
            "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": os.getenv("MODEL_RERANKER", "gte-rerank-v2"),
                "input": {
                    "query": query,
                    "documents": documents,
                },
                "parameters": {
                    "top_n": top_n,
                    "return_documents": True,
                },
            },
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        
        results = data.get("output", {}).get("results", [])
        elapsed = (time.time() - t0) * 1000
        
        ranked = []
        for item in results:
            ranked.append({
                "content": item.get("document", {}).get("text", documents[item.get("index", 0)]),
                "score": item.get("relevance_score", 0.0),
            })
        
        scores = ', '.join([f"{r['score']:.3f}" for r in ranked])
        logger.info(f"🎯 Rerank 精排完成 ({elapsed:.0f}ms): {len(documents)} → {len(ranked)} 条, scores=[{scores}]")
        return ranked
    except Exception as e:
        logger.warning(f"⚠️ Rerank 调用失败 ({e})，fallback 到粗排直出")
        return [{"content": doc, "score": 0.0} for doc in documents[:top_n]]


def _bm25_search(query: str, project_id: str, top_n: int = 15) -> List[Dict]:
    """
    BM25 关键词粗排：从 memories 表全量加载后做 BM25 检索。
    返回 List[Dict] 含 id/content/s/u/r_last。
    """
    import jieba
    from rank_bm25 import BM25Okapi
    
    session = ScopedSession()
    try:
        from sqlalchemy import text as sql_text
        
        sql = sql_text("""
            SELECT id, content, success_count, usage_count, last_used_round
            FROM memories
            WHERE (scope = 'global' OR (scope = 'project' AND project_id = :pid))
        """)
        result = session.execute(sql, {"pid": project_id})
        all_rows = [(row[0], row[1], row[2] or 0, row[3] or 0, row[4] or 0) for row in result if row[1]]
        
        if not all_rows:
            return []
        
        t0 = time.time()
        
        # jieba 分词
        all_contents = [r[1] for r in all_rows]
        tokenized_docs = [list(jieba.cut(doc)) for doc in all_contents]
        tokenized_query = list(jieba.cut(query))
        
        # BM25 检索
        bm25 = BM25Okapi(tokenized_docs)
        scores = bm25.get_scores(tokenized_query)
        
        # 取 Top N（排除零分）
        scored = [(all_rows[i], scores[i]) for i in range(len(all_rows)) if scores[i] > 0]
        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[:top_n]
        
        elapsed = (time.time() - t0) * 1000
        if top:
            bm25_scores = ', '.join([f"{s:.2f}" for _, s in top[:5]])
            logger.info(f"📊 BM25 粗排完成 ({elapsed:.0f}ms): {len(all_rows)}篇 → {len(top)} 条, scores=[{bm25_scores}]")
        
        return [{
            "id": row[0], "content": row[1],
            "s": row[2], "u": row[3], "r_last": row[4],
            "similarity": 0.0,
        } for row, _ in top]
    except Exception as e:
        logger.warning(f"⚠️ BM25 检索失败: {e}")
        return []
    finally:
        ScopedSession.remove()


def _vector_search_with_filter(
    query_embedding, project_id: str, top_n: int,
    similarity_threshold: float, tech_stacks_filter: list = None,
    universal_only: bool = False
) -> List[Dict]:
    """
    pgvector 向量粗排（内部方法）。
    - tech_stacks_filter: 主管线按技术栈过滤
    - universal_only: 副管线只捞通用经验 (tech_stacks = '{}')
    """
    session = ScopedSession()
    try:
        from sqlalchemy import text as sql_text
        
        if universal_only:
            # 副管线: 只捞 tech_stacks 为空数组的通用经验
            sql = sql_text("""
                SELECT id, content, success_count, usage_count, last_used_round,
                       1 - (embedding <=> CAST(:qvec AS vector)) AS similarity
                FROM memories
                WHERE (scope = 'global' OR (scope = 'project' AND project_id = :pid))
                  AND (tech_stacks = '{}' OR tech_stacks IS NULL)
                  AND 1 - (embedding <=> CAST(:qvec AS vector)) >= :threshold
                ORDER BY embedding <=> CAST(:qvec AS vector)
                LIMIT :n
            """)
            result = session.execute(sql, {
                "qvec": str(query_embedding), "pid": project_id,
                "threshold": similarity_threshold, "n": top_n,
            })
        elif tech_stacks_filter:
            # 主管线: 按 tech_stacks 过滤 + 通用经验
            sql = sql_text("""
                SELECT id, content, success_count, usage_count, last_used_round,
                       1 - (embedding <=> CAST(:qvec AS vector)) AS similarity
                FROM memories
                WHERE (scope = 'global' OR (scope = 'project' AND project_id = :pid))
                  AND (tech_stacks && CAST(:stacks AS VARCHAR[]) OR tech_stacks = '{}' OR tech_stacks IS NULL)
                  AND 1 - (embedding <=> CAST(:qvec AS vector)) >= :threshold
                ORDER BY embedding <=> CAST(:qvec AS vector)
                LIMIT :n
            """)
            result = session.execute(sql, {
                "qvec": str(query_embedding), "pid": project_id,
                "stacks": "{" + ",".join(tech_stacks_filter) + "}",
                "threshold": similarity_threshold, "n": top_n,
            })
        else:
            # 无过滤: 全量
            sql = sql_text("""
                SELECT id, content, success_count, usage_count, last_used_round,
                       1 - (embedding <=> CAST(:qvec AS vector)) AS similarity
                FROM memories
                WHERE (scope = 'global' OR (scope = 'project' AND project_id = :pid))
                  AND 1 - (embedding <=> CAST(:qvec AS vector)) >= :threshold
                ORDER BY embedding <=> CAST(:qvec AS vector)
                LIMIT :n
            """)
            result = session.execute(sql, {
                "qvec": str(query_embedding), "pid": project_id,
                "threshold": similarity_threshold, "n": top_n,
            })
        
        rows = list(result)
        return [{
            "id": row[0], "content": row[1],
            "s": row[2] or 0, "u": row[3] or 0, "r_last": row[4] or 0,
            "similarity": float(row[5]),
        } for row in rows]
    except Exception as e:
        logger.error(f"向量检索失败: {e}")
        return []
    finally:
        ScopedSession.remove()


def _apply_amc_and_fuse(items: List[Dict], global_r: int, alpha: float = 0.7) -> List[Dict]:
    """
    L3+L4: 对一批候选记忆计算 AMC 分数 → 归一化 → 加权融合排序。
    Final_Score = α * sim_norm + (1-α) * score_norm
    """
    if not items:
        return []
    
    # L3: 计算 AMC score
    for item in items:
        delta_r = global_r - item.get("r_last", 0)
        item["amc"] = amc_score(item.get("s", 0), item.get("u", 0), delta_r)
    
    # 归一化 similarity
    sims = [item["similarity"] for item in items]
    sim_min, sim_max = min(sims), max(sims)
    for item in items:
        item["sim_norm"] = (item["similarity"] - sim_min) / (sim_max - sim_min) if sim_max > sim_min else 0.5
    
    # 归一化 AMC score
    amcs = [item["amc"] for item in items]
    amc_min, amc_max = min(amcs), max(amcs)
    for item in items:
        item["score_norm"] = (item["amc"] - amc_min) / (amc_max - amc_min) if amc_max > amc_min else 0.5
    
    # L4: 加权融合
    for item in items:
        item["final_score"] = alpha * item["sim_norm"] + (1 - alpha) * item["score_norm"]
    
    items.sort(key=lambda x: x["final_score"], reverse=True)
    return items


def _run_main_pipeline(
    query: str, query_embedding, project_id: str, 
    tech_stacks: list, top_n: int, similarity_threshold: float,
    global_r: int, caller: str
) -> List[Dict]:
    """主管线: L0→L1(双路)→L2(Rerank)→L3(AMC)→L4(融合)→Top N"""
    t0 = time.time()
    
    # L0+L1: pgvector
    vector_results = _vector_search_with_filter(
        query_embedding, project_id, top_n=15,
        similarity_threshold=similarity_threshold,
        tech_stacks_filter=tech_stacks if tech_stacks else None,
    )
    
    # L1: BM25 (已返回 List[Dict] 含 id)
    bm25_results = _bm25_search(query, project_id, top_n=15)
    
    # 合并去重
    seen = set()
    merged = []
    for item in vector_results + bm25_results:
        key = item["content"].strip()[:100]
        if key not in seen:
            seen.add(key)
            merged.append(item)
    
    if not merged:
        return []
    
    logger.info(f"🔍 [{caller}] 主管线 L1: 向量{len(vector_results)} + BM25{len(bm25_results)} → {len(merged)}")
    
    # L2: Rerank (缩至 Top 10)
    if len(merged) > 10:
        rerank_docs = [item["content"] for item in merged]
        ranked = _rerank(query, rerank_docs, top_n=10)
        content_map = {item["content"].strip()[:100]: item for item in merged}
        reranked = []
        for r in ranked:
            key = r["content"].strip()[:100]
            original = content_map.get(key, {"id": -1, "s": 0, "u": 0, "r_last": 0})
            reranked.append({**original, "content": r["content"], "similarity": r["score"]})
        merged = reranked
    
    # L3+L4: AMC + 融合
    fused = _apply_amc_and_fuse(merged, global_r)
    
    elapsed = (time.time() - t0) * 1000
    logger.info(f"🔍 [{caller}] 主管线完成 ({elapsed:.0f}ms) → Top {min(top_n, len(fused))}")
    
    return fused[:top_n]


def _run_universal_pipeline(
    query: str, query_embedding, project_id: str,
    similarity_threshold: float, global_r: int, caller: str
) -> Optional[Dict]:
    """副管线: L0(通用)→L1(pgvector)→L3(AMC)→L4→Top 1 (跳过 Rerank)"""
    t0 = time.time()
    
    vector_results = _vector_search_with_filter(
        query_embedding, project_id, top_n=3,
        similarity_threshold=similarity_threshold,
        universal_only=True,
    )
    
    if not vector_results:
        return None
    
    fused = _apply_amc_and_fuse(vector_results, global_r)
    
    elapsed = (time.time() - t0) * 1000
    logger.info(f"🔍 [{caller}] 副管线(通用) ({elapsed:.0f}ms) → Top 1")
    
    return fused[0] if fused else None


def recall(
    query: str,
    n_results: int = 3,
    project_id: str = "global",
    similarity_threshold: float = 0.6,
    caller: str = "Unknown",
    tech_stacks: list = None,
) -> List[Dict]:
    """
    四段式双管线长期记忆召回 (v1.2.2):
    
    主管线: L0 tech_stacks → L1 pgvector+BM25 → L2 Rerank → L3 AMC → L4 融合 → Top 3
    副管线: L0 universal  → L1 pgvector       → L3 AMC    → L4      → Top 1
    合并去重 → 3~4 条
    
    返回: [{"id": 12, "content": "...", "similarity": 0.87}, ...]
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    logger.info(f"🔍 [{caller}] 四段式召回启动... query='{query[:50]}...' stacks={tech_stacks}")
    t0 = time.time()
    
    query_embedding = get_embedding(query)
    if not query_embedding:
        logger.warning(f"🔍 [{caller}] Embedding 失败，返回空")
        return []
    
    global_r = get_global_round()
    
    # 双管线并行
    main_result = []
    univ_result = None
    
    with ThreadPoolExecutor(max_workers=2) as executor:
        future_main = executor.submit(
            _run_main_pipeline, query, query_embedding, project_id,
            tech_stacks or [], n_results, similarity_threshold, global_r, caller
        )
        future_univ = executor.submit(
            _run_universal_pipeline, query, query_embedding, project_id,
            similarity_threshold, global_r, caller
        )
        
        main_result = future_main.result()
        univ_result = future_univ.result()
    
    # 合并去重
    main_ids = {item["id"] for item in main_result}
    if univ_result and univ_result["id"] not in main_ids:
        main_result.append(univ_result)
        logger.info(f"🔍 [{caller}] 通用经验保底席位生效: id={univ_result['id']}")
    
    # 清理内部计算字段，返回干净 Dict
    final = []
    for item in main_result:
        final.append({
            "id": item.get("id", -1),
            "content": item.get("content", ""),
            "similarity": item.get("similarity", 0.0),
        })
    
    total_elapsed = (time.time() - t0) * 1000
    ids_str = [f"id={r['id']}" for r in final]
    logger.info(f"🔍 [{caller}] 四段式召回完成 ({total_elapsed:.0f}ms): {len(final)} 条 {ids_str}")
    
    return final



def recall_reviewer_experience(
    query: str,
    n_results: int = 2,
    caller: str = "Reviewer",
) -> List[str]:
    """
    Reviewer 测试经验轻量召回（exp_type='reviewer_test'）。
    
    纯向量相似度，不走 BM25/Rerank/AMC，开销极小。
    返回: ["经验1内容", "经验2内容"]
    """
    t0 = time.time()
    embedding = get_embedding(query)
    if not embedding:
        return []
    
    session = ScopedSession()
    try:
        results = session.query(Memory).filter(
            Memory.exp_type == "reviewer_test",
            Memory.scope == "global",
        ).order_by(
            Memory.embedding.cosine_distance(embedding)
        ).limit(n_results).all()
        
        contents = [r.content for r in results]
        elapsed = (time.time() - t0) * 1000
        logger.info(f"🧪 [{caller}] Reviewer 测试经验召回 ({elapsed:.0f}ms): {len(contents)} 条")
        return contents
    except Exception as e:
        logger.error(f"Reviewer 测试经验召回失败: {e}")
        return []
    finally:
        ScopedSession.remove()


# ============================================================
# 7. 项目元数据操作
# ============================================================

def create_project_meta(project_id: str, name: str = None):
    """创建项目元数据记录。"""
    session = ScopedSession()
    try:
        existing = session.query(ProjectMeta).filter(ProjectMeta.project_id == project_id).first()
        if not existing:
            record = ProjectMeta(project_id=project_id, name=name or project_id)
            session.add(record)
            session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"创建项目元数据失败: {e}")
    finally:
        ScopedSession.remove()


def update_project_status(project_id: str, status: str):
    """更新项目状态 (in_progress / success / failed)。"""
    session = ScopedSession()
    try:
        session.query(ProjectMeta).filter(
            ProjectMeta.project_id == project_id
        ).update({"status": status, "updated_at": datetime.utcnow()})
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"更新项目状态失败: {e}")
    finally:
        ScopedSession.remove()


def rename_project_meta(old_id: str, new_id: str, new_name: str = None):
    """项目重命名时同步更新元数据。"""
    session = ScopedSession()
    try:
        updates = {"project_id": new_id, "updated_at": datetime.utcnow()}
        if new_name:
            updates["name"] = new_name
        session.query(ProjectMeta).filter(
            ProjectMeta.project_id == old_id
        ).update(updates)
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"重命名项目元数据失败: {e}")
    finally:
        ScopedSession.remove()


# ============================================================
# 8. 项目经验毕业 & 级联清理
# ============================================================

def graduate_project_experience(project_id: str) -> int:
    """
    将项目的短期经验"毕业"为全局长期记忆。
    从 session_events 中捞出 experience_project 事件，向量化后写入 memories 表 scope=global。
    
    返回：毕业的经验条数
    """
    session = ScopedSession()
    try:
        # 捞出该项目的所有 project 经验
        events = session.query(SessionEvent).filter(
            SessionEvent.project_id == project_id,
            SessionEvent.event_type == "experience_project"
        ).all()
        
        if not events:
            logger.info(f"🎓 项目 {project_id} 无 project 经验可毕业")
            return 0
        
        count = 0
        for event in events:
            content = event.content
            if not content or not content.strip():
                continue
            
            embedding = get_embedding(content)
            if not embedding:
                continue
            
            record = Memory(
                project_id=None,  # global 不关联特定项目
                scope="global",
                content=content,
                embedding=embedding,
                metadata_={
                    "source": "graduated",
                    "original_project": project_id,
                    **(event.metadata_ or {}),
                },
            )
            session.add(record)
            count += 1
        
        # 更新项目状态为 graduated
        session.query(ProjectMeta).filter(
            ProjectMeta.project_id == project_id
        ).update({"status": "graduated", "updated_at": datetime.utcnow()})
        
        session.commit()
        logger.info(f"🎓 项目 {project_id} 经验毕业完成: {count} 条 project 经验升级为 global")
        return count
    except Exception as e:
        session.rollback()
        logger.error(f"经验毕业失败: {e}")
        return 0
    finally:
        ScopedSession.remove()


def delete_project_events(project_id: str):
    """删除项目时级联清理短期记忆（session_events 中该 project 的所有记录）。"""
    session = ScopedSession()
    try:
        count = session.query(SessionEvent).filter(
            SessionEvent.project_id == project_id
        ).delete()
        session.commit()
        logger.info(f"🗑️ 项目 {project_id} 短期记忆已清除: {count} 条事件")
    except Exception as e:
        session.rollback()
        logger.error(f"清除项目短期记忆失败: {e}")
    finally:
        ScopedSession.remove()


# ============================================================
# Blackboard Checkpoint 持久化 (v1.3)
# ============================================================

def save_checkpoint(project_id: str, state_json: str):
    """UPSERT Blackboard 状态快照到 PostgreSQL"""
    session = ScopedSession()
    try:
        existing = session.query(BlackboardCheckpoint).filter(
            BlackboardCheckpoint.project_id == project_id
        ).first()
        if existing:
            existing.state_json = state_json
            existing.updated_at = datetime.utcnow()
        else:
            session.add(BlackboardCheckpoint(
                project_id=project_id,
                state_json=state_json,
            ))
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"❌ Checkpoint 保存失败: {e}")
        raise
    finally:
        ScopedSession.remove()


def load_checkpoint(project_id: str) -> Optional[str]:
    """从 PostgreSQL 加载 Blackboard 状态快照"""
    session = ScopedSession()
    try:
        row = session.query(BlackboardCheckpoint).filter(
            BlackboardCheckpoint.project_id == project_id
        ).first()
        return row.state_json if row else None
    except Exception as e:
        logger.error(f"❌ Checkpoint 加载失败: {e}")
        return None
    finally:
        ScopedSession.remove()


def delete_checkpoint(project_id: str):
    """清理已完成/放弃的 Blackboard 快照"""
    session = ScopedSession()
    try:
        session.query(BlackboardCheckpoint).filter(
            BlackboardCheckpoint.project_id == project_id
        ).delete()
        session.commit()
        logger.info(f"🗑️ Checkpoint 已清理: {project_id}")
    except Exception as e:
        session.rollback()
        logger.error(f"❌ Checkpoint 清理失败: {e}")
    finally:
        ScopedSession.remove()


def list_pending_checkpoints() -> List[Dict[str, Any]]:
    """列出所有未完成的 Checkpoint（用于前端恢复弹窗）"""
    session = ScopedSession()
    try:
        rows = session.query(BlackboardCheckpoint).all()
        return [
            {
                "project_id": r.project_id,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
            }
            for r in rows
        ]
    except Exception as e:
        logger.error(f"❌ Checkpoint 列表查询失败: {e}")
        return []
    finally:
        ScopedSession.remove()
