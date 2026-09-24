#!/usr/bin/env python3
"""夜间复盘：用 DeepSeek 点评当天模拟盘决策，提炼一条经验教训入库。

用法（在 ai-quant 目录下）: .venv/bin/python scripts/night_review.py
- 无今日复盘文件 → 跳过
- 今日已入库 → 跳过
"""
import json
import os
import re
import sys
from datetime import date

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
)

from memory.otc_memory import append_lesson, load_lessons
from models.deepseek import DeepSeekModel

today = date.today().isoformat()
# 优先股票版复盘（股票模式），回退旧基金版（etf-c 模式）
for name in (f"股票复盘-{today}.md", f"复盘-{today}.md"):
    review_path = os.path.expanduser(f"~/ai-quant/memory/daily/{name}")
    if os.path.exists(review_path):
        break
else:
    print(f"[night-review] {today} 无复盘文件，跳过")
    sys.exit(0)
if any(x.get("date") == today for x in load_lessons(60)):
    print(f"[night-review] {today} 已复盘，跳过")
    sys.exit(0)

with open(review_path, encoding="utf-8") as f:
    review_text = f.read()

client = DeepSeekModel()
prompt = (
    "你是股票模拟盘的盘后复盘教练（CLI选股+A股规则+LLM组合决策）。"
    "下面是今天的模拟盘复盘记录。\n"
    "请基于它提炼一条可执行的经验教训：只讲'以后遇到类似情况该怎么做'，"
    "一句话中文，40 字内。并给今天决策打个 1-10 分。\n"
    "只输出 JSON：{\"lesson\": \"...\", \"score\": 1-10}\n\n"
    "=== 今日复盘 ===\n" + review_text[-2500:]
)
try:
    resp = client.client.chat.completions.create(
        model=client.model,
        temperature=0.4,
        messages=[{"role": "user", "content": prompt}],
        timeout=60,
    )
    raw = resp.choices[0].message.content
    m = re.search(r"\{.*\}", raw, re.S)
    data = json.loads(m.group(0)) if m else {}
    lesson = str(data.get("lesson", "")).strip() or raw.strip()[:60]
    score = data.get("score", 5)
except Exception as e:
    print(f"[night-review] API 失败: {e}")
    sys.exit(1)

append_lesson(today, "", lesson)
print(f"[night-review] {today} 教训已入库: {lesson} (score={score})")
