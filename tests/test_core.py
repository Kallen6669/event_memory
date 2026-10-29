"""行为回归：合成材料验证工程约束，不冒充真实语义或非作者验收。"""

import json
import tempfile
import unittest
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path

from event_memory.errors import InvalidOutput, MemoryError
from event_memory.model import Ledger
from event_memory.pipeline import Pipeline
from event_memory.context import expand_reading
from event_memory.store import Store
from event_memory.transcript import build_batches, parse_transcript
from event_memory.validation import validate_output

SAMPLE = """# 合成样例
[13:00:00.000 – 13:00:02.000] SPK_1: 我们考虑点鸡公煲。
[13:00:02.000 – 13:00:04.000] SPK_2+unknown: 如果没贴膜，就撕下来；贴了就不用撕。
[13:00:05.000 – 13:00:06.000] unknown: 点好了。
[13:00:06.000 – 13:00:07.000] EVENT/bell: 叮。
"""


def output_for(batch):
    refs = [row["ref"] for row in batch["primary"]]
    claims = []
    for row in batch["primary"]:
        kind = "condition" if "如果" in row["text"] else "statement"
        claims.append({"kind": kind, "quote": row["text"], "refs": [row["ref"]]})
    return {"events": [{"title": "合成讨论", "summary": "说话人讨论安排，原话保留条件。",
                        "evidence_refs": refs, "claims": claims,
                        "topics": [{"existing_id": None, "name": "合成主题"}],
                        "continuation_of": None, "relations": [], "uncertainties": []}],
            "unclassified": []}


class FixtureModel:
    def __init__(self, hook=None):
        self.calls = 0
        self.hook = hook

    def generate(self, call_id, job_id, messages):
        self.calls += 1
        batch = expand_reading(json.loads(messages[1]["content"]))
        if self.hook:
            self.hook(job_id)
        return {"content": json.dumps(output_for(batch), ensure_ascii=False)}


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "memory.sqlite")
        self.source = self.store.import_source("alice", SAMPLE, "recording-A")

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def process(self, model=None, operation="op"):
        model = model or FixtureModel()
        result = Pipeline(self.store, model).process("alice", self.source["id"], operation)
        return result, model

    def test_exact_offsets_overlap_and_unknown(self):
        parsed = parse_transcript(SAMPLE.replace("\n", "\r\n"))
        self.assertEqual(len(parsed.segments), 4)
        self.assertEqual(parsed.segments[1].speakers, ("SPK_2", "unknown"))
        for segment in parsed.segments:
            self.assertEqual(parsed.raw[segment.char_start:segment.char_end], segment.text)
        self.assertEqual(parsed.segments[-1].kind, "acoustic")

    def test_invalid_transcript_is_not_silently_dropped(self):
        for text in ("[99:00:00 – 13:00:01] SPK: 错误", "[13:00:04 – 13:00:01] SPK: 错误", "[错误时间] SPK: 不可丢弃"):
            with self.subTest(text=text), self.assertRaises(MemoryError):
                parse_transcript(text)

    def test_batch_primary_exactly_once_context_not_new_evidence(self):
        parsed = parse_transcript(SAMPLE)
        batches = build_batches(parsed.segments, 110, 90)
        primary = [s["ref"] for b in batches for s in b["primary"]]
        self.assertEqual(primary, [s.ref for s in parsed.segments if s.kind == "speech"])
        self.assertTrue(any(b["context"] for b in batches[1:]))

    def test_source_version_conflict_and_stale_read(self):
        old = self.source
        with self.assertRaises(MemoryError):
            self.store.import_source("alice", SAMPLE + "修改", "recording-A", old["revision"])
        new = self.store.import_source("alice", SAMPLE + "# 修改\n", "recording-A", "rev2")
        self.assertNotEqual(old["id"], new["id"])
        with self.assertRaises(MemoryError):
            self.store.evidence("alice", old["id"], ["U000001"])

    def test_invalid_quote_coverage_and_condition_rejected(self):
        batch = build_batches(parse_transcript(SAMPLE).segments)[0]
        candidates = {"events": [], "topics": []}
        for change in ("coverage", "condition", "reference", "continuation"):
            value = output_for(batch)
            if change == "coverage":
                value["events"] = []
            elif change == "condition":
                value["events"][0]["claims"][1]["kind"] = "decision"
            elif change == "reference":
                value["events"][0]["evidence_refs"].append("U999999")
            else:
                value["events"][0]["continuation_of"] = "别人的已发布经历"
            with self.subTest(change=change), self.assertRaises(InvalidOutput):
                validate_output(value, batch, candidates)

    def test_nonverbatim_model_quote_cannot_be_replaced_to_publish_a_false_claim(self):
        batch = build_batches(parse_transcript(SAMPLE).segments)[0]
        value = output_for(batch)
        value["events"][0]["claims"][0]["quote"] = "已经付钱并送达"
        with self.assertRaises(InvalidOutput):
            validate_output(value, batch, {"events": [], "topics": []})

    def test_partially_unexplained_input_is_explicitly_unclassified(self):
        batch = build_batches(parse_transcript(SAMPLE).segments)[0]
        value = output_for(batch)
        value["events"][0]["evidence_refs"].remove("U000003")
        value["events"][0]["claims"] = value["events"][0]["claims"][:2]
        result = validate_output(value, batch, {"events": [], "topics": []})
        self.assertEqual(result["unclassified"][0]["ref"], "U000003")
        self.assertIn("模型未提供", result["unclassified"][0]["reason"])

    def test_claims_can_only_expand_evidence_to_explicitly_offered_refs(self):
        batch = build_batches(parse_transcript(SAMPLE).segments)[0]
        value = output_for(batch)
        value["events"][0]["evidence_refs"].remove("U000003")
        result = validate_output(value, batch, {"events": [], "topics": []})
        self.assertIn("U000003", result["events"][0]["evidence_refs"])
        self.assertEqual(result["unclassified"], [])
        value["events"][0]["claims"][-1]["refs"] = ["U999999"]
        with self.assertRaises(InvalidOutput):
            validate_output(value, batch, {"events": [], "topics": []})

    def test_unknown_topic_identity_remains_unresolved_not_authoritative(self):
        batch = build_batches(parse_transcript(SAMPLE).segments)[0]
        value = output_for(batch)
        value["events"][0]["topics"][0]["existing_id"] = "unoffered-topic-id"
        result = validate_output(value, batch, {"events": [], "topics": []})
        self.assertEqual(result["events"][0]["topics"], [])
        self.assertEqual(result["normalization_warnings"][0]["action"], "unresolved_topic_suggestion")

    def test_unoffered_unclassified_id_does_not_create_fake_evidence(self):
        batch = build_batches(parse_transcript(SAMPLE).segments)[0]
        value = output_for(batch)
        value["unclassified"] = [{"ref": "U999999", "reason": "模型额外编号"}]
        result = validate_output(value, batch, {"events": [], "topics": []})
        self.assertEqual(result["unclassified"], [])
        self.assertEqual(result["normalization_warnings"][0]["action"], "discard_unoffered_unclassified")
        self.assertNotIn("U999999", result["events"][0]["evidence_refs"])

    def test_full_pipeline_coverage_acoustic_and_idempotence(self):
        result, model = self.process()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["unclassified_count"], 1)
        event = self.store.list_events("alice")[0]
        self.assertEqual(len(self.store.evidence("alice", event["source_id"], event["refs"])), 3)
        Pipeline(self.store, model).process("alice", self.source["id"], "op")
        Pipeline(self.store, model).process("alice", self.source["id"], "another-operation")
        self.assertEqual(model.calls, 1)
        self.assertEqual(len(self.store.list_events("alice")), 1)
        self.assertFalse(event["quality"]["semantic_verified"])

    def test_provider_migration_keeps_successful_old_results_without_new_calls(self):
        self.process()
        newer=FixtureModel()
        newer.config=SimpleNamespace(model="gpt-6.1-sol",principal_hash="new-identity",provider="chatgpt_plan")
        result=Pipeline(self.store,newer).process("alice",self.source["id"],"op")
        self.assertTrue(result["reused_existing_result"])
        self.assertEqual(result["effective_model"],"test-fixture")
        self.assertEqual(result["configured_model"],"gpt-6.1-sol")
        self.assertEqual(newer.calls,0)
        self.assertEqual(len(self.store.list_events("alice")),1)

    def test_model_account_binding_blocks_resume_even_before_first_inference(self):
        from event_memory.prompt import PROMPT_VERSION
        settings={"model":"gpt-6.1-sol","prompt_version":PROMPT_VERSION,
                  "batch_chars":6000,"context_chars":900,"model_principal_hash":"original"}
        job=self.store.create_job("alice",self.source["id"],"bound-op",settings)
        model=FixtureModel()
        model.config=SimpleNamespace(model="gpt-6.1-sol",principal_hash="different",provider="chatgpt_plan")
        with self.assertRaises(MemoryError) as error:Pipeline(self.store,model).resume("alice",job["id"])
        self.assertEqual(error.exception.code,"account_changed")
        self.assertEqual(model.calls,0)

    def test_duplicate_classification_is_explicitly_normalized_not_lost(self):
        batch = build_batches(parse_transcript(SAMPLE).segments)[0]
        value = output_for(batch)
        value["unclassified"] = [{"ref": "U000001", "reason": "模型重复标记"}]
        result = validate_output(value, batch, {"events": [], "topics": []})
        self.assertEqual(result["unclassified"], [])
        self.assertIn("U000001", result["events"][0]["evidence_refs"])
        self.assertEqual(result["normalization_warnings"][0]["ref"], "U000001")

    def test_context_unclassified_does_not_reassign_preceding_batch(self):
        batches = build_batches(parse_transcript(SAMPLE).segments, 110, 90)
        batch = next(b for b in batches if b["context"])
        value = output_for(batch)
        context_ref = batch["context"][0]["ref"]
        value["unclassified"] = [{"ref": context_ref, "reason": "前置上下文"}]
        result = validate_output(value, batch, {"events": [], "topics": []})
        self.assertEqual(result["unclassified"], [])
        self.assertEqual(result["normalization_warnings"][0]["action"], "context_only")

    def test_cross_account_and_topic_correction_replay_conflict(self):
        self.process()
        event = self.store.list_events("alice")[0]
        self.assertEqual(self.store.list_events("bob"), [])
        for action in (lambda: self.store.event("bob", event["id"]),
                       lambda: self.store.evidence("bob", self.source["id"], ["U000001"]),
                       lambda: self.store.correct_topics("bob", event["id"], 1, ["错误"])):
            with self.assertRaises(MemoryError):
                action()
        changed = self.store.correct_topics("alice", event["id"], 1, ["人工纠正"])
        self.process(operation="op")
        self.assertEqual(self.store.event("alice", event["id"])["topics"][0]["name"], "人工纠正")
        with self.assertRaises(MemoryError):
            self.store.correct_topics("alice", event["id"], 1, ["旧修订"])
        self.assertEqual(changed["association_revision"], 2)

    def test_selection_does_not_expand_and_rejects_revision_change(self):
        self.process()
        event = self.store.list_events("alice")[0]
        selected = self.store.select("alice", [event["id"]], "生成材料")
        source_b = self.store.import_source("alice", SAMPLE, "recording-B")
        Pipeline(self.store, FixtureModel()).process("alice", source_b["id"], "b")
        self.assertEqual(len(self.store.selection("alice", selected["id"])["events"]), 1)
        self.store.correct_topics("alice", event["id"], 1, ["另一个主题"])
        with self.assertRaises(MemoryError):
            self.store.selection("alice", selected["id"])

    def test_salient_refinement_does_not_rewrite_successful_events_or_corrections(self):
        class SalientModel(FixtureModel):
            def generate(self, call_id, job_id, messages):
                self.calls += 1
                batch = expand_reading(json.loads(messages[1]["content"]))
                output = output_for(batch)
                if "review_focus" not in batch:
                    output["events"][0]["evidence_refs"].remove("U000003")
                    output["events"][0]["claims"] = output["events"][0]["claims"][:2]
                    output["unclassified"] = [{"ref":"U000003", "reason":"短句待核对"}]
                else:
                    output["events"][0]["claims"][0]["kind"] = "claimed_completion"
                return {"content": json.dumps(output,ensure_ascii=False)}
        model = SalientModel()
        result,_ = self.process(model)
        original = self.store.list_events("alice")[0]
        self.store.correct_topics("alice",original["id"],1,["人工保留"])
        before = self.store.event("alice",original["id"])
        fixed = self.store.select("alice",[original["id"]],"固定材料")
        pipeline = Pipeline(self.store,model)
        refined = pipeline.refine_unclassified("alice",result["job_id"])
        self.assertEqual(refined["status"],"succeeded")
        self.assertEqual(self.store.event("alice",original["id"]),before)
        self.assertEqual(len(self.store.list_events("alice")),2)
        self.assertEqual(len(self.store.selection("alice",fixed["id"])["events"]),1)
        self.assertEqual(pipeline.refine_unclassified("alice",result["job_id"])["model_calls"],0)
        self.assertEqual(model.calls,2)

    def test_revoke_cancel_and_clear_fence_late_results(self):
        for action in ("revoke", "cancel", "clear"):
            with self.subTest(action=action):
                store = Store(":memory:")
                source = store.import_source("owner", SAMPLE, action)
                def hook(job_id):
                    if action == "revoke": store.set_access("owner", False)
                    elif action == "cancel": store.job_state("owner", job_id, "cancelled")
                    else: store.clear_recording("owner", action)
                with self.assertRaises(MemoryError):
                    Pipeline(store, FixtureModel(hook)).process("owner", source["id"], "operation")
                store.set_access("owner", True)
                self.assertEqual(store.list_events("owner"), [])
                if action == "clear":
                    with self.assertRaises(MemoryError):
                        store.import_source("owner", SAMPLE, action, "new-revision")
                store.close()

    def test_empty_is_rejected_without_model_and_unknown_keeps_state(self):
        model = FixtureModel()
        with self.assertRaises(MemoryError) as error:
            self.store.import_source("alice", "# 空文档\n", "empty")
        self.assertEqual(error.exception.code, "empty_input")
        self.assertEqual(model.calls, 0)
        def fail(_):
            raise MemoryError("unknown_call", "合成超时")
        with self.assertRaises(MemoryError):
            self.process(FixtureModel(fail))
        self.assertEqual(self.store.db.execute("SELECT status FROM jobs WHERE operation_key='op'").fetchone()[0], "unknown")

    def test_unknown_request_reservation_survives_restart_and_no_new_budget(self):
        path = Path(self.temp.name) / "ledger.sqlite"
        ledger = Ledger(path, Decimal("3"))
        payload = {"model": "fixture", "messages": []}
        ledger.reserve("call", "job", payload, 1_000_000)
        ledger.close()
        ledger = Ledger(path, Decimal("10"))
        try:
            self.assertEqual(ledger.summary()["cap_cny"], 3)
            self.assertEqual(ledger.summary()["available_cny"], 2)
            with self.assertRaises(MemoryError): ledger.reserve("call", "job", payload, 1_000_000)
            with self.assertRaises(MemoryError): ledger.reserve("large", "job", payload, 2_100_000)
            ledger.reconcile("call", {"not_billed": True, "note": "合成供应商核对证明"})
            self.assertEqual(ledger.summary()["available_cny"], 3)
        finally:
            ledger.close()

    def test_bounded_history_recall_beyond_five_unrelated_recordings(self):
        class TextModel(FixtureModel):
            def generate(self, call_id, job_id, messages):
                batch = expand_reading(json.loads(messages[1]["content"]))
                value = output_for(batch)
                text = batch["primary"][0]["text"]
                value["events"][0]["title"] = text
                value["events"][0]["summary"] = "说话人表示：" + text
                return {"content": json.dumps(value, ensure_ascii=False)}
        for index in range(8):
            topic = "论文甲研究方法" if index == 0 else "今天给猫称重"
            raw = f"[10:00:00 – 10:00:01] SPK: {topic}。\n"
            source = self.store.import_source("history", raw, f"day-{index}")
            Pipeline(self.store, TextModel()).process("history", source["id"], f"op-{index}")
        events = self.store.list_events("history")
        original = next(e for e in events if "论文甲" in e["title"])
        candidates = self.store.candidates("history", "论文甲研究方法", "a-new-job", limit=2)
        self.assertEqual(len(candidates["events"]), 2)
        self.assertIn(original["id"], [e["id"] for e in candidates["events"]])
        self.assertFalse(next(e for e in candidates["events"] if e["id"] == original["id"])["can_continue"])
        self.assertEqual(len(events), 8)

    def test_shared_budget_concurrent_reservations_cannot_overspend(self):
        path = Path(self.temp.name) / "concurrent-ledger.sqlite"
        Ledger(path, Decimal("3")).close()
        def reserve(index):
            ledger = Ledger(path, Decimal("3"))
            try:
                ledger.reserve(f"call-{index}", "job", {"model": "fixture"}, 2_000_000)
                return "reserved"
            except MemoryError as exc:
                return exc.code
            finally:
                ledger.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, range(2)))
        self.assertEqual(sorted(results), ["budget_exceeded", "reserved"])


if __name__ == "__main__":
    unittest.main()
