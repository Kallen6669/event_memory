"""精简调用账本：一条逻辑请求一行，重试历史随请求保存。"""
import json
import sqlite3
from contextlib import contextmanager
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

from .config import env_values
from .errors import MemoryError
from .migrations import backup_before_migration, execute_schema, table_exists
from .store import dumps, fingerprint, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_calls(
 id TEXT PRIMARY KEY,job_id TEXT NOT NULL,request_hash TEXT NOT NULL,state TEXT NOT NULL,
 model TEXT,billing_path TEXT NOT NULL,principal_hash TEXT,request_payload TEXT,response TEXT,
 elapsed REAL,history TEXT NOT NULL DEFAULT '[]',metadata TEXT NOT NULL DEFAULT '{}',
 created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS model_call_job ON model_calls(job_id,state);
CREATE VIEW IF NOT EXISTS calls AS SELECT id,job_id,request_hash,state,
 COALESCE(json_extract(metadata,'$.reserved'),0) AS reserved,
 COALESCE(json_extract(metadata,'$.charged'),0) AS charged,response,
 COALESCE(json_extract(response,'$.usage'),json_extract(metadata,'$.usage')) AS usage,
 elapsed,model,created_at,updated_at,json_extract(metadata,'$.pricing_basis') AS pricing_basis,
 request_payload,COALESCE(json_extract(metadata,'$.content_cleared'),0) AS content_cleared,
 billing_path,principal_hash,json_extract(metadata,'$.diagnostic') AS diagnostic FROM model_calls;
CREATE VIEW IF NOT EXISTS call_retries AS SELECT COALESCE(json_extract(h.value,'$.legacy_id'),c.id||':'||h.key) AS id,c.id AS call_id,c.job_id,
 json_extract(h.value,'$.previous_state') AS previous_state,
 json_extract(h.value,'$.created_at') AS created_at FROM model_calls c,json_each(c.history) h;
"""


def _micros(value):
    return int((value*1_000_000).to_integral_value(rounding=ROUND_CEILING))


def _small_usage(raw):
    usage=json.loads(raw) if isinstance(raw,str) else raw
    return {k:v for k,v in (usage or {}).items() if k!='attribution'} if isinstance(usage,dict) else {}


def migrate_ledger(db,path):
    version=db.execute('PRAGMA user_version').fetchone()[0]
    if version>2:raise MemoryError('unsupported_schema','调用账本版本高于当前程序支持范围，未进行降级。')
    if version==2:return
    if not table_exists(db,'calls'):
        execute_schema(db,SCHEMA)
        db.execute('PRAGMA user_version=2')
        return
    backup_before_migration(db,path)
    db.execute('BEGIN IMMEDIATE')
    try:
        if not table_exists(db,'calls'):
            db.commit()
            return
        rows=[dict(r) for r in db.execute('SELECT * FROM calls')]
        histories={}
        if table_exists(db,'call_retries'):
            for row in db.execute('SELECT * FROM call_retries ORDER BY id'):
                histories.setdefault(row['call_id'],[]).append({'legacy_id':row['id'],'previous_state':row['previous_state'],'created_at':row['created_at']})
        cap=db.execute('SELECT cap FROM budget WHERE id=1').fetchone() if table_exists(db,'budget') else None
        for name in ('calls','call_retries','budget','pricing_adjustments','pricing_cache_adjustments'):
            if table_exists(db,name):db.execute('DROP TABLE '+name)
        execute_schema(db,SCHEMA)
        if cap:db.execute('INSERT INTO ledger_meta VALUES(?,?)',('legacy_cash_cap',str(cap[0])))
        for row in rows:
            metadata={k:row[k] for k in ('charged','reserved','pricing_basis','content_cleared') if row.get(k)}
            if row.get('diagnostic'):metadata['diagnostic']=json.loads(row['diagnostic'])
            response=json.loads(row['response']) if row.get('response') else {}
            if row.get('usage') and not response.get('usage'):metadata['usage']=_small_usage(row['usage'])
            db.execute('INSERT INTO model_calls VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(
                row['id'],row['job_id'],row['request_hash'],row['state'],row.get('model'),
                row.get('billing_path') or 'deepseek_api',row.get('principal_hash'),row.get('request_payload'),
                row.get('response'),row.get('elapsed'),dumps(histories.get(row['id'],[])),dumps(metadata),
                row.get('created_at') or now(),row.get('updated_at') or now()))
        db.execute('PRAGMA user_version=2')
        db.commit()
    except BaseException:
        db.rollback()
        raise


class Ledger:
    def __init__(self,path: Path | str,budget: Decimal | None = None):
        self.path=str(path)
        if self.path!=':memory:':Path(path).parent.mkdir(parents=True,exist_ok=True)
        self.db=sqlite3.connect(self.path,isolation_level=None,timeout=10)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        try:
            migrate_ledger(self.db,self.path)
        except BaseException:
            self.db.close()
            raise
        if budget is not None:
            with self.transaction():
                row=self.db.execute("SELECT value FROM ledger_meta WHERE key='legacy_cash_cap'").fetchone()
                value=min(int(row[0]),_micros(budget)) if row else _micros(budget)
                self.db.execute("INSERT OR REPLACE INTO ledger_meta VALUES('legacy_cash_cap',?)",(str(value),))

    def close(self):self.db.close()

    def bind_store(self, store):
        self._store = store
        self._sync_purges()

    def _sync_purges(self):
        if getattr(self, '_store', None) is not None and not self.db.in_transaction and not getattr(self, '_syncing', False):
            self._syncing = True
            try:
                self._store.flush_purges(self)
            finally:
                self._syncing = False

    def _check_not_purged(self, job_id):
        self._sync_purges()
        if getattr(self, '_store', None) is not None and self._store.db.execute('SELECT 1 FROM content_purges WHERE job_id=?', (job_id,)).fetchone():
            raise MemoryError('cleared', '该作业的输入或依赖已清除，禁止重新发送。')
        if getattr(self, '_store', None) is not None:
            job = self._store.db.execute('SELECT owner FROM jobs WHERE id=?', (job_id,)).fetchone()
            if not job:
                raise MemoryError('not_found', '待发送请求不属于绑定事实库中的有效作业。')
            self._store.check_job(job['owner'], job_id)

    @contextmanager
    def transaction(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def get(self,call_id):
        self._sync_purges()
        row=self.db.execute('SELECT * FROM calls WHERE id=?',(call_id,)).fetchone()
        return dict(row) if row else None

    def summary(self):
        cap=self.db.execute("SELECT value FROM ledger_meta WHERE key='legacy_cash_cap'").fetchone()
        cap=int(cap[0]) if cap else None
        spent,reserved,count=self.db.execute("SELECT COALESCE(SUM(charged),0),COALESCE(SUM(CASE WHEN state IN ('pending','unknown') THEN reserved ELSE 0 END),0),COUNT(*) FROM calls").fetchone()
        rows=self.db.execute("SELECT state,usage FROM calls WHERE billing_path='chatgpt_plan'").fetchall()
        usages=[_small_usage(r['usage']) for r in rows]
        def total(key):return sum(u[key] for u in usages if type(u.get(key)) is int and u[key]>=0)
        partial=any(r['state'] in {'pending','unknown'} or type(u.get('input_tokens')) is not int or type(u.get('output_tokens')) is not int for r,u in zip(rows,usages))
        return {'cap_cny':cap/1_000_000 if cap is not None else None,'estimated_spent_cny':spent/1_000_000,
                'reserved_or_unknown_cny':reserved/1_000_000,'available_cny':max(0,cap-spent-reserved)/1_000_000 if cap is not None else None,
                'calls':count,'cost_basis':'人民币仅为保留的历史现金账务；GPT使用套餐，未知用量不按零处理',
                'billing_path':env_values().get('MODEL_PROVIDER','chatgpt_plan'),'active_model':env_values().get('OPENAI_MODEL',''),
                'subscription':{'calls':len(rows),'unknown_calls':sum(r['state'] in {'pending','unknown'} for r in rows),
                                'input_tokens':total('input_tokens'),'output_tokens':total('output_tokens'),
                                'usage_is_partial':partial,'allowance':'以官方套餐用量为准，不推断余额'}}

    def _insert(self,call_id,job_id,payload,billing,identity,metadata):
        self.db.execute('INSERT INTO model_calls(id,job_id,request_hash,state,model,billing_path,principal_hash,request_payload,metadata,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (call_id,job_id,fingerprint(payload),'pending',payload['model'],billing,identity,dumps(payload),dumps(metadata),now(),now()))

    def reserve(self,call_id,job_id,payload,reservation):
        self._check_not_purged(job_id)
        if type(reservation) is not int or reservation<0:
            raise MemoryError('invalid_reservation','预算预留须为非负整数微元。')
        with self.transaction():
            row=self.get(call_id)
            if row:
                if row['job_id']!=job_id or row['request_hash']!=fingerprint(payload):raise MemoryError('request_conflict','原请求身份或参数已变化。')
                if row['state']=='received':return row
                raise MemoryError('unknown_call' if row['state'] in {'pending','unknown'} else 'rejected_call','原请求已有记录，不能自动重发未知请求。')
            available=self.summary()['available_cny']
            if available is None or reservation>round(available*1_000_000):raise MemoryError('budget_exceeded','历史现金预算未配置或余额不足。')
            self._insert(call_id,job_id,payload,'deepseek_api',None,{'reserved':reservation,'pricing_basis':'cny_peak_cache_20261027'})

    def reserve_subscription(self,call_id,job_id,payload,identity,limit):
        self._check_not_purged(job_id)
        with self.transaction():
            row=self.get(call_id)
            if row:
                if row['job_id']!=job_id or row['billing_path']!='chatgpt_plan' or row['principal_hash']!=identity:
                    raise MemoryError('request_conflict','原请求所属作业、账号或计费路径不一致。')
                if row['content_cleared']:raise MemoryError('cleared','原模型内容已清除，不允许恢复。')
                if row['state'] in {'pending','unknown'}:raise MemoryError('unknown_call','原调用结果未知，须先核对，不自动重发。')
                if row['request_hash']!=fingerprint(payload):raise MemoryError('request_conflict','原请求参数已变化。')
                if row['state']=='received':return row
            attempts=self.db.execute('SELECT COALESCE(SUM(1+json_array_length(history)),0) FROM model_calls WHERE job_id=?',(job_id,)).fetchone()[0]
            if attempts>=limit:raise MemoryError('call_limit','当前作业达到本地调用次数上限。')
            if row:
                saved=self.db.execute('SELECT history,metadata FROM model_calls WHERE id=?',(call_id,)).fetchone()
                history=json.loads(saved['history']);metadata=json.loads(saved['metadata'])
                history.append({'previous_state':row['state'],'created_at':now(),'diagnostic':metadata.pop('diagnostic',None)})
                self.db.execute("UPDATE model_calls SET state='pending',history=?,metadata=?,updated_at=? WHERE id=?",(dumps(history),dumps(metadata),now(),call_id))
            else:self._insert(call_id,job_id,payload,'chatgpt_plan',identity,{})

    def _update(self,call_id,state,metadata_updates=None,**columns):
        row=self.db.execute('SELECT metadata FROM model_calls WHERE id=?',(call_id,)).fetchone()
        if not row:raise MemoryError('not_found','模型调用不存在。')
        metadata=json.loads(row[0]);metadata.update(metadata_updates or {})
        columns.update(state=state,metadata=dumps(metadata),updated_at=now())
        self.db.execute('UPDATE model_calls SET '+','.join(k+'=?' for k in columns)+' WHERE id=?',[*columns.values(),call_id])

    def received(self,call_id,response,charged,elapsed):
        self._sync_purges()
        with self.transaction():
            row=self.get(call_id)
            if not row:raise MemoryError('not_found','模型调用不存在。')
            metadata={'charged':charged,'reserved':0}
            if row['content_cleared']:metadata['usage']=_small_usage(response.get('usage',{}))
            self._update(call_id,'received',metadata,response=None if row['content_cleared'] else dumps(response),elapsed=elapsed)

    def unknown(self,call_id,diagnostic=None):
        with self.transaction():self._update(call_id,'unknown',{'diagnostic':diagnostic})

    def rejected(self,call_id):
        with self.transaction():self._update(call_id,'rejected',{'reserved':0})

    def not_sent(self,call_id,diagnostic):
        with self.transaction():
            row=self.get(call_id)
            if row and row['state']=='pending':self._update(call_id,'rejected',{'reserved':0,'diagnostic':diagnostic})

    def reconcile(self,call_id,evidence):
        row=self.get(call_id)
        if not row or row['state'] not in {'pending','unknown'}:raise MemoryError('invalid_reconciliation','只能核对待定或未知的原调用。')
        if evidence.get('response'):
            response=evidence['response']
            if not isinstance(response,dict) or not (isinstance(response.get('choices'),list) or response.get('status')=='completed' and isinstance(response.get('output'),list)):
                raise MemoryError('invalid_reconciliation','必须提供供应商原始响应对象。')
            self.received(call_id,response,row['reserved'],0)
        elif evidence.get('not_billed') is True and isinstance(evidence.get('note'),str) and evidence['note'].strip():self.rejected(call_id)
        else:raise MemoryError('invalid_reconciliation','需要原响应或明确未计费的核对证明。')

    def purge_jobs(self,job_ids):
        with self.transaction():
            for job_id in job_ids:
                for row in self.db.execute('SELECT * FROM calls WHERE job_id=? AND content_cleared=0',(job_id,)).fetchall():
                    self._update(row['id'],row['state'],{'content_cleared':1,'usage':_small_usage(row['usage'])},response=None,request_payload=None)
