"""实际候选暴露、迟到响应和跨库清除：仅使用合成材料及模拟模型。"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from event_memory.errors import MemoryError
from event_memory.ledger import Ledger
from event_memory.pipeline import Pipeline
from event_memory.store import Store
from tests.test_core import FixtureModel, SAMPLE


class RecordedModel(FixtureModel):
    def __init__(self, ledger, callback=None):
        super().__init__()
        self.ledger, self.callback = ledger, callback

    def generate(self, call_id, job_id, messages):
        self.ledger.reserve_subscription(call_id, job_id, {"model":"test-fixture", "messages":messages}, "合成账号", 128)
        if self.callback:
            self.callback(job_id)
        result = super().generate(call_id, job_id, messages)
        self.ledger.received(call_id, {**result, "usage":{"input_tokens":10,"output_tokens":5}}, 0, 0.1)
        return result


class InputDependencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = Store(self.root / "memory.sqlite")
        self.ledger = Ledger(self.root / "calls.sqlite")

    def tearDown(self):
        self.ledger.close()
        self.store.close()
        self.temp.cleanup()

    def run_recording(self, recording, callback=None, owner="a"):
        source = self.store.import_source(owner,SAMPLE,recording)
        result = Pipeline(self.store,RecordedModel(self.ledger,callback)).process(owner,source["id"],recording)
        return source, result

    def test_clear_during_generation_blocks_publish_and_purges_late_response(self):
        self.run_recording("A")
        def clear(_):
            other = Store(self.root / "memory.sqlite")
            try:
                other.clear_recording("a","A")  # 另一连接提交清除；持久清除队列由绑定账本消费。
            finally:
                other.close()
        with self.assertRaises(MemoryError):
            self.run_recording("B",clear)
        self.assertEqual(self.store.list_events("a"),[])
        self.ledger._sync_purges()
        rows = self.ledger.db.execute("SELECT * FROM calls").fetchall()
        self.assertEqual(len(rows),2)
        self.assertTrue(all(r["response"] is None and r["request_payload"] is None and r["content_cleared"] for r in rows))
        self.assertEqual(self.ledger.summary()["subscription"]["input_tokens"],20)
        self.assertTrue(all(r[0] is None for r in self.store.db.execute("SELECT context FROM batch_inputs")))

    def test_transitive_clear_with_no_output_relations_preserves_unexposed_and_other_owner(self):
        self.run_recording("A")
        _, b = self.run_recording("B")
        _, c = self.run_recording("C")
        self.assertTrue(all(not row["relations"] for row in self.store.list_events("a")))
        with patch.object(self.store,"candidates",return_value={"events":[],"topics":[]}):
            self.run_recording("D")
        self.run_recording("E",owner="other")
        c_event = next(e for e in self.store.list_events("a") if e["job_id"]==c["job_id"])
        selection = self.store.select("a",[c_event["id"]],"合成选择")
        self.store.clear_recording("a","A",self.ledger)
        self.assertEqual(len(self.store.list_events("a")),1)
        self.assertEqual(len(self.store.list_events("other")),1)
        with self.assertRaises(MemoryError):
            self.store.selection("a",selection["id"])
        for job in (b,c):
            self.assertEqual(self.store.job("a",job["job_id"])["status"],"cancelled")
            self.assertIsNone(self.ledger.db.execute("SELECT request_payload FROM calls WHERE job_id=?",(job["job_id"],)).fetchone()[0])

    def test_source_revision_invalidates_published_dependents_and_fixed_selections(self):
        _, a = self.run_recording("A")
        _, b = self.run_recording("B")
        event = next(e for e in self.store.list_events("a") if e["job_id"]==b["job_id"])
        selected = self.store.select("a",[event["id"]],"旧结果")
        self.store.import_source("a",SAMPLE+"# 新修订\n","A","revision2")
        self.assertEqual(self.store.job("a",b["job_id"])["status"],"invalidated")
        with self.assertRaises(MemoryError): self.store.selection("a",selected["id"])
        self.assertEqual(self.store.list_events("a"),[])

    def test_topic_only_input_has_dependencies_even_without_event_candidate(self):
        self.run_recording("A")
        original = self.store.candidates
        def topic_only(*args,**kwargs):
            value = original(*args,**kwargs)
            return {**value,"events":[]}
        with patch.object(self.store,"candidates",side_effect=topic_only):
            _, b = self.run_recording("B")
        self.store.clear_recording("a","A",self.ledger)
        self.assertEqual(self.store.job("a",b["job_id"])["status"],"cancelled")

    def test_topic_correction_invalidates_dependents_but_preserves_corrected_event(self):
        _, a = self.run_recording("A")
        _, b = self.run_recording("B")
        event = next(e for e in self.store.list_events("a") if e["job_id"]==a["job_id"])
        self.store.correct_topics("a",event["id"],1,["人工新主题"])
        self.assertEqual(len(self.store.list_events("a")),1)
        self.assertEqual(self.store.job("a",b["job_id"])["status"],"invalidated")

    def test_clear_outbox_survives_restart_and_revoke_prevents_reappearance(self):
        self.run_recording("A")
        self.run_recording("B")
        self.store.clear_recording("a","A")
        self.ledger.close()
        self.store.close()
        self.store = Store(self.root / "memory.sqlite")
        self.ledger = Ledger(self.root / "calls.sqlite")
        self.ledger.bind_store(self.store)
        self.assertEqual(self.ledger.db.execute("SELECT COUNT(*) FROM calls WHERE request_payload IS NOT NULL OR response IS NOT NULL").fetchone()[0],0)
        self.run_recording("C")
        self.store.set_access("a",False)
        self.store.flush_purges(self.ledger)
        self.store.set_access("a",True)
        self.assertEqual(self.store.list_events("a"),[])
        self.assertEqual(self.ledger.db.execute("SELECT COUNT(*) FROM calls WHERE content_cleared=0").fetchone()[0],0)

    def test_dependency_version_changed_before_input_freeze_is_rejected(self):
        self.run_recording("A")
        source = self.store.import_source("a",SAMPLE,"B")
        job = self.store.create_job("a",source["id"],"B",{"model":"test-fixture"})
        candidates = self.store.candidates("a","讨论",job["id"])
        event = self.store.list_events("a")[0]
        self.store.correct_topics("a",event["id"],1,["变化后的主题"])
        with self.assertRaises(MemoryError):
            self.store.frozen_input("a",job["id"],0,{"candidates":candidates,"batch":{},"messages":[]})
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM batch_inputs WHERE job_id=?",(job["id"],)).fetchone()[0],0)

    def test_invalidated_between_prepare_and_send_never_reserves_another_call(self):
        self.run_recording("A")
        outer = self
        class BeforeSendModel(RecordedModel):
            def generate(self,call_id,job_id,messages):
                outer.store.import_source("a",SAMPLE+"# 新版本\n","A","v2")
                return super().generate(call_id,job_id,messages)
        source = self.store.import_source("a",SAMPLE,"B")
        with self.assertRaises(MemoryError) as error:
            Pipeline(self.store,BeforeSendModel(self.ledger)).process("a",source["id"],"B")
        self.assertEqual(error.exception.code,"dependency_changed")
        self.assertEqual(self.ledger.db.execute("SELECT COUNT(*) FROM calls").fetchone()[0],1)

    def test_unknown_during_clear_keeps_unknown_usage_but_removes_context(self):
        self.run_recording("A")
        def fail(job_id):
            self.store.clear_recording("a","A")
            call = self.ledger.db.execute("SELECT id FROM calls WHERE job_id=?",(job_id,)).fetchone()[0]
            self.ledger.unknown(call)
            raise MemoryError("unknown_call","模拟断网")
        with self.assertRaises(MemoryError): self.run_recording("B",fail)
        self.assertEqual(self.ledger.db.execute("SELECT COUNT(*) FROM calls WHERE request_payload IS NOT NULL OR response IS NOT NULL").fetchone()[0],0)
        self.assertEqual(self.ledger.summary()["subscription"]["unknown_calls"],1)
        self.assertTrue(self.ledger.summary()["subscription"]["usage_is_partial"])

    def test_legacy_unknown_dependencies_are_not_invented_and_missing_recording_is_noop(self):
        self.run_recording("A")
        self.run_recording("B")
        self.store.db.execute("DELETE FROM input_dependencies")  # 模拟升级前未追踪依赖的历史作业。
        self.store.clear_recording("a","不存在",self.ledger)
        self.assertEqual(len(self.store.list_events("a")),2)
        self.store.clear_recording("a","A",self.ledger)
        self.assertEqual(self.store.list_events("a"),[])
        self.assertEqual(self.ledger.db.execute("SELECT COUNT(*) FROM calls WHERE content_cleared=0").fetchone()[0],0)

    def test_v3_upgrade_preserves_unknown_job_and_refuses_untracked_frozen_input(self):
        source = self.store.import_source("a",SAMPLE,"旧作业")
        job = self.store.create_job("a",source["id"],"旧操作",{"model":"test-fixture"})
        self.store.job_state("a",job["id"],"unknown","unknown_call")
        self.store.db.execute("INSERT INTO batch_inputs VALUES(?,?,?)",(job["id"],0,json.dumps({"batch":{},"candidates":{"events":[],"topics":[]},"messages":[]})))
        self.store.close()
        db = sqlite3.connect(self.root / "memory.sqlite")
        db.executescript("DROP TABLE input_dependencies; DROP TABLE content_purges; PRAGMA user_version=3;")
        db.close()
        self.store = Store(self.root / "memory.sqlite")
        self.assertEqual(self.store.job("a",job["id"])["status"],"unknown")
        self.assertEqual(self.store.db.execute("PRAGMA user_version").fetchone()[0],5)
        with self.assertRaises(MemoryError) as error:
            self.store.frozen_input("a",job["id"],0)
        self.assertEqual(error.exception.code,"dependency_untracked")
        self.assertEqual(len(list((self.root / "migration-backups").glob("*.before-v5-*.sqlite"))),1)
