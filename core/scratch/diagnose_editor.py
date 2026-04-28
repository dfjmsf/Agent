"""
诊断 Coder Editor 差量编辑匹配失败的根因
分析 search/replace 匹配失败的常见模式
"""
import json

# ============================================
# 实际代码（模拟从真理区读到的文件）
# ============================================
sample_code = """from flask import Flask, render_template, request, redirect, url_for
import sqlite3
import os

app = Flask(__name__)
DB_PATH = os.path.join(os.path.dirname(__file__), 'data.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@app.route('/')
def index():
    db = get_db()
    items = db.execute('SELECT * FROM items').fetchall()
    db.close()
    return render_template('index.html', items=items)
"""

def apply_edits(code, edits):
    """复刻 coder.py 的 _apply_edits_in_memory 逻辑"""
    lines = code.split("\n")
    success = 0
    fail = 0
    
    for edit in edits:
        search = edit.get("search", "")
        replace = edit.get("replace", "")
        if not search:
            continue
        
        search_lines = search.split("\n")
        found = False
        
        for i in range(len(lines) - len(search_lines) + 1):
            match = True
            for j, sl in enumerate(search_lines):
                if lines[i + j].rstrip() != sl.rstrip():
                    match = False
                    break
            if match:
                replace_lines = replace.split("\n")
                lines[i:i + len(search_lines)] = replace_lines
                found = True
                success += 1
                break
        
        if not found:
            fail += 1
            print(f"  ❌ 匹配失败!")
            print(f"     search 有 {len(search_lines)} 行")
            print(f"     search 首行: {repr(search_lines[0])}")
            # 尝试在代码中逐行找最相似的
            for idx, cl in enumerate(lines):
                if search_lines[0].strip() in cl:
                    print(f"     → 在代码 L{idx} 找到近似: {repr(cl)}")
                    if len(search_lines) > 1:
                        print(f"       search[1]: {repr(search_lines[1])}")
                        if idx + 1 < len(lines):
                            print(f"       code[{idx+1}]:  {repr(lines[idx+1])}")
                    break
    
    return "\n".join(lines), success, fail


print("=" * 60)
print("测试 1: 正常匹配（应该成功）")
print("=" * 60)
edits_ok = [{"search": "def get_db():\n    conn = sqlite3.connect(DB_PATH)\n    conn.row_factory = sqlite3.Row\n    return conn", 
             "replace": "def get_db():\n    conn = sqlite3.connect(DB_PATH)\n    conn.row_factory = sqlite3.Row\n    conn.execute('PRAGMA journal_mode=WAL')\n    return conn"}]
_, s, f = apply_edits(sample_code, edits_ok)
print(f"  结果: {s} 成功, {f} 失败")


print("\n" + "=" * 60)
print("测试 2: LLM 重新格式化缩进（tab vs space）")
print("=" * 60)
edits_tab = [{"search": "def get_db():\n\tconn = sqlite3.connect(DB_PATH)\n\tconn.row_factory = sqlite3.Row\n\treturn conn",
              "replace": "def get_db():\n\tconn = sqlite3.connect(DB_PATH)\n\treturn conn"}]
_, s, f = apply_edits(sample_code, edits_tab)
print(f"  结果: {s} 成功, {f} 失败")


print("\n" + "=" * 60)
print("测试 3: LLM 少了一行或多了一行")
print("=" * 60)
edits_partial = [{"search": "def get_db():\n    conn = sqlite3.connect(DB_PATH)",
                   "replace": "def get_db():\n    conn = sqlite3.connect(DB_PATH, check_same_thread=False)"}]
_, s, f = apply_edits(sample_code, edits_partial)
print(f"  结果: {s} 成功, {f} 失败")


print("\n" + "=" * 60) 
print("测试 4: LLM 凭记忆重写（微调了引号或空格）")
print("=" * 60)
edits_memo = [{"search": "def get_db():\n    conn = sqlite3.connect(DB_PATH)\n    conn.row_factory  = sqlite3.Row\n    return conn",  # 多了一个空格
               "replace": "def get_db():\n    return None"}]
_, s, f = apply_edits(sample_code, edits_memo)
print(f"  结果: {s} 成功, {f} 失败")


print("\n" + "=" * 60)
print("测试 5: JSON 解析后 \\n 是字面字符还是换行？")
print("=" * 60)
# 这是 LLM 实际输出的 JSON，tool_call.function.arguments 是一个 JSON string
# 问题核心：当 LLM 在 JSON 中输出 "search": "line1\nline2" 
# JSON 解析后 \n 是真换行，这是正确的
# 但如果 LLM 输出 "search": "line1\\nline2" (双反斜杠)
# JSON 解析后变成 "line1\nline2" 也是真换行 (因为 \\n 在 JSON 中就是 \n)
# 真正的问题是 LLM 输出 "search": "line1\nline2" 其中 \n 是字面换行符
# 这是 OpenAI API 的标准行为

# 模拟 LLM 的 tool_call JSON 输出
tool_json = '{"edits": [{"search": "def get_db():\\n    conn = sqlite3.connect(DB_PATH)\\n    conn.row_factory = sqlite3.Row\\n    return conn", "replace": "def get_db():\\n    return None"}]}'
parsed = json.loads(tool_json)
edits_from_json = parsed["edits"]
print(f"  JSON 解析后 search 行数: {len(edits_from_json[0]['search'].splitlines())}")
print(f"  search 首行: {repr(edits_from_json[0]['search'].splitlines()[0])}")
_, s, f = apply_edits(sample_code, edits_from_json)
print(f"  结果: {s} 成功, {f} 失败")


print("\n" + "=" * 60)
print("测试 6: CRLF 文件 + LF search")  
print("=" * 60)
# Windows 读取的文件如果是 binary mode 或 encoding 特殊，可能保留 \r\n
code_crlf = sample_code.replace("\n", "\r\n")
edits_lf = [{"search": "def get_db():\n    conn = sqlite3.connect(DB_PATH)\n    conn.row_factory = sqlite3.Row\n    return conn",
             "replace": "def get_db():\n    return None"}]
_, s, f = apply_edits(code_crlf, edits_lf)
print(f"  结果: {s} 成功, {f} 失败")
# rstrip() 会去掉 \r，所以 CRLF 不应该是问题

print()
print("=" * 60)
print("测试 7: Coder 上一轮输出的代码（非真理区）作为 existing_code")
print("=" * 60)
# 这是真正的关键场景：
# 首次生成 → Coder 输出代码 → 存入 VFS draft → Reviewer 测试失败
# → feedback 回来 → existing_code 从哪里取？
# 
# Engine 路径: existing_code = vfs.read_truth(target_file) 或 task.code_draft
# Manager 路径: execute_tdd_loop → generate_code(feedback, task_meta)
#   但 task_meta 里没有 existing_code！
# → Coder 的 generate_code 中: existing_code = task_meta.get("existing_code", "")
# → 空！→ feedback 路径 → 但 edit_instruction = feedback
# → 进入 _fix_with_editor，existing_code="" 
# → CODER_FIX_SYSTEM 中 current_code="" → LLM 看到空代码

print("""
关键发现: Manager 路径下的 execute_tdd_loop:
- task_meta 只有 project_spec + dependencies + all_tasks
- 没有 existing_code!
- 而 Coder.generate_code() 第 453 行:
    existing_code = (task_meta or {}).get("existing_code", "")
  → 修复模式时 existing_code = "" (空!)
- 但 _fix_with_editor 仍然被调用（因为 feedback ≠ None）
- prompt 里 current_code="" → LLM 看到空代码 → 它输出的 search 是它凭记忆编的!
""")

print("=" * 60)
print("根因确认!")
print("=" * 60)
print("""
Manager execute_tdd_loop 第 697 行调用 coder.generate_code 时:
1. 第一次 (retry=0): feedback=None → 首次生成 → OK
2. Reviewer 退回 → feedback="...", 但 task_meta["existing_code"] 仍然空!
3. Coder 进入修复模式 → existing_code="" → prompt 里代码为空
4. LLM 凭记忆编 search → 自然不匹配 → fallback!

修复方案: 在 execute_tdd_loop 重试时, 把最新的 code_draft 
注入 task_meta["existing_code"]
""")
