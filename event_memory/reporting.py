"""只读统计请求账本：统一用量字段，不混用套餐用量与现金费用。"""

import json
import math
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

CASH_PATHS = {"deepseek_api", "legacy_cash_api"}


def _counter(*values):
    for value in values:
        if type(value) is int and value >= 0:
            return value
    return None


def normalize_usage(raw) -> dict:
    try:
        usage = json.loads(raw) if isinstance(raw, str) else raw
    except (ValueError, TypeError):
        usage = {}
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("input_tokens_details")
    details = details if isinstance(details, dict) else {}
    legacy_details = usage.get("prompt_tokens_details")
    legacy_details = legacy_details if isinstance(legacy_details, dict) else {}
    result = {
        "input_tokens": _counter(usage.get("input_tokens"), usage.get("prompt_tokens")),
        "output_tokens": _counter(usage.get("output_tokens"), usage.get("completion_tokens")),
        "cached_input_tokens": _counter(details.get("cached_tokens"), usage.get("prompt_cache_hit_tokens"), legacy_details.get("cached_tokens")),
    }
    if result["input_tokens"] is not None and result["cached_input_tokens"] is not None and result["cached_input_tokens"] > result["input_tokens"]:
        result["cached_input_tokens"] = None
    return result


def _metrics(rows, retries):
    usage = [normalize_usage(row.get("usage")) for row in rows]
    fields = ("input_tokens", "output_tokens", "cached_input_tokens")
    totals, known = {}, {}
    for field in fields:
        values = [u[field] for u in usage if u[field] is not None]
        totals[field] = sum(values) if values else (None if rows else 0)
        known[field] = len(values)
    states = dict(Counter(row["state"] for row in rows))
    latencies = [float(row["elapsed"]) for row in rows
                 if row["state"] == "received" and type(row.get("elapsed")) in (int, float)
                 and math.isfinite(row["elapsed"]) and row["elapsed"] >= 0]
    cash = [row for row in rows if row["billing_path"] in CASH_PATHS]
    cash_values = [row["charged"] for row in cash if type(row.get("charged")) is int and row["charged"] >= 0]
    return {
        "calls": len(rows), "registered_attempts": len(rows) + retries,
        "explicit_retries": retries, "states": states,
        "received_calls": states.get("received", 0), "unknown_calls": states.get("unknown", 0),
        "pending_calls": states.get("pending", 0),
        "provider_usage": totals, "usage_known_calls": known,
        "usage_is_partial": bool(states.get("unknown", 0) or states.get("pending", 0)
                                 or known["input_tokens"] < len(rows) or known["output_tokens"] < len(rows)),
        "mean_latency_seconds": round(sum(latencies)/len(latencies), 3) if latencies else None,
        "latency_samples": len(latencies),
        "estimated_cost_cny": sum(cash_values)/1_000_000 if cash_values else None,
        "cash_cost_is_partial": bool(cash and (len(cash_values) < len(cash)
                                      or any(row["state"] in {"pending", "unknown"} for row in cash))),
        "cost_scope": "仅已登记现金API费用估算；不包含ChatGPT套餐的货币定价",
    }


def summarize_model_runs(ledger_path: Path | str, job_ids: list[str]) -> dict:
    path = Path(ledger_path)
    if not path.is_file():
        return {"available": False, "reason": "ledger_not_found", "calls": None,
                "provider_usage": None, "estimated_cost_cny": None}
    ledger = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    ledger.row_factory = sqlite3.Row
    try:
        ledger.execute("BEGIN")
        columns = {row[1] for row in ledger.execute("PRAGMA table_info(calls)")}
        required = {"id", "job_id", "state", "charged", "usage", "elapsed", "model"}
        if not required.issubset(columns):
            return {"available": False, "reason": "unsupported_ledger_schema", "calls": None,
                    "provider_usage": None, "estimated_cost_cny": None}
        ids = list(dict.fromkeys(job_ids))
        if ids:
            where = "job_id IN (" + ",".join("?" for _ in ids) + ")"
            billing = "billing_path" if "billing_path" in columns else "'legacy_cash_api' AS billing_path"
            rows = [dict(r) for r in ledger.execute(
                "SELECT id,job_id,state,charged,usage,elapsed,model," + billing + " FROM calls WHERE " + where, ids)]
        else:
            rows = []
        retries = Counter()
        has_retries = ledger.execute("SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name='call_retries'").fetchone()
        if has_retries and ids:
            retries.update({r["call_id"]: r["n"] for r in ledger.execute(
                "SELECT call_id,COUNT(*) AS n FROM call_retries WHERE " + where + " GROUP BY call_id", ids)})
        groups = defaultdict(list)
        models = defaultdict(list)
        for row in rows:
            row["billing_path"] = row["billing_path"] or "unknown"
            row["model"] = row["model"] or "unknown"
            groups[row["billing_path"]].append(row)
            models[(row["billing_path"], row["model"])].append(row)
        def summarize(group):
            return _metrics(group, sum(retries[r["id"]] for r in group))
        return {
            "available": True, "ledger_mode": "read_only", **summarize(rows),
            "models": sorted({row["model"] for row in rows}),
            "by_billing_path": {key: summarize(group) for key, group in sorted(groups.items())},
            "by_model": [{"billing_path": billing, "model": model, **summarize(group)}
                         for (billing, model), group in sorted(models.items())],
            "note": "只统计所选作业的已登记请求及明确恢复尝试；收到响应不等于语义通过。缺失用量保留未知，延迟只平均有记录的响应。ChatGPT套餐不套用历史DS人民币单价，套餐总额度以官方为准。",
        }
    finally:
        ledger.close()
