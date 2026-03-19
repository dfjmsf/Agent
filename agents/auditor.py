import os
import json
import re
import logging
from typing import Dict, Any, List
from core.llm_client import default_llm
from core.prompt import Prompts

logger = logging.getLogger("AuditorAgent")


class AuditorAgent:
    """
    审计 Agent (Auditor) — 独立于 TDD 编排之外
    专职：分析最终代码是否实质性采用了召回的历史经验。
    不参与代码生成、不读写记忆，仅做归因审计。
    """
    def __init__(self):
        self.model = os.getenv("MODEL_AUDITOR", "qwen3-max")

    def audit(self, final_code: str, memories: List[Dict]) -> Dict:
        """
        审计最终代码与注入的记忆之间的归因关系。
        
        Args:
            final_code: 最终通过测试的代码
            memories: 注入 Coder 的记忆列表 [{"id": 12, "content": "..."}, ...]
        
        Returns:
            {"results": [{"memory_id": 12, "adopted": True, "confidence": 0.9, "evidence": "..."}, ...]}
        """
        if not memories:
            logger.info("📋 Auditor: 无记忆需要审计，跳过")
            return {"results": []}
        
        # 构建记忆清单
        memory_list = "\n".join([
            f"  [{m['id']}] {m.get('content', '')[:300]}"
            for m in memories
        ])
        
        system_content = Prompts.AUDITOR_SYSTEM.format(
            final_code=final_code[:3000],
            memory_list=memory_list,
        )
        
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": "请严格审计以上代码与记忆的归因关系。输出纯净 JSON。"},
        ]
        
        try:
            logger.info(f"📋 Auditor 正在审计 {len(memories)} 条记忆的归因...")
            resp = default_llm.chat_completion(messages, model=self.model, temperature=0.1)
            parsed = self._parse_response(resp.content, memories)
            
            adopted_count = sum(1 for r in parsed["results"] if r["adopted"])
            logger.info(f"📋 Auditor 审计完成: {adopted_count}/{len(memories)} 条被采用")
            
            # 列出每条记忆的当前 AMC 分数
            self._log_memory_scores(parsed, memories)
            
            return parsed
            
        except Exception as e:
            logger.error(f"❌ Auditor 审计异常: {e}")
            # 异常时保守策略：全部标记为 adopted，避免错杀
            return {"results": [
                {"memory_id": m["id"], "adopted": True, "confidence": 0.5, "evidence": "审计异常，保守通过"}
                for m in memories
            ]}
    
    def _log_memory_scores(self, audit_result: Dict, memories: List[Dict]):
        """审计后列出每条记忆的当前 AMC 分数。"""
        try:
            from core.database import ScopedSession, Memory, amc_score, get_global_round
            session = ScopedSession()
            global_r = get_global_round()
            
            lines = []
            result_map = {r["memory_id"]: r for r in audit_result.get("results", [])}
            
            for m in memories:
                mid = m.get("id", -1)
                if mid <= 0:
                    continue
                row = session.query(Memory).filter(Memory.id == mid).first()
                if not row:
                    continue
                s = row.success_count or 0
                u = row.usage_count or 0
                r_last = row.last_used_round or 0
                delta_r = global_r - r_last
                score = amc_score(s, u, delta_r)
                
                audit_info = result_map.get(mid, {})
                adopted = "✅功臣" if audit_info.get("adopted") else "🚶陪跑"
                conf = audit_info.get("confidence", 0)
                
                lines.append(
                    f"    [id={mid:>3d}] AMC={score:.4f} | S={s} U={u} ΔR={delta_r} | {adopted} (conf={conf:.2f}) | {(row.content or '')[:60]}"
                )
            
            ScopedSession.remove()
            
            if lines:
                logger.info("📊 记忆 AMC 分数明细:\n" + "\n".join(lines))
        except Exception as e:
            logger.warning(f"⚠️ AMC 分数日志失败: {e}")
    
    def _parse_response(self, raw: str, memories: List[Dict]) -> Dict:
        """解析 LLM 输出的 JSON，做容错处理。"""
        try:
            # 清洗 Markdown 代码块
            cleaned = raw.strip()
            if "```json" in cleaned:
                cleaned = cleaned.split("```json")[1].split("```")[0].strip()
            elif "```" in cleaned:
                cleaned = cleaned.split("```")[1].split("```")[0].strip()
            
            data = json.loads(cleaned)
            results = data.get("results", [])
            
            # 验证并标准化
            validated = []
            for item in results:
                memory_id = item.get("memory_id", -1)
                adopted = item.get("adopted", False)
                confidence = float(item.get("confidence", 0.5))
                evidence = item.get("evidence", "")
                
                # 仅 confidence >= 0.7 的 adopted 才算真正采用
                effective_adopted = adopted and confidence >= 0.7
                
                validated.append({
                    "memory_id": memory_id,
                    "adopted": effective_adopted,
                    "confidence": confidence,
                    "evidence": evidence[:200],
                })
            
            return {"results": validated}
            
        except (json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(f"⚠️ Auditor JSON 解析失败 ({e})，降级为保守策略")
            return {"results": [
                {"memory_id": m["id"], "adopted": True, "confidence": 0.5, "evidence": "JSON解析失败，保守通过"}
                for m in memories
            ]}
