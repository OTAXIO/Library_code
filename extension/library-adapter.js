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
    const primary=c?.kind==='publisher-verified-v1';
    const journal=c?.document_type==='journal-article';
    const urlLabel=journal?'URL':'相关网址',venueLabel=journal?'发表期刊':'会议名称';
    const publisherSource=()=>{
      if(!primary||!/^[a-f0-9]{64}$/.test(c.primary_evidence_sha256??'')||c.fields?.[urlLabel]!==c.primary_url)return false;
      if(journal)return c.model_name==='期刊论文'&&/^10\.\d{4,9}\/\S+$/.test(c.doi)&&(
        c.source==='Nature出版社'&&c.primary_url==='https://www.nature.com/articles/'+c.doi.replace(/^10\.1038\//,'')&&c.doi.startsWith('10.1038/')||
        c.source==='SciOpen出版社'&&String(c.primary_url).toLowerCase()==='https://www.sciopen.com/article/'+c.doi&&c.doi.startsWith('10.26599/'));
      return c.source==='ICLR官方论文集'&&/^https:\/\/proceedings\.iclr\.cc\/paper_files\/paper\/\d{4}\/hash\/[a-f0-9]{32}-Abstract-Conference\.html$/.test(c.primary_url)||
        c.source==='PMLR官方论文集'&&/^https:\/\/proceedings\.mlr\.press\/v\d+\/[a-z0-9]+\.html$/.test(c.primary_url);
    };
    const metadata=command.action==='import_excel_resolve'&&(primary||c?.kind==='crossref-openalex-v1');
    if((command.action!=="import_resolve"&&!metadata)||!c||typeof c.title!=="string"||!norm(c.title)||
       (metadata ? c.wos!==''||(primary?!publisherSource()||c.doi!==''&&!/^10\.\d{4,9}\/\S+$/.test(c.doi):!/^10\.\d{4,9}\/\S+$/.test(c.doi)||c.source!=='Crossref+OpenAlex') : !/^WOS:\d{15}$/.test(c.wos))||typeof c.doi!=="string"||c.sjtu!==true||
       !/^[a-f0-9]{64}$/.test(c.sha256))fail("缺少真实 TXT 文献证据");
    const primaryFields=command.primary_fields;
    if(primary&&(!primaryFields||[urlLabel,'年份',venueLabel].some(label=>
      typeof primaryFields[label]!=='string'||!primaryFields[label]||!c.fields?.[label])||
      new Set(Object.values(primaryFields)).size!==3||!Array.isArray(c.author_names)||!c.author_names.length))
      fail('官方来源原生身份字段未确认，未检索或上传');
    const metadataTitle=value=>{
      if(typeof value!=='string'||value.length>16000)fail('元数据题名格式不兼容');
      const box=document.createElement('textarea');
      box.innerHTML=value.replace(/<[^>]+>/g,'');
      const text=box.value;
      if(/[\x00-\x08\x0b\x0c\x0e-\x1f]/.test(text))fail('元数据题名含无效字符');
      const key=norm(text).replace(/[\u2010-\u2014\u2212]/g,'-')
        .replace(/[\u2018\u2019]/g,"'").replace(/[\u201c\u201d]/g,'"')
        .replace(/\s*-\s*/g,'-').replace(/[ .]+$/g,'');
      if(!key)fail('元数据题名为空');
      return key;
    };
    // Match the public-metadata identity policy: presentation only, never
    // fuzzy titles or changed words/digits. WOS retains its existing policy.
    const titleKey=metadata?metadataTitle:norm;
    const expectedTitle=titleKey(c.title);
    const origin=one(components().filter(v=>v.$options?.name==='BatchManage'&&v.$refs?.wosDataText&&v.$refs?.batchPushModal),'批次管理组件');
    if(Object.values(origin.$refs).some(v=>v?.drawer||v?.dialogVisible))fail("存在上传或推送窗口，不能切换检索页");
    router=origin.$router;if(typeof router?.push!=="function")fail("本库页面路由结构不兼容");
    switched=true;await router.push('/item/entryManage');
    await wait(()=>location.hash.split('?')[0]==='#/item/entryManage'&&
      components().some(v=>v.$options?.name==='EntryManage'&&v.$refs?.itemDetail&&v.$options?.components?.ItemEdit));
    const vm=one(components().filter(v=>v.$options?.name==='EntryManage'&&v.$refs?.itemDetail&&v.$options?.components?.ItemEdit&&visible(v.$el)),'数据管理查询组件');
    const nativeRead=vm.$options?.methods?.getData;
    if(!vm.page||typeof nativeRead!=="function"||!Array.isArray(vm.tableData))fail("本库查询结构不兼容");
    const page={current:1,size:100};
    const read=async key=>{
      const label=key.doi?'DOI':'题名';
      const query={containsChild:true,emptyYear:false,...key};
      vm.page={...page};vm.queryForm={...query};vm.queryFormShow=true;
      // EntryManage.getModels() starts its own asynchronous getData() after
      // navigation. Other initialization methods also change vm.loading.
      // Neither a new visible array nor !vm.loading identifies OUR response:
      // they can even turn an existing paper into a false zero. Invoke the
      // observed unbound native read on a private result holder and await its
      // Promise. Native HTTP/encryption/session handling remains untouched.
      const own={loading:false,total:null,tableData:[]};
      let finished=false,rejected=false,pending;
      try {pending=nativeRead.call(own,{...page},{...query});}
      catch {fail('本库'+label+'查询失败，不能判定缺失');}
      if(!pending||typeof pending.then!=='function')fail('本库原生查询不支持完成确认，未继续上传');
      Promise.resolve(pending).then(()=>{finished=true;},()=>{rejected=true;finished=true;});
      await wait(()=>finished);
      if(rejected)fail('本库'+label+'查询失败，不能判定缺失');
      // The live method catches failed HTTP/business responses itself. A new
      // private holder keeps its null sentinel on that path, independently of
      // unrelated successful requests or stale visible page counts.
      if(own.loading!==false||!['number','string'].includes(typeof own.total)||
        !/^\d+$/.test(String(own.total))||!Number.isSafeInteger(Number(own.total)))
        fail('本库'+label+'查询未确认成功（未返回有效总数），不能判定缺失');
      const total=Number(own.total);
      if(total>page.size)fail('本库'+label+'查询记录过多（'+total+' 条），需人工查重');
      if(!Array.isArray(own.tableData)||own.tableData.length!==total)
        fail('本库'+label+'查询结果不完整，不能判定缺失');
      return [...own.tableData];
    };
    // DOI absence alone is not proof of a missing paper: an existing entry
    // can lack DOI. Always cross-check the full title before any new upload.
    const rows=c.doi?await read({doi:c.doi}):[];
    rows.push(...await read({title:c.title}));
    const scalar=(meta,key)=>Array.isArray(meta?.[key])&&meta[key].length===1&&typeof meta[key][0]==='string'?meta[key][0]:'';
    const items=[];
    const seen=new Set();
    for(const row of rows){
      // A changed response schema is not evidence of an absent paper. In
      // particular, silently treating string metadata or missing titles as
      // empty would turn an unrecognised existing entry into a new upload.
      if(!row||typeof row!=='object'||Array.isArray(row)||!row.metadata||
         typeof row.metadata!=='object'||Array.isArray(row.metadata)||
         !Array.isArray(row.metadata.title)||row.metadata.title.length!==1||
         typeof row.metadata.title[0]!=='string'||!norm(row.metadata.title[0]))
        fail('本库查询记录结构不兼容，不能判定缺失');
      for(const key of ['doi','wosId']){
        const value=row.metadata[key];
        if(value!==undefined&&(!Array.isArray(value)||value.length>1||value.some(v=>typeof v!=='string')))
          fail('本库查询标识结构不兼容，不能判定缺失');
      }
      const title=scalar(row.metadata,'title'), foundDoi=doi(scalar(row.metadata,'doi'));
      const foundTitle=titleKey(title);
      const ut=scalar(row.metadata,'wosId').trim().toUpperCase().replace(/^WOS:/,'');
      // A matching query alone is not proof. A contradictory same-ID record
      // makes automatic resolution unsafe, not an excuse to upload a duplicate.
      let primaryIdentity;
      if(primary&&foundTitle===expectedTitle){
        const authors=row.metadata.author;
        if(!Array.isArray(authors)||authors.length!==c.author_names.length||
          authors.some((author,i)=>norm(typeof author==='string'?author:author?.fullname)!==norm(c.author_names[i])))
          fail('本库同题名作者顺序不同或缺失，需人工查重');
        const url=scalar(row.metadata,primaryFields[urlLabel]);
        // Native batch preview stores the imported year as text, but the
        // public EntryManage index normalizes it to [2026]. Only this observed
        // four-digit scalar may be canonicalized; never coerce booleans,
        // arrays, fractional years or identity fields generically.
        const rawYear=row.metadata[primaryFields['年份']];
        const year=Array.isArray(rawYear)&&rawYear.length===1&&(
          typeof rawYear[0]==='string'&&/^\d{4}$/.test(rawYear[0])||
          typeof rawYear[0]==='number'&&Number.isInteger(rawYear[0])&&rawYear[0]>=1000&&rawYear[0]<=9999)
          ?String(rawYear[0]):'';
        const rawVenue=row.metadata[primaryFields[venueLabel]];
        const venueValue=Array.isArray(rawVenue)&&rawVenue.length===1?rawVenue[0]:null;
        const venue=journal&&venueValue&&typeof venueValue==='object'&&!Array.isArray(venueValue)
          ?(typeof venueValue.name==='string'?venueValue.name:''):scalar(row.metadata,primaryFields[venueLabel]);
        if(url!==c.primary_url||year!==c.fields['年份']||venue!==c.fields[venueLabel]||foundDoi!==c.doi)
          fail('本库同题名官方网址、年份、期刊/会议或 DOI 不同/缺失，需人工查重');
        primaryIdentity={url,year,venue,authors:[...c.author_names]};
      }
      const sameId=Boolean(!metadata&&ut===c.wos.slice(4))||Boolean(c.doi&&foundDoi===c.doi)||Boolean(primaryIdentity);
      if(sameId&&(foundTitle!==expectedTitle||(c.doi&&foundDoi!==c.doi)||!metadata&&ut!==c.wos.slice(4)))
        fail("本库相同标识的记录与 TXT 存在冲突，禁止重复导入");
      if(foundTitle===expectedTitle&&!sameId)fail("本库同题名记录标识不同或缺失，需人工查重");
      if(sameId){
        if(typeof row.id!=='string'||!/^\d{1,40}$/.test(row.id))fail("平台唯一号不是精确数字文本");
        if(seen.has(row.id))continue;
        seen.add(row.id);
        items.push({id:row.id,title,doi:foundDoi,wos:ut?'WOS:'+ut:'',...(primary?{primary_identity:primaryIdentity}:{})});
      }
    }
    return {ok:true,data:{verified:true,library_resolution_protocol:2,items}};
  }catch(error){return {ok:false,error:'[本库只读核验暂停] '+error.message};}
  finally {
    if(switched&&router){
      try {
        await router.push('/collectItem/batchManage');
        // A completed restoration is still a success when the query exhausted
        // its deadline. Do not replace the real query error with a false
        // "please return manually" warning after the route already returned.
        if(location.hash.split('?')[0]!=='#/collectItem/batchManage')
          await wait(()=>location.hash.split('?')[0]==='#/collectItem/batchManage');
      }
      catch {return {ok:false,error:'[本库只读核验暂停] 未能返回导入管理页，请人工返回；未自动上传'};}
    }
  }
}
if(typeof module!=="undefined")module.exports={resolveLibraryRecord};
