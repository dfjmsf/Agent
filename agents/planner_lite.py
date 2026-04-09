"""
PlannerLite Agent — 轻量级规划组

Phase 2.5.2 核心组件：
- 从 PM 标准化后的 structured_req 生成人类可读的 plan.md
- 使用 flash 级 LLM（~300 tokens），成本极低
- plan.md 三位一体：用户预览 + PM 口语化归纳 + Manager 执行合同

输出内容：技术栈 + 核心功能（不含文件树，文件拆分是 Manager 的工作）
"""
import os
import json
import logging
from core.llm_client import default_llm
from core.prompt import Prompts

logger = logging.getLogger("PlannerLite")


class PlannerLiteAgent:
    """轻量级规划组 Agent — 生成 plan.md（技术栈 + 功能清单）"""

    def __init__(self):
        self.model = os.getenv("MODEL_PLANNER_LITE", "deepseek-chat")

    def generate_plan(self, structured_req: dict, project_dir: str = None) -> str:
        """
        从标准化需求生成 plan.md（Markdown 文档）。

        Args:
            structured_req: PM 标准化后的需求 JSON
            project_dir: 项目目录路径，传入时自动保存 plan.md 到 .astrea/

        Returns:
            Markdown 格式的技术方案文本
        """
        logger.info(f"规划组正在生成 plan.md (summary: {structured_req.get('summary', '未知')})")

        req_text = json.dumps(structured_req, ensure_ascii=False, indent=2)

        try:
            response = default_llm.chat_completion(
                model=self.model,
                messages=[
                    {"role": "system", "content": Prompts.PLANNER_LITE_SYSTEM},
                    {"role": "user", "content": f"请根据以下结构化需求生成技术方案文档：\n\n{req_text}"}
                ],
                temperature=0.3,
            )
            plan_md = response.content.strip()

            # 清理可能的 Markdown 代码块包裹
            if plan_md.startswith("```markdown"):
                plan_md = plan_md[len("```markdown"):].strip()
            if plan_md.startswith("```"):
                plan_md = plan_md[3:].strip()
            if plan_md.endswith("```"):
                plan_md = plan_md[:-3].strip()

            logger.info(f"plan.md 生成完毕 ({len(plan_md)} 字符)")

        except Exception as e:
            logger.error(f"规划组生成失败: {e}")
            plan_md = self._fallback_plan(structured_req)

        # 持久化到磁盘
        if project_dir:
            self._save_to_disk(plan_md, project_dir)

        return plan_md

    def _save_to_disk(self, plan_md: str, project_dir: str):
        """将 plan.md 保存到 {project_dir}/.astrea/plan.md"""
        try:
            astrea_dir = os.path.join(project_dir, ".astrea")
            os.makedirs(astrea_dir, exist_ok=True)
            plan_path = os.path.join(astrea_dir, "plan.md")
            with open(plan_path, "w", encoding="utf-8") as f:
                f.write(plan_md)
            logger.info(f"plan.md 已保存: {plan_path}")
        except Exception as e:
            logger.warning(f"plan.md 保存失败: {e}")

    @staticmethod
    def _fallback_plan(req: dict) -> str:
        """降级方案：当 LLM 调用失败时，用模板生成基础 plan"""
        summary = req.get("summary", "未命名项目")
        features = req.get("core_features", [])
        tech = req.get("tech_preferences", {})
        defaults = req.get("defaults_applied", [])

        lines = [f"# {summary}", "", "## 技术栈"]
        backend = tech.get('backend', 'Flask')
        database = tech.get('database', 'SQLite')
        frontend = tech.get('frontend', 'Jinja2 SSR')

        # 标注默认值
        default_fields = {d['field'] for d in defaults} if defaults else set()
        lines.append(f"- **后端**：{backend}{'（默认）' if '后端' in default_fields else ''}")
        lines.append(f"- **数据库**：{database}{'（默认）' if '数据库' in default_fields else ''}")
        lines.append(f"- **前端**：{frontend}{'（默认）' if '前端' in default_fields else ''}")

        lines.extend(["", "## 核心功能"])
        for i, f in enumerate(features, 1):
            lines.append(f"{i}. **{f}**")

        return "\n".join(lines)
