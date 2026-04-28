"""Sprint 3-A 验证脚本：测试所有 playbook 的分层裁剪效果"""
from core.playbook_loader import PlaybookLoader

loader = PlaybookLoader()

tests = [
    (['Flask', 'SQLite'], 'src/routes.py', 'Flask BE'),
    (['Flask'], 'templates/index.html', 'Flask FE'),
    (['FastAPI', 'SQLite'], 'src/routes.py', 'FastAPI BE'),
    (['Django'], 'app/views.py', 'Django BE'),
    (['Express', 'SQLite'], 'src/routes/users.js', 'Express BE'),
    (['Vue 3 CDN'], 'app.js', 'Vue3CDN FE'),
    (['Vue 3 Vite'], 'src/App.vue', 'Vue3Vite FE'),
    (['React Vite'], 'src/App.jsx', 'React FE'),
    (['Next.js'], 'src/app/page.js', 'Next.js FE'),
    (['vanilla'], 'index.html', 'VanillaJS FE'),
]

print("Tech Stack       File                     Result (chars)  Status")
print("-" * 70)

all_ok = True
for tech, file, label in tests:
    result = loader.load_for_coder(tech, file)
    size = len(result) if result else 0
    status = "OK" if size > 0 else "EMPTY!"
    if size == 0:
        all_ok = False
    print(f"  {label:<14} {file:<25} {size:>10}  {status}")

print()

# 分层裁剪详情
print("=== 分层裁剪详情 ===")
playbooks = [
    ('flask', 'playbooks/coder/flask.md'),
    ('fastapi', 'playbooks/coder/fastapi.md'),
    ('django', 'playbooks/coder/django.md'),
    ('express', 'playbooks/coder/express.md'),
    ('vanilla_js', 'playbooks/coder/vanilla_js.md'),
    ('vue3_cdn', 'playbooks/coder/vue3_cdn.md'),
    ('vue3_vite', 'playbooks/coder/vue3_vite.md'),
    ('react_vite', 'playbooks/coder/react_vite.md'),
    ('nextjs', 'playbooks/coder/nextjs.md'),
]

total_before = 0
total_after = 0

for name, file_path in playbooks:
    with open(file_path, encoding='utf-8') as f:
        raw = f.read()
    p0 = loader._extract_tier(raw, 'P0')
    p1 = loader._extract_tier(raw, 'P1')
    p2 = loader._extract_tier(raw, 'P2')
    total = len(raw)
    trimmed = len(p0) + len(p1) if (p0 or p1) else total
    saved = total - trimmed
    pct = saved * 100 // total if total > 0 else 0
    total_before += total
    total_after += trimmed
    print(f"  {name:<14}: P0={len(p0):>5} P1={len(p1):>5} P2={len(p2):>5} | {total:>5} -> {trimmed:>5} (-{pct}%)")

total_saved = total_before - total_after
print(f"\n  TOTAL: {total_before} -> {total_after} (saved {total_saved} chars, -{total_saved*100//total_before}%)")
print(f"\n  All load tests: {'PASS' if all_ok else 'FAIL'}")
