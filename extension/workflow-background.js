/* Called only by the authenticated bridge dispatcher, never by webpage messages. */
const workflowRole = action => action.startsWith("import_") ? "importTabId" : action.startsWith("wos_") ? "wosTabId" : null;
const wosOrigins = ["https://www.webofscience.com", "https://webofscience.clarivate.cn"];
const isWOSPage = url => {
  try {const u=new URL(url);return wosOrigins.includes(u.origin) && !u.username && !u.password && u.pathname.startsWith("/wos/");}
  catch {return false;}
};
const validRolePage = (url,role) => {
  try {const u=new URL(url);
    return role==="wosTabId" ? isWOSPage(url) :
      role==="importTabId" && ["http:","https:"].includes(u.protocol) && u.hostname==="admin.ir.lib.sjtu.edu.cn" &&
      u.hash.split("?")[0]==="#/collectItem/batchManage";
  } catch {return false;}
};
function workflowBindingError(url,role,sameSATab) {
  if(sameSATab)return "当前标签页已用于 SA 比对，请保留它，并在独立标签页打开 WOS 或导入管理页后绑定";
  if(validRolePage(url,role))return "";
  if(role==="wosTabId")return "当前 WOS 网址不受支持或仍在登录入口。请切换到 www.webofscience.com 或 webofscience.clarivate.cn 的 /wos/ 页面，再点击“绑定当前 WOS 页”";
  if(role==="importTabId") {
    try {const u=new URL(url);
      if(u.hostname==="admin.ir.lib.sjtu.edu.cn" && u.hash.split("?")[0]==="#/item/entryManage")
        return "当前是普通“数据管理”页，不是导入页。请点击左侧“数据导入与批次管理”，再绑定当前导入管理页";
    } catch {}
    return "当前不是后台“数据导入与批次管理”页。请在独立后台标签页从左侧菜单进入该页（#/collectItem/batchManage），不要绑定首页或 WOS 页";
  }
  return "未知工作页类型";
}
function isExpectedWOSDownload(item,recordURL) {
  try {
    const record=new URL(recordURL), download=new URL(item.url);
    if(!isWOSPage(recordURL) || !/^\/wos\/woscc\/full-record\/WOS:\d{15}\/?$/.test(decodeURI(record.pathname)))return false;
    // Empty referrers are allowed only for a blob created on the bound origin.
    // Another supported WOS domain must not supply this task's download.
    if(download.protocol==="blob:") {if(download.origin!==record.origin)return false;}
    else if(download.protocol!=="https:" || download.username || download.password)return false;
    if(item.referrer) {
      const ref=new URL(item.referrer);
      return !ref.username && !ref.password && ref.origin===record.origin && ref.pathname===record.pathname;
    }
    return download.protocol==="blob:";
  } catch {return false;}
}
async function dispatchWorkflow(command,pair) {
  if(!Number.isFinite(command.expires) || Date.now()>=command.expires-2500)throw new Error("工作命令已过期，未执行");
  const role=workflowRole(command.action), id=pair[role];
  if(!Number.isInteger(id) || id===pair.tabId)throw new Error("请在扩展中分别绑定 WOS 与“数据导入与批次管理”标签页");
  let tab=await chrome.tabs.get(id);
  if(!validRolePage(tab.url,role))throw new Error("绑定的工作标签页已切换或未登录，请人工返回");
  const workOrigin=new URL(tab.url).origin;
  const execute=async(fn,cmd)=>{
    const current=await chrome.tabs.get(id);
    if(!validRolePage(current.url,role))throw new Error("工作标签页目标发生变化");
    if(role==="wosTabId" && new URL(current.url).origin!==workOrigin)throw new Error("WOS 域名在执行中发生变化，请核验页面后重新绑定");
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
      await chrome.tabs.update(id,{url:workOrigin+"/wos/woscc/basic-search"});
      const end=Math.min(Date.now()+25000,command.expires-10000);
      let ready=false;
      while(Date.now()<end){
        tab=await chrome.tabs.get(id);
        if(tab.status==="complete") {
          if(new URL(tab.url).origin!==workOrigin)throw new Error("WOS 跳转到了其他域名，请完成机构访问后重新绑定，未继续检索");
          if(validRolePage(tab.url,role) && new URL(tab.url).pathname.endsWith("/woscc/basic-search")){ready=true;break;}
        }
        await new Promise(r=>setTimeout(r,200));
      }
      if(!ready)throw new Error("WOS 文献检索页未加载完成，请核验登录/页面后再继续");
    }
    return execute(runWOSCommand,command);
  }
  if(command.action!=="wos_export")throw new Error("未知 WOS 调度命令");
  const prepared=await execute(runWOSCommand,{...command,action:"wos_prepare_export"});
  if(!prepared.ok)return prepared;
  const recordURL=prepared.data.record_url;
  if(!isWOSPage(recordURL) || new URL(recordURL).origin!==workOrigin)throw new Error("WOS 导出记录域名与绑定页面不一致，未开始下载");
  // Listen only during this single export. Download origin/referrer must bind it
  // to this WOS record; never pick the newest arbitrary file from Downloads.
  const found=[];
  const listener=item=>{
    if(isExpectedWOSDownload(item,recordURL))found.push(item.id);
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
if(typeof module!=="undefined")module.exports={validRolePage,workflowBindingError,isExpectedWOSDownload};
