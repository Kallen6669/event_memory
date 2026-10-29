"""输入转换必须保持原文、引用和幂等身份，不调用模型转换文本。"""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from event_memory.cli import main
from event_memory.errors import MemoryError
from event_memory.input_adapter import prepare_input, load_input, convert_txt_file
from event_memory.pipeline import Pipeline
from event_memory.store import Store
from event_memory.transcript import parse_transcript
from tests.test_core import SAMPLE, FixtureModel


class InputAdapterTests(unittest.TestCase):
    def test_txt_preserves_body_lines_positions_and_hash(self):
        for raw in (SAMPLE, SAMPLE.replace("\n","\r\n")):
            with self.subTest(crlf="\r\n" in raw):
                prepared=prepare_input(raw,"会议.TXT")
                self.assertEqual(prepared.markdown,raw)
                self.assertEqual(parse_transcript(prepared.markdown),parse_transcript(raw))
                self.assertEqual(prepared.metadata["input_format"],"txt")
                self.assertEqual(prepared.metadata["input_text_sha256"],parse_transcript(raw).content_hash)

    def test_conversion_handles_utf8_bom_without_rewriting_or_overwriting_source(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/"录音.txt"
            raw=SAMPLE.replace("\n","\r\n")
            original=b"\xef\xbb\xbf"+raw.encode("utf-8")
            source.write_bytes(original)
            target=convert_txt_file(source)
            self.assertEqual(source.read_bytes(),original)
            self.assertEqual(target.read_bytes(),raw.encode("utf-8"))
            self.assertEqual(load_input(target).markdown,raw)
            with self.assertRaises(MemoryError) as error:convert_txt_file(source)
            self.assertEqual(error.exception.code,"output_exists")

    def test_unsupported_or_missing_evidence_does_not_invent_timestamps(self):
        for text,name in [("只有一段普通正文。","plain.txt"),(SAMPLE+"漏掉时间的行","partial.txt"),(SAMPLE,"file.json")]:
            with self.subTest(name=name),self.assertRaises(MemoryError):prepare_input(text,name)
        with tempfile.TemporaryDirectory() as folder:
            file=Path(folder)/"bad.txt";file.write_bytes(b"\xff\xfe")
            with self.assertRaises(MemoryError) as error:load_input(file)
            self.assertEqual(error.exception.code,"invalid_encoding")

    def test_same_recording_txt_and_md_reuse_identity_and_existing_model_result(self):
        store=Store(":memory:");model=FixtureModel()
        try:
            ids=[];jobs=[]
            for suffix in ("md","txt"):
                prepared=prepare_input(SAMPLE,"录音."+suffix)
                source=store.import_source("a",prepared.markdown,"同一录音",metadata=prepared.metadata)
                ids.append(source["id"])
                jobs.append(Pipeline(store,model).process("a",source["id"],"same-op")["job_id"])
            self.assertEqual(ids[0],ids[1]);self.assertEqual(jobs[0],jobs[1]);self.assertEqual(model.calls,1)
        finally:store.close()

    def test_cli_convert_and_inspect_do_not_open_a_database_or_model(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/"input.txt";source.write_bytes(SAMPLE.encode("utf-8"))
            with patch("event_memory.cli.Store",side_effect=AssertionError("转换不得创建数据库")):
                for args in (["txt-to-md",str(source)],["inspect",str(source)]):
                    output=io.StringIO()
                    with contextlib.redirect_stdout(output):self.assertEqual(main(args),0)
                    self.assertEqual(json.loads(output.getvalue())["model_calls"],0)
            self.assertTrue(source.with_suffix(".md").exists())

    def test_cli_import_uses_txt_adapter_and_preserves_source_text(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/"input.txt";source.write_bytes(SAMPLE.encode("utf-8"))
            db=Path(folder)/"memory.sqlite";output=io.StringIO()
            with contextlib.redirect_stdout(output):
                self.assertEqual(main(["--db",str(db),"import",str(source)]),0)
            value=json.loads(output.getvalue())
            store=Store(db)
            try:
                self.assertEqual(store.source("local",value["id"])["raw"],SAMPLE)
                self.assertEqual(json.loads(value["metadata"])["input_format"],"txt")
                self.assertEqual(store.db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0],0)
            finally:store.close()
