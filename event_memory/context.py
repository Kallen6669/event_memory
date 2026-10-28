"""确定性构建精简上下文：正文不改字，定位信息只在本地保留。"""
import re

READING_COLUMNS = ["ref", "start_ms", "end_ms", "speakers", "text"]
CONTEXT_VERSION = "compact-1"


def excerpt(summary: str, query: str, budget: int) -> str:
    """选取完整句子，兼顾开头、相关句和近况，不在字符位置硬切条件句。"""
    sentences = [s.strip() for s in re.findall(r"[^。！？!?\n]+[。！？!?]?", summary) if s.strip()]
    if len(summary) <= budget:
        return summary
    if not sentences:
        return ""
    grams = {query[i:i+2] for i in range(len(query)-1)}
    ranked = sorted(range(len(sentences)), key=lambda i: (
        sum(g in sentences[i] for g in grams) / max(1, len(sentences[i]) ** .5), i), reverse=True)
    order = list(dict.fromkeys([0, len(sentences)-1] + ranked))
    selected, size = [], 0
    for index in order:
        sentence = sentences[index]
        if any(sentences[previous] == sentence for previous in selected):
            continue
        cost = len(sentence) + bool(selected)
        if size + cost <= budget:
            selected.append(index)
            size += cost
    if not selected:
        return sentences[-1] if len(sentences[-1]) <= 1800 else ""
    return "\n".join(sentences[i] for i in sorted(selected))


def compact_payload(batch: dict, candidates: dict, source: dict, review_focus: str | None = None) -> dict:
    query = "\n".join(row["text"] for row in batch["primary"])
    items = candidates["events"]
    allowance = min(700, max(180, 4800 // max(1, len(items))))
    events = []
    for item in items:
        events.append({key: item[key] for key in ("id", "title", "can_continue", "start_ms", "end_ms", "speaker_candidates") if key in item})
        events[-1]["summary"] = excerpt(item["summary"], query, allowance)
    result = {
        "format": CONTEXT_VERSION, "columns": READING_COLUMNS,
        "primary": [[row[key] for key in READING_COLUMNS] for row in batch["primary"]],
        "context": [[row[key] for key in READING_COLUMNS] for row in batch["context"]],
        "candidates": {"events": events,
                       "topics": [{"id": t["id"], "name": t["name"]} for t in candidates["topics"]],
                       "summaries_are_excerpts": True},
        "source": source,
    }
    if review_focus:
        result["review_focus"] = review_focus
    return result


def expand_reading(payload: dict) -> dict:
    """用于检查、测试与离线比较；不改变实际模型输入。"""
    if payload.get("format") != CONTEXT_VERSION:
        return payload
    return {**payload, **{key: [dict(zip(payload["columns"], row)) for row in payload[key]]
                         for key in ("primary", "context")}}
