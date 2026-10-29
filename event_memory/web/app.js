"use strict";
const token=document.querySelector('meta[name="local-token"]').content;
const $=id=>document.getElementById(id);
const selected=new Set();
let offset=0,currentEvents=[];
const kinds={
  proposal:"提议",decision:"决定表达",denial:"否定",condition:"条件",cancellation:"取消",claimed_completion:"声称完成",statement:"陈述"
}
;
function node(tag,text,cls){
  const e=document.createElement(tag);
  if(text!==undefined)e.textContent=text;
  if(cls)e.className=cls;
  return e;
}
function message(text,error=false){
  $("message").textContent=text;
  $("message").classList.toggle("error",error);
}
async function api(path,body){
  const response=await fetch(path,{
    method:body===undefined?"GET":"POST",headers:{
      "X-Local-Token":token,"Content-Type":"application/json"
    }
    ,body:body===undefined?undefined:JSON.stringify(body)
  }
  );
  const value=await response.json();
  if(!response.ok){const error=new Error(value.message||"本地请求失败");error.status=response.status;error.code=value.error;throw error;}
  return value;
}
const operations=LocalOperations.create({api,storage:localStorage,randomUUID:()=>crypto.randomUUID(),namespace:document.querySelector('meta[name="local-store"]').content});
function action(label,fn,secondary=false){
  const b=node("button",label,secondary?"secondary":"");
  b.addEventListener("click",async()=>{
    b.disabled=true;
    try{
      await fn();
    }
    catch(e){
      message(e.message,true);
    }
    finally{
      b.disabled=false;
    }
  }
  );
  return b;
}
function time(ms){
  return new Date(ms).toISOString().slice(11,23);
}
function updateSelected(){
  $("selected").textContent=`已选 ${selected.size} 条`
}
async function loadEvents(){
  const query=new URLSearchParams({
    q:$("query").value,limit:"20",offset:String(offset)
  }
  );
  if($("topic").value)query.set("topic",$("topic").value);
  currentEvents=await api("/api/events?"+query);
  $("events").replaceChildren();
  $("count").textContent=`本页 ${currentEvents.length} 条`;
  for(const event of currentEvents){
    const card=node("article",undefined,"event");
    const row=node("div",undefined,"row");
    const checkbox=node("input");
    checkbox.type="checkbox";
    checkbox.checked=selected.has(event.id);
    checkbox.setAttribute("aria-label","选择经历："+event.title);
    checkbox.addEventListener("change",()=>{
      if(checkbox.checked)selected.add(event.id);
      else selected.delete(event.id);
      updateSelected();
    }
    );
    row.append(checkbox,node("h3",event.title));
    card.append(row,node("p",event.summary,"summary"));
    const tags=node("div",undefined,"tags");
    for(const topic of event.topics)tags.append(node("span",topic.name,"tag"));
    card.append(tags,node("p",`版本 ${event.version} · 关联修订 ${event.association_revision} · 模型 ${event.model} · 待语义复核`,"muted"),action("查看原文与主张",()=>showDetail(event.id),true));
    $("events").append(card);
  }
  if(!currentEvents.length)$("events").append(node("p","暂无匹配的已完成经历。处理失败和未归类内容请查看处理状态。","muted"));
  $("previous").disabled=offset===0;
  $("next").disabled=currentEvents.length<20;
  $("page").textContent=`第 ${offset/20+1} 页`;
}
async function loadTopics(){
  const value=$("topic").value;
  const topics=await api("/api/topics");
  $("topic").replaceChildren(new Option("全部主题",""));
  for(const t of topics)$("topic").add(new Option(`${t.name} (${t.event_count})`,t.id));
  $("topic").value=value;
}
async function showDetail(id){
  const {
    event,evidence
  }
  =await api("/api/events/"+id);
  const panel=$("detail");
  panel.hidden=false;
  panel.replaceChildren(node("h2",event.title),node("p",event.summary,"summary"),node("p","以下保留说话人的表达，不代表已验证的现实状态。","muted"));
  panel.append(node("h3","有限主张"));
  for(const c of event.claims){
    const item=node("div",undefined,"evidence");
    item.append(node("small",`${kinds[c.kind]||c.kind} · ${c.refs.join("、")}`),node("p",c.quote));
    panel.append(item);
  }
  panel.append(node("h3","纠正主题"));
  const row=node("div",undefined,"row");
  const names=node("input");
  names.value=event.topics.map(t=>t.name).join("，");
  names.setAttribute("aria-label","主题名称，用中文逗号分隔");
  row.append(names,action("保存主题纠正",async()=>{
    await operations.submit(`/api/events/${id}/topics`,"correct_topics",{
      revision:event.association_revision,names:names.value.split(/[，,]/).map(s=>s.trim()).filter(Boolean)
    }
    );
    message("主题已纠正，旧修订不会覆盖新结果。");
    await loadTopics();
    await loadEvents();
    await showDetail(id);
  }
  ));
  panel.append(row);
  if(event.relations?.length){
    panel.append(node("h3","相关经历"));
    const kinds={same_theme:"同主题建议",possible_continuation:"可能延续",unknown:"关系待判断"};
    const statuses={valid:"目标可用，关系仍待核对",stale:"目标版本已过时",unavailable:"目标已不可用"};
    for(const relation of event.relations){
      const entry=node("div",undefined,"evidence");
      entry.append(node("p",`${kinds[relation.kind]||"关系候选"} · ${statuses[relation.status]||"状态未知"}`));
      if(relation.status==="valid"){
        entry.append(action(relation.target_title||"查看相关经历",()=>showDetail(relation.target_id),true));
      }
      panel.append(entry);
    }
  }
  panel.append(node("h3","精确原文"));
  for(const s of evidence){
    const item=node("div",undefined,"evidence");
    item.append(node("small",`${s.ref} · ${time(s.start_ms)}—${time(s.end_ms)} · ${s.speakers.join(" + ")} · 原文第 ${s.line} 行`),node("p",s.text));
    panel.append(item);
  }
  if(event.quality.uncertainties?.length)panel.append(node("p","未知信息："+event.quality.uncertainties.join("；"),"muted"));
  if(event.quality.normalization_warnings?.length){
    const notes=node("details");
    notes.append(node("summary","查看引用与分类复核标记"));
    for(const warning of event.quality.normalization_warnings)notes.append(node("p",warning.reason,"muted"));
    panel.append(notes);
  }
  panel.scrollIntoView({
    behavior:"smooth",block:"start"
  }
  );
}
async function showJob(id){
  const value=await api('/api/jobs/'+id);
  const panel=$('detail');
  panel.hidden=false;
  panel.replaceChildren(node('h2','处理详情与未归类原文'),node('p',`作业 ${value.job.id} · 状态 ${value.job.status} · 已保存 ${value.job.next_batch} 批`));
  for(const item of value.unclassified){
    const block=node('div',undefined,'evidence');
    block.append(node('small',`${item.ref} · ${time(item.evidence.start_ms)} · ${item.evidence.speakers.join(' + ')}`),node('p',item.evidence.text),node('p',item.reason,'muted'));
    panel.append(block);
  }
  if(!value.unclassified.length)panel.append(node('p','当前没有已保存的未归类片段；未完成或失败作业不代表所有内容都已理解。','muted'));
  panel.scrollIntoView({
    behavior:'smooth',block:'start'
  }
  );
}
async function refreshStatus(){
  const [sources,jobs,budget]=await Promise.all([api("/api/sources"),api("/api/jobs"),api("/api/budget")]);
  $("budget").textContent=budget.billing_path==="chatgpt_plan" ? `GPT ${budget.active_model} · 项目请求 ${budget.subscription.calls} 条（${budget.subscription.unknown_calls} 条待核对） · 已知 ${budget.subscription.input_tokens + budget.subscription.output_tokens} 个 token · 历史DS ¥${budget.estimated_spent_cny.toFixed(3)}` : `历史预算 ¥${budget.available_cny.toFixed(3)} · 历史支出 ¥${budget.estimated_spent_cny.toFixed(3)}`;
  $("sources").replaceChildren();
  for(const s of sources){
    const div=node("div",undefined,"source"),row=node("div",undefined,"row");
    row.append(node("strong",s.recording),node("small",({current:"当前候选来源",stale:"已被新修订替代",cleared:"已清除"}[s.state]||"来源状态未知")));
    if(s.state==="current")row.append(action("生成经历 / 恢复",async()=>{
      const result=await api("/api/process",{
        source_id:s.id
      }
      );
      message(result.status==="succeeded" ? "已保留既有成功版本（模型："+result.effective_model+"），不会自动用新模型覆盖。" : "GPT 作业已提交："+result.job_id);
      await refreshStatus();
    }
    ));
    div.append(row,node("small",`来源 ${s.id} · 修订 ${s.revision}`));
    $("sources").append(div);
  }
  $("jobs").replaceChildren();
  const states={
    queued:"待处理",running:"处理中",succeeded:"处理完成",failed:"处理失败",unknown:"调用结果待核对",cancelled:"已取消"
  }
  ;
  for(const j of jobs){
    const div=node("div",undefined,"job"),row=node("div",undefined,"row");
    row.append(node("span",`${states[j.status]||j.status} · 已保存 ${j.next_batch} 批`),action("查看处理详情",()=>showJob(j.id),true));
    if(j.status==="succeeded")row.append(action("复核高信息未归类",async()=>{await api(`/api/jobs/${j.id}/refine`,{});message("已提交未归类复核；没有高信息候选时不会调用模型。");await refreshStatus();},true));
if(["queued","running","unknown","failed"].includes(j.status))row.append(action("取消作业",async()=>{
      await api(`/api/jobs/${j.id}/cancel`,{
      }
      );
      await refreshStatus();
    }
    ,true));
    div.append(row,node("small",j.id+(j.error?" · "+({invalid_output:"模型结果未通过校验",budget_exceeded:"剩余预算不足",unknown_call:"调用结果待核对",cleared:"来源已清除",request_conflict:"原请求身份冲突",local_processing_error:"本地处理异常",request_not_sent:"连接建立失败，请求尚未发送，可恢复原作业",subscription_rejected:"套餐请求被拒绝，请查看授权或用量",account_changed:"授权账号变化，已停止发送",call_limit:"达到本地作业调用上限",login_required:"需要为本应用登录授权"}[j.error]||"错误代码："+j.error):"")));
    $("jobs").append(div);
  }
}
$("import").addEventListener("click",async()=>{
  try{
    const f=$("file").files[0];
    if(!f)throw new Error("请先选择 UTF-8 转录文档。");
    const result=await api("/api/sources",{
      recording:$("recording").value.trim()||f.name,text:await f.text()
    }
    );
    message("文档已导入；点击生成经历后才会调用模型。");
    await refreshStatus();
  }
  catch(e){
    message(e.message,true);
  }
}
);
$("search").addEventListener("click",()=>{
  offset=0;
  loadEvents().catch(e=>message(e.message,true));
}
);
$("query").addEventListener("keydown",e=>{
  if(e.key==="Enter")$("search").click();
}
);
$("topic").addEventListener("change",()=>$("search").click());
$("previous").addEventListener("click",()=>{
  offset=Math.max(0,offset-20);
  loadEvents().catch(e=>message(e.message,true));
}
);
$("next").addEventListener("click",()=>{
  offset+=20;
  loadEvents().catch(e=>message(e.message,true));
}
);
$("export").addEventListener("click",async()=>{
  try{
    if(!selected.size)throw new Error("请先勾选经历。");
    const purpose=$("purpose").value.trim();
    if(!purpose)throw new Error("请填写本次导出用途。");
    const snapshot=await operations.submit("/api/selections","select",{
      event_ids:[...selected],purpose
    }
    );
    const data=await api("/api/selections/"+snapshot.id);
    const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{
      type:"application/json;charset=utf-8"
    }
    ));
    const link=node("a");
    link.href=url;
    link.download="经历选择-"+snapshot.id+".json";
    link.click();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
    message("已按固定事件版本和原文范围导出，之后新增主题成员不会扩大本次内容。");
  }
  catch(e){
    message(e.message,true);
  }
}
);
async function initial(){
  await refreshStatus();
  await loadTopics();
  await loadEvents();
}
initial().catch(e=>message(e.message,true));
$("check-operation").addEventListener("click",async()=>{
  try{
    const receipt=await operations.recover();
    message(`操作 ${receipt.operation_id}：${receipt.status==="applied"?"已执行":"已清除"}；${receipt.current_state==="valid"?"结果当前有效":receipt.current_state==="changed"?"之后已有新修订":"结果已不可用"}。`);
    if(receipt.kind==="select"&&receipt.current_state==="valid"&&receipt.result){
      const data=await api("/api/selections/"+receipt.result.id);
      const url=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:"application/json;charset=utf-8"}));
      const link=node("a");link.href=url;link.download="经历选择-"+receipt.result.id+".json";link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
    }
    await loadTopics();await loadEvents();
  }catch(error){message(error.message,true);}
});
setInterval(()=>refreshStatus().catch(e=>message(e.message,true)),10000);
