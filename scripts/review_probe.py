"""独立复现评审原反例；只用合成数据、模拟模型和本机HTTP，无真实推理。"""
import hashlib,json,sys,tempfile,threading,re,urllib.request,urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

# 可显式选择解压后的代码根目录，避免误加载开发机已安装的另一份包。
ROOT=Path(sys.argv[1]).resolve() if len(sys.argv)>1 else Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import event_memory
from event_memory.errors import MemoryError,InvalidOutput
from event_memory.store import Store
from event_memory.transcript import parse_transcript,build_batches
from event_memory.validation import validate_output
from event_memory.pipeline import Pipeline
from event_memory.context import expand_reading
from event_memory.ledger import Ledger
from event_memory.openai_model import ChatGPTModel
from event_memory.server import create_server

def raw(text):return '[10:00:00.000 – 10:00:02.000] SPK_1: '+text+'\n'
def output(batch,summary=None):
    rows=batch['primary']
    return {'events':[{'title':'合成讨论','summary':summary or '合成讨论',
        'evidence_refs':[r['ref'] for r in rows],
        'claims':[{'kind':'statement','quote':r['text'],'refs':[r['ref']]} for r in rows],
        'topics':[],'relations':[],'continuation_of':None,'uncertainties':[]}],'unclassified':[]}

class Model:
    def __init__(self,ledger=None,hook=None):
        self.calls=0;self.hook=hook
        if ledger is not None:self.ledger=ledger
    def generate(self,call_id,job_id,messages):
        self.calls+=1
        batch=expand_reading(json.loads(messages[1]['content']))
        history=batch['candidates']['events']
        summary='甲来源独有标记_A123' if not history else '引用历史：'+history[0]['summary']
        if hasattr(self,'ledger'):
            self.ledger.reserve_subscription(call_id,job_id,{'model':'test-fixture','messages':messages},'probe',128)
        if self.hook:self.hook()
        response={'content':json.dumps(output(batch,summary),ensure_ascii=False)}
        if hasattr(self,'ledger'):self.ledger.received(call_id,{**response,'usage':{'input_tokens':10,'output_tokens':5}},0,0)
        return response

result={'loaded_module':str(Path(event_memory.__file__).resolve()),'root':str(ROOT),'new_live_model_calls':0,'checks':{}}
result['python_executable']=sys.executable
manifest=ROOT/'release-manifest.json'
if manifest.exists():
    saved=json.loads(manifest.read_text(encoding='utf-8'))
    hashes=saved.get('files',saved.get('file_sha256',{}))
    result['package_commit']=saved.get('code_commit')
    result['package_code_matches_manifest']=all((ROOT/name).is_file() and hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==sha for name,sha in hashes.items() if name.startswith('event_memory/'))
text='\n'.join(f'[09:00:0{i}.000 – 09:00:0{i+1}.000] SPK_1: {word}' for i,word in enumerate(['我们','没有','决定采用net2。']))
batch=build_batches(parse_transcript(text).segments)[0];value=output(batch)
value['events'][0].update(summary='我们决定采用net2。',evidence_refs=['U000001','U000003'],claims=[{'kind':'decision','quote':'我们决定采用net2。','refs':['U000001','U000003']}])
try:validate_output(value,batch,{'events':[],'topics':[]});accepted=True
except InvalidOutput:accepted=False
result['checks']['1_skip_negation']={'passed':not accepted,'false_claim_accepted':accepted}
value['events'][0].update(evidence_refs=['U000003'],claims=[{'kind':'decision','quote':'决定采用net2。','refs':['U000003']}])
try:validate_output(value,batch,{'events':[],'topics':[]});accepted=True
except InvalidOutput:accepted=False
result['checks']['1b_single_fragment_negation']={'passed':not accepted,'false_claim_accepted':accepted}
store=Store(':memory:');model=Model()
try:
    try:
        source=store.import_source('probe','没有时间标签的普通正文。','bad')
        status=Pipeline(store,model).process('probe',source['id'],'bad')['status']
    except MemoryError as exc:status=exc.code
    result['checks']['3_plain_body']={'passed':status!='succeeded','status':status,'model_calls':model.calls}
finally:store.close()

store=Store(':memory:');ledger=Ledger(':memory:')
try:
    a=store.import_source('probe',raw('甲来源独有标记_A123'),'A')
    a_job=Pipeline(store,Model(ledger)).process('probe',a['id'],'A')['job_id']
    def clear():
        store.clear_recording('probe','A')
        ledger.purge_jobs([a_job]) # 兼容旧CLI清除路径；新版依赖清除队列由绑定账本继续消费。
    b=store.import_source('probe',raw('乙来源新话'),'B')
    try:status=Pipeline(store,Model(ledger,clear)).process('probe',b['id'],'B')['status']
    except MemoryError as exc:status=exc.code
    exposed=[dict(r) for r in ledger.db.execute('SELECT job_id,request_payload,response FROM calls WHERE job_id<>?',(a_job,))]
    leak=any('甲来源独有标记_A123' in ((r['request_payload'] or '')+(r['response'] or '')) for r in exposed)
    result['checks']['2_clear_during_generation']={'passed':status!='succeeded' and not leak,'status':status,'dependent_payload_retained':leak}
finally:ledger.close();store.close()

ledger=Ledger(':memory:')
try:
    config=SimpleNamespace(model='gpt-6.1-sol',principal_hash='probe')
    model=ChatGPTModel(config,ledger)
    original=[{'role':'user','content':'原请求'}]
    ledger.reserve_subscription('saved','job',model.payload(config.model,original),'probe',128)
    ledger.received('saved',{'status':'completed','output':[{'type':'message','content':[{'type':'output_text','text':'原结果'}]}]},0,0)
    with patch('event_memory.openai_model.authorized_profile',side_effect=AssertionError('不得真实授权')):
        try:model.generate('saved','job',[{'role':'user','content':'改变后的请求'}]);status='reused_wrong_response'
        except MemoryError as exc:status=exc.code
    result['checks']['4_changed_request']={'passed':status=='request_conflict','status':status}
finally:ledger.close()

work=ROOT/'.runtime/review-probe';work.mkdir(parents=True,exist_ok=True)
with tempfile.TemporaryDirectory(prefix='review-http-',dir=work) as directory:
    db=Path(directory)/'memory.sqlite';store=Store(db)
    source=store.import_source('local',raw('合成材料'),'http')
    Pipeline(store,Model()).process('local',source['id'],'http');event=store.list_events('local')[0];store.close()
    server=create_server(db,port=0,ledger_path=Path(directory)/'ledger.sqlite')
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start();base=f'http://127.0.0.1:{server.server_port}'
    try:
        html=urllib.request.urlopen(base).read().decode();token=re.search(r'name="local-token" content="([^"]+)"',html)[1]
        def request(path,body=None):
            req=urllib.request.Request(base+path,data=None if body is None else json.dumps(body).encode(),headers={'X-Local-Token':token,'Content-Type':'application/json'})
            try:
                with urllib.request.urlopen(req) as r:return r.status,json.load(r)
            except urllib.error.HTTPError as exc:return exc.code,json.load(exc)
        path='/api/events/'+event['id']+'/topics';body={'operation_id':'lost','revision':1,'names':['纠正主题']}
        first,_=request(path,body);repeat,replay=request(path,body);receipt,_=request('/api/operations/lost')
        sel={'operation_id':'selection','event_ids':[event['id']],'purpose':'合成导出'}
        _,one=request('/api/selections',sel);_,two=request('/api/selections',sel)
        result['checks']['5_http_receipts']={'passed':first==200 and repeat==200 and receipt==200 and one.get('id')==two.get('id'),
            'first_http':first,'repeat_http':repeat,'receipt_http':receipt,'same_selection_id':one.get('id')==two.get('id')}
    finally:server.shutdown();server.server_close();thread.join()
result['all_original_cases_passed']=all(v['passed'] for key,v in result['checks'].items() if key!='1b_single_fragment_negation')
result['all_checks_passed']=all(v['passed'] for v in result['checks'].values())
print(json.dumps(result,ensure_ascii=False,indent=2))
