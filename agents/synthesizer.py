"""
Synthesizer Agent — 知识提炼者
在代码工作流成功或熔断后，对本次工作流进行总结，
生成 Contrastive Pair（对比对）或 Anti-pattern（反模式），存入 PostgreSQL。
"""
import os
import json
import logging
import re
from typing import Dict, Any, Optional

from core.llm_client import default_llm
from core.prompt import Prompts
from core.database import memorize, append_event

logger = logging.getLogger("SynthesizerAgent")


class SynthesizerAgent:
    """
    知识提炼者 Agent
    
    职责：
    1. 成功时：从三里程碑（初始错误 → 报错路径 → 通关代码）提炼 Contrastive Pair
    2. 熔断时：从失败记录提炼 Anti-pattern
    3. LLM Routing：自主判定 scope (global/project)，后端按标签写入不同分区
    """

    def __init__(self, project_id: str = "default_project"):
        self.model = os.getenv("MODEL_SYNTHESIZER", "qwen3-max")
        self.project_id = project_id

    def _parse_json_response(self, raw_text: str) -> Optional[Dict[str, Any]]:
        """从 LLM 的原始输出中解析 JSON，带 fallback 容错。"""
        # 尝试直接解析
        try:
            return json.loads(raw_text.strip())
        except json.JSONDecodeError:
            pass
        
        # 尝试剥离 markdown 代码块
        pattern = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)
        match = pattern.search(raw_text)
        if match:
            try:
                return json.loads(match.group(1).strip())
            except json.JSONDecodeError:
                pass
        
        # 尝试找到第一个 {...}
        brace_match = re.search(r'\{[^{}]*\}', raw_text, re.DOTALL)
        if brace_match:
            try:
                return json.loads(brace_match.group(0))
            except json.JSONDecodeError:
                pass
        
        logger.warning(f"⚠️ Synthesizer JSON 解析失败，原始输出: {raw_text[:200]}")
        return None

    def _write_experience(self, parsed: Dict[str, Any]):
        """
        将提炼的经验按 scope 写入不同存储：
        - scope=global → memories 表（长期记忆）
        - scope=project → session_events 表（短期记忆，与 project_id 共存亡）
        """
        scope = parsed.get("scope", "project")
        # 兼容新旧格式：优先取 content，fallback 到旧 experience
        content = parsed.get("content") or parsed.get("experience", "")
        tech_stacks = parsed.get("tech_stacks", [])
        exp_type = parsed.get("exp_type", "general")
        scenario = parsed.get("scenario", "")

        
        if not content or content == "一次通过，无踩坑经验":
            logger.info(f"📝 Synthesizer: 一次通过无经验可提炼，跳过写入")
            return
        
        if scope not in ("global", "project"):
            logger.warning(f"⚠️ Synthesizer scope 非法: '{scope}'，降级为 'project'")
            scope = "project"
        
        meta = {
            "source": "synthesizer",
            "scope_reason": "llm_routing",
            "exp_type": exp_type,
        }
        
        if scope == "global":
            # 全局经验 → 长期记忆（memories 表），跨项目永久保留
            memorize(
                text=content,
                scope="global",
                project_id=self.project_id,
                metadata=meta,
                tech_stacks=tech_stacks,
                exp_type=exp_type,
                scenario=scenario,
            )
            logger.info(f"📝 经验写入 [长期记忆/global]: '{content[:50]}...' stacks={tech_stacks}")
        else:
            # 项目经验 → 短期记忆（session_events 表），带 embedding 支持轻量 RAG
            from core.database import get_embedding
            exp_embedding = get_embedding(content)
            append_event(
                "synthesizer", "experience_project",
                content, project_id=self.project_id,
                metadata=meta,
                embedding=exp_embedding,
            )
            logger.info(f"📝 经验写入 [短期记忆/project +vec]: '{content[:50]}...' stacks={tech_stacks}")

    def synthesize_success(
        self,
        milestones: Dict[str, str],
        user_req: str,
        plan: dict = None
    ):
        """
        成功时提炼 Contrastive Pair（对比对）。
        
        Args:
            milestones: {"a": 初始代码, "b": 报错摘要流, "c": 通关代码}
            user_req: 用户原始需求
            plan: Manager 的任务规划
        """
        logger.info("🧪 Synthesizer 正在提炼成功经验 (Contrastive Pair)...")
        
        milestone_a = milestones.get("a", "（一次通过，无初始错误）")
        milestone_b = milestones.get("b", "（一次通过，无报错记录）")
        milestone_c = milestones.get("c", "（缺失）")
        
        system_content = Prompts.SYNTHESIZER_SUCCESS_SYSTEM.format(
            milestone_a=milestone_a[:2000],
            milestone_b=milestone_b[:1000],
            milestone_c=milestone_c[:2000],
            user_req=user_req[:500]
        )
        
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": "请根据以上三个里程碑，提炼一条技术经验。输出纯净 JSON。"}
        ]
        
        try:
            resp = default_llm.chat_completion(messages, model=self.model, temperature=0.3)
            parsed = self._parse_json_response(resp.content)
            
            if parsed:
                self._write_experience(parsed)
                logger.info("✨ Synthesizer 成功经验提炼完毕！")
            else:
                # JSON 解析失败时降级：以 project scope 存入短期记忆
                logger.warning("⚠️ Synthesizer JSON 解析失败，降级存储到短期记忆")
                append_event(
                    "synthesizer", "experience_project",
                    resp.content.strip()[:500],
                    project_id=self.project_id,
                    metadata={"source": "synthesizer_fallback"}
                )
        except Exception as e:
            logger.error(f"❌ Synthesizer 成功提炼异常: {e}")

    def synthesize_failure(
        self,
        milestones: Dict[str, str],
        user_req: str,
        plan: dict = None
    ):
        """
        熔断时提炼 Anti-pattern（反模式）。
        
        Args:
            milestones: {"a": 初始代码, "b": 连续失败报错摘要}
            user_req: 用户原始需求
            plan: Manager 的任务规划
        """
        logger.info("🧪 Synthesizer 正在提炼失败教训 (Anti-pattern)...")
        
        milestone_a = milestones.get("a", "（缺失）")
        milestone_b = milestones.get("b", "（缺失）")
        
        system_content = Prompts.SYNTHESIZER_FAILURE_SYSTEM.format(
            milestone_a=milestone_a[:2000],
            milestone_b=milestone_b[:1500],
            user_req=user_req[:500]
        )
        
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": "请分析这次连续失败的根因，提炼一条反模式警告。输出纯净 JSON。"}
        ]
        
        try:
            resp = default_llm.chat_completion(messages, model=self.model, temperature=0.3)
            parsed = self._parse_json_response(resp.content)
            
            if parsed:
                self._write_experience(parsed)
                logger.info("✨ Synthesizer 失败教训提炼完毕！")
            else:
                logger.warning("⚠️ Synthesizer JSON 解析失败，降级存储到短期记忆")
                append_event(
                    "synthesizer", "experience_project",
                    resp.content.strip()[:500],
                    project_id=self.project_id,
                    metadata={"source": "synthesizer_fallback"}
                )
        except Exception as e:
            logger.error(f"❌ Synthesizer 失败提炼异常: {e}")
