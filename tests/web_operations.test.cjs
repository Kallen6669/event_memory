const {test}=require('node:test');
const assert=require('node:assert/strict');
require('../event_memory/web/operations.js');
function storage(){const map=new Map();return {getItem:k=>map.get(k)||null,setItem:(k,v)=>map.set(k,v),removeItem:k=>map.delete(k)};}
const missing=()=>Object.assign(new Error('未找到'),{status:404});
test('回包丢失后查询原回执，不再次执行',async()=>{
  let writes=0;const store=storage();
  const ops=LocalOperations.create({storage:store,randomUUID:()=> 'op1',api:async(path,body)=>{
    if(body){writes++;throw new TypeError('连接中断');}
    return {operation_id:'op1',kind:'select',status:'applied',current_state:'valid',result:{id:'fixed'}};
  }});
  assert.equal((await ops.submit('/api/selections','select',{purpose:'材料'})).id,'fixed');assert.equal(writes,1);
});
test('404不能证明未知请求没执行，刷新后保留编号并禁止换内容',async()=>{
  const store=storage();let sent=[];
  const api=async(path,body)=>{if(body){sent.push(body.operation_id);throw new TypeError('断网');}throw missing();};
  let ops=LocalOperations.create({storage:store,randomUUID:()=> 'original',api});
  await assert.rejects(()=>ops.submit('/api/selections','select',{purpose:'A'}));
  ops=LocalOperations.create({storage:store,randomUUID:()=> 'must-not-use',api});
  await assert.rejects(()=>ops.submit('/api/selections','select',{purpose:'B'}));
  await assert.rejects(()=>ops.submit('/api/selections','select',{purpose:'A'}));
  assert.deepEqual(sent,['original','original']);
});
test('明确拒绝后显示原错误，不伪报未知',async()=>{
  const error=Object.assign(new Error('修订已变化'),{status:409});
  const ops=LocalOperations.create({storage:storage(),randomUUID:()=> 'known',api:async(p,b)=>{if(b)throw error;throw missing();}});
  await assert.rejects(()=>ops.submit('/topics','correct_topics',{revision:1}),/修订已变化/);
});
test('核对清除回执不复活旧选择',async()=>{
  const store=storage();store.setItem('event-memory.last-operation','cleared');
  const ops=LocalOperations.create({storage:store,randomUUID:()=> 'new',api:async()=>({operation_id:'cleared',status:'cleared',current_state:'unavailable',result:null})});
  assert.equal((await ops.recover()).result,null);
});
test('不同事实库或账号的浏览器操作编号互相隔离',async()=>{
  const store=storage();
  const a=LocalOperations.create({storage:store,namespace:'A',randomUUID:()=> 'a',api:async(p,b)=>{if(b)throw new TypeError('断网');throw missing();}});
  await assert.rejects(()=>a.submit('/api/selections','select',{purpose:'A'}));
  const b=LocalOperations.create({storage:store,namespace:'B',randomUUID:()=> 'b',api:async()=>({id:'b-fixed'})});
  assert.equal((await b.submit('/api/selections','select',{purpose:'B'})).id,'b-fixed');
  assert.notEqual(store.getItem('event-memory.A.pending-operation'),null);
});
