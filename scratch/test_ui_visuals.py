import os
os.environ["MODEL_QA_VISION"] = "deepseek-chat" # fall back model check
from core.skills.check_ui_visuals import CheckUIVisualsSkill

skill = CheckUIVisualsSkill("test_dir")
response = skill.execute(**{"url": "https://www.example.com", "query": "帮我看看这个例子网页有没有元素错位的问题"})
print(response)
