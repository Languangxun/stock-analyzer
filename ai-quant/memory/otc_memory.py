"""场外基金交易记忆（RAG）：决策/结果向量化入库 + 相似检索。

- 每日决策+市场快照+结果 → 文本 → qwen3-embedding 向量 → embeddings.json
- 决策时检索相似历史，供模型参考
"""
import json
import os

from models.embedding import OllamaEmbedding

MEMORY_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "memory", "embeddings.json",
)


def _load():
    if not os.path.exists(MEMORY_FILE):
        return []
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, TypeError):
        return []


def _save(items):
    os.makedirs(os.path.dirname(MEMORY_FILE), exist_ok=True)
    tmp = MEMORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    os.replace(tmp, MEMORY_FILE)


def build_text(date, market_summary, decision, result_summary):
    return (
        f"日期:{date} "
        f"市场:{market_summary} "
        f"决策:{decision} "
        f"结果:{result_summary}"
    )


def add_memory(date, market_summary, decision, result_summary):
    """把一次决策及其结果写入向量记忆。失败不抛出（RAG 是增强功能）。"""
    try:
        text = build_text(date, market_summary, decision, result_summary)
        vector = OllamaEmbedding().embed(text)
        items = _load()
        items.append({"text": text, "vector": vector, "time": date})
        # 只保留最近 2000 条，避免文件膨胀
        if len(items) > 2000:
            items = items[-2000:]
        _save(items)
        return True
    except Exception as e:
        print(f"[memory] add failed: {e}")
        return False


def search(query_text, limit=3):
    """余弦相似检索。失败返回空列表。"""
    try:
        from numpy import array, dot
        from numpy.linalg import norm
        vector = OllamaEmbedding().embed(query_text)
        items = _load()
        if not items:
            return []
        results = []
        for item in items:
            v = array(item["vector"])
            score = float(dot(array(vector), v) / (norm(array(vector)) * norm(v)))
            results.append({"score": score, "text": item["text"]})
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:limit]
    except Exception as e:
        print(f"[memory] search failed: {e}")
        return []



LESSONS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "memory", "lessons.json",
)


def load_lessons(limit=5):
    """最近的经验教训（按时间升序，取末尾 limit 条）。失败返回空列表。"""
    try:
        with open(LESSONS_FILE, encoding="utf-8") as f:
            items = json.load(f)
        return items[-limit:]
    except Exception:
        return []


def append_lesson(date, decision, lesson):
    """把一条复盘教训写入经验库（同日去重，最多保留 60 条）。失败不抛出。"""
    try:
        items = []
        if os.path.exists(LESSONS_FILE):
            with open(LESSONS_FILE, encoding="utf-8") as f:
                items = json.load(f)
        items = [x for x in items if x.get("date") != date]
        items.append({"date": date, "decision": decision, "lesson": lesson})
        items = items[-60:]
        tmp = LESSONS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, LESSONS_FILE)
        return True
    except Exception as e:
        print(f"[lessons] append failed: {e}")
        return False
