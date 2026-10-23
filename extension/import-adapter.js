/* Grounded in the public BatchManage / WosDataText / BatchPushModal components.
 * No REST endpoints, authentication headers or private store are accessed. */
async function runImportCommand(command) {
  let submitted = false;
  const fail = text => { throw new Error(text); };
  const visible = el => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";
  const all = selector => [...document.querySelectorAll(selector)].filter(visible);
  const norm = x => String(x ?? "").normalize("NFKC").toLowerCase().replace(/\s+/g, " ").trim();
  const check = () => {
    const u = new URL(location.href);
    if (u.hostname !== "admin.ir.lib.sjtu.edu.cn" || !["http:", "https:"].includes(u.protocol) ||
        u.hash.split("?")[0] !== "#/collectItem/batchManage") fail("请在已绑定的导入页进入“数据导入与批次管理”");
    if (!Number.isFinite(command.expires) || Date.now() >= command.expires - 2500) fail("操作到时，请核验结果，勿重复提交");
    if (all(".el-dialog, .el-message-box").length) fail("出现其他操作窗口，请人工处理并关闭");
  };
  const wait = async (fn, label, ms=30000) => {
    const end = Math.min(Date.now()+ms, command.expires-3000);
    while (Date.now() < end) { check(); if (fn()) return; await new Promise(r=>setTimeout(r,150)); }
    fail(label + "未完成。等待后台处理后只读核验，不能重试写入");
  };
  const descendants = root => {
    const seen=new Set();
    const visit=vm=>{if(!vm||seen.has(vm))return;seen.add(vm);(vm.$children||[]).forEach(visit);};
    (Array.isArray(root)?root:[root]).forEach(visit); return [...seen];
  };
  const one = (items, label) => { if(items.length!==1)fail(label+"不是唯一对象，请人工处理"); return items[0]; };
  try {
    check();
    const vms=descendants([...document.querySelectorAll("*")].map(el=>el.__vue__).filter(Boolean));
    // Several unrelated components also use the name BatchManage; require refs.
    const vm=one(vms.filter(v=>v.$options?.name==="BatchManage" && v.$refs?.wosDataText && v.$refs?.batchPushModal),"导入管理页");
    const primary=command.candidate?.kind==='publisher-verified-v1';
    const excel=primary||command.candidate?.kind==='crossref-openalex-v1';
    const source=excel?'人工提交':'WOS';
    const drawer=excel?vm.$refs.batch:vm.$refs.wosDataText, push=vm.$refs.batchPushModal, view=vm.$refs.batchViewModal;
    if (!vm.page || !Array.isArray(vm.tableData) || typeof vm.getData!=="function" || !view ||
        typeof vm.importWosDataText!=="function" || !drawer || (!excel&&typeof drawer.onSubmit!=="function") ||
        typeof push.onSubmit!=="function") fail("批次页面结构发生变化");
    if(command.action==='import_capabilities')return {ok:true,data:{zero_match_protocol:1,library_resolution:true}};
    if (!["import_scan","import_upload","import_submit","import_check","import_push"].includes(command.action)) fail("未知导入命令");
    if (typeof command.sa_id!=="string" || !/^[\w-]{1,160}$/.test(command.sa_id) ||
        command.instructions!=="SA补充-"+command.sa_id) fail("导入说明必须严格绑定当前名单 ID");
    const candidate=command.candidate;
    const publisherSource=primary&&/^[a-f0-9]{64}$/.test(candidate.primary_evidence_sha256??'')&&candidate.fields?.['相关网址']===candidate.primary_url&&(
      candidate.source==='ICLR官方论文集'&&/^https:\/\/proceedings\.iclr\.cc\/paper_files\/paper\/\d{4}\/hash\/[a-f0-9]{32}-Abstract-Conference\.html$/.test(candidate.primary_url)||
      candidate.source==='PMLR官方论文集'&&/^https:\/\/proceedings\.mlr\.press\/v\d+\/[a-z0-9]+\.html$/.test(candidate.primary_url));
    if (!candidate || !candidate.title || (excel ? candidate.wos!==''||(primary?!publisherSource||candidate.doi!==''&&!/^10\.\d{4,9}\/\S+$/.test(candidate.doi):candidate.source!=='Crossref+OpenAlex'||
        !/^10\.\d{4,9}\/\S+$/.test(candidate.doi))||!Array.isArray(candidate.author_names)||!candidate.author_names.length : !/^WOS:\d{15}$/.test(candidate.wos)) || candidate.sjtu!==true ||
        !/^[a-f0-9]{64}$/.test(candidate.sha256)) fail("缺少已核验的单篇 WOS 证据");
    for (const child of Object.values(vm.$refs)) {
      if (child?.drawer && !(child.__saImport?.sa_id===command.sa_id && [drawer,push,view].includes(child)))
        fail("存在人工打开的导入/编辑窗口，请关闭后再操作");
    }
    const snapshot = row => {
      if (typeof row.id!=="string" || !row.id || typeof row.modelId!=="string" || !row.modelId) fail("批次编号不是精确文本");
      const result={};
      for (const k of ["id","modelId","batchNumber","source","instructions","total","actual","fail","status",
        "increase","duplicateSkip","duplicateIncreaseUpdate","duplicateOverallCoverage"])
        result[k]=row[k] ?? "";
      if(row.source!==source || row.instructions!==command.instructions)fail("批次来源或说明不一致");
      return result;
    };
    let scannedCount=0;
    const batchTotal = () => {
      const value=vm.page.total;
      if(!['number','string'].includes(typeof value)||!/^\d+$/.test(String(value))||
          !Number.isSafeInteger(Number(value)))fail('批次数量未知，不能证明批次不存在');
      return Number(value);
    };
    const query = async page => {
      await wait(()=>!vm.loading,"等待批次列表");
      const old=vm.tableData;
      vm.page.currentPage=page; vm.page.pageSize=100;
      vm.queryForm={source,batchNumber:"",batchName:"",startTime:"",endTime:""}; vm.queryFormShow=true;
      vm.getData(page,100,{...vm.queryForm});
      await wait(()=>!vm.loading && vm.tableData!==old,"刷新批次列表");
      batchTotal();
      if(!Array.isArray(vm.tableData)||vm.tableData.length>100)fail('批次分页结构异常，未继续');
    };
    class BatchScanChanged extends Error {
      constructor() { super("扫描期间批次总数持续变化，请等待后只读重查"); }
    }
    const scanOnce = async () => {
      const result=[],seen=new Set(); let total;
      for(let p=1;p<=50;p++) {
        await query(p);
        if(total!==undefined && total!==batchTotal())throw new BatchScanChanged();
        total=batchTotal();
        if(total>5000)fail("WOS 批次超过安全扫描上限，请人工查找目标批次");
        for(const row of vm.tableData){
          if(!row||typeof row.id!=='string'||!row.id)fail('批次分页编号不是精确文本');
          if(seen.has(row.id))fail("批次分页重复，不能证明不存在同说明批次"); seen.add(row.id);
          if(row.instructions===command.instructions)result.push(snapshot(row));
        }
        if(p*100>=total){if(seen.size!==total)fail("批次扫描不完整");scannedCount=seen.size;return result;}
      }
      fail("批次扫描未完成");
    };
    const scan = async () => {
      // A just-submitted import, or another librarian's batch, can change the
      // total between pages. Discard that partial read and restart from page 1
      // at most twice. Only a complete, stable scan can authorize any write.
      for(let attempt=0;attempt<3;attempt++) {
        try { return await scanOnce(); }
        catch(error) {
          if(!(error instanceof BatchScanChanged) || attempt===2 || Date.now()>command.expires-6000)throw error;
          check(); await new Promise(resolve=>setTimeout(resolve,350));
        }
      }
    };
    const verify = async (batch,uniqueDescription=true) => {
      snapshot(batch);
      if(command.model_id!==undefined && (typeof command.model_id!=='string'||!command.model_id||batch.modelId!==command.model_id))
        fail('批次模型与原入库断点不一致，未推送或结案');
      for(const key of ["total","actual","fail","status"])
        if(!/^\d+$/.test(String(batch[key])))fail("批次计数/状态未知，不能推送");
      // 'actual' is the post-push count; an imported, unpushed batch can be zero.
      if(Number(batch.total)!==1 || ![0,1].includes(Number(batch.actual)) || Number(batch.fail)!==0 ||
         ![1,2].includes(Number(batch.status)) || (Number(batch.status)===2 && Number(batch.actual)!==1))
        fail("目标批次未成功导入恰好一篇，或有失败记录");
      if(typeof view.showDrawer!=="function")fail("缺少批次文献浏览组件");
      if(view.drawer){view.drawer=false; await vm.$nextTick();}
      view.__saImport={sa_id:command.sa_id};
      view.showDrawer(batch.id);
      await wait(()=>view.drawer && !view.loading && view.id===batch.id && view.tableData.length>0,"读取批次文献");
      if(Number(view.page.total)!==1 || view.tableData.length!==1)fail("目标批次不是一篇文献");
      const meta=view.tableData[0].metadata;
      const scalar = key => Array.isArray(meta?.[key]) && meta[key].length===1 ? String(meta[key][0]) : "";
      const id=scalar("wosId").toUpperCase().replace(/^WOS:/,"");
      if(norm(scalar("title"))!==norm(candidate.title) || !excel&&id!==candidate.wos.replace(/^WOS:/,""))
        fail("导入后的题名或 WOS 入藏号不一致/缺失");
      if(candidate.doi && norm(scalar("doi")).replace(/^https?:\/\/doi.org\//,"")!==candidate.doi)
        fail("导入后的 DOI 不一致");
      if(excel){
        const authors=meta?.author;
        if(!Array.isArray(authors)||authors.length!==candidate.author_names.length||
           authors.some((a,i)=>norm(typeof a==='string'?a:a?.fullname)!==norm(candidate.author_names[i])))
          fail("Excel 导入后的作者顺序/姓名未确认，未推送");
        if(!Array.isArray(meta?.authorInstitution)||!meta.authorInstitution.length||
           !/Shanghai\s+(?:Jiao\s*Tong|Jiaotong)\s+Univ(?:ersity)?|上海交通大学/i.test(JSON.stringify(meta.authorInstitution)))
          fail("Excel 导入后未回读到交大署名单位，未推送");
        if(primary){
          const fields=command.primary_fields;
          if(!fields||new Set(Object.values(fields)).size!==3||['相关网址','年份','会议名称'].some(label=>
            typeof fields[label]!=='string'||!fields[label]||!candidate.fields[label]||scalar(fields[label])!==candidate.fields[label]))
            fail('正式论文导入后的官方网址、会议、年份未确认，未推送');
        }
      }
      const ownViewPanels=descendants(view).filter(v=>v.$options?.name==='ElDrawer').map(v=>v.$refs?.drawer||v.$el);
      view.drawer=false; await vm.$nextTick();
      await wait(()=>view.__saImport?.sa_id===command.sa_id&&view.drawer===false&&ownViewPanels.every(p=>!visible(p)),
        '关闭本条只读批次窗口',10000);
      if(excel&&uniqueDescription&&drawer.drawer&&drawer.__saImport?.sa_id===command.sa_id&&drawer.__saImport?.submitted){
        // Close only our successfully verified wizard, never an unrelated drawer.
        const ownUploadPanels=descendants(drawer).filter(v=>v.$options?.name==='ElDrawer').map(v=>v.$refs?.drawer||v.$el);
        drawer.drawer=false;await vm.$nextTick();
        await wait(()=>drawer.drawer===false&&ownUploadPanels.every(p=>!visible(p)),'关闭本条已核验 Excel 向导',10000);
        delete drawer.__saImport;
      }
      // Completion may clear the temporary upload marker while the native
      // wizard retains active=2. Keep a distinct, read-only receipt for exactly
      // this closed form, but ONLY after actual successful push verification.
      // It authorizes native UI reset for the next paper, never a second import.
      if(excel&&uniqueDescription&&!drawer.drawer&&Number(batch.status)===2&&Number(batch.actual)===1&&
          drawer.form?.instructions===command.instructions&&drawer.form.modelId===batch.modelId&&
          drawer.form.datasource==='人工提交'){
        drawer.__saCompleted={sa_id:command.sa_id,batch_id:batch.id,form:JSON.stringify(drawer.form)};
      }
      return batch;
    };
    if(command.action==="import_scan")return {ok:true,data:{batches:await scan(),scanned_count:scannedCount}};
    if(command.action==="import_upload") {
      if(drawer.drawer || (drawer.__saImport?.sa_id===command.sa_id && drawer.__saImport?.uploaded))fail("上传窗口/上传记录已存在，禁止自动重复上传");
      if((await scan()).length)fail("已有相同说明批次，禁止再次导入");
      if(typeof command.content!=="string" || command.content.length>700000 || command.contentSha!==candidate.sha256)
        fail("文件未通过扩展校验");
      const bytes=Uint8Array.from(atob(command.content),c=>c.charCodeAt(0));
      if(!bytes.length || bytes.length>524288)fail("文件大小异常");
      vm.importWosDataText(); await vm.$nextTick();
      if(!drawer.drawer || !Array.isArray(drawer.fileList) || drawer.fileList.length)fail("未取得空白 WOS TXT 上传窗口");
      drawer.__saImport={sa_id:command.sa_id,sha256:candidate.sha256,uploaded:false,submitted:false};
      const tree=one(descendants(drawer).filter(v=>v.$options?.name==="selectTree"),"所属机构选择器");
      await wait(()=>Array.isArray(tree.treeData)&&tree.treeData.length>0,"读取机构树");
      const nodes=[];const walk=items=>items.forEach(n=>{if(n.name==="上海交通大学")nodes.push(n);if(n.children)walk(n.children);});walk(tree.treeData);
      const institution=one(nodes,"上海交通大学机构");
      if(typeof institution.id!=="string" || !institution.id)fail("所属机构编号不可靠");
      tree.handleNodeClick(institution); drawer.form.instructions=command.instructions; await vm.$nextTick();
      if(drawer.form.datasetId!==institution.id || tree.label!=="上海交通大学")fail("机构选择未成功");
      const input=one([...drawer.$el.querySelectorAll('input[type="file"]')],"WOS TXT 上传控件");
      if(input.accept!==".txt" || drawer.fileList.length)fail("不是空白 TXT 上传控件");
      const filename="SA-WOS-"+command.sa_id+".txt";
      const dt=new DataTransfer();dt.items.add(new File([bytes],filename,{type:"text/plain"}));
      submitted=true; input.files=dt.files; input.dispatchEvent(new Event("change",{bubbles:true}));
      await wait(()=>drawer.fileList.length===1 && drawer.fileList[0].status==="success","上传 TXT");
      const file=drawer.fileList[0];
      if(file.name!==filename || file.size!==bytes.length || !file.response?.data?.name ||
         (file.response.success!==true && file.response.code!==200))fail("上传响应未确认成功");
      drawer.__saImport.uploaded=true; drawer.__saImport.serverName=file.response.data.name;
      drawer.__saImport.datasetId=institution.id;
      return {ok:true,data:{uploaded:true,sha256:candidate.sha256,dataset_id:institution.id,filename}};
    }
    if(command.action==="import_submit") {
      const state=drawer.__saImport;
      if(!drawer.drawer || !state || state.sa_id!==command.sa_id || state.sha256!==candidate.sha256 || !state.uploaded || state.submitted ||
        drawer.form.datasetId!==state.datasetId || drawer.form.instructions!==command.instructions ||
        command.upload?.dataset_id!==state.datasetId || command.upload?.sha256!==state.sha256 || drawer.fileList.length!==1 ||
        drawer.fileList[0].response?.data?.name!==state.serverName)fail("上传窗口已变更/丢失或已提交，请人工核验");
      if((await scan()).length)fail("提交前发现已有批次，停止重复导入");
      // Guard immediately after the asynchronous scan, not only before it.
      if(!drawer.drawer || drawer.form.instructions!==command.instructions || drawer.form.datasetId!==state.datasetId ||
          drawer.fileList[0]?.response?.data?.name!==state.serverName)fail("扫描期间上传表单被修改");
      check();state.submitted=true;submitted=true;drawer.onSubmit();
      await wait(()=>!drawer.drawer,"提交导入",10000);
      return {ok:true,data:{submitted:true}}; // Deliberately NOT 'imported'.
    }
    let batches=await scan();
    // Existing WOS callers serialize an absent previous ID as null or "".
    // Those mean no saved ID, not permission to pick among multiple batches.
    const savedIdPresent=command.batch_id!==undefined&&command.batch_id!==null&&command.batch_id!=='';
    const exactReadback = () => command.action==='import_check' && savedIdPresent;
    if(exactReadback() && (typeof command.batch_id!=='string'||!/^\w[\w-]{0,159}$/.test(command.batch_id)))
      fail('原批次 ID 不是精确文本，未选择其他批次');
    const targets = () => exactReadback()?batches.filter(b=>b.id===command.batch_id):batches;
    if(command.action==="import_check") {
      const deadline=Math.min(Date.now()+40000,command.expires-8000);
      while(Date.now()<deadline && (targets().length===0 || (targets().length===1 &&
        ((Number(targets()[0].total)===0 && Number(targets()[0].fail)===0) ||
         (command.expect_pushed===true && Number(targets()[0].status)===1 && Number(targets()[0].fail)===0))))) {
        await new Promise(r=>setTimeout(r,2000));check();batches=await scan();
      }
    }
    const matches=targets();
    if(!matches.length)fail((exactReadback()?'未找到原批次 ID':'未找到本条导入批次')+
      '（完整扫描 '+scannedCount+' 条，同说明 '+batches.length+' 条）；原提交结果不明，保留断点，未重传');
    if(matches.length!==1)fail('找到 '+matches.length+' 个同说明批次，无法唯一确认原提交；保留断点，未选择第一条或重传');
    // Only a read with a previously saved exact ID can distinguish an owned
    // batch from other same-description rows. Push still requires uniqueness.
    const batch=matches[0];
    if(command.batch_id && batch.id!==command.batch_id)fail("批次 ID 改变");
    await verify(batch,batches.length===1);
    if(command.action==="import_check")return {ok:true,data:{verified:true,batch}};
    if(command.action==="import_push") {
      const expected=command.batch;
      if(!expected || ["id","modelId","instructions","total","actual","fail","status"].some(k=>expected[k]!==batch[k]))
        fail("批次在核验后已变化");
      if(Number(batch.status)!==1 || (push.__saImport?.sa_id===command.sa_id && push.__saImport?.submitted))fail("批次已推送或已发出推送，禁止重试");
      push.__saImport={sa_id:command.sa_id,submitted:false};
      vm.batchPush(batch); await vm.$nextTick();
      await wait(()=>push.drawer && push.modelFieldsList?.some(f=>f.fieldName==="title") && push.duplicateQueryTypes?.length,"读取推送设置");
      // Never guess an enum value. Resolve the exact PPT option from live labels.
      const clean=s=>norm(s).replace(/[\s（）()]/g,"");
      const desired="唯一标识+期刊&发表时间&页码&卷&期&题名相似度+题名相似度";
      const option=one(push.duplicateQueryTypes.filter(o=>clean(o.label)===desired),"PPT 指定查重方式");
      if(typeof option.value!=="string" || ["custom",""].includes(option.value))fail("查重选项值未知");
      Object.assign(push.form,{duplicateChecking:true,duplicateQueryType:option.value,duplicateItemProcessingType:"4",
        newItemProcessingType:"1",owner:true,updateFields:[]});
      await vm.$nextTick(); check();
      if(push.form.batchId!==batch.id || push.form.modelId!==batch.modelId || push.form.duplicateChecking!==true ||
        push.form.duplicateQueryType!==option.value || push.form.duplicateItemProcessingType!=="4" ||
        push.form.newItemProcessingType!=="1" || push.form.owner!==true || push.form.updateFields.length)fail("推送设置验证失败");
      push.__saImport.submitted=true;submitted=true;push.onSubmit();
      await wait(()=>!push.drawer,"提交推送",10000);
      return {ok:true,data:{submitted:true,batch_id:batch.id}};
    }
    fail("未执行未知命令");
  } catch(error) { return {ok:false,error:(submitted?"[已提交或结果不明，勿重试] ":"[已暂停] ")+error.message}; }
}
if(typeof module!=="undefined")module.exports={runImportCommand};
