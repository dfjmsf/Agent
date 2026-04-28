"""
seed_memories.py — 全局种子经验注入器 (Memory A-1)

首次启动时向 memories 长期记忆表注入预置的高质量经验，
让 ASTrea 从第一天起就具备框架避坑意识。

幂等设计：如果已检测到 exp_type='seed' 的记录存在，则跳过注入。
"""
import os
import sys
import json
import logging

# 确保能找到 core 模块
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.database import memorize, ScopedSession, Memory

logger = logging.getLogger("SeedMemories")

SEED_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config", "seed_memories.json"
)


def _already_seeded() -> bool:
    """检查是否已经注入过种子经验（幂等守卫）"""
    session = ScopedSession()
    try:
        count = session.query(Memory).filter(
            Memory.exp_type == "seed",
            Memory.scope == "global",
        ).count()
        return count > 0
    except Exception as e:
        logger.warning(f"⚠️ 种子检测查询失败: {e}")
        return False
    finally:
        ScopedSession.remove()


def seed_global_memories():
    """
    从 config/seed_memories.json 读取种子经验并批量写入 memories 表。
    幂等：如果已有 seed 类型的记录则跳过。
    """
    if not os.path.isfile(SEED_FILE):
        logger.info("ℹ️ 未找到 seed_memories.json，跳过种子注入")
        return 0

    if _already_seeded():
        logger.info("ℹ️ 种子经验已存在，跳过重复注入")
        return 0

    try:
        with open(SEED_FILE, "r", encoding="utf-8") as f:
            seeds = json.load(f)
    except Exception as e:
        logger.error(f"❌ 读取 seed_memories.json 失败: {e}")
        return 0

    if not isinstance(seeds, list):
        logger.error("❌ seed_memories.json 格式错误：根元素必须是 JSON 数组")
        return 0

    injected = 0
    for i, entry in enumerate(seeds):
        content = entry.get("content", "").strip()
        if not content:
            continue

        try:
            memorize(
                text=content,
                scope="global",
                project_id="global",
                tech_stacks=entry.get("tech_stacks", []),
                exp_type="seed",  # 固定标记为种子经验，供幂等检测
                scenario=entry.get("scenario", ""),
                domain=entry.get("domain", "general"),
            )
            injected += 1
            logger.info(f"🌱 [{i+1}/{len(seeds)}] 种子注入成功: {content[:40]}...")
        except Exception as e:
            logger.warning(f"⚠️ 种子 #{i+1} 注入失败: {e}")

    logger.info(f"🌱 种子经验注入完成: {injected}/{len(seeds)} 条")
    return injected


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    count = seed_global_memories()
    print(f"\n🌱 注入完成，共 {count} 条种子经验写入长期记忆库。")
