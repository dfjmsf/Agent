"""验证 prompt.py 改造结果"""
import sys
sys.path.insert(0, '.')

# 只导入 Prompts 类（不触发完整的数据库 import 链）
from core.prompt import Prompts

print("=== Import 测试 ===")
print("✅ Prompts 导入成功")

# 测试 CODER_BACKEND_SYSTEM.format()
backend_result = Prompts.CODER_BACKEND_SYSTEM.format(
    target_file="src/main.py",
    description="test",
    memory_hint="none",
    project_spec="none",
    vfs_context="none",
    playbook="## FastAPI 规则\nPydantic BaseModel 铁律"
)
assert "{playbook}" not in backend_result
assert "FastAPI 规则" in backend_result
print("✅ CODER_BACKEND_SYSTEM.format() 正常")

# 测试 CODER_FRONTEND_SYSTEM.format()
frontend_result = Prompts.CODER_FRONTEND_SYSTEM.format(
    target_file="frontend/app.js",
    description="test",
    memory_hint="none",
    project_spec="none",
    vfs_context="none",
    playbook="## Vue3 CDN 规则\nVue.createApp"
)
assert "{playbook}" not in frontend_result
assert "Vue3 CDN 规则" in frontend_result
print("✅ CODER_FRONTEND_SYSTEM.format() 正常")

# 测试 MANAGER_SYSTEM.format()
manager_result = Prompts.MANAGER_SYSTEM.format(
    manager_playbook="## Web 项目通用规则\n前端必须拆分"
)
assert "{manager_playbook}" not in manager_result
assert "Web 项目通用规则" in manager_result
print("✅ MANAGER_SYSTEM.format() 正常")

# 验证通用规则还在
assert "Windows" in Prompts.CODER_BACKEND_SYSTEM, "通用规则缺失: Windows"
assert "SQLite" in Prompts.CODER_BACKEND_SYSTEM, "通用规则缺失: SQLite"
print("✅ 通用规则保留完好")

# 验证硬编码规则已删除
assert "Pydantic BaseModel" not in Prompts.CODER_BACKEND_SYSTEM, "FastAPI 硬编码未删除"
assert "getElementById" not in Prompts.CODER_FRONTEND_SYSTEM, "getElementById 硬编码未删除"
assert "DOMContentLoaded" not in Prompts.CODER_FRONTEND_SYSTEM, "DOMContentLoaded 硬编码未删除"
print("✅ 硬编码规则已成功删除")

# 验证 PlaybookLoader
from core.playbook_loader import PlaybookLoader
loader = PlaybookLoader()
pb = loader.load_for_coder(["FastAPI", "Vue 3"], "src/main.py")
assert pb and "Pydantic" in pb, "PlaybookLoader 后端加载失败"
print("✅ PlaybookLoader: 后端入口文件加载正常")

pb2 = loader.load_for_coder(["FastAPI", "Vue 3"], "frontend/app.js")
assert pb2 and "Vue" in pb2, "PlaybookLoader 前端加载失败"
print("✅ PlaybookLoader: 前端文件加载正常")

mpb = loader.load_for_manager(["FastAPI", "Vue 3"])
assert mpb and "Vue" in mpb, "Manager playbook 加载失败"
print("✅ PlaybookLoader: Manager playbook 加载正常")

print("\n🎉 所有验证通过！Playbook 方案 D 改造完成！")

if __name__ == "__main__":
    pass
