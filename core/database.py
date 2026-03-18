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
    logger.info("✅ 数据库初始化完成 (含 v1.2.2 迁移 + GIN 索引)")


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
    metadata: dict = None
):
    """追加一条事件到短期记忆流。"""
    logger.info(f"📝 短期记忆写入中... [{role}/{event_type}] project={project_id}")
    t0 = time.time()
    session = ScopedSession()
    try:
        record = SessionEvent(
            project_id=project_id,
            role=role,
            event_type=event_type,
            content=content,
            metadata_=metadata or {},
        )
        session.add(record)
        session.commit()
        elapsed = (time.time() - t0) * 1000
        logger.info(f"✅ 短期记忆写入完成 [{role}/{event_type}] ({elapsed:.0f}ms)")
    except Exception as e:
        session.rollback()
        logger.error(f"写入 session_events 失败: {e}")
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
    """从轨迹表中捞出该 task 历次召回的所有记忆 ID，去重并集。"""
    session = ScopedSession()
    try:
        from sqlalchemy import text as sql_text
        sql = sql_text("""
            SELECT DISTINCT UNNEST(recalled_memory_ids)
            FROM astrea_task_trajectories
            WHERE project_id = :pid AND task_id = :tid
              AND recalled_memory_ids IS NOT NULL
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


def _bm25_search(query: str, project_id: str, top_n: int = 15) -> List[str]:
    """
    BM25 关键词粗排：从 memories 表全量加载后做 BM25 检索。
    使用 jieba 分词处理中文。
    """
    import jieba
    from rank_bm25 import BM25Okapi
    
    session = ScopedSession()
    try:
        from sqlalchemy import text as sql_text
        
        sql = sql_text("""
            SELECT content FROM memories
            WHERE (scope = 'global' OR (scope = 'project' AND project_id = :pid))
        """)
        result = session.execute(sql, {"pid": project_id})
        all_docs = [row[0] for row in result if row[0]]
        
        if not all_docs:
            return []
        
        t0 = time.time()
        
        # jieba 分词
        tokenized_docs = [list(jieba.cut(doc)) for doc in all_docs]
        tokenized_query = list(jieba.cut(query))
        
        # BM25 检索
        bm25 = BM25Okapi(tokenized_docs)
        scores = bm25.get_scores(tokenized_query)
        
        # 取 Top N（排除零分）
        scored_docs = [(all_docs[i], scores[i]) for i in range(len(all_docs)) if scores[i] > 0]
        scored_docs.sort(key=lambda x: x[1], reverse=True)
        top_docs = scored_docs[:top_n]
        
        elapsed = (time.time() - t0) * 1000
        if top_docs:
            bm25_scores = ', '.join([f"{s:.2f}" for _, s in top_docs[:5]])
            logger.info(f"📊 BM25 粗排完成 ({elapsed:.0f}ms): {len(all_docs)}篇 → {len(top_docs)} 条, scores=[{bm25_scores}]")
        
        return [doc for doc, _ in top_docs]
    except Exception as e:
        logger.warning(f"⚠️ BM25 检索失败: {e}")
        return []
    finally:
        ScopedSession.remove()


def recall(
    query: str,
    n_results: int = 5,
    project_id: str = "global",
    similarity_threshold: float = 0.6,
    caller: str = "Unknown"
) -> List[Dict]:
    """
    三阶段双路长期记忆召回（返回 Dict 含 id 用于 Auditor 追踪）：
    1. pgvector 向量粗排 Top 15 (threshold≥0.6)
    2. BM25 关键词粗排 Top 15
    3. 合并去重 → DashScope Rerank 精排 Top N
    
    返回: [{"id": 12, "content": "...", "similarity": 0.87}, ...]
    """
    COARSE_TOP_N = 15
    
    logger.info(f"🔍 [{caller}] 长期记忆召回中... query='{query[:50]}...' project={project_id}")
    t0 = time.time()
    
    # === 路径 1: 向量粗排（返回 id + content + similarity） ===
    vector_results = []
    query_embedding = get_embedding(query)
    if query_embedding:
        session = ScopedSession()
        try:
            from sqlalchemy import text as sql_text
            sql = sql_text("""
                SELECT id, content, 1 - (embedding <=> CAST(:qvec AS vector)) AS similarity
                FROM memories
                WHERE (scope = 'global' OR (scope = 'project' AND project_id = :pid))
                  AND 1 - (embedding <=> CAST(:qvec AS vector)) >= :threshold
                ORDER BY embedding <=> CAST(:qvec AS vector)
                LIMIT :n
            """)
            result = session.execute(sql, {
                "qvec": str(query_embedding),
                "pid": project_id,
                "threshold": similarity_threshold,
                "n": COARSE_TOP_N,
            })
            rows = list(result)
            vector_results = [{"id": row[0], "content": row[1], "similarity": float(row[2])} for row in rows]
            
            if rows:
                sims = ', '.join([f"{r['similarity']:.3f}" for r in vector_results])
                logger.info(f"🔍 [{caller}] 向量粗排命中 {len(rows)} 条, 相似度=[{sims}]")
        except Exception as e:
            logger.error(f"向量粗排失败: {e}")
        finally:
            ScopedSession.remove()
    else:
        logger.warning(f"🔍 [{caller}] Embedding 失败，仅使用 BM25 路径")
    
    # === 路径 2: BM25 粗排（无 id，设为 -1 占位） ===
    bm25_raw = _bm25_search(query, project_id, top_n=COARSE_TOP_N)
    bm25_results = [{"id": -1, "content": doc, "similarity": 0.0} for doc in bm25_raw]
    
    # === 合并去重 ===
    seen = set()
    merged = []
    for item in vector_results + bm25_results:
        doc_key = item["content"].strip()[:100]
        if doc_key not in seen:
            seen.add(doc_key)
            merged.append(item)
    
    merge_elapsed = (time.time() - t0) * 1000
    logger.info(f"🔍 [{caller}] 双路合并: 向量{len(vector_results)} + BM25 {len(bm25_results)} → 去重后 {len(merged)} 条 ({merge_elapsed:.0f}ms)")
    
    if not merged:
        return []
    
    # 如果合并结果 ≤ n_results，直接返回
    if len(merged) <= n_results:
        logger.info(f"🔍 [{caller}] 合并结果 ≤ {n_results} 条，跳过 Rerank")
        return merged
    
    # === Rerank 精排 ===
    rerank_docs = [item["content"] for item in merged]
    ranked = _rerank(query, rerank_docs, top_n=n_results)
    
    # 将 Rerank 结果映射回 Dict（保留 id）
    content_to_item = {item["content"].strip()[:100]: item for item in merged}
    final_results = []
    for r in ranked:
        key = r["content"].strip()[:100]
        original = content_to_item.get(key, {})
        final_results.append({
            "id": original.get("id", -1),
            "content": r["content"],
            "similarity": r["score"],
        })
    
    total_elapsed = (time.time() - t0) * 1000
    logger.info(f"🔍 [{caller}] 召回完成 ({total_elapsed:.0f}ms): 向量{len(vector_results)} + BM25 {len(bm25_results)} → 合并{len(merged)} → 精排{len(final_results)}")
    
    return final_results



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
