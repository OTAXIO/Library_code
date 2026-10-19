/* Native general-Excel wizard and its observed public request contract.
 * No credential/header/store reads or private authentication implementation.
 * Source is 人工提交, with Crossref/OpenAlex provenance in the private archive,
 * DOI/URL/thirdId fields and list. Never creates WOS IDs or author claims. */
async function runExcelCommand(command) {
  let submitted=false;
  const fail=message=>{throw Error(message);};
  const visible=el=>el&&el.getClientRects().length&&getComputedStyle(el).visibility!=='hidden';
  const norm=x=>String(x??'').normalize('NFKC').toLowerCase().replace(/\s+/g,' ').trim();
  const descendants=root=>{const seen=new Set();const visit=v=>{if(!v||seen.has(v))return;seen.add(v);(v.$children||[]).forEach(visit);};root.forEach(visit);return [...seen];};
  const one=(items,label)=>{if(items.length!==1)fail(label+'未唯一识别');return items[0];};
  const errorText=error=>{
    const message=[error?.response?.data?.msg,error?.data?.msg,error?.msg,error?.message,
      typeof error==='string'?error:''].find(value=>typeof value==='string'&&value.trim());
    const status=error?.response?.status;
    return (Number.isInteger(status)?'HTTP '+status+'：':'')+
      (message?message.replace(/[\u0000-\u001f]/g,' ').slice(0,400):'网页未返回可读错误；须只读核验原批次，不重复提交');
  };
  const check=()=>{
    const u=new URL(location.href);
    if(!['http:','https:'].includes(u.protocol)||u.hostname!=='admin.ir.lib.sjtu.edu.cn'||
       u.hash.split('?')[0]!=='#/collectItem/batchManage')fail('导入管理页未就绪');
    if(!Number.isFinite(command.expires)||Date.now()>=command.expires-3000)fail('Excel 操作到时；只读核验，不重复写入');
    if([...document.querySelectorAll('.el-dialog,.el-message-box')].some(visible))fail('存在其他操作弹窗，未关闭');
  };
  const wait=async(fn,label)=>{
    while(Date.now()<command.expires-5000){check();if(fn())return;await new Promise(r=>setTimeout(r,150));}
    fail(label+'未完成；请续验，不重复提交');
  };
  try {
    check();
    const vms=descendants([...document.querySelectorAll('*')].map(e=>e.__vue__).filter(Boolean));
    const vm=one(vms.filter(v=>v.$options?.name==='BatchManage'&&v.$refs?.wosDataText&&v.$refs?.batchPushModal),'批次管理组件');
    const drawer=vm.$refs.batch;
    if(!drawer||typeof drawer.showDrawer!=='function'||typeof drawer.getModelFields!=='function'||
       typeof drawer.next!=='function'||typeof drawer.submitForm!=='function'||typeof drawer.importData!=='function')
      fail('通用 Excel 向导结构不兼容');
    for(const child of Object.values(vm.$refs)){
      if(child?.drawer&&!(child===drawer&&child.__saImport?.sa_id===command.sa_id))
        fail('存在人工打开的窗口，未关闭或覆盖');
    }
    const modelName=command.model_name??command.candidate?.model_name??'期刊论文';
    if(!['期刊论文','会议论文'].includes(modelName))fail('文献模型未明确，未操作');
    let zero=drawer.$refs.stepZero, openedProbe=false;
    let model;
    if(['import_excel_probe','import_excel_upload'].includes(command.action)){
      if(drawer.drawer)fail('Excel 向导正在使用，未覆盖原表单');
      if(drawer.__saImport?.uploaded&&(!drawer.__saImport.submitted||drawer.__saImport.sa_id===command.sa_id))
        fail('原 Excel 上传尚待核验，未重置或重复上传');
      // showDrawer() only toggles visibility on the real site. It neither
      // resets the previous active=2 step nor mounts a lazy cold drawer until
      // opened. Reset ONLY our submitted, closed form, through native load().
      if(drawer.active!==0){
        const receipt=drawer.__saCompleted;
        const verified=receipt&&receipt.sa_id!==command.sa_id&&typeof receipt.batch_id==='string'&&receipt.batch_id&&
          receipt.form===JSON.stringify(drawer.form)&&drawer.form.instructions==='SA补充-'+receipt.sa_id;
        if(drawer.loading||!verified||typeof drawer.load!=='function')
          fail('前一 Excel 向导尚未确认完成，未重置或重复上传');
        drawer.load();await vm.$nextTick();
        if(drawer.active!==0)fail('原生继续导入没有返回第一步');
        delete drawer.__saCompleted;
      }
      if(!drawer.$refs.stepZero){
        drawer.__saImport={sa_id:command.sa_id,probeOnly:true,uploaded:false,submitted:false};
        drawer.showDrawer();openedProbe=true;await vm.$nextTick();
      }
      await wait(()=>{
        zero=drawer.$refs.stepZero;
        return zero&&Array.isArray(zero.modelOptions)&&zero.modelOptions.length&&Array.isArray(zero.source)&&zero.source.length;
      },'等待原生机构/模型选择器初始化');
      if(!zero.source.includes('人工提交'))
        fail('缺少真实人工提交来源或数据模型');
      model=one(zero.modelOptions.filter(m=>m.name===modelName&&typeof m.id==='string'),modelName+'模型');
    } else {
      // The real wizard v-if destroys stepZero after advancing. Resume is bound
      // to the original upload's marker and desktop's native model probe, not to
      // an invisible selector which no longer exists. Never use a guessed ID.
      const state=drawer.__saImport;
      if(!state||state.sa_id!==command.sa_id||!state.uploaded||state.submitted||
         typeof command.model_id!=='string'||!command.model_id||drawer.form.modelId!==command.model_id||
         state.modelId&&state.modelId!==command.model_id||drawer.form.datasource!=='人工提交')
        fail('原上传模型或来源无法核验，未重复上传');
      model={id:command.model_id};
    }
    if(command.action==='import_excel_probe'){
      drawer.form.modelId=model.id;drawer.modelFields=[];drawer.getModelFields();
      await wait(()=>drawer.modelFields.some(f=>f.fieldName==='title'),'读取模型字段');
      if(openedProbe){
        if(drawer.__saImport?.sa_id!==command.sa_id||!drawer.__saImport.probeOnly||drawer.active!==0||
           drawer.__saImport.uploaded||drawer.$refs.stepZero!==zero)fail('预检期间向导发生变化，未关闭');
        drawer.drawer=false;await vm.$nextTick();
        const panels=()=>[...(drawer.$el?.querySelectorAll?.('.el-drawer')||[])];
        await wait(()=>!drawer.drawer&&!panels().some(visible),'关闭本次空白预检向导');
      }
      return {ok:true,data:{metadata_excel_protocol:1,source:'人工提交',model_id:model.id,model_name:modelName,
        fields:drawer.modelFields.map(f=>({label:f.actualLabel,field:f.fieldName,required:f.required}))}};
    }
    if(!['import_excel_upload','import_excel_upload_status','import_excel_submit'].includes(command.action))fail('未知 Excel 操作');
    const c=command.candidate;
    if(!c||c.kind!=='crossref-openalex-v1'||c.source!=='Crossref+OpenAlex'||c.sjtu!==true||c.wos!==''||
       !/^10\.\d{4,9}\/\S+$/.test(c.doi)||!c.title||!Array.isArray(c.author_names)||!c.author_names.length||
       !/^[a-f0-9]{64}$/.test(c.sha256)||!/^[a-f0-9]{64}$/.test(command.xlsx_sha256)||
       !/^[\w-]{1,160}$/.test(command.sa_id)||command.instructions!=='SA补充-'+command.sa_id)
      fail('缺少完整单篇元数据及 Excel 哈希证明');
    if((modelName==='会议论文')!==(c.document_type==='conference-paper')||
       (modelName==='会议论文'&&(!c.fields?.['会议名称']||!c.fields?.['出处']||c.fields?.['发表期刊'])))
      fail('文献类型与模型或会议字段不一致，未上传');
    if(command.model_id!==model.id)fail('数据模型已改变');
    const sameForm=state=>state&&state.sa_id===command.sa_id&&state.sha256===command.xlsx_sha256&&
      state.candidate===c.sha256&&drawer.form.modelId===model.id&&drawer.form.datasource==='人工提交'&&
      drawer.form.instructions===command.instructions&&drawer.form.beginLine===2&&drawer.form.datasetId===state.datasetId;
    const verifyMapping=()=>{
      const fields=drawer.modelFields;
      if(!Array.isArray(fields)||!fields.length||!Array.isArray(drawer.columns)||!drawer.tableForm)fail('列映射未就绪');
      for(const label of Object.keys(c.fields)){
        const field=one(fields.filter(f=>f.actualLabel===label),'Excel 字段 '+label);
        if(drawer.tableForm[label]!==field.fieldName||!drawer.columns.includes(label))fail('Excel 字段映射改变：'+label);
      }
      for(const field of fields.filter(f=>Number(f.required)===1)){
        const labels=Object.keys(drawer.tableForm).filter(k=>drawer.tableForm[k]===field.fieldName);
        if(labels.length!==1||!c.fields[labels[0]])fail('缺少必填字段 '+field.actualLabel);
      }
      if(drawer.columns.length!==Object.keys(c.fields).length||drawer.previewTableData.length!==1)
        fail('上传预览不是恰好一篇、完整字段');
      const preview=drawer.previewTableData[0];
      for(const [label,value] of Object.entries(c.fields))
        if(String(preview[label]??'')!==value)fail('后台 Excel 预览与归档内容不符：'+label);
    };
    const finishUpload=async state=>{
      if(!drawer.drawer||drawer.active!==2||!sameForm(state)||!state.uploaded||state.submitted||
         drawer.form.filename!==state.serverName)fail('原上传窗口无法只读确认，未重复上传');
      // getModelFields hydrates tableForm in a separate async pass. A nonempty
      // modelFields array alone did not mean DOI/URL mappings were ready.
      await wait(()=>Object.keys(c.fields).every(label=>{
        const fs=drawer.modelFields.filter(f=>f.actualLabel===label);
        return fs.length===1&&drawer.tableForm[label]===fs[0].fieldName;
      }),'等待所有 Excel 列映射');
      verifyMapping();drawer.isNext=false;
      // Live native submitForm sends URLSearchParams through a JSON-encrypting
      // client, and the server responds 400 "missing modelId". Its published
      // request contract is the four named parameters below. Reuse the page's
      // PUBLIC client with params (as its own readData/getDuplicateField do),
      // never read authentication headers or recreate its encryption/auth stack.
      if(typeof vm.$http!=='function')fail('页面公开请求客户端不可用');
      drawer.fieldColumnRelations=Object.entries(drawer.tableForm).map(([label,field])=>field+':'+label);
      const response=await vm.$http({url:'/api/cspace-collection/collectItem/verificationDataFormat',method:'post',
        params:{modelId:model.id,filename:state.serverName,beginLine:2,fieldColumnRelations:drawer.fieldColumnRelations.join(',')}});
      if(response?.status!==200||response.data?.success!==true||response.data?.code!==200)
        fail('格式校验接口未确认成功，未导入');
      const checked=response.data.data;
      if(checked!==null&&(!checked||!Array.isArray(checked.errorInfoList)||checked.errorInfoList.length))
        fail('后台 Excel 格式校验存在错误，未导入');
      drawer.errorInfoList=[];drawer.cellStyleList=[];drawer.isNext=true;
      verifyMapping();state.mapping=JSON.stringify(drawer.fieldColumnRelations);
      return {ok:true,data:{uploaded:true,sha256:command.xlsx_sha256,dataset_id:state.datasetId,
        model_id:model.id,filename:'SA-Metadata-'+command.sa_id+'.xlsx',validated:true}};
    };
    if(command.action==='import_excel_upload_status')return await finishUpload(drawer.__saImport);
    if(command.action==='import_excel_upload'){
      if(drawer.drawer&&!openedProbe||drawer.__saImport?.uploaded&&
          (!drawer.__saImport.submitted||drawer.__saImport.sa_id===command.sa_id))fail('已存在 Excel 上传窗口/记录，未重复上传');
      if(typeof command.content!=='string'||command.content.length>700000||command.contentSha!==command.xlsx_sha256)
        fail('Excel 未通过独立哈希核验');
      const bytes=Uint8Array.from(atob(command.content),v=>v.charCodeAt(0));
      if(bytes.length<100||bytes.length>524288||bytes[0]!==80||bytes[1]!==75)fail('不是有效 XLSX 文件');
      if(!drawer.drawer)drawer.showDrawer();await vm.$nextTick();
      if(!drawer.drawer||drawer.active!==0)fail('未取得空白 Excel 向导');
      const z=drawer.$refs.stepZero;
      const tree=one(descendants([z]).filter(v=>v.$options?.name==='selectTree'),'机构选择器');
      await wait(()=>Array.isArray(tree.treeData)&&tree.treeData.length,'读取机构树');
      const nodes=[];const walk=rows=>rows.forEach(n=>{if(n.name==='上海交通大学')nodes.push(n);if(n.children)walk(n.children);});walk(tree.treeData);
      const institution=one(nodes,'上海交通大学机构');
      if(typeof institution.id!=='string'||!institution.id)fail('机构编号不是文本');
      drawer.__saImport={sa_id:command.sa_id,sha256:command.xlsx_sha256,candidate:c.sha256,uploaded:false,
        submitted:false,datasetId:institution.id,modelId:model.id};
      tree.handleNodeClick(institution);
      Object.assign(z.form,{modelId:model.id,beginLine:2,datasource:'人工提交',instructions:command.instructions});
      if(z.form.datasetId!==institution.id||tree.label!=='上海交通大学')fail('机构选择未成功');
      drawer.next();await wait(()=>drawer.active===1&&drawer.$refs.stepOne,'进入上传步骤');
      const step=drawer.$refs.stepOne;
      if(!Array.isArray(step.fileList)||step.fileList.length)fail('不是空白 Excel 上传列表');
      const input=one([...step.$el.querySelectorAll('input[type="file"]')],'Excel 上传控件');
      if(!input.accept.includes('.xlsx'))fail('不是 Excel 上传控件');
      const filename='SA-Metadata-'+command.sa_id+'.xlsx';
      const dt=new DataTransfer();dt.items.add(new File([bytes],filename,{type:'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'}));
      submitted=true;input.files=dt.files;input.dispatchEvent(new Event('change',{bubbles:true}));
      await wait(()=>step.fileList.length===1&&step.fileList[0].status==='success','上传 Excel');
      const file=step.fileList[0];
      if(file.name!==filename||file.size!==bytes.length||!file.response?.data?.name||
         file.response.success!==true&&file.response.code!==200)fail('Excel 上传回执未确认');
      const state=drawer.__saImport;state.uploaded=true;state.serverName=file.response.data.name;
      drawer.next();await wait(()=>drawer.active===2&&drawer.modelFields.length&&drawer.columns.length,'读取 Excel 预览及模型');
      if(!sameForm(state)||drawer.form.filename!==state.serverName)fail('Excel 上传表单改变');
      return await finishUpload(state);
    }
    const state=drawer.__saImport, upload=command.upload;
    if(!drawer.drawer||drawer.active!==2||!sameForm(state)||!state.uploaded||state.submitted||
       !upload?.validated||upload.sha256!==command.xlsx_sha256||upload.dataset_id!==state.datasetId||
       drawer.form.filename!==state.serverName||!drawer.isNext||drawer.errorInfoList?.length||
       state.mapping!==JSON.stringify(drawer.fieldColumnRelations))fail('Excel 上传状态丢失、被改变或已提交，未重复导入');
    verifyMapping();check();
    // The native importData wrapper has the same URLSearchParams defect as
    // validation. This is its observed native endpoint and exact form fields,
    // sent through the public client, not a guessed endpoint or copied token.
    if(typeof vm.$http!=='function'||typeof drawer.initProgress!=='function')fail('页面公开请求客户端或原生进度初始化不可用');
    // Native load() omits checkDuplicationField although the initial form has
    // it as ''. Keep the original explicit "no import-level duplicate field"
    // parameter after a reset too; never override an operator's selection.
    if(drawer.form.checkDuplicationField!=null&&drawer.form.checkDuplicationField!=='')
      fail('导入级查重字段已被选择，未覆盖人工设置');
    const params={...drawer.form,checkDuplicationField:'',fieldColumnRelations:drawer.fieldColumnRelations.join(',')};
    clearInterval(drawer.timer);drawer.loading=true;drawer.percentage=0;drawer.tips=null;drawer.batchNumber=null;
    drawer.initProgress(); // Same native progress setup/order as importData().
    state.submitted=true;submitted=true;
    let response;
    try{response=await vm.$http({url:'/api/cspace-collection/collectItem/importData',method:'post',params});}
    catch(error){state.error=errorText(error);throw error;}
    if(response?.status!==200||response.data?.success!==true||response.data?.code!==200)
      fail('导入提交回执未确认，请只读核验批次');
    return {ok:true,data:{submitted:true}}; // Import success is confirmed separately.
  } catch(error){return {ok:false,error:(submitted?'[已提交或结果不明，勿重试] ':'[Excel 已暂停] ')+errorText(error)};}
}
if(typeof module!=='undefined')module.exports={runExcelCommand};
