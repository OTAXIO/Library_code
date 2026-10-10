/* Called only by the authenticated bridge dispatcher, never by webpage messages. */
const workflowRole = action => action.startsWith("import_") ? "importTabId" : action.startsWith("wos_") ? "wosTabId" : null;
const wosOrigins = ["https://www.webofscience.com", "https://webofscience.clarivate.cn"];
const staleWOSZeroError = "[WOS 已暂停] WOS 检索页保留上一条零结果，需刷新检索页";
const shouldReloadWOSSearch = result => result?.ok === false && result.error === staleWOSZeroError;
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
const decodedWOSPath = url => {
  try {const u=new URL(url);return /%(?:2f|5c)/i.test(u.pathname)?"":decodeURIComponent(u.pathname);}
  catch {return "";}
};
const isWOSRecordPage = (url,origin) => {
  try {const u=new URL(url);return isWOSPage(url) && u.origin===origin && /^\/wos\/woscc\/full-record\/WOS:\d{15}\/?$/.test(decodedWOSPath(url));}
  catch {return false;}
};
const transientInjectionError = error => /(?:execution context (?:was )?destroyed|frame (?:with ID \d+ )?(?:was removed|is not ready)|no frame with id|cannot find context with specified id|WOS 只读探针在页面切换期间未返回结果|WOS 只读探针响应超时)/i.test(String(error?.message||error));
async function boundedWOSProbe(promise,expires) {
  // Only reads may time out and be observed again. Never put Search/export or
  // import submissions in this race: their late outcomes must not be retried.
  let timer;
  const budget=Math.max(1,Math.min(8000,expires-Date.now()-12000));
  try {
    return await Promise.race([promise,new Promise((_,reject)=>{
      timer=setTimeout(()=>reject(new Error('WOS 只读探针响应超时')),budget);
    })]);
  } finally {if(typeof clearTimeout==='function')clearTimeout(timer);}
}
function wosSearchTimeout(diagnostic) {
  // Never echo arbitrary page text, query strings, titles or target URLs in an
  // error. These few typed fields also help distinguish a parser gap from WOS
  // still being on the search form or showing a progress indicator.
  const count=Number.isInteger(diagnostic?.result_total)&&diagnostic.result_total>=0&&diagnostic.result_total<=999999?
    String(diagnostic.result_total):"未确认";
  const links=Number.isInteger(diagnostic?.canonical_record_link_count)&&diagnostic.canonical_record_link_count>=0&&diagnostic.canonical_record_link_count<=999999?
    String(diagnostic.canonical_record_link_count):"未确认";
  const page=diagnostic?.record_route===true?"单篇页":diagnostic?.summary_route===true?"结果页":"尚未识别结果页";
  const busy=diagnostic?.busy===true?"是":"否";
  return `[WOS 已暂停] WOS 检索结果超时（${page}；文献总数 ${count}；安全单篇链接 ${links}；加载中 ${busy}），未自动重复 Search；请点击扩展“查看连接诊断”核对结果诊断后再继续`;
}
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
    const recordPath=!/%(?:2f|5c)/i.test(record.pathname)&&isWOSPage(recordURL)?decodeURIComponent(record.pathname):"";
    if(!/^\/wos\/woscc\/full-record\/WOS:\d{15}\/?$/.test(recordPath))return false;
    // Empty referrers are allowed only for a blob created on the bound origin.
    // Another supported WOS domain must not supply this task's download.
    if(download.protocol==="blob:") {if(download.origin!==record.origin)return false;}
    else if(download.protocol!=="https:" || download.username || download.password)return false;
    if(item.referrer) {
      const ref=new URL(item.referrer);
      const refPath=/%(?:2f|5c)/i.test(ref.pathname)?"":decodeURIComponent(ref.pathname);
      return !ref.username && !ref.password && ref.origin===record.origin && refPath===recordPath;
    }
    return download.protocol==="blob:";
  } catch {return false;}
}
async function dispatchWorkflow(command,pair) {
  if(!Number.isFinite(command.expires) || Date.now()>=command.expires-2500)throw new Error("工作命令已过期，未执行");
  const role=workflowRole(command.action), id=pair[role];
  if(!Number.isInteger(id) || (id===pair.tabId && !(role==="wosTabId" && pair.mode==="wos")))
    throw new Error(role==="wosTabId"?"请连接或绑定 WOS 标签页":"请绑定数据导入与批次管理标签页");
  let tab=await chrome.tabs.get(id);
  if(!validRolePage(tab.url,role))throw new Error("绑定的工作标签页已切换或未登录，请人工返回");
  const workOrigin=new URL(tab.url).origin;
  // Operate on the visible bound work tab so site timers/async UI transitions
  // are not throttled. Diagnostics remain read-only and do not switch tabs.
  if(command.action!=="wos_diagnose" && tab.active===false)
    await chrome.tabs.update(id,{active:true});
  const execute=async(fn,cmd)=>{
    const current=await chrome.tabs.get(id);
    if(!validRolePage(current.url,role))throw new Error("工作标签页目标发生变化");
    if(role==="wosTabId" && new URL(current.url).origin!==workOrigin)throw new Error("WOS 域名在执行中发生变化，请核验页面后重新绑定");
    // A visible WOS SPA can still have a loading tab because other resources
    // have not finished. Do not defer its semantic DOM checks to document_idle.
    // The adapter itself waits for the required controls. Backend import timing
    // remains unchanged. See Chrome's ScriptInjection.injectImmediately API.
    // Result probes are read-only. Use the same isolated JS environment as
    // inspectWorkPage so the site's overridden globals/DOM prototypes cannot
    // make a visible record readable in diagnostics but absent during a run.
    // Search/export/import interactions retain their existing MAIN environment.
    const readOnly=["wos_read_results","wos_diagnose"].includes(cmd.action);
    const world=readOnly?"ISOLATED":"MAIN";
    const pending=chrome.scripting.executeScript({target:{tabId:id},world,func:fn,args:[cmd],
      ...(role==="wosTabId"?{injectImmediately:true}:{})});
    const results=readOnly?await boundedWOSProbe(pending,cmd.expires):await pending;
    const result=results[0]?.result;
    if(!result)throw new Error(readOnly?"WOS 只读探针在页面切换期间未返回结果":"工作页面没有返回结果");
    return result;
  };
  if(command.action==='wos_diagnose'){
    // Capability/access snapshot only. Smart Search may have navigation but no
    // input, and a background tab may not hydrate until activated. Preparation
    // belongs to the activated search command, never this read-only handshake.
    const observed=await execute(inspectWorkPage,command);
    if(observed.error)throw new Error(observed.error);
    const flags=['core_search_route','wos_error','site_timeout','login_required','busy'];
    const counts=['query_input_count','dialog_count'];
    if(flags.some(key=>typeof observed[key]!=="boolean") ||
       counts.some(key=>!Number.isInteger(observed[key])||observed[key]<0||observed[key]>99999))
      throw new Error('WOS 页面就绪检查未返回完整结果；本轮未提交检索');
    const page=Object.fromEntries([...flags,...counts].map(key=>[key,observed[key]]));
    if(['login','verification',''].includes(observed.access_gate))page.access_gate=observed.access_gate;
    return {ok:true,data:{extension_version:chrome.runtime.getManifest().version,
      wos_download_protocol:1,search_prepare_protocol:1,result_reader:'shared-diagnostic',read_results_world:'ISOLATED',page}};
  }
  if(role==="importTabId") {
    if(command.action==='import_resolve')return execute(resolveLibraryRecord,command);
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
    // A forced reset happens only when the adapter found the previous query's
    // no-result banner BEFORE clicking Search, so the real query remains at-most-once.
    const ensureSearchPage=async force=>{
      tab=await chrome.tabs.get(id);
      const path=new URL(tab.url).pathname;
      const alreadySearch=/^\/wos\/woscc\/(?:basic-search|advanced-search|fielded-search)\/?$/.test(path);
      if(!force&&alreadySearch)return;
      if(force&&new URL(tab.url).origin===workOrigin&&path==="/wos/woscc/basic-search"){
        await chrome.tabs.reload(id);
      } else await chrome.tabs.update(id,{url:workOrigin+"/wos/woscc/basic-search"});
      const end=Math.min(Date.now()+25000,command.expires-12000);
      let ready=false;
      while(Date.now()<end){
        tab=await chrome.tabs.get(id);
        if(tab.url) {
          if(new URL(tab.url).origin!==workOrigin)throw new Error("WOS 跳转到了其他域名，请完成机构访问后重新绑定，未继续检索");
          if(validRolePage(tab.url,role) && new URL(tab.url).pathname.endsWith("/woscc/basic-search")){
            if(!force){ready=true;break;}
            // A reload keeps the same URL and may return while the previous
            // document/SPA is still mounted. Wait on semantic, read-only DOM
            // evidence, not a fixed delay or tabs.status. No Search has been
            // submitted for this paper, so only observation is retried here.
            let observed;
            try {observed=await execute(inspectWorkPage,{...command,action:"wos_diagnose"});}
            catch(error){
              if(transientInjectionError(error)){await new Promise(r=>setTimeout(r,200));continue;}
              throw error;
            }
            if(observed.error)throw new Error(observed.error);
            if(['core_search_route','zero_result','busy','wos_error','site_timeout','login_required']
                .some(key=>typeof observed[key]!=="boolean") ||
               ['query_input_count','dialog_count'].some(key=>!Number.isInteger(observed[key])||observed[key]<0||observed[key]>99999))
              throw new Error("WOS 刷新诊断不完整，未提交当前论文检索");
            if(observed.wos_error)throw new Error("WOS 刷新后网站报错，请恢复检索页；未提交当前论文检索");
            if(observed.site_timeout)throw new Error("WOS 刷新后返回 5xx/连接超时页；未提交当前论文检索");
            if(observed.login_required)throw new Error("WOS 刷新后需登录或人工验证；未提交当前论文检索");
            if(observed.dialog_count)throw new Error("WOS 刷新后有操作弹窗，请人工处理；未提交当前论文检索");
            if(observed.core_search_route && !observed.zero_result && !observed.busy){
              ready=true;break;
            }
          }
        }
        await new Promise(r=>setTimeout(r,200));
      }
      if(!ready)throw new Error(force?"WOS 刷新后的检索页尚未就绪，上一条提示未清除或输入区仍在加载；未提交当前论文检索，请核验页面后继续":
        "WOS 文献检索页未加载完成，请核验登录/页面后再继续");
    };
    await ensureSearchPage(false);
    // Navigation/hydration is a resumable phase with no Search or query writes.
    // Only known navigation controls are followed and each route/control is
    // clicked once. The sole final submit below is NEVER retried on uncertainty.
    const prepareEnd=Math.min(Date.now()+45000,command.expires-12000);
    const attempted=new Set();let prepared=false,reset=false;
    while(Date.now()<prepareEnd){
      let preparation;
      try {preparation=await execute(runWOSCommand,{...command,action:'wos_prepare_search',attempted_navigation:[...attempted]});}
      catch(error){
        if(transientInjectionError(error)||String(error?.message||error)==='工作页面没有返回结果'){
          await new Promise(r=>setTimeout(r,200));continue;
        }
        throw error;
      }
      if(!preparation.ok)return preparation;
      const state=preparation.data?.state;
      if(state==='ready'){prepared=true;break;}
      if(state==='stale_zero'){
        if(reset)throw new Error('WOS 刷新后仍保留旧零结果；未提交当前论文检索');
        reset=true;await ensureSearchPage(true);attempted.clear();continue;
      }
      if(state==='navigating'){
        const key=preparation.data?.navigation_key;
        if(typeof key!=='string'||!/^\/wos\/woscc\/(?:basic-search|advanced-search|fielded-search)\/?\|(?:advanced|fielded)$/.test(key))
          throw new Error('WOS 返回未知准备导航；未提交检索');
        if(attempted.has(key))throw new Error('WOS 准备导航发生重复；未提交检索');
        attempted.add(key);
      }else if(state!=='loading')throw new Error('WOS 返回未知检索准备状态；未提交检索');
      await new Promise(r=>setTimeout(r,200));
    }
    if(!prepared)throw new Error('WOS 检索页面准备超时：未能确认唯一字段、输入框及检索按钮；尚未提交 Search，名单保持原样。请核验高级检索/字段检索或登录状态');
    // Search submission is deliberately a short injected step. The adapter
    // schedules exactly one click and returns before a real WOS navigation can
    // destroy the execution context. Results are then observed read-only.
    const result=await execute(runWOSCommand,{...command,action:"wos_start_search",require_prepared:true});
    if(!result.ok)return result;
    if(result.data?.submitted!==true)throw new Error('WOS 未确认 Search 提交回执；未自动重试，请核验当前网页');
    const end=Math.min(Date.now()+90000,command.expires-12000);
    let navigated=false,navigationTarget,lastDiagnostic;
    while(Date.now()<end){
      tab=await chrome.tabs.get(id);
      let url;
      try {url=new URL(tab.url);} catch {throw new Error("WOS 工作页网址无效，已停止整批");}
      if(url.origin!==workOrigin)throw new Error("WOS 域名在检索中发生变化，请核验登录/机构访问后重新绑定");
      if(!isWOSPage(tab.url))throw new Error("WOS 检索跳转离开了已绑定工作区，请人工核验");
      let probe;
      try {probe=await execute(inspectWorkPage,{...command,action:"wos_read_results"});}
      catch(error){
        if(transientInjectionError(error)){await new Promise(r=>setTimeout(r,500));continue;}
        throw error;
      }
      if(!probe.ok)return probe;
      lastDiagnostic=probe.data?.diagnostic;
      const state=probe.data?.state;
      if(state==="zero")return {ok:false,error:"[WOS 已暂停] WOS 未找到记录；这不等于未发表，也不自动标记完成"};
      if(state==="multiple")return {ok:false,error:"[WOS 已暂停] WOS 结果不是可确认的唯一记录，请人工选择并核对后使用“只下载 WOS 当前打开的论文”"};
      if(state==="record"){
        if(!isWOSRecordPage(probe.data.record_url,workOrigin))throw new Error("WOS 单篇记录网址未通过安全校验");
        if(!navigated || decodedWOSPath(probe.data.record_url).replace(/\/$/,'')===decodedWOSPath(navigationTarget).replace(/\/$/,''))return probe;
        // tabs.update resolves before the new document necessarily commits. A
        // read-only probe may still see the previous record; never accept it.
      }
      if(state==="single"){
        const target=probe.data?.navigate_url;
        if(!isWOSRecordPage(target,workOrigin))throw new Error("WOS 唯一结果链接未通过安全校验，未打开");
        if(navigated){
          if(decodedWOSPath(target).replace(/\/$/,'')!==decodedWOSPath(navigationTarget).replace(/\/$/,''))
            throw new Error("WOS 唯一结果在导航期间发生变化，请人工核验");
          // The old list is temporarily still visible. Wait; do not navigate or
          // click again, and do not classify this normal transition as a failure.
        }else{
          navigated=true;navigationTarget=target;
          await chrome.tabs.update(id,{url:target});
        }
      } else if(!["loading","record"].includes(state))throw new Error("WOS 返回了未知检索状态，请人工核验");
      await new Promise(r=>setTimeout(r,500));
    }
    return {ok:false,error:wosSearchTimeout(lastDiagnostic)};
  }
  if(command.action!=="wos_export")throw new Error("未知 WOS 调度命令");
  const prepared=await execute(runWOSCommand,{...command,action:"wos_prepare_export"});
  if(!prepared.ok)return prepared;
  const recordURL=prepared.data.record_url;
  if(!isWOSPage(recordURL) || new URL(recordURL).origin!==workOrigin)throw new Error("WOS 导出记录域名与绑定页面不一致，未开始下载");
  // Listen only during this single export. Download origin/referrer must bind it
  // to this WOS record; never pick the newest arbitrary file from Downloads.
  const found=[];let rejected=0;
  const listener=item=>{
    if(isExpectedWOSDownload(item,recordURL))found.push(item.id);
    else rejected++;
  };
  chrome.downloads.onCreated.addListener(listener);
  try {
    const clicked=await execute(runWOSCommand,{...command,action:"wos_download"});
    if(!clicked.ok)return clicked;
    // Leave the same result-delivery margin used by the page adapter. The
    // authenticated /result POST has its own eight-second timeout.
    const end=Math.min(Date.now()+35000,command.expires-12000);
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
      throw new Error(`未取得可确认的单篇 TXT 下载（本次关联下载 ${found.length} 个；来源校验排除 ${rejected} 个）。请检查 WOS 导出/浏览器下载提示；不会采用无关或历史文件`);
    return {ok:true,data:{path:completed.filename,download_id:completed.id,sa_id:command.sa_id,record_url:recordURL}};
  } finally {chrome.downloads.onCreated.removeListener(listener);}
}
if(typeof module!=="undefined")module.exports={validRolePage,workflowBindingError,isExpectedWOSDownload,
  shouldReloadWOSSearch,isWOSRecordPage,transientInjectionError,wosSearchTimeout,boundedWOSProbe};
