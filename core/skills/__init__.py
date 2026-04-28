"""
core.skills — ASTrea Skill 模块

分层架构：
- base.py:          BaseSkill 抽象基类
- file_reader.py:   文件读取（可参数化 base_dir，未来 PM / QA 共用）
- port_checker.py:  端口检测（无状态，可公共化）
- sandbox_terminal.py: 沙盒终端（QA 特化，含进程生命周期管理）
- sandbox_http.py:  沙盒 HTTP 请求（QA 特化，锁定 localhost）
"""
