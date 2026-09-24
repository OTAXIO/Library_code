/* Called only by the authenticated bridge dispatcher, never by webpage messages. */
const workflowRole = action => action.startsWith("import_") ? "importTabId" : action.startsWith("wos_") ? "wosTabId" : null;
const validRolePage = (url,role) => {
  try {const u=new URL(url);
    return role==="wosTabId" ? u.origin==="https://www.webofscience.com" && u.pathname.startsWith("/wos/") :
      role==="importTabId" && ["http:","https:"].includes(u.protocol) && u.hostname==="admin.ir.lib.sjtu.edu.cn" &&
      u.hash.split("?")[0]==="#/collectItem/batchManage";
  } catch {return false;}
};
async function dispatchWorkflow(command,pair) {
  if(!Number.isFinite(command.expires) || Date.now()>=command.expires-2500)throw new Error("工作命令已过期，未执行");
  const role=workflowRole(command.action), id=pair[role];
  if(!Number.isInteger(id) || id===pair.tabId)throw new Error("请在扩展中分别绑定 WOS 与“数据导入与批次管理”标签页");
  let tab=await chrome.tabs.get(id);
  if(!validRolePage(tab.url,role))throw new Error("绑定的工作标签页已切换或未登录，请人工返回");
  const execute=async(fn,cmd)=>{
    const current=await chrome.tabs.get(id);
    if(!validRolePage(current.url,role))throw new Error("工作标签页目标发生变化");
    const results=await chrome.scripting.executeScript({target:{tabId:id},world:"MAIN",func:fn,args:[cmd]});
    const result=results[0]?.result;
    if(!result)throw new Error("工作页面没有返回结果");
    return result;
  };
  if(role==="importTabId") {
    if(command.action==="import_upload"){
      if(typeof command.content!=="string" || command.content.length>700000)throw new Error("TXT 文件大小异常");
      const bytes=Uint8Array.from(atob(command.content),c=>c.charCodeAt(0));
      const hash=[...new Uint8Array(await crypto.subtle.digest("SHA-256",bytes))].map(x=>x.toString(16).padStart(2,"0")).join("");
      if(hash!==command.candidate?.sha256)throw new Error("TXT 文件哈希不一致，拒绝上传");
      command={...command,contentSha:hash};
    }
    return execute(runImportCommand,command);
  }
  if(command.action==="wos_search") {
    // User explicitly binds a disposable working WOS tab. Never navigate SA tab.
    if(!/^\/wos\/woscc\/(?:basic-search|advanced-search|fielded-search)\/?$/.test(new URL(tab.url).pathname)){
      await chrome.tabs.update(id,{url:"https://www.webofscience.com/wos/woscc/basic-search"});
      const end=Math.min(Date.now()+25000,command.expires-10000);
      while(Date.now()<end){
        tab=await chrome.tabs.get(id);
        if(tab.status==="complete" && new URL(tab.url).pathname.endsWith("/woscc/basic-search"))break;
        await new Promise(r=>setTimeout(r,200));
      }
    }
    return execute(runWOSCommand,command);
  }
  if(command.action!=="wos_export")throw new Error("未知 WOS 调度命令");
  const prepared=await execute(runWOSCommand,{...command,action:"wos_prepare_export"});
  if(!prepared.ok)return prepared;
  const recordURL=prepared.data.record_url;
  // Listen only during this single export. Download origin/referrer must bind it
  // to this WOS record; never pick the newest arbitrary file from Downloads.
  const found=[];
  const listener=item=>{
    const ref=String(item.referrer||"").split("?")[0];
    const blob=String(item.url||"").startsWith("blob:https://www.webofscience.com/");
    if(ref===recordURL || (blob && !ref))found.push(item.id);
  };
  chrome.downloads.onCreated.addListener(listener);
  try {
    const clicked=await execute(runWOSCommand,{...command,action:"wos_download"});
    if(!clicked.ok)return clicked;
    const end=Math.min(Date.now()+35000,command.expires-3000);
    let completed;
    while(Date.now()<end){
      if(found.length>1)throw new Error("导出期间出现多个候选下载，请人工核验，未上传任何文件");
      if(found.length===1){
        const [item]=await chrome.downloads.search({id:found[0]});
        if(item?.state==="interrupted")throw new Error("WOS 下载中断，请人工处理");
        if(item?.state==="complete") {completed=item;break;}
      }
      await new Promise(r=>setTimeout(r,200));
    }
    if(!completed || !/\.txt$/i.test(completed.filename) || completed.fileSize>524288 || completed.fileSize<=0)
      throw new Error("未取得可确认的单篇 TXT 下载。请检查 WOS 导出/浏览器下载提示");
    return {ok:true,data:{path:completed.filename,download_id:completed.id,sa_id:command.sa_id,record_url:recordURL}};
  } finally {chrome.downloads.onCreated.removeListener(listener);}
}
