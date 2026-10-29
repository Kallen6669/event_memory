"""上下文保真、版本恢复与真实结构迁移保护；模型使用明确替身。"""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from decimal import Decimal

from event_memory.context import compact_payload, expand_reading, excerpt, READING_COLUMNS
from event_memory.errors import MemoryError
from event_memory.ledger import Ledger
from event_memory.pipeline import Pipeline
from event_memory.prompt import LEGACY_PROMPT_VERSION
from event_memory.store import Store, dumps, fingerprint, now
from event_memory.transcript import parse_transcript, build_batches
from tests.test_core import SAMPLE, FixtureModel, output_for


class ContextOptimizationTests(unittest.TestCase):
    def test_compact_reading_preserves_all_evidence_and_speakers(self):
        batch=build_batches(parse_transcript(SAMPLE).segments)[0]
        packed=compact_payload(batch,{'events':[],'topics':[]},{'recording':'demo'})
        unpacked=expand_reading(packed)
        for key in ('primary','context'):
            self.assertEqual(unpacked[key],[{k:r[k] for k in READING_COLUMNS} for r in batch[key]])
        self.assertNotIn('char_start',dumps(packed))
        self.assertLess(len(dumps(packed)),len(dumps({**batch,'candidates':{'events':[],'topics':[]},'source':{'recording':'demo'}})))

    def test_excerpt_keeps_complete_conditions_and_recent_statement(self):
        text='介绍了某个项目。'+'讨论无关天气。'*70+'如果没招到合适的人，才续费。'+'费用尚未支付。'
        value=excerpt(text,'招聘续费',100)
        self.assertIn('如果没招到合适的人，才续费。',value)
        self.assertIn('费用尚未支付。',value)
        self.assertLess(len(value),101)

    def test_repair_keeps_only_latest_bad_answer(self):
        class RepairModel:
            config=SimpleNamespace(model='fixture')
            lengths=[]
            def generate(self,call_id,job_id,messages):
                self.lengths.append(len(messages))
                if len(self.lengths)<4:return {'content':'不完整JSON'}
                return {'content':dumps(output_for(expand_reading(json.loads(messages[1]['content']))))}
        store=Store(':memory:')
        try:
            source=store.import_source('a',SAMPLE,'demo');model=RepairModel()
            Pipeline(store,model).process('a',source['id'],'op')
            self.assertEqual(model.lengths,[2,4,4,4])
        finally:store.close()

    def test_legacy_unfinished_job_keeps_original_input_format(self):
        class OldModel(FixtureModel):
            def generate(self,call_id,job_id,messages):
                payload=json.loads(messages[1]['content'])
                assert 'format' not in payload
                assert 'char_start' in payload['primary'][0]
                return super().generate(call_id,job_id,messages)
        store=Store(':memory:')
        try:
            source=store.import_source('a',SAMPLE,'demo')
            job=store.create_job('a',source['id'],'old',{'batch_chars':6000,'context_chars':900,'model':'test-fixture','prompt_version':LEGACY_PROMPT_VERSION})
            self.assertEqual(Pipeline(store,OldModel()).resume('a',job['id'])['status'],'succeeded')
        finally:store.close()

    def test_new_recording_reuses_topics_without_replacing_old_events(self):
        store=Store(':memory:')
        try:
            for recording in ('A','B'):
                source=store.import_source('a',SAMPLE,recording)
                Pipeline(store,FixtureModel()).process('a',source['id'],recording)
            self.assertEqual(len(store.list_events('a')),2)
            self.assertEqual(len(store.topics('a')),1)
            store.clear_recording('a','A')
            # B实际读取过A的候选，清除A时B也必须撤下，不能只删除关系边。
            self.assertEqual(len(store.list_events('a')),0)
        finally:store.close()


def legacy_business(path,bad_text=False):
    db=sqlite3.connect(path)
    db.executescript((Path(__file__).parent/'fixtures/schema_v1.sql').read_text(encoding='utf-8'))
    parsed=parse_transcript(SAMPLE);settings={'model':'fixture','prompt_version':LEGACY_PROMPT_VERSION}
    db.execute("INSERT INTO accounts VALUES('a',0,1)")
    db.execute('INSERT INTO sources VALUES(?,?,?,?,?,?,?,?,?,?)',('S','a','R','v1','1',parsed.content_hash,SAMPLE,'{}','current',now()))
    for s in parsed.segments:
        payload=s.payload()
        if bad_text:payload['text']='不匹配'
        db.execute('INSERT INTO segments VALUES(?,?,?)',('S',s.ref,dumps(payload)))
    db.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('J','a','op','S',fingerprint(['S',settings]),0,'unknown',0,dumps(settings),'unknown_call',now(),now()))
    db.execute("INSERT INTO tombstones VALUES('a','cleared-recording')")
    db.commit();db.close()


class MigrationTests(unittest.TestCase):
    def test_newer_schema_is_not_silently_downgraded(self):
        with tempfile.TemporaryDirectory() as temp:
            for kind in ('business','ledger'):
                path=Path(temp)/(kind+'.sqlite')
                db=sqlite3.connect(path);db.execute('PRAGMA user_version=99');db.close()
                with self.assertRaises(MemoryError):
                    Store(path) if kind=='business' else Ledger(path)
                db=sqlite3.connect(path)
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],99)
                db.close()

    def test_business_migration_preserves_identity_evidence_and_clear_marker(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'memory.sqlite';legacy_business(path)
            store=Store(path)
            try:
                self.assertEqual(store.job('a','J')['status'],'unknown')
                self.assertEqual([r['text'] for r in store.evidence('a','S')],[s.text for s in parse_transcript(SAMPLE).segments])
                self.assertNotIn('owner',{r[1] for r in store.db.execute('PRAGMA table_info(events)')})
                with self.assertRaises(MemoryError):store.import_source('a',SAMPLE,'cleared-recording')
            finally:store.close()
            Store(path).close()
            self.assertEqual(len(list((Path(temp)/'migration-backups').glob('*.sqlite'))),1)

    def test_bad_offsets_roll_back_schema_and_records(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'memory.sqlite';legacy_business(path,True)
            with self.assertRaises(MemoryError):Store(path)
            db=sqlite3.connect(path)
            self.assertIn('payload',{r[1] for r in db.execute('PRAGMA table_info(segments)')})
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sources').fetchone()[0],1)
            db.close()

    def test_ledger_migration_preserves_unknown_usage_and_retry_limit(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'ledger.sqlite';db=sqlite3.connect(path)
            db.executescript('CREATE TABLE budget(id INTEGER PRIMARY KEY,cap INTEGER); INSERT INTO budget VALUES(1,3000000); CREATE TABLE calls(id TEXT PRIMARY KEY,job_id TEXT,request_hash TEXT,state TEXT,reserved INTEGER,charged INTEGER,response TEXT,usage TEXT,elapsed REAL,model TEXT,created_at TEXT,updated_at TEXT,billing_path TEXT,principal_hash TEXT,request_payload TEXT); CREATE TABLE call_retries(id INTEGER PRIMARY KEY,call_id TEXT,job_id TEXT,previous_state TEXT,created_at TEXT);')
            payload={'model':'gpt-6.1-sol'}
            db.execute('INSERT INTO calls VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',('one','J',fingerprint(payload),'unknown',0,0,None,None,None,'gpt-6.1-sol',now(),now(),'chatgpt_plan','account',dumps(payload)))
            db.execute('INSERT INTO call_retries VALUES(?,?,?,?,?)',(1,'one','J','rejected',now()))
            db.commit();db.close()
            ledger=Ledger(path,Decimal('10'))
            try:
                self.assertEqual(ledger.summary()['cap_cny'],3)
                self.assertEqual(ledger.summary()['subscription']['unknown_calls'],1)
                self.assertEqual(ledger.db.execute('SELECT COUNT(*) FROM call_retries').fetchone()[0],1)
                self.assertEqual(ledger.db.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0],2)
                with self.assertRaises(MemoryError) as exc:ledger.reserve_subscription('one','J',payload,'account',10)
                self.assertEqual(exc.exception.code,'unknown_call')
                with self.assertRaises(MemoryError) as exc:ledger.reserve_subscription('two','J',payload,'account',2)
                self.assertEqual(exc.exception.code,'call_limit')
            finally:ledger.close()

    def test_new_gpt_ledger_does_not_need_cash_budget(self):
        ledger=Ledger(':memory:')
        try:
            ledger.reserve_subscription('one','J',{'model':'gpt-6.1-sol'},'account',2)
            self.assertEqual(ledger.get('one')['state'],'pending')
            self.assertIsNone(ledger.summary()['cap_cny'])
        finally:ledger.close()
