"""
engine_refactor_smoke.py — 冒烟测试：验证 Facade 层接口完整性
"""
import sys

def main():
    # 1. 导入
    from core.engine import AstreaEngine
    print("[PASS] Import AstreaEngine from core.engine")

    # 2. 类方法
    assert hasattr(AstreaEngine, "resume"), "Missing classmethod: resume"
    print("[PASS] AstreaEngine.resume exists")

    # 3. 实例方法
    required_methods = ["run", "abort_and_rollback", "_phase_execution", "_get_manager", "_get_coder", "_get_reviewer"]
    for m in required_methods:
        assert hasattr(AstreaEngine, m), f"Missing method: {m}"
    print(f"[PASS] All {len(required_methods)} instance methods exist")

    # 4. 构造
    try:
        engine = AstreaEngine("smoke_test_project")
        print("[PASS] Constructor works")
    except Exception as e:
        print(f"[FAIL] Constructor error: {e}")
        sys.exit(1)

    # 5. 属性
    assert hasattr(engine, "blackboard"), "Missing: blackboard"
    assert hasattr(engine, "patcher"), "Missing: patcher"
    assert hasattr(engine, "vfs"), "Missing: vfs"
    assert hasattr(engine, "_shutdown"), "Missing: _shutdown"
    assert hasattr(engine, "_abort_requested"), "Missing: _abort_requested"
    assert hasattr(engine, "_pre_execution_git_head"), "Missing: _pre_execution_git_head"
    assert hasattr(engine, "_pending_project_rename"), "Missing: _pending_project_rename"
    assert hasattr(engine, "_manager"), "Missing: _manager"
    assert hasattr(engine, "_coder"), "Missing: _coder"
    assert hasattr(engine, "_reviewer"), "Missing: _reviewer"
    print("[PASS] All instance attributes initialized")

    # 6. project_id 属性
    assert engine.project_id == "smoke_test_project", f"project_id mismatch: {engine.project_id}"
    print("[PASS] project_id property works")

    # 7. 子模块导入
    from core.engine.modes.create import run_create_mode
    from core.engine.modes.patch import run_patch_mode
    from core.engine.modes.continue_mode import run_continue_mode
    from core.engine.modes.extend import run_extend_mode
    from core.engine.modes.rollback import run_rollback_mode
    print("[PASS] All mode modules importable")

    from core.engine.pipeline import phase_planning, phase_execution, phase_settlement
    print("[PASS] Pipeline module importable")

    from core.engine.helpers import resolve_output_dir, persist_blackboard_artifacts, infer_tech_stack
    print("[PASS] Helpers module importable")

    from core.engine.lifecycle import init_engine, resume_engine, abort_and_rollback
    print("[PASS] Lifecycle module importable")

    print("\n=== ALL SMOKE TESTS PASSED ===")

if __name__ == "__main__":
    main()
