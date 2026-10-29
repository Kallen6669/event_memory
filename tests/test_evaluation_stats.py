"""统计报告使用供应商实际字段，缺失或未知不能写成零消耗/已通过。"""

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from event_memory.evaluation import summarize_model_runs, evaluate, report_cases, run_cases
from event_memory.model import Ledger
from event_memory.store import Store
from event_memory.prompt import PROMPT_VERSION
from event_memory.store import fingerprint
import json
from types import SimpleNamespace
from event_memory.errors import MemoryError


class EvaluationStatisticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "ledger.sqlite"
        self.ledger = Ledger(self.path, Decimal("3"))

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def gpt(self, call, job, usage=None, elapsed=None):
        self.ledger.reserve_subscription(call, job, {"model": "gpt-6.1-sol"}, "identity", 10)
        if usage is None:
            self.ledger.unknown(call)
        else:
            self.ledger.received(call, {"status": "completed", "output": [], "usage": usage}, 0, elapsed)

    def test_gpt_usage_is_reported_and_subscription_is_not_priced_as_free_cash(self):
        self.gpt("gpt", "job", {"input_tokens":1842,"output_tokens":1074,"input_tokens_details":{"cached_tokens":128}}, 2.5)
        report = summarize_model_runs(self.path, ["job"])
        self.assertEqual(report["provider_usage"], {"input_tokens":1842,"output_tokens":1074,"cached_input_tokens":128})
        self.assertIsNone(report["estimated_cost_cny"])
        self.assertEqual(report["by_billing_path"]["chatgpt_plan"]["calls"], 1)
        self.assertFalse(report["usage_is_partial"])

    def test_legacy_cash_and_gpt_usage_are_separate(self):
        self.ledger.reserve("ds", "cash-job", {"model":"deepseek-flash"}, 100000)
        self.ledger.received("ds", {"usage":{"prompt_tokens":100,"completion_tokens":50,"prompt_cache_hit_tokens":20}}, 600, 1.0)
        self.gpt("gpt", "plan-job", {"input_tokens":30,"output_tokens":10,"input_tokens_details":{"cached_tokens":0}}, 3.0)
        report = summarize_model_runs(self.path, ["cash-job", "plan-job"])
        self.assertEqual(report["provider_usage"]["input_tokens"], 130)
        self.assertEqual(report["provider_usage"]["output_tokens"], 60)
        self.assertAlmostEqual(report["estimated_cost_cny"], .0006)
        self.assertIsNone(report["by_billing_path"]["chatgpt_plan"]["estimated_cost_cny"])
        self.assertEqual(report["mean_latency_seconds"], 2.0)

    def test_unknown_and_absent_usage_are_not_reported_as_zero_or_fast(self):
        self.gpt("good", "job", {"input_tokens":20,"output_tokens":10}, 4.0)
        self.gpt("unknown", "job")
        report = summarize_model_runs(self.path, ["job"])
        self.assertTrue(report["usage_is_partial"])
        self.assertEqual(report["unknown_calls"], 1)
        self.assertEqual(report["mean_latency_seconds"], 4.0)
        self.assertEqual(report["latency_samples"], 1)
        self.assertEqual(report["provider_usage"]["input_tokens"], 20)
        self.assertIsNone(report["provider_usage"]["cached_input_tokens"])

    def test_real_zero_stays_zero_and_bool_or_negative_is_not_a_token_count(self):
        self.gpt("one", "job", {"input_tokens":0,"prompt_tokens":999,"output_tokens":False,"input_tokens_details":{"cached_tokens":-5}}, 0)
        report = summarize_model_runs(self.path, ["job"])
        self.assertEqual(report["provider_usage"]["input_tokens"], 0)
        self.assertIsNone(report["provider_usage"]["output_tokens"])
        self.assertIsNone(report["provider_usage"]["cached_input_tokens"])

    def test_missing_ledger_is_unavailable_not_a_new_empty_database(self):
        missing = self.path.parent / "missing.sqlite"
        report = summarize_model_runs(missing, ["job"])
        self.assertFalse(report["available"])
        self.assertIsNone(report["calls"])
        self.assertFalse(missing.exists())

    def test_explicit_retries_count_as_attempts_without_fake_latency_samples(self):
        payload = {"model":"gpt-6.1-sol"}
        self.ledger.reserve_subscription("retry", "job", payload, "identity", 10)
        self.ledger.rejected("retry")
        self.ledger.reserve_subscription("retry", "job", payload, "identity", 10)
        self.ledger.received("retry", {"usage":{"input_tokens":10,"output_tokens":5}}, 0, 2)
        report = summarize_model_runs(self.path, ["job"])
        self.assertEqual(report["calls"], 1)
        self.assertEqual(report["registered_attempts"], 2)
        self.assertEqual(report["mean_latency_seconds"], 2)
        self.assertEqual(report["latency_samples"], 1)

    def test_source_report_includes_unknown_jobs(self):
        store = Store(":memory:")
        try:
            source = store.import_source("a", "[10:00:00 – 10:00:01] S: 测试。\n", "r")
            job = store.create_job("a", source["id"], "op", {"model":"gpt-6.1-sol"})
            store.job_state("a", job["id"], "unknown", "unknown_call")
            self.gpt("unknown", job["id"])
            report = evaluate(store, "a", source["id"], ledger_path=self.path)
            self.assertEqual(report["model_runs"]["unknown_calls"], 1)
            self.assertIsNone(report["model_runs"]["provider_usage"]["input_tokens"])
            self.assertFalse(report["engineering"]["coverage_complete"])
            self.assertEqual(report["job_states"], {"unknown":1})
            self.assertEqual(report["jev"]["status"], "excluded_by_user")
        finally: store.close()

    def test_live_runner_returns_partial_report_and_stops_after_unknown(self):
        definition = {"role":"synthetic_acceptance","cases":[
            {"id":"one","text":"[10:00:00 – 10:00:01] S: 合成材料。\n","required_claim_kinds":["condition"]},
            {"id":"two","text":"[10:00:00 – 10:00:01] S: 另一材料。\n","required_claim_kinds":["denial"]}]}
        path = self.path.parent / "runner-cases.json"
        path.write_text(json.dumps(definition), encoding="utf-8")
        class UnknownModel:
            config = SimpleNamespace(model="audit-fixture")
            calls = 0
            def generate(self, *args):
                self.calls += 1
                raise MemoryError("unknown_call", "合成未知调用")
        store, model = Store(":memory:"), UnknownModel()
        try:
            result = run_cases(store, model, path)
            self.assertEqual(model.calls, 1)
            self.assertEqual(result["overall_status"], "incomplete")
            self.assertEqual(result["case_counts"]["unknown"], 1)
            self.assertEqual(result["case_counts"]["not_run"], 1)
            self.assertFalse(result["passed_defined_checks"])
            self.assertEqual(result["execution_stopped"]["error"], "unknown_call")
        finally: store.close()

    def test_case_report_counts_unknown_and_not_run_without_triggering_models(self):
        definition = {"role":"synthetic_acceptance","cases":[
            {"id":"one","text":"[10:00:00 – 10:00:01] S: 合成材料。\n","required_claim_kinds":["condition"]},
            {"id":"two","text":"[10:00:00 – 10:00:01] S: 另一材料。\n","required_claim_kinds":["denial"]}]}
        path = self.path.parent / "cases.json"
        path.write_text(json.dumps(definition), encoding="utf-8")
        store = Store(":memory:")
        try:
            case = definition["cases"][0]
            source = store.import_source("evaluation",case["text"],"synthetic-one")
            job = store.create_job("evaluation",source["id"],"evaluate-"+fingerprint(definition)[:16]+"one",{"model":"gpt-6.1-sol","prompt_version":PROMPT_VERSION})
            store.job_state("evaluation",job["id"],"unknown","unknown_call")
            self.gpt("unknown",job["id"])
            before = self.ledger.summary()["calls"]
            report = report_cases(store,"gpt-6.1-sol",path,ledger_path=self.path)
            self.assertEqual(report["case_counts"],{"total":2,"passed":0,"failed":0,"unknown":1,"not_run":1,"other_incomplete":0})
            self.assertEqual(report["overall_status"],"incomplete")
            self.assertFalse(report["passed_defined_checks"])
            self.assertIsNone(report["results"][0]["passed_defined_checks"])
            self.assertEqual(self.ledger.summary()["calls"],before)
        finally: store.close()
