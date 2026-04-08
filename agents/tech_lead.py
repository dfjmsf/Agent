"""
TechLead Agent — 跨文件冲突仲裁者

职责：
- 仅在跨文件 L0.6 冲突 + Coder 重试 3+ 次后被唤醒
- 阅读冲突双方文件 + 需求上下文 → 判断"谁改 + 怎么改"
- 不直接写代码，只输出仲裁指令

可控性约束：
- 每个 task 最多唤醒 1 次（task.tech_lead_invoked 标记）
- 只读多文件，不写任何文件
- 修复仍由 Coder 执行（保持单文件写入原则）
"""
import os
import json
import logging
from typing import Optional, Dict

from core.llm_client import default_llm
from core.prompt import Prompts

logger = logging.getLogger("TechLead")


class TechLeadAgent:
    """技术骨干 — 仅在跨文件冲突时被唤醒的仲裁者"""

    def __init__(self):
        self.model = os.getenv("MODEL_TECH_LEAD", "qwen3-max")

    def arbitrate(
        self,
        current_file: str,
        current_code: str,
        conflict_file: str,
        conflict_code: str,
        l06_error: str,
        user_requirement: str,
    ) -> Optional[Dict]:
        """
        跨文件冲突仲裁。

        Args:
            current_file: 当前正在编写的目标文件
            current_code: 当前文件的最新代码
            conflict_file: L0.6 检测到的冲突来源文件
            conflict_code: 冲突来源文件的代码
            l06_error: Reviewer L0.6 的完整错误描述
            user_requirement: 用户原始需求（语义判断依据）

        Returns:
            {
                "guilty_file": "routes.py",       # 谁需要修改
                "fix_instruction": "...",           # 给 Coder 的精确修复指令
                "reasoning": "..."                 # 推理过程（日志用）
            }
            失败时返回 None
        """
        logger.info(
            f"⚖️ TechLead 仲裁启动: {current_file} ↔ {conflict_file}"
        )

        prompt = Prompts.TECH_LEAD_ARBITRATE.format(
            current_file=current_file,
            current_code=current_code[:3000],  # 截断防 token 爆炸
            conflict_file=conflict_file,
            conflict_code=conflict_code[:3000],
            l06_error=l06_error,
            user_requirement=user_requirement[:500],
        )

        try:
            response = default_llm.chat_completion(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,  # 低温度，仲裁要确定性
            )
            raw = response.content.strip()

            # 清理 Markdown 代码块
            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0].strip()
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0].strip()

            verdict = json.loads(raw)

            # 校验必要字段
            if "guilty_file" not in verdict or "fix_instruction" not in verdict:
                logger.error(f"❌ TechLead 返回缺少必要字段: {verdict}")
                return None

            logger.info(
                f"⚖️ TechLead 仲裁结果: guilty={verdict['guilty_file']} | "
                f"reason={verdict.get('reasoning', '无')[:80]}"
            )
            return verdict

        except json.JSONDecodeError as e:
            logger.error(f"❌ TechLead 返回非法 JSON: {e}")
            return None
        except Exception as e:
            logger.error(f"❌ TechLead 调用失败: {e}")
            return None
