"""最终评审反例，仅在复制代码和合成临时库上运行。"""
import contextlib,io,json,sys,shutil,uuid
from pathlib import Path
PROJECT=Path(__file__).resolve().parents[1]
BASE=PROJECT/'.runtime'/('known-issues-'+uuid.uuid4().hex[:12])
BASE.mkdir(parents=True)
shutil.copytree(PROJECT/'event_memory',BASE/'sandbox/event_memory',ignore=shutil.ignore_patterns('__pycache__'))
sys.path.insert(0,str(BASE/'sandbox'))
from event_memory.store import Store
from event_memory.pipeline import Pipeline
from event_memory.context import expand_reading
from event_memory.transcript import parse_transcript,build_batches
from event_memory.validation import validate_output
from event_memory.cli import main
from event_memory.ledger import Ledger

def event(ref,text,kind,quote):
    return {'title':'方案讨论','summary':text,'evidence_refs':[ref],
            'claims':[{'kind':kind,'quote':quote,'refs':[ref]}], 'topics':[], 'relations':[]}

result={'live_model_calls':0,'findings':{}}
raw='[09:00:00 – 09:00:02] S: 我们没有决定采用net2。\n'
batch=build_batches(parse_transcript(raw).segments)[0]
for name,item in [('denial_claim_positive_summary',event('U000001','我们已经决定采用net2。','denial','我们没有决定采用net2。')),
                  ('statement_omits_negation',event('U000001','我们决定采用net2。','statement','决定采用net2。'))]:
    try:
        validate_output({'events':[item],'unclassified':[]},batch,{'events':[],'topics':[]})
        accepted=True
    except Exception as exc:accepted=False
    result['findings'][name]={'incorrect_output_accepted':accepted}

class Model:
    def __init__(self,mode):self.mode=mode
    def generate(self,call_id,job_id,messages):
        payload=expand_reading(json.loads(messages[1]['content']));row=payload['primary'][0]
        if self.mode=='summary':item=event(row['ref'],'我们已经决定采用net2。','denial',row['text'])
        elif row['ref']=='U000001':item=event(row['ref'],'说话人表示没有作出决定。','denial','我们没有')
        else:item=event(row['ref'],'我们决定采用net2。','decision',row['text'])
        return {'content':json.dumps({'events':[item],'unclassified':[]},ensure_ascii=False)}

store=Store(':memory:')
try:
    source=store.import_source('review',raw,'contradiction')
    output=Pipeline(store,Model('summary')).process('review',source['id'],'contradiction')
    result['findings']['published_summary_contradicts_denial']={'job_status':output['status'],'published_summary':store.list_events('review')[0]['summary']}
finally:store.close()

raw='[09:00:00 – 09:00:01] S: '+('背景'*2940)+'，我们没有\n[09:00:01 – 09:00:02] S: 决定采用net2。\n'
batches=build_batches(parse_transcript(raw).segments,6000,900)
assert len(batches)==2 and not batches[1]['context']
store=Store(':memory:')
try:
    source=store.import_source('review',raw,'boundary')
    output=Pipeline(store,Model('boundary')).process('review',source['id'],'boundary')
    result['findings']['default_batch_boundary_loses_negation']={'batch_parameters':[6000,900],'batches':len(batches),
       'second_context_count':len(batches[1]['context']),'job_status':output['status'],
       'published_decision':any(c['kind']=='decision' for e in store.list_events('review') for c in e['claims'])}
finally:store.close()

db=BASE/'evaluate.sqlite';calls=BASE/'separate-calls.sqlite';report=BASE/'evaluate.json'
store=Store(db)
try:
    source=store.import_source('review','[09:00:00 – 09:00:01] S: 合成材料。\n','statistics')
    class Plain:
        def generate(self,call_id,job_id,messages):
            b=expand_reading(json.loads(messages[1]['content']));r=b['primary'][0]
            return {'content':json.dumps({'events':[event(r['ref'],'合成材料','statement',r['text'])],'unclassified':[]},ensure_ascii=False)}
    job=Pipeline(store,Plain()).process('review',source['id'],'statistics')
finally:store.close()
ledger=Ledger(calls)
try:
    ledger.reserve_subscription('stat-call',job['job_id'],{'model':'test-fixture'},'review-account',10)
    ledger.received('stat-call',{'usage':{'input_tokens':123,'output_tokens':7}},0,0.1)
finally:ledger.close()
with contextlib.redirect_stdout(io.StringIO()):
    code=main(['--db',str(db),'--ledger',str(calls),'--owner','review','evaluate','--source-id',source['id'],'--out',str(report)])
assert code==0
data=json.loads(report.read_text(encoding='utf-8'))
result['findings']['cli_ignores_explicit_evaluation_ledger']={'requested_ledger':str(calls),'expected_calls':1,'expected_input_tokens':123,'actual_model_runs':data['model_runs']}
result['expected_issues_reproduced']=all([result['findings']['denial_claim_positive_summary']['incorrect_output_accepted'],result['findings']['statement_omits_negation']['incorrect_output_accepted'],result['findings']['default_batch_boundary_loses_negation']['published_decision'],result['findings']['cli_ignores_explicit_evaluation_ledger']['actual_model_runs']['available'] is False])
result['product_acceptance']='not_passed_known_issues'
(BASE/'evidence.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False,indent=2))
