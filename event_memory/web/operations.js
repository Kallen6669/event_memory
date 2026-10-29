"use strict";
// 操作结果未知时保存原编号；只保存本机界面操作，不保存模型账户凭据。
globalThis.LocalOperations={create({api,storage,randomUUID,namespace}){
  const prefix=namespace?"event-memory."+namespace:"event-memory";
  const key=prefix+".pending-operation",lastKey=prefix+".last-operation";
  const read=()=>JSON.parse(storage.getItem(key)||"null");
  const remember=value=>{storage.setItem(key,JSON.stringify(value));storage.setItem(lastKey,value.operation_id);};
  const finish=()=>storage.removeItem(key);
  async function recover(){
    const pending=read(),id=pending?.operation_id||storage.getItem(lastKey);
    if(!id)throw new Error("暂无可核对的操作。");
    const receipt=await api("/api/operations/"+encodeURIComponent(id));
    if(receipt.status==="applied"||receipt.status==="cleared")finish();
    return receipt;
  }
  return {recover,async submit(path,kind,body){
    const existing=read();
    if(existing&&(existing.path!==path||existing.kind!==kind||JSON.stringify(existing.body)!==JSON.stringify(body)))
      throw new Error("上次操作尚待核对，请先点击核对上次操作，避免将未知结果当作未执行。");
    const pending=existing||{path,kind,body,operation_id:randomUUID()};
    remember(pending);
    try{
      const result=await api(path,{...body,operation_id:pending.operation_id});
      finish();return result;
    }catch(error){
      if(error.code==="operation_conflict")throw error;
      try{
        const receipt=await recover();
        if(receipt.kind===kind&&receipt.status==="applied"&&receipt.result)
          return {...receipt.result,operation_id:receipt.operation_id,
                  receipt:{status:receipt.status,current_state:receipt.current_state}};
        throw new Error("原操作有回执，但当前结果已失效或清除；请重新读取当前数据。");
      }catch(checkError){
        if(checkError.status===404&&error.status>=400&&error.status<500){finish();throw error;}
        if(checkError.status===404||!checkError.status&&read())
          throw new Error("操作结果待核对，编号："+pending.operation_id+"。请保留原编号并核对；不会自动换号重做。");
        throw checkError;
      }
    }
  }};
}};
