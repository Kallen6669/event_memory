"""真实 HTTP 消费路径与官方接口适配的边界回归；网络模型使用明确替身。"""

import json
import re
import tempfile
import threading
import unittest
import uuid
import urllib.error
import urllib.request
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from event_memory.config import ModelConfig
from event_memory.errors import MemoryError
from event_memory.model import DeepSeekModel, Ledger, usage_charge
from event_memory.pipeline import Pipeline
from event_memory.server import create_server
from event_memory.store import Store
from tests.test_core import FixtureModel, SAMPLE


class Reply:
    def __init__(self, value): self.value = value
    def __enter__(self): return self
    def __exit__(self, *_): return False
    def read(self, _): return json.dumps(self.value).encode()


class LegacyRecordTests(unittest.TestCase):
    """迁移后只保留 DS 账务/回读，不继续验证已下线的网络生成。"""
    def test_deepseek_is_retired_but_old_responses_and_costs_remain_readable(self):
        ledger=Ledger(":memory:",Decimal("3"))
        try:
            response={"choices":[{"finish_reason":"stop","message":{"content":"历史结果"}}]}
            ledger.reserve("legacy","job",{"model":"deepseek-flash"},1000)
            ledger.received("legacy",response,600,0.1)
            model=DeepSeekModel(ModelConfig(key="unused"),ledger)
            self.assertEqual(model.replay("legacy","job")["content"],"历史结果")
            with patch("urllib.request.OpenerDirector.open") as network:
                with self.assertRaises(MemoryError) as error:model.generate("new","job",[])
                self.assertEqual(error.exception.code,"provider_retired")
                network.assert_not_called()
            self.assertEqual(ledger.summary()["estimated_spent_cny"],0.0006)
        finally:ledger.close()

    def test_old_cache_pricing_and_cleared_unknown_finance_are_preserved(self):
        self.assertEqual(usage_charge({"prompt_tokens":100,"completion_tokens":50,"prompt_cache_hit_tokens":100}),404)
        self.assertEqual(usage_charge({"prompt_tokens":100,"completion_tokens":50,"prompt_cache_hit_tokens":999}),600)
        ledger=Ledger(":memory:",Decimal("3"))
        try:
            ledger.reserve("late","job",{"model":"legacy"},1000)
            ledger.unknown("late")
            ledger.purge_jobs(["job"])
            ledger.reconcile("late",{"response":{"choices":[]}})
            self.assertIsNone(ledger.get("late")["response"])
            self.assertGreater(ledger.summary()["estimated_spent_cny"],0)
        finally:ledger.close()


class HTTPConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "memory.sqlite"
        store = Store(self.path)
        source = store.import_source("local", SAMPLE, "synthetic-recording")
        Pipeline(store, FixtureModel()).process("local", source["id"], "op")
        self.event_id = store.list_events("local")[0]["id"]
        store.close()
        self.server = create_server(self.path, port=0, ledger_path=Path(self.temp.name) / "ledger.sqlite")
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        with urllib.request.urlopen(self.base) as reply:
            html = reply.read().decode()
        self.token = re.search(r'name="local-token" content="([^"]+)"', html).group(1)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, path, body=None, headers=None):
        headers = {"X-Local-Token": self.token, **(headers or {})}
        if body is not None:
            headers["Content-Type"] = "application/json"
            if path == "/api/selections" or path.endswith("/topics"):
                body = {"operation_id":uuid.uuid4().hex, **body}
        request = urllib.request.Request(self.base+path, data=None if body is None else json.dumps(body).encode(), headers=headers)
        with urllib.request.urlopen(request) as reply:
            return json.loads(reply.read())

    def test_list_detail_evidence_correction_and_export_real_consumer(self):
        events = self.request("/api/events")
        self.assertEqual(len(events), 1)
        detail = self.request("/api/events/"+self.event_id)
        self.assertEqual(len(detail["evidence"]), 3)
        self.assertEqual(self.request("/api/topics")[0]["event_count"], 1)
        corrected = self.request(f"/api/events/{self.event_id}/topics", {"revision": 1, "names": ["人工检查主题"]})
        self.assertEqual(corrected["association_revision"], 2)
        selected = self.request("/api/selections", {"event_ids": [self.event_id], "purpose": "合成导出验证"})
        exported = self.request("/api/selections/"+selected["id"])
        self.assertEqual(len(exported["items"]), 1)
        self.assertEqual(len(exported["items"][0]["evidence"]), 3)

    def test_authentication_cross_origin_and_host_rebinding_rejected(self):
        for headers in ({"X-Local-Token": "wrong"}, {"Origin": "http://evil.invalid"}, {"Host": "evil.invalid"}):
            with self.subTest(headers=headers), self.assertRaises(urllib.error.HTTPError) as error:
                self.request("/api/events", headers=headers)
            self.assertEqual(error.exception.code, 403)

    def test_client_cannot_override_owner_or_escape_asset_directory(self):
        self.assertEqual(len(self.request("/api/events?owner=someone-else")), 1)
        with self.assertRaises(urllib.error.HTTPError):
            self.request("/../.env")

    def test_stale_correction_returns_conflict(self):
        self.request(f"/api/events/{self.event_id}/topics", {"revision": 1, "names": ["新主题"]})
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(f"/api/events/{self.event_id}/topics", {"revision": 1, "names": ["过时主题"]})
        self.assertEqual(error.exception.code, 409)

    def test_lost_reply_can_be_queried_and_repeated_without_second_effect(self):
        path=f"/api/events/{self.event_id}/topics"
        body={"operation_id":"lost-correction","revision":1,"names":["可核对主题"]}
        self.request(path,body)  # 模拟调用方没有保留首次200回包。
        receipt=self.request("/api/operations/lost-correction")
        self.assertEqual(receipt["status"],"applied")
        self.assertEqual(receipt["result"]["association_revision"],2)
        replay=self.request(path,body)
        self.assertEqual(replay["association_revision"],2)
        self.assertEqual(self.request("/api/events/"+self.event_id)["event"]["association_revision"],2)
        selection={"operation_id":"lost-selection","event_ids":[self.event_id],"purpose":"可核对导出"}
        first=self.request("/api/selections",selection)
        second=self.request("/api/selections",selection)
        self.assertEqual(first,second)
        self.assertEqual(self.request("/api/operations/lost-selection")["result"]["id"],first["id"])
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(path,{**body,"names":["换内容不能复用编号"]})
        self.assertEqual(error.exception.code,409)

    def test_operation_read_requires_same_local_session_and_missing_id_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/operations/absent",headers={"X-Local-Token":"wrong"})
        self.assertEqual(error.exception.code,403)
        request=urllib.request.Request(self.base+"/api/selections",data=json.dumps({"event_ids":[self.event_id],"purpose":"无操作编号"}).encode(),headers={"X-Local-Token":self.token,"Content-Type":"application/json"})
        with self.assertRaises(urllib.error.HTTPError) as error: urllib.request.urlopen(request)
        self.assertEqual(error.exception.code,400)

    def test_txt_upload_preserves_verbatim_evidence_and_does_not_generate(self):
        imported=self.request("/api/sources",{"text":SAMPLE,"filename":"会议.TXT","recording":"上传TXT"})
        self.assertEqual(json.loads(imported["metadata"])["input_format"],"txt")
        store=Store(self.path)
        try:
            self.assertEqual(store.source("local",imported["id"])["raw"],SAMPLE)
            self.assertEqual(store.db.execute("SELECT COUNT(*) FROM jobs WHERE source_id=?",(imported["id"],)).fetchone()[0],0)
        finally:store.close()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request("/api/sources",{"text":"无时间标签正文","filename":"bad.txt","recording":"拒绝TXT"})
        self.assertEqual(error.exception.code,400)


if __name__ == "__main__": unittest.main()
