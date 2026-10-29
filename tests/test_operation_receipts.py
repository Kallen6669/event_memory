"""操作回执的事务、生命周期和并发复现，不调用大模型。"""
import concurrent.futures
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from event_memory.errors import MemoryError
from event_memory.pipeline import Pipeline
from event_memory.store import Store
from tests.test_core import FixtureModel, SAMPLE


class OperationReceiptTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.path=Path(self.temp.name)/"memory.sqlite"
        self.store=Store(self.path)
        source=self.store.import_source("a",SAMPLE,"A")
        Pipeline(self.store,FixtureModel()).process("a",source["id"],"A")
        self.event=self.store.list_events("a")[0]

    def tearDown(self):
        self.store.close();self.temp.cleanup()

    def test_same_id_replays_and_changed_or_cross_kind_request_conflicts(self):
        first=self.store.correct_topics("a",self.event["id"],1,["主题"],"correct-once")
        self.assertEqual(self.store.correct_topics("a",self.event["id"],1,["主题"],"correct-once"),first)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM corrections").fetchone()[0],1)
        for change in (lambda:self.store.correct_topics("a",self.event["id"],1,["其他"],"correct-once"),
                       lambda:self.store.select("a",[self.event["id"]],"导出","correct-once")):
            with self.assertRaises(MemoryError) as error:change()
            self.assertEqual(error.exception.code,"operation_conflict")
        with self.assertRaises(MemoryError):self.store.operation("other","correct-once")

    def test_receipt_and_effect_survive_restart_and_failed_transaction_has_no_receipt(self):
        with self.assertRaises(MemoryError):self.store.correct_topics("a",self.event["id"],1,[""],"failed-op")
        self.assertEqual(self.store.event("a",self.event["id"])["association_revision"],1)
        with self.assertRaises(MemoryError):self.store.operation("a","failed-op")
        first=self.store.select("a",[self.event["id"]],"固定材料","selection-once")
        self.store.close();self.store=Store(self.path)
        self.assertEqual(self.store.select("a",[self.event["id"]],"固定材料","selection-once"),first)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM selections").fetchone()[0],1)

    def test_later_changes_are_explicit_and_clearing_erases_receipt_content(self):
        self.store.correct_topics("a",self.event["id"],1,["主题"],"c1")
        self.store.select("a",[self.event["id"]],"材料","s1")
        self.store.correct_topics("a",self.event["id"],2,["新主题"],"c2")
        self.assertEqual(self.store.operation("a","c1")["current_state"],"changed")
        self.assertEqual(self.store.operation("a","s1")["current_state"],"unavailable")
        self.assertEqual(self.store.correct_topics("a",self.event["id"],1,["主题"],"c1")["association_revision"],2)
        self.store.clear_recording("a","A")
        for id in ("c1","c2","s1"):
            receipt=self.store.operation("a",id)
            self.assertEqual(receipt["status"],"cleared");self.assertIsNone(receipt["result"])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM operation_receipts WHERE result IS NOT NULL").fetchone()[0],0)

    def test_parallel_duplicate_commits_once(self):
        path,event_id=self.path,self.event["id"]
        def write(_):
            store=Store(path)
            try:return store.correct_topics("a",event_id,1,["并发主题"],"parallel")
            finally:store.close()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(write,range(2)))
        self.assertEqual(results[0],results[1])
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM corrections").fetchone()[0],1)

    def test_cross_source_clear_also_clears_derived_operation_receipts(self):
        source=self.store.import_source("a",SAMPLE,"B")
        result=Pipeline(self.store,FixtureModel()).process("a",source["id"],"B")
        event=next(e for e in self.store.list_events("a") if e["job_id"]==result["job_id"])
        self.store.correct_topics("a",event["id"],1,["派生主题"],"derived-correct")
        self.store.select("a",[event["id"]],"派生材料","derived-select")
        self.store.clear_recording("a","A")
        for operation in ("derived-correct","derived-select"):
            receipt=self.store.operation("a",operation)
            self.assertEqual(receipt["status"],"cleared")
            self.assertIsNone(receipt["result"])

    @unittest.skipUnless(shutil.which("node"),"未安装Node，前端回执逻辑测试需单独运行")
    def test_browser_unknown_and_reload_recovery(self):
        result=subprocess.run(["node","--test",str(Path(__file__).with_name("web_operations.test.cjs"))],capture_output=True,text=True,encoding="utf-8")
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
