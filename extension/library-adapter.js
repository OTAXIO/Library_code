/* Read-only library resolution. Grounded in the shipped EntryManage component
 * (ItemEdit + ItemDetail refs, getData(page, query), metadata arrays).
 * Reuse the bound import tab's public router; restore its route in finally.
 * No guessed REST endpoints, staging IDs, cookies or authentication headers. */
async function resolveLibraryRecord(command) {
  const fail=text=>{throw new Error(text);};
  const norm=x=>String(x??"").normalize("NFKC").toLowerCase().replace(/\s+/g," ").trim();
  const doi=x=>norm(x).replace(/^https?:\/\/doi\.org\//,"").replace(/^doi:\s*/,"");
  const visible=el=>el&&el.getClientRects().length>0&&getComputedStyle(el).visibility!=="hidden";
  const components=()=>{
    const seen=new Set();
    const visit=vm=>{if(!vm||seen.has(vm))return;seen.add(vm);(vm.$children||[]).forEach(visit);};
    [...document.querySelectorAll('*')].forEach(el=>visit(el.__vue__));return [...seen];
  };
  const one=(items,label)=>{if(items.length!==1)fail(label+"未唯一识别，未写入任何数据");return items[0];};
  const check=()=>{
    const url=new URL(location.href);
    if(!['http:','https:'].includes(url.protocol)||url.hostname!=="admin.ir.lib.sjtu.edu.cn"||url.username||url.password)
      fail("不是机构库后台");
    if(Date.now()>=command.expires-4000)fail("本库只读检索超时，请重新核验");
    if([...document.querySelectorAll('.el-dialog,.el-message-box,.el-drawer')].some(visible))
      fail("本库有人工操作窗口，请先处理，未关闭该窗口");
  };
  const wait=async fn=>{while(Date.now()<command.expires-6000){check();if(fn())return;await new Promise(r=>setTimeout(r,150));}fail("本库查询或页面切换未完成");};
  let router, switched=false;
  try {
    check();
    if(location.hash.split('?')[0]!=="#/collectItem/batchManage")fail("请先返回绑定的导入管理页");
    const c=command.candidate;
    if(command.action!=="import_resolve"||!c||typeof c.title!=="string"||!c.title||
       !/^WOS:\d{15}$/.test(c.wos)||typeof c.doi!=="string"||c.sjtu!==true||
       !/^[a-f0-9]{64}$/.test(c.sha256))fail("缺少真实 TXT 文献证据");
    const origin=one(components().filter(v=>v.$options?.name==='BatchManage'&&v.$refs?.wosDataText&&v.$refs?.batchPushModal),'批次管理组件');
    if(Object.values(origin.$refs).some(v=>v?.drawer||v?.dialogVisible))fail("存在上传或推送窗口，不能切换检索页");
    router=origin.$router;if(typeof router?.push!=="function")fail("本库页面路由结构不兼容");
    switched=true;await router.push('/item/entryManage');
    await wait(()=>location.hash.split('?')[0]==='#/item/entryManage'&&
      components().some(v=>v.$options?.name==='EntryManage'&&v.$refs?.itemDetail&&v.$options?.components?.ItemEdit));
    const vm=one(components().filter(v=>v.$options?.name==='EntryManage'&&v.$refs?.itemDetail&&v.$options?.components?.ItemEdit&&visible(v.$el)),'数据管理查询组件');
    if(!vm.page||typeof vm.getData!=="function"||!Array.isArray(vm.tableData))fail("本库查询结构不兼容");
    await wait(()=>!vm.loading);
    const page={current:1,size:100};
    const read=async key=>{
      const query={containsChild:true,emptyYear:false,...key};
      vm.page={...page};vm.queryForm={...query};vm.queryFormShow=true;
      // The live method catches HTTP errors. Invalidate the old total before
      // querying so its empty error list cannot masquerade as a proven zero.
      vm.total=null;
      const old=vm.tableData;vm.getData({...page},{...query});
      await wait(()=>!vm.loading&&vm.tableData!==old);
      if(vm.total===null||typeof vm.total==='boolean'||!/^\d+$/.test(String(vm.total))||
        !Number.isInteger(Number(vm.total))||Number(vm.total)>100||vm.tableData.length!==Number(vm.total))
        fail("本库查询未确认成功、结果不完整或过多，不能判定缺失");
      return [...vm.tableData];
    };
    // DOI absence alone is not proof of a missing paper: an existing entry
    // can lack DOI. Always cross-check the full title before any new upload.
    const rows=c.doi?await read({doi:c.doi}):[];
    rows.push(...await read({title:c.title}));
    const scalar=(meta,key)=>Array.isArray(meta?.[key])&&meta[key].length===1&&typeof meta[key][0]==='string'?meta[key][0]:'';
    const items=[];
    const seen=new Set();
    for(const row of rows){
      const title=scalar(row.metadata,'title'), foundDoi=doi(scalar(row.metadata,'doi'));
      const ut=scalar(row.metadata,'wosId').trim().toUpperCase().replace(/^WOS:/,'');
      // A matching query alone is not proof. A contradictory same-ID record
      // makes automatic resolution unsafe, not an excuse to upload a duplicate.
      const sameId=ut===c.wos.slice(4)||Boolean(c.doi&&foundDoi===c.doi);
      if(sameId&&(norm(title)!==norm(c.title)||(c.doi&&foundDoi!==c.doi)||ut!==c.wos.slice(4)))
        fail("本库相同标识的记录与 TXT 存在冲突，禁止重复导入");
      if(norm(title)===norm(c.title)&&!sameId)fail("本库同题名记录标识不同或缺失，需人工查重");
      if(sameId){
        if(typeof row.id!=='string'||!/^\d{1,40}$/.test(row.id))fail("平台唯一号不是精确数字文本");
        if(seen.has(row.id))continue;
        seen.add(row.id);
        items.push({id:row.id,title,doi:foundDoi,wos:'WOS:'+ut});
      }
    }
    return {ok:true,data:{verified:true,items}};
  }catch(error){return {ok:false,error:'[本库只读核验暂停] '+error.message};}
  finally {
    if(switched&&router){
      try {await router.push('/collectItem/batchManage');await wait(()=>location.hash.split('?')[0]==='#/collectItem/batchManage');}
      catch {return {ok:false,error:'[本库只读核验暂停] 未能返回导入管理页，请人工返回；未自动上传'};}
    }
  }
}
if(typeof module!=="undefined")module.exports={resolveLibraryRecord};
