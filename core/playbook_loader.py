"""
PlaybookLoader — Playbook 分层加载器

职责：
1. 根据 tech_stack 和当前 target_file 智能加载最小 playbook
2. 分两层：Manager playbook + Coder playbook
3. 入口文件自动追加跨栈补丁（static_mount）
4. 模糊匹配 LLM 生成的 tech_stack 值
5. 匹配失败时 fallback 到默认 playbook
"""

import os
import re
import logging
from typing import List, Optional

logger = logging.getLogger("PlaybookLoader")

# Playbook 根目录（相对于项目根目录）
_BASE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "playbooks")


class PlaybookLoader:
    """
    Playbook 分层加载器。
    
    加载策略（Strategy D）：
    - 按文件后缀路由，只加载一份对应 playbook
    - 入口文件额外追加跨栈补丁
    - 匹配失败 fallback 到默认 playbook
    """

    # ============================
    # 别名映射（模糊匹配）
    # ============================

    BACKEND_MAP = {
        "fastapi": "fastapi.md",
        "fast api": "fastapi.md",
        "fast-api": "fastapi.md",
        "flask": "flask.md",
        "django": "django.md",
        "django rest framework": "django.md",
        "drf": "django.md",
        "express": "express.md",
        "express.js": "express.md",
        "expressjs": "express.md",
        "node": "express.md",
        "node.js": "express.md",
        "nodejs": "express.md",
    }

    FRONTEND_MAP = {
        # Next.js（优先级最高，因为包含 react 关键词）
        "next": "nextjs.md",
        "next.js": "nextjs.md",
        "nextjs": "nextjs.md",
        # Vite 构建模式（优先级高于 CDN 模式）
        "vite": "vue3_vite.md",
        "vue3 vite": "vue3_vite.md",
        "vue 3 vite": "vue3_vite.md",
        "vue vite": "vue3_vite.md",
        "composition api": "vue3_vite.md",
        "composition": "vue3_vite.md",
        "react": "react_vite.md",
        "react vite": "react_vite.md",
        "react.js": "react_vite.md",
        "reactjs": "react_vite.md",
        # CDN 模式
        "vue3": "vue3_cdn.md",
        "vue 3": "vue3_cdn.md",
        "vue.js 3": "vue3_cdn.md",
        "vue.js": "vue3_cdn.md",
        "vue": "vue3_cdn.md",
        "vanilla": "vanilla_js.md",
        "vanilla js": "vanilla_js.md",
        "javascript": "vanilla_js.md",
        "html": "vanilla_js.md",
        "html5": "vanilla_js.md",
    }

    MANAGER_FRONTEND_MAP = {
        # Vite 构建模式
        "vite": "vue3_vite.md",
        "vue3 vite": "vue3_vite.md",
        "vue 3 vite": "vue3_vite.md",
        "vue vite": "vue3_vite.md",
        "composition api": "vue3_vite.md",  # Composition API → Vite 模式
        "composition": "vue3_vite.md",
        "react": "vue3_vite.md",        # React 复用 Vite 任务拆分规则
        "react vite": "vue3_vite.md",
        # CDN 模式
        "vue3": "vue3_cdn_frontend.md",
        "vue 3": "vue3_cdn_frontend.md",
        "vue.js 3": "vue3_cdn_frontend.md",
        "vue.js": "vue3_cdn_frontend.md",
        "vue": "vue3_cdn_frontend.md",
        "vanilla": "vanilla_frontend.md",
        "vanilla js": "vanilla_frontend.md",
        "javascript": "vanilla_frontend.md",
        "html": "vanilla_frontend.md",
        "html5": "vanilla_frontend.md",
    }

    # 默认 fallback（匹配不到时使用）
    DEFAULT_BACKEND = "fastapi.md"
    DEFAULT_FRONTEND = "vanilla_js.md"
    DEFAULT_MANAGER_FRONTEND = "vanilla_frontend.md"

    # 入口文件名集合（触发跨栈补丁）
    ENTRY_FILENAMES = {"main.py", "app.py", "server.py", "run.py",
                       "index.js", "server.js", "app.js"}

    # 文件后缀分类
    BACKEND_EXTS = {".py"}
    # Express.js 后端的 .js 会先匹配 FRONTEND_MAP，但如果 tech_stack 有 express/node，
    # _match_tech 会优先走 BACKEND_MAP（在 load_for_coder 中筛选）
    FRONTEND_EXTS = {".html", ".htm", ".css", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".json"}

    # Addon 补丁映射（仅当 tech_stack 包含关键词时激活，不会默认注入）
    # 格式：{"tech_stack关键词": ("_patches/文件名", "显示名", “限定后缀集”)}
    # 限定后缀集为 None 表示对所有文件类型生效
    ADDON_PATCHES = {
        "tailwind": ("tailwind_cdn.md", "Tailwind CSS CDN", FRONTEND_EXTS),
        "tailwindcss": ("tailwind_cdn.md", "Tailwind CSS CDN", FRONTEND_EXTS),
        "tailwind css": ("tailwind_cdn.md", "Tailwind CSS CDN", FRONTEND_EXTS),
        "composition api": ("composition_api.md", "Vue3 Composition API", FRONTEND_EXTS),
        "composition": ("composition_api.md", "Vue3 Composition API", FRONTEND_EXTS),
        "sqlalchemy": ("sqlalchemy_orm.md", "SQLAlchemy ORM", {".py"}),
        "sql alchemy": ("sqlalchemy_orm.md", "SQLAlchemy ORM", {".py"}),
        "orm": ("sqlalchemy_orm.md", "SQLAlchemy ORM", {".py"}),
    }

    # ============================
    # 公共 API
    # ============================

    # ============================
    # Playbook 分层预算帽 (Sprint 3-A)
    # ============================
    DEFAULT_BUDGET_CHARS = 4000  # 主 playbook 预算帽（不含 patches）

    def load_for_coder(self, tech_stack: List[str], target_file: str,
                       budget_chars: int = None,
                       architecture_contract: dict = None) -> str:
        """
        为 Coder 加载 playbook（P0/P1/P2 分层 + 预算帽策略）。

        Sprint 3-A 新机制：
        - 主 playbook 按 <!-- P0:START --> 等标记拆分为三层
        - P0（铁律）必须全部填入
        - P1（核心规则）按剩余预算填入
        - P2（参考示例）在预算充裕时才填入（弹性设计：宁缺勿截断）
        - Patches 不受预算帽限制（体量小且属于 P1 级）

        Args:
            tech_stack: 项目技术栈列表
            target_file: 当前要编写的文件路径
            budget_chars: 主 playbook 预算帽（不含 patches），None 则使用默认值
        """
        if budget_chars is None:
            budget_chars = self.DEFAULT_BUDGET_CHARS

        ext = os.path.splitext(target_file)[1].lower()
        parts = []

        # 检测是否是 Node.js 后端项目（.js 文件应走后端路由）
        _all_tech = " ".join(t.lower() for t in tech_stack)
        is_node_backend = any(kw in _all_tech for kw in
                              ("express", "node", "koa", "nestjs", "nest"))

        # === 第一层：按文件后缀路由，加载主 playbook（P0/P1/P2 分层）===
        raw_pb = None
        if ext in self.BACKEND_EXTS:
            raw_pb = self._match_and_load(tech_stack, self.BACKEND_MAP,
                                          "coder", self.DEFAULT_BACKEND)
        elif ext in {".js", ".ts"} and is_node_backend:
            raw_pb = self._match_and_load(tech_stack, self.BACKEND_MAP,
                                          "coder", "express.md")
        elif ext in self.FRONTEND_EXTS:
            raw_pb = self._match_and_load(tech_stack, self.FRONTEND_MAP,
                                          "coder", self.DEFAULT_FRONTEND)

        if raw_pb:
            # 尝试分层提取
            trimmed_pb = self._apply_budget(raw_pb, budget_chars)
            parts.append(trimmed_pb)

        # === 第二层：入口文件追加跨栈补丁（不受预算帽限制）===
        basename = os.path.basename(target_file).lower()
        if basename in self.ENTRY_FILENAMES:
            patch = self._load_file("coder", "_patches", "static_mount.md")
            if patch:
                parts.append(f"\n\n【跨栈补丁：前端静态文件挂载】\n{patch}")
                logger.info(f"📎 入口文件 {basename} 追加挂载补丁")

        # === 第三层：Addon 补丁（不受预算帽限制）===
        loaded_addons = set()
        all_tech_lower = " ".join(t.lower() for t in tech_stack)
        is_vite_project = any(kw in all_tech_lower for kw in ["vite", "react", "composition"])

        # v4.4: ORM 模式感知 — architecture_contract.orm_mode 决定加载哪个 ORM 补丁
        _orm_mode = (architecture_contract or {}).get("orm_mode", "") if architecture_contract else ""
        if _orm_mode == "flask_sqlalchemy" and ext == ".py":
            # 强制注入 Flask-SQLAlchemy 补丁，排斥 sqlalchemy_orm.md
            addon = self._load_file("coder", "_patches", "flask_sqlalchemy.md")
            if addon:
                parts.append(f"\n\n【ORM 补丁：Flask-SQLAlchemy（强制）】\n{addon}")
                loaded_addons.add("flask_sqlalchemy.md")
                loaded_addons.add("sqlalchemy_orm.md")  # 排斥原生 SQLAlchemy 补丁
                logger.info(f"🧩 ORM 补丁: Flask-SQLAlchemy（orm_mode 感知）")

        for tech in tech_stack:
            tech_lower = tech.lower().strip()
            for keyword, (filename, display_name, allowed_exts) in self.ADDON_PATCHES.items():
                if keyword in tech_lower and filename not in loaded_addons:
                    if allowed_exts is not None and ext not in allowed_exts:
                        continue
                    actual_filename = filename
                    actual_display = display_name
                    if filename == "tailwind_cdn.md" and is_vite_project:
                        actual_filename = "tailwind_vite.md"
                        actual_display = "Tailwind CSS (Vite/PostCSS)"
                    addon = self._load_file("coder", "_patches", actual_filename)
                    if addon:
                        parts.append(f"\n\n【Addon 补丁：{actual_display}】\n{addon}")
                        loaded_addons.add(filename)
                        logger.info(f"🧩 Addon 补丁: {actual_display}")

        # === 第四层：隐式环境补丁（不受预算帽限制）===
        # 4.1 SSR 模板补丁
        if any(kw in all_tech_lower for kw in ["flask", "django"]):
            if "ssr_template.md" not in loaded_addons and ext in {".py", ".html"}:
                addon = self._load_file("coder", "_patches", "ssr_template.md")
                if addon:
                    parts.append(f"\n\n【环境补丁：SSR 及跨文件契约规范】\n{addon}")
                    loaded_addons.add("ssr_template.md")
                    logger.info(f"🧩 环境补丁: SSR 及跨文件契约规范")

        # 4.2 SQLite 原生补丁
        if "sqlite" in all_tech_lower and not any(kw in all_tech_lower for kw in ["sqlalchemy", "orm", "sequelize", "prisma"]):
            if "sqlite_native.md" not in loaded_addons and ext == ".py":
                addon = self._load_file("coder", "_patches", "sqlite_native.md")
                if addon:
                    parts.append(f"\n\n【环境补丁：SQLite 原生安全规范】\n{addon}")
                    loaded_addons.add("sqlite_native.md")
                    logger.info(f"🧩 环境补丁: SQLite 原生安全规范")

        # 4.3 前端 UI 设计系统补丁（所有前端文件自动注入）
        if ext in self.FRONTEND_EXTS and "ui_design_system.md" not in loaded_addons:
            addon = self._load_file("coder", "_patches", "ui_design_system.md")
            if addon:
                # 对设计系统也做分层裁剪（受 budget_chars 剩余空间约束）
                trimmed = self._apply_budget(addon, budget_chars)
                parts.append(f"\n\n【环境补丁：前端 UI 设计系统】\n{trimmed}")
                loaded_addons.add("ui_design_system.md")
                logger.info(f"🧩 环境补丁: 前端 UI 设计系统")

        content = "\n\n".join(parts)
        if content:
            content = (
                "⚠️ 以下是当前技术栈的编码规范（P1 级别），你必须严格遵守！\n"
                "仅当与项目规划书（Spec）直接矛盾时以 Spec 为准。\n\n"
                + content
            )
            logger.info(f"📖 Coder Playbook 加载完成 ({len(content)} chars) for {target_file}")
        else:
            logger.warning(f"⚠️ 未找到匹配的 Coder Playbook for {target_file}")

        return content

    def load_for_manager(self, tech_stack: List[str]) -> str:
        """
        为 Manager 加载前端拆分 playbook。

        Args:
            tech_stack: 项目技术栈列表

        Returns:
            playbook 内容字符串
        """
        # 检测是否是 Web 项目（含前端技术栈）
        has_frontend = self._has_frontend_tech(tech_stack)

        parts = []

        # 通用 Web 规则（始终注入）
        if has_frontend:
            parts.append(self._web_common_rules())

        # 前端拆分 playbook
        if has_frontend:
            pb = self._match_and_load(tech_stack, self.MANAGER_FRONTEND_MAP,
                                      "manager", self.DEFAULT_MANAGER_FRONTEND)
            if pb:
                parts.append(pb)

        content = "\n\n".join(parts)
        if content:
            content = (
                "ℹ️ 以下是任务拆分规范（P1.5 级别），必须遵守。\n\n"
                + content
            )
            logger.info(f"📖 Manager Playbook 加载完成 ({len(content)} chars)")
        return content

    # ============================
    # 内部方法
    # ============================

    def _match_and_load(self, tech_stack: List[str], mapping: dict,
                        category: str, default: str) -> Optional[str]:
        """
        模糊匹配 tech_stack → playbook 文件名，加载内容。
        匹配失败时使用 default fallback。
        """
        filename = self._match_tech(tech_stack, mapping)
        if filename:
            content = self._load_file(category, filename)
            if content:
                return content
            logger.warning(f"⚠️ Playbook 文件不存在: {category}/{filename}")

        # Fallback
        content = self._load_file(category, default)
        if content:
            logger.info(f"📖 使用默认 Playbook: {category}/{default}")
            return content

        return None

    def _match_tech(self, tech_stack: List[str], mapping: dict) -> Optional[str]:
        """
        模糊匹配 tech_stack 列表中的每个元素，返回第一个匹配到的 playbook 文件名。
        匹配策略：
        1. 优先级扫描：先看是否有构建工具关键词（vite, react），这些优先于框架名
        2. 精确匹配：小写 + 去空格 → 在 mapping 中查找
        3. 包含匹配：模糊包含关系
        """
        # 优先级关键词：构建工具/API风格 > 框架名（防止 "Vue 3" 遮蔽 "Vite" 或 "Composition API"）
        PRIORITY_KEYWORDS = ["vite", "react", "composition"]

        # Pass 1: 优先级扫描（合并所有 tech_stack 文本查找构建工具）
        all_tech_lower = " ".join(t.lower() for t in tech_stack)
        for priority_key in PRIORITY_KEYWORDS:
            if priority_key in all_tech_lower and priority_key in mapping:
                logger.info(f"🎯 优先级匹配: '{priority_key}' → {mapping[priority_key]}")
                return mapping[priority_key]

        # Pass 2: 常规逐项匹配
        for tech in tech_stack:
            key = tech.lower().strip()
            # 精确匹配
            if key in mapping:
                return mapping[key]
            # 去掉版本号后缀尝试（如 "Vue 3.4" → "vue"）
            base = key.split()[0] if " " in key else key
            if base in mapping:
                return mapping[base]
            # 包含匹配（如 tech_stack 里写 "Vue.js 3 (CDN)"，mapping 有 "vue.js"）
            for map_key, map_val in mapping.items():
                if map_key in key or key in map_key:
                    return map_val

        return None

    def _has_frontend_tech(self, tech_stack: List[str]) -> bool:
        """判断 tech_stack 中是否包含前端技术"""
        frontend_keywords = {
            "html", "css", "javascript", "js", "vue", "react", "angular",
            "svelte", "frontend", "前端", "vanilla", "typescript", "ts"
        }
        for tech in tech_stack:
            for kw in frontend_keywords:
                if kw in tech.lower():
                    return True
        return False

    def _load_file(self, *path_parts: str) -> Optional[str]:
        """加载 playbook 文件内容"""
        filepath = os.path.join(_BASE_DIR, *path_parts)
        if os.path.isfile(filepath):
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    return f.read().strip()
            except Exception as e:
                logger.error(f"❌ 读取 Playbook 失败: {filepath}: {e}")
        return None

    def _extract_tier(self, content: str, tier: str) -> str:
        """
        提取指定层级的 playbook 内容。

        解析 <!-- P0:START --> ... <!-- P0:END --> 等标记。
        同一层级可以有多个区块，全部合并返回。

        Args:
            content: 完整 playbook 文本
            tier: 层级名，"P0" | "P1" | "P2"

        Returns:
            该层级的内容（去除首尾空白），无标记则返回空字符串
        """
        pattern = rf"<!--\s*{tier}:START\s*-->(.+?)<!--\s*{tier}:END\s*-->"
        matches = re.findall(pattern, content, re.DOTALL)
        if not matches:
            return ""
        return "\n\n".join(m.strip() for m in matches)

    def _apply_budget(self, raw_content: str, budget_chars: int) -> str:
        """
        对主 playbook 施加 P0/P1/P2 分层预算帽。

        策略：
        1. P0（铁律）：必须全部填入，不受预算限制
        2. P1（核心规则）：按剩余预算填入（整块填入，不截断）
        3. P2（参考示例）：预算充裕时才填入（弹性设计：宁缺勿截断）
        4. 无标记文件：全文视为 P0（向后兼容）

        Args:
            raw_content: 完整 playbook 原文
            budget_chars: 字符预算帽

        Returns:
            裁剪后的 playbook 文本
        """
        p0 = self._extract_tier(raw_content, "P0")
        p1 = self._extract_tier(raw_content, "P1")
        p2 = self._extract_tier(raw_content, "P2")

        # 无标记文件 → 降级：全文视为 P0（向后兼容，不裁剪）
        if not p0 and not p1 and not p2:
            logger.debug("📖 Playbook 无分层标记，全文视为 P0（向后兼容）")
            return raw_content

        # P0 必须全填
        result = p0

        # P1（核心规则）也必须全填 — 它是正确实现的基础
        if p1:
            result += "\n\n" + p1

        used = len(result)

        # P2 在预算充裕时才填入（整块，宁缺勿截断）
        if p2:
            if used + len(p2) <= budget_chars:
                result += "\n\n" + p2
                logger.debug(f"📖 P2 填入: {len(p2)} chars (累计 {len(result)})")
            else:
                logger.info(f"📖 P2 被预算帽裁剪: {len(p2)} chars 超出剩余 {budget_chars - used}")

        logger.info(f"📖 Playbook 分层裁剪: 原文 {len(raw_content)} → {len(result)} chars "
                     f"(P0={len(p0)}, P1={len(p1)}, P2={len(p2)}, 预算={budget_chars})")
        return result


    @staticmethod
    def _web_common_rules() -> str:
        """通用 Web 项目规则（不特定于任何前端框架）"""
        return """【Web 项目通用规则】
1. Web 项目必须同时规划前端和后端文件，禁止只规划后端！
2. 后端入口文件必须配置 CORS 中间件，否则前端无法请求 API。
3. 如果项目包含 frontend 目录，后端入口文件必须挂载前端静态文件到根路径 `/`。"""


if __name__ == "__main__":
    # 快速自测
    loader = PlaybookLoader()

    # Test 1: FastAPI + Vue3 入口文件
    pb = loader.load_for_coder(["FastAPI", "Vue 3", "SQLite"], "src/main.py")
    assert pb and "Pydantic" in pb, "入口文件应包含 FastAPI 规则"
    assert "跨栈补丁" in pb, "入口文件应包含挂载补丁"
    print("✅ Test 1: FastAPI 入口文件 — PASS")

    # Test 2: Vue3 前端文件
    pb2 = loader.load_for_coder(["FastAPI", "Vue 3"], "frontend/app.js")
    assert pb2 and "Vue" in pb2, "前端文件应包含 Vue 规则"
    print("✅ Test 2: Vue3 前端文件 — PASS")

    # Test 3: 后端非入口文件
    pb3 = loader.load_for_coder(["FastAPI", "Vue 3"], "src/routes.py")
    assert pb3 and "Pydantic" in pb3, "后端非入口应包含 FastAPI 规则"
    assert "跨栈补丁" not in pb3, "非入口文件不应有挂载补丁"
    print("✅ Test 3: 后端非入口文件 — PASS")

    # Test 4: Manager playbook
    mpb = loader.load_for_manager(["FastAPI", "Vue 3"])
    assert mpb and "Vue" in mpb, "Manager 应加载 Vue 前端拆分规则"
    print("✅ Test 4: Manager Vue3 Playbook — PASS")

    # Test 5: Fallback（未知技术栈）
    pb5 = loader.load_for_coder(["Tornado", "jQuery"], "frontend/app.js")
    assert pb5, "未知技术栈应 fallback 到默认 playbook"
    print("✅ Test 5: Fallback 默认 Playbook — PASS")

    # Test 6: 纯后端项目
    mpb2 = loader.load_for_manager(["Python 3", "SQLite"])
    assert mpb2 == "", "纯后端项目 Manager playbook 应为空"
    print("✅ Test 6: 纯后端项目 — PASS")

    # Test 7: Addon 补丁 — Tailwind
    pb7 = loader.load_for_coder(["FastAPI", "Vue 3", "Tailwind CSS"], "frontend/index.html")
    assert "Addon 补丁" in pb7 and "Tailwind" in pb7, "应包含 Tailwind 补丁"
    print("✅ Test 7: Tailwind Addon 补丁 — PASS")

    # Test 8: Addon 补丁 — Composition API
    pb8 = loader.load_for_coder(["FastAPI", "Vue 3 (Composition API)"], "frontend/app.js")
    assert "Addon 补丁" in pb8 and "Composition" in pb8, "应包含 Composition API 补丁"
    print("✅ Test 8: Composition API Addon 补丁 — PASS")

    # Test 9: 无 Addon — 普通 Vue3（不应激活 Tailwind/Composition）
    pb9 = loader.load_for_coder(["FastAPI", "Vue 3"], "frontend/app.js")
    assert "Addon 补丁" not in pb9, "普通 Vue3 不应激活任何 Addon"
    print("✅ Test 9: 普通 Vue3 无 Addon — PASS")

    # Test 10: Addon 不对后端生效
    pb10 = loader.load_for_coder(["FastAPI", "Vue 3", "Tailwind CSS"], "src/routes.py")
    assert "Tailwind" not in pb10 or "Addon" not in pb10, "Tailwind 不应对 .py 文件生效"
    print("✅ Test 10: Addon 后缀限制 — PASS")

    # Test 11: 同时激活 Tailwind + Composition
    pb11 = loader.load_for_coder(
        ["FastAPI", "Vue 3 (Composition API)", "Tailwind CSS"], "frontend/app.js"
    )
    assert "Tailwind" in pb11 and "Composition" in pb11, "应同时包含两个 Addon"
    print("✅ Test 11: 双 Addon 同时激活 — PASS")

    # Test 12: Vite + Vue3 前端路由
    pb12 = loader.load_for_coder(["FastAPI", "Vue 3", "Vite"], "src/App.vue")
    assert pb12 and "vite" in pb12.lower(), "Vite 项目前端应加载 vue3_vite playbook"
    assert "package.json" in pb12, "Vite playbook 应包含 package.json 规则"
    print("✅ Test 12: Vite + Vue3 前端路由 — PASS")

    # Test 13: React 前端路由
    pb13 = loader.load_for_coder(["FastAPI", "React"], "src/App.jsx")
    assert pb13 and "react" in pb13.lower(), "React 项目应加载 react_vite playbook"
    print("✅ Test 13: React 前端路由 — PASS")

    # Test 14: Vite 优先级（Vue 3 + Vite → 应走 Vite 而非 CDN）
    pb14 = loader.load_for_coder(["FastAPI", "Vue 3", "Vite"], "src/main.js")
    assert "vite" in pb14.lower() or "SFC" in pb14, "Vite 应优先于 CDN"
    print("✅ Test 14: Vite 优先级高于 CDN — PASS")

    # Test 15: Manager Vite 路由
    mpb3 = loader.load_for_manager(["FastAPI", "Vue 3", "Vite"])
    assert mpb3 and "Vite" in mpb3, "Manager 应加载 Vite 拆分规则"
    print("✅ Test 15: Manager Vite Playbook — PASS")

    # Test 16: 纯 Vue3 CDN 不受 Vite 影响
    pb16 = loader.load_for_coder(["FastAPI", "Vue 3"], "frontend/app.js")
    assert "CDN" in pb16 or "unpkg" in pb16 or "vue.global" in pb16, "纯 Vue3 应走 CDN 模式"
    print("✅ Test 16: 纯 Vue3 仍走 CDN — PASS")

    # Test 17: Vite + Tailwind → 应加载 tailwind_vite.md（PostCSS 模式）
    pb17 = loader.load_for_coder(
        ["Vue 3", "Vite", "Tailwind CSS"], "src/App.vue"
    )
    assert "PostCSS" in pb17 or "export default" in pb17, "Vite+Tailwind 应用 PostCSS 模式"
    assert "module.exports" not in pb17 or "禁止" in pb17, "Vite+Tailwind 不应出现 CJS module.exports"
    print("✅ Test 17: Vite + Tailwind → PostCSS 模式 — PASS")

    # Test 18: CDN + Tailwind → 应加载 tailwind_cdn.md
    pb18 = loader.load_for_coder(
        ["FastAPI", "Vue 3", "Tailwind CSS"], "frontend/index.html"
    )
    assert "cdn.tailwindcss.com" in pb18, "CDN+Tailwind 应包含 CDN 链接"
    print("✅ Test 18: CDN + Tailwind → CDN 模式 — PASS")

    # Test 19: React + Tailwind → 也应走 PostCSS 模式
    pb19 = loader.load_for_coder(
        ["React", "Tailwind CSS"], "src/App.jsx"
    )
    assert "PostCSS" in pb19 or "export default" in pb19, "React+Tailwind 应用 PostCSS 模式"
    print("✅ Test 19: React + Tailwind → PostCSS 模式 — PASS")

    print("\n🎉 所有测试通过！")
