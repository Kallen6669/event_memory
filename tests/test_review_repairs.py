"""评审反例：全部为合成输入，不发送真实材料或模型请求。"""
import json
import unittest
from unittest.mock import patch
from event_memory.errors import MemoryError, InvalidOutput
from event_memory.store import Store
from event_memory.pipeline import Pipeline
from event_memory.transcript import parse_transcript, build_batches
from event_memory.validation import validate_output
from tests.test_core import FixtureModel, SAMPLE, output_for


class ReviewRepairTests(unittest.TestCase):
    def test_claim_cannot_join_across_omitted_negation_but_event_can_have_gaps(self):
        raw = "\n".join(f"[09:00:0{i}.000 – 09:00:0{i+1}.000] SPK_1: {text}"
                        for i,text in enumerate(["我们", "没有", "决定采用net2。"]))
        batch = build_batches(parse_transcript(raw).segments)[0]
        value = output_for(batch)
        value["events"][0]["claims"] = [{"kind":"decision", "quote":"我们决定采用net2。", "refs":["U000001","U000003"]}]
        with self.assertRaises(InvalidOutput):
            validate_output(value,batch,{"events":[],"topics":[]})
        value["events"][0]["claims"] = [{"kind":"statement", "quote":"我们没有决定采用net2。", "refs":["U000001","U000002","U000003"]}]
        self.assertTrue(validate_output(value,batch,{"events":[],"topics":[]})["events"])
        value["events"][0]["evidence_refs"] = ["U000001","U000003"]
        value["events"][0]["claims"] = []
        self.assertTrue(validate_output(value,batch,{"events":[],"topics":[]})["events"])

    def test_invalid_input_classes_and_partial_body_are_not_success(self):
        for raw, code in [("", "empty_input"), ("# 标题\n", "empty_input"),
                          ("没有时间标签的正文", "unsupported_transcript"),
                          (SAMPLE + "没有时间标签的遗漏正文", "partial_transcript"),
                          ("[09:00:00 – 09:00:01] EVENT/bell: 叮。", "no_speech")]:
            with self.subTest(code=code), self.assertRaises(MemoryError) as error:
                parse_transcript(raw)
            self.assertEqual(error.exception.code,code)

    def test_resume_reuses_frozen_batch_instead_of_recalling_changed_candidates(self):
        store = Store(":memory:")
        model = FixtureModel(lambda _: (_ for _ in ()).throw(MemoryError("unknown_call","模拟中断")))
        try:
            source = store.import_source("review",SAMPLE,"录音")
            with self.assertRaises(MemoryError):
                Pipeline(store,model).process("review",source["id"],"op")
            job = store.db.execute("SELECT id FROM jobs").fetchone()[0]
            before = store.frozen_input("review",job,0)
            with patch.object(store,"candidates",side_effect=AssertionError("不能重新召回")):
                self.assertEqual(Pipeline(store,FixtureModel()).resume("review",job)["status"],"succeeded")
            self.assertEqual(store.frozen_input("review",job,0),before)
            store.clear_recording("review","录音")
            self.assertIsNone(store.db.execute("SELECT context FROM batch_inputs").fetchone()[0])
        finally:
            store.close()
