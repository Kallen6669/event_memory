"""最终评审反例及相邻边界；合成原文、临时库，不请求真实模型。"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from event_memory.cli import main
from event_memory.context import expand_reading
from event_memory.errors import InvalidOutput
from event_memory.evaluation import run_cases
from event_memory.ledger import Ledger
from event_memory.pipeline import Pipeline
from event_memory.store import Store
from event_memory.transcript import build_batches, parse_transcript
from event_memory.validation import validate_output
from tests.test_core import FixtureModel, output_for


def item(ref, summary, kind, quote):
    return {"title": "方案讨论", "summary": summary, "evidence_refs": [ref],
            "claims": [{"kind": kind, "quote": quote, "refs": [ref]}], "topics": [], "relations": []}


class FinalReviewGroundingTests(unittest.TestCase):
    def check(self, text, summary, kind="denial", quote=None, title="方案讨论"):
        segments = parse_transcript(f"[09:00:00 – 09:00:01] S: {text}\n").segments
        value = item("U000001", summary, kind, quote or text)
        value["title"] = title
        return validate_output({"events": [value], "unclassified": []},
                               build_batches(segments)[0], {"events": [], "topics": []})

    def test_denial_cannot_be_reversed_in_summary_or_title(self):
        for kind in ("denial", "statement"):
            for field in ("title", "summary"):
                with self.subTest(kind=kind, field=field), self.assertRaises(InvalidOutput):
                    self.check("我们没有决定采用net2。", "我们已经决定采用net2。" if field == "summary" else "讨论采用方案",
                               kind, title="已经决定采用net2" if field == "title" else "方案讨论")

    def test_statement_and_other_labels_cannot_omit_negation(self):
        for kind in ("statement", "proposal", "denial", "decision"):
            with self.subTest(kind=kind), self.assertRaises(InvalidOutput):
                self.check("我们没有决定采用net2。", "讨论方案", kind, "决定采用net2。")

    def test_cautious_summary_different_object_and_independent_decision_are_preserved(self):
        for summary in ("我们没有决定采用net2。", "尚未决定采用net2。", "不能确认采用net2。",
                        "没有证据表明采用net2。", "是否采用net2仍待讨论。", "尚未作出决定采用net2。", "还没有正式决定采用net2。"):
            with self.subTest(summary=summary):
                self.check("我们没有决定采用net2。", summary)
        self.check("我们没有决定采用net2。我们决定采用net3。", "决定采用net3。")
        self.check("我们没有决定采用net2。后来我们决定采用net2。", "后来决定采用net2。")

    def test_summary_without_claims_still_checks_original_negation(self):
        batch = build_batches(parse_transcript("[09:00:00 – 09:00:01] S: 我们没有决定采用net2。\n").segments)[0]
        value = item("U000001", "我们已经决定采用net2。", "denial", "我们没有决定采用net2。")
        value["claims"] = []
        with self.assertRaises(InvalidOutput):
            validate_output({"events": [value], "unclassified": []}, batch, {"events": [], "topics": []})

    def test_negation_before_contrast_does_not_cover_new_assertion(self):
        with self.assertRaises(InvalidOutput):
            self.check("我们没有决定采用net2。", "没有证据表明已经采用net2，但是我们已经决定采用net2。")

    def test_full_source_guards_both_sides_of_batch_without_expanding_refs(self):
        for words, selected in [(["背景" * 2940 + "，我们没有", "决定采用net2。"], 1),
                                (["背景" * 2940 + "，我们决定采用net2", "的话，需要先评审。"], 0)]:
            raw = "\n".join(f"[09:00:0{i} – 09:00:0{i+1}] S: {text}" for i, text in enumerate(words))
            segments = parse_transcript(raw).segments
            batches = build_batches(segments)
            row = segments[selected].payload()
            value = {"events": [item(row["ref"], "方案讨论", "decision", "决定采用net2")], "unclassified": []}
            with self.subTest(words=selected), self.assertRaises(InvalidOutput):
                validate_output(value, batches[selected], {"events": [], "topics": []},
                                source_segments=[s.payload() for s in segments])
        value["events"][0]["evidence_refs"] = ["U000002"]
        with self.assertRaises(InvalidOutput):
            validate_output(value, batches[0], {"events": [], "topics": []},
                            source_segments=[s.payload() for s in segments])

    def test_pipeline_does_not_publish_default_boundary_false_decision(self):
        raw = ("[09:00:00 – 09:00:01] S: " + "背景" * 2940 + "，我们没有\n"
               "[09:00:01 – 09:00:02] S: 决定采用net2。\n")
        class BoundaryModel:
            def generate(self, call_id, job_id, messages):
                batch = expand_reading(json.loads(messages[1]["content"]))
                row = batch["primary"][0]
                value = item(row["ref"], "方案讨论", "denial" if row["ref"] == "U000001" else "decision",
                             "我们没有" if row["ref"] == "U000001" else row["text"])
                return {"content": json.dumps({"events": [value], "unclassified": []}, ensure_ascii=False)}
        store = Store(":memory:")
        try:
            source = store.import_source("review", raw, "boundary")
            with self.assertRaises(InvalidOutput):
                Pipeline(store, BoundaryModel()).process("review", source["id"], "boundary")
            self.assertEqual(store.list_events("review"), [])
            self.assertEqual(store.db.execute("SELECT status FROM jobs").fetchone()[0], "failed")
        finally:
            store.close()


class EvaluationLedgerPathTests(unittest.TestCase):
    def test_cli_source_report_uses_explicit_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = Store(root / "business.sqlite")
            ledger = Ledger(root / "separate.sqlite")
            try:
                source = store.import_source("review", "[09:00:00 – 09:00:01] S: 合成材料。\n", "report")
                job = Pipeline(store, FixtureModel()).process("review", source["id"], "report")
                ledger.reserve_subscription("report-call", job["job_id"], {"model": "test-fixture"}, "test", 10)
                ledger.received("report-call", {"usage": {"input_tokens": 123, "output_tokens": 7}}, 0, 0.1)
                with contextlib.redirect_stdout(io.StringIO()):
                    code = main(["--db", str(root / "business.sqlite"), "--ledger", str(ledger.path),
                                 "--owner", "review", "evaluate", "--source-id", source["id"],
                                 "--out", str(root / "report.json")])
                self.assertEqual(code, 0)
                report = json.loads((root / "report.json").read_text(encoding="utf-8"))
                self.assertEqual(report["model_runs"]["calls"], 1)
                self.assertEqual(report["model_runs"]["provider_usage"]["input_tokens"], 123)
            finally:
                ledger.close()
                store.close()

    def test_cli_case_report_passes_db_and_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = {"overall_status": "incomplete", "case_counts": {}}
            store = Store(root / "cases.sqlite")
            store.account("指定测试库")
            store.close()
            owners = []
            def capture(store, *args, **kwargs):
                owners.extend(row[0] for row in store.db.execute("SELECT owner FROM accounts"))
                return report
            with patch("event_memory.evaluation.report_cases", side_effect=capture) as reporter:
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(main(["--db", str(root / "cases.sqlite"), "--ledger", str(root / "calls.sqlite"),
                                           "evaluate-cases", "--report-only", "--out", str(root / "report.json")]), 0)
            self.assertEqual(reporter.call_args.kwargs["ledger_path"], root / "calls.sqlite")
            self.assertEqual(owners, ["指定测试库"])

    def test_cli_live_cases_use_explicit_database_and_ledger_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = {"passed_defined_checks": True, "results": []}
            def capture(store, model, cases):
                self.assertEqual(Path(store.path), root / "cases.sqlite")
                self.assertEqual(Path(model.ledger.path), root / "calls.sqlite")
                return report
            with patch("event_memory.cli.create_model", side_effect=lambda ledger: SimpleNamespace(ledger=ledger)):
                with patch("event_memory.evaluation.run_cases", side_effect=capture):
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.assertEqual(main(["--db", str(root / "cases.sqlite"), "--ledger", str(root / "calls.sqlite"),
                                               "evaluate-cases", "--live", "--out", str(root / "report.json")]), 0)

    def test_runner_statistics_use_model_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = root / "cases.json"
            cases.write_text(json.dumps({"role": "synthetic", "cases": [{"id": "one",
                "text": "[09:00:00 – 09:00:01] S: 合成材料。\n", "required_claim_kinds": ["statement"]}]}), encoding="utf-8")
            ledger = Ledger(root / "calls.sqlite")
            class LoggedModel:
                config = SimpleNamespace(model="test-fixture")
                def __init__(self):
                    self.ledger = ledger
                def generate(self, call_id, job_id, messages):
                    self.ledger.reserve_subscription(call_id, job_id, {"model": "test-fixture"}, "test", 10)
                    batch = expand_reading(json.loads(messages[1]["content"]))
                    self.ledger.received(call_id, {"usage": {"input_tokens": 123, "output_tokens": 7}}, 0, 0.1)
                    return {"content": json.dumps(output_for(batch), ensure_ascii=False)}
            store = Store(":memory:")
            try:
                report = run_cases(store, LoggedModel(), cases)
                self.assertEqual(report["model_runs"]["calls"], 1)
                self.assertEqual(report["model_runs"]["provider_usage"]["input_tokens"], 123)
            finally:
                ledger.close()
                store.close()
