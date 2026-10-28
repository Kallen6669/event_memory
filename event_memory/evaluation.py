"""工程评价与人工复核材料；不使用模型自评分冒充独立验收。"""

import json
from collections import Counter
from pathlib import Path

from .store import Store, fingerprint, now
from .transcript import clock_ms
from .transcript import parse_transcript
from .reporting import summarize_model_runs
from .errors import MemoryError
from .config import default_ledger_path

DEFAULT_LEDGER = default_ledger_path()

RUBRIC = {
    "critical": ["越权或非法引用发布", "明确条件/否定丢失", "把声称完成升级为已验证事实",
                 "只凭同主题误合并不同经历", "选择范围静默扩大", "清除后复活"],
    "hard_gate": "工程必测用例全部通过；语义关键错误必须为零；未知可保留但应说明原因",
    "metrics": ["候选召回遗漏", "经历误合并/误拆分", "主题错误", "概括的证据支持程度",
                "条件和否定保留", "人工纠正成本", "调用次数/延迟/费用"],
    "scoring": "每项人工标为通过、失败或未知并注明引用；未知不算通过。探索材料不作为独立盲测。",
}


def run_cases(store: Store, model, cases_path: Path) -> dict:
    """先读取并固定样例，再运行；不根据模型输出修改预期。"""
    definition = json.loads(cases_path.read_text(encoding="utf-8"))
    cases_hash = fingerprint(definition)
    from .pipeline import Pipeline
    from .prompt import PROMPT_VERSION
    model_name = getattr(getattr(model, "config", None), "model", "test-fixture")
    stopped = None
    for case in definition["cases"]:
        owner = "evaluation-" + PROMPT_VERSION + "-" + model_name + "-" + case["id"]
        try:
            source = store.import_source(owner, case["text"], "synthetic-" + case["id"])
            Pipeline(store, model).process(owner, source["id"], "evaluate-" + cases_hash[:16] + case["id"])
        except MemoryError as exc:
            stopped = {"case": case["id"], "error": exc.code, "message": str(exc)}
            break
    report = report_cases(store, model_name, cases_path)
    report["execution_stopped"] = stopped
    return report


def report_cases(store: Store, model_name: str, cases_path: Path,
                 ledger_path: Path | str = DEFAULT_LEDGER, prompt_version: str | None = None) -> dict:
    """只统计既有评测记录，未知/失败/未运行分别表达，不触发模型。"""
    from .prompt import PROMPT_VERSION
    prompt_version = prompt_version or PROMPT_VERSION
    definition = json.loads(cases_path.read_text(encoding="utf-8"))
    cases_hash = fingerprint(definition)
    results, job_ids = [], []
    counts = {"total": len(definition["cases"]), "passed": 0, "failed": 0,
              "unknown": 0, "not_run": 0, "other_incomplete": 0}
    for case in definition["cases"]:
        operation = "evaluate-" + cases_hash[:16] + case["id"]
        content_hash = parse_transcript(case["text"]).content_hash
        matches = []
        for row in store.db.execute("SELECT j.*,s.state AS source_state,s.content_hash FROM jobs j JOIN sources s ON s.id=j.source_id WHERE j.operation_key=? ORDER BY j.created_at,j.id", (operation,)):
            settings = json.loads(row["settings"])
            if settings.get("model") == model_name and settings.get("prompt_version") == prompt_version and row["content_hash"] == content_hash:
                matches.append(dict(row))
        job_ids.extend(row["id"] for row in matches)
        result = {"case": case["id"], "required_claim_kinds": case["required_claim_kinds"],
                  "passed_defined_checks": None, "actual_claim_kinds": None, "missing_kinds": None,
                  "events": [], "matched_job_count": len(matches)}
        if not matches:
            result["status"] = "not_run"
            counts["not_run"] += 1
        else:
            job = matches[-1]
            result.update({"job_id": job["id"], "source_id": job["source_id"], "status": job["status"], "error": job["error"]})
            if job["status"] == "succeeded" and job["source_state"] == "current":
                events = [store._event_dict(row) for row in store.db.execute("SELECT * FROM event_details WHERE job_id=? AND owner=?", (job["id"], job["owner"]))]
                kinds = {claim["kind"] for event in events for claim in event["claims"]}
                missing = sorted(set(case["required_claim_kinds"]) - kinds)
                result.update({"events": events, "actual_claim_kinds": sorted(kinds), "missing_kinds": missing,
                               "passed_defined_checks": not missing, "status": "passed" if not missing else "failed_checks"})
                counts["passed" if not missing else "failed"] += 1
            elif job["status"] == "unknown":
                counts["unknown"] += 1
            else:
                counts["other_incomplete"] += 1
        results.append(result)
    complete = bool(results) and counts["passed"] + counts["failed"] == counts["total"]
    return {"created_at": now(), "cases_hash": cases_hash, "sample_role": definition["role"],
            "model": model_name, "prompt_version": prompt_version, "results": results,
            "case_counts": counts, "overall_status": "complete" if complete else "incomplete",
            "passed_defined_checks": complete and counts["failed"] == 0,
            "independent_semantic_review": "pending", "model_runs": summarize_model_runs(ledger_path, job_ids),
            "jev": {"status": "excluded_by_user", "reason": "用户明确本轮不测试Jev"},
            "limits": "只统计作者预设样例的既有结果；不把未运行或未知计为通过，不冒充独立语义验收"}


def evaluate(store: Store, owner: str, source_id: str, ledger_path: Path | str = DEFAULT_LEDGER) -> dict:
    source = store.source(owner, source_id)
    all_jobs = [dict(row) for row in store.db.execute("SELECT * FROM jobs WHERE source_id=? AND owner=?", (source_id, owner))]
    jobs = [job for job in all_jobs if job["status"] == "succeeded"]
    rows = [store._event_dict(row) for row in store.db.execute("SELECT e.* FROM event_details e JOIN jobs j ON j.id=e.job_id WHERE e.source_id=? AND e.owner=? AND j.status='succeeded'", (source_id, owner))]
    segments = {row["ref"]: row for row in store.evidence(owner, source_id)}
    covered, missing_quotes, review_items = set(), [], []
    for event in rows:
        covered.update(event["refs"])
        for claim in event["claims"]:
            joined = "\n".join(segments[r]["text"] for r in claim["refs"])
            plain = "".join(segments[r]["text"] for r in claim["refs"])
            if claim["quote"] not in joined and claim["quote"] not in plain:
                missing_quotes.append(event["id"])
        review_items.append({"event": event, "evidence": [segments[r] for r in event["refs"]],
                             "human_checks": {metric: None for metric in RUBRIC["metrics"][:6]}})
    unclassified = []
    for job in jobs:
        for row in store.db.execute("SELECT ref,reason FROM unclassified WHERE job_id=?", (job["id"],)):
            covered.add(row["ref"])
            unclassified.append({**dict(row), "evidence": segments[row["ref"]]})
    anchors = [
        ("EX-001", ["17:10:40.240", "17:10:54.080", "17:14:57.440"], "保留双人份鸡公煲的选择与点好了的声称，不推断口味、支付或送达；点餐延续应有据，不能混入全部聊猫内容"),
        ("EX-002", ["17:04:32.880", "17:04:48.880"], "保留贴膜不撕、未贴才撕的两侧条件，不认定已经执行"),
        ("EX-003", ["16:48:39.920", "16:49:04.160", "16:49:08.400"], "保留面试安排、平台到期说法及招不到合适人才续费的条件，不认定面试已发生或已续费"),
        ("EX-004", ["13:31:00.800", "13:33:33.040"], "重复介绍不作为独立事实支持，不验证招生数据，不认定用户个人观点"),
        ("EX-005", ["17:30:23.040", "17:30:30.560"], "保留房租、工资未发和手头紧等条件及帮助意向，不认定已转账或真实身份"),
    ]
    case_review = []
    for label, times, expected in anchors:
        refs = {ref for ref, row in segments.items() if row["start_ms"] in {clock_ms(t) for t in times}}
        if not refs:
            continue
        matching = [e for e in rows if set(e["refs"]) & refs]
        case_review.append({"case": label, "expected": expected, "anchor_refs": sorted(refs),
                            "matched_event_ids": [e["id"] for e in matching],
                            "summaries": [e["summary"] for e in matching], "human_verdict": None})
    warnings = []
    for job in jobs:
        for row in store.db.execute("SELECT batch_index,warnings FROM batch_results WHERE job_id=? ORDER BY batch_index", (job["id"],)):
            warnings.extend({"batch": row["batch_index"]+1, **w} for w in json.loads(row["warnings"]))
    model_runs = summarize_model_runs(ledger_path, [job["id"] for job in all_jobs])
    return {"created_at": now(), "source": {k: v for k, v in source.items() if k != "raw"},
            "rubric": RUBRIC, "sample_role": "exploration_real_transcript",
            "engineering": {"successful_job": bool(jobs), "segments": len(segments),
                            "events": len(rows), "unclassified": len(unclassified),
                            "coverage_complete": bool(jobs) and covered == set(segments),
                            "invalid_quote_events": missing_quotes,
                            "normalization_warnings": len(warnings)},
            "independent_semantic_review": "pending",
            "jev": {"status": "excluded_by_user", "reason": "用户明确本轮不测试Jev"},
            "job_states": dict(Counter(job["status"] for job in all_jobs)),
            "case_review": case_review, "normalization_warnings": warnings,
            "model_runs": model_runs,
            "review_items": review_items, "unclassified_items": unclassified,
            "limitations": ["原始材料是候选转录，未证明正式上游证据资格",
                            "工程通过不证明语义正确；需要非作者复核",
                            "不代表原系统多服务接入或用户最终验收通过"]}
