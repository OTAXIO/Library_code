const assert=require('node:assert/strict');
const fs=require('node:fs');const path=require('node:path');
const {chromium}=require('playwright');
const {runExcelCommand}=require('../extension/excel-adapter.js');
const {runImportCommand}=require('../extension/import-adapter.js');
const {runSACommand}=require('../extension/adapter.js');
const fixture=fs.readFileSync(path.join(__dirname,'fixtures/excel.html'),'utf8');
const saFixture=fs.readFileSync(path.join(__dirname,'fixtures/compare.html'),'utf8');
const candidate={kind:'crossref-openalex-v1',source:'Crossref+OpenAlex',title:'Synthetic paper',doi:'10.1234/test',wos:'',
  sjtu:true,sha256:'a'.repeat(64),authors:'Alice Test',author_names:['Alice Test'],addresses:['Shanghai Jiao Tong University, China'],
  affiliation:'Shanghai Jiao Tong University, China',crossref_url:'https://api.crossref.org/works/10.1234%2Ftest',
  openalex_url:'https://openalex.org/W123',fields:{'题名':'Synthetic paper','作者':'Alice Test(1)',
    '作者单位':'(1)Shanghai Jiao Tong University, China','发表日期':'2025-01','年份':'2025','发表期刊':'Synthetic Journal',
    'DOI':'10.1234/test','URL':'https://doi.org/10.1234/test'}};
const base=()=>({id:'metadata-test',expires:Date.now()+60000,sa_id:'demo-001',instructions:'SA补充-demo-001',
  candidate,model_id:'journal-test',xlsx_sha256:'b'.repeat(64),contentSha:'b'.repeat(64),
  content:Buffer.from('PK'+'x'.repeat(110)).toString('base64')});
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.SA_TEST_BROWSER||'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
 try{
  const ctx=await browser.newContext();let activeFixture=fixture;
  await ctx.route('**/*',r=>r.fulfill({status:200,contentType:'text/html',body:activeFixture}));
  const page=await ctx.newPage();
  for(const scenario of ['valid','delayedMapping','validationError','previewChanged','wrongSource','wrongHash','manualWindow']){
   await page.goto('about:blank');await page.goto('http://admin.ir.lib.sjtu.edu.cn/#/collectItem/batchManage');
   await page.evaluate(({candidate,scenario})=>{testConfig.candidate=candidate;testConfig[scenario]=true;
    if(scenario==='manualWindow')drawer.drawer=true;},{candidate,scenario});
   const command={...base(),action:'import_excel_upload'};
   if(scenario==='wrongSource')command.candidate={...candidate,source:'WOS'};
   if(scenario==='wrongHash')command.contentSha='c'.repeat(64);
   const result=await page.evaluate(runExcelCommand,command);
   const valid=['valid','delayedMapping'].includes(scenario);
   assert.equal(result.ok,valid,scenario+JSON.stringify(result));
   assert.equal(await page.evaluate(()=>imports),0);
   if(valid){
    const upload=result.data;
    const recovered=await page.evaluate(runExcelCommand,{...base(),action:'import_excel_upload_status'});
    assert.equal(recovered.ok,true,JSON.stringify(recovered));
    assert.equal(await page.evaluate(()=>uploads),1,'resume must not upload twice');
    const first=await page.evaluate(runExcelCommand,{...base(),action:'import_excel_submit',upload});
    assert.equal(first.ok,true,JSON.stringify(first));assert.equal(await page.evaluate(()=>imports),1);
    const repeat=await page.evaluate(runExcelCommand,{...base(),action:'import_excel_submit',upload});
    assert.equal(repeat.ok,false);assert.equal(await page.evaluate(()=>imports),1);
   }
   console.log('PASS metadata Excel '+scenario);
  }
  for(const scenario of ['cold','nextPaper','unsubmitted','wrongCompletedForm']){
   await page.goto('about:blank');await page.goto('http://admin.ir.lib.sjtu.edu.cn/#/collectItem/batchManage');
   await page.evaluate(({candidate,scenario})=>{
    testConfig.candidate=candidate;
    if(scenario==='cold'){testConfig.cold=true;delete drawer.$refs.stepZero;}
    else {drawer.active=2;delete drawer.$refs.stepZero;
      drawer.form={instructions:'SA补充-previous-one',modelId:'journal-test',datasource:'人工提交'};
      if(scenario==='unsubmitted')drawer.__saImport={sa_id:'previous-one',uploaded:true,submitted:false};
      else drawer.__saCompleted={sa_id:'previous-one',batch_id:'previous-batch',
        form:scenario==='wrongCompletedForm'?'changed':JSON.stringify(drawer.form)};}
   },{candidate,scenario});
   const probe=await page.evaluate(runExcelCommand,{...base(),action:'import_excel_probe'});
   assert.equal(probe.ok,['cold','nextPaper'].includes(scenario),scenario+JSON.stringify(probe));
   assert.equal(await page.evaluate(()=>uploads),0);
   assert.equal(await page.evaluate(()=>resets),scenario==='nextPaper'?1:0);
   if(probe.ok){
    const upload=await page.evaluate(runExcelCommand,{...base(),action:'import_excel_upload'});
    assert.equal(upload.ok,true,JSON.stringify(upload));
    const submitted=await page.evaluate(runExcelCommand,{...base(),action:'import_excel_submit',upload:upload.data});
    assert.equal(submitted.ok,true,JSON.stringify(submitted));
    assert.equal(await page.evaluate(()=>uploads),1);assert.equal(await page.evaluate(()=>imports),1);
   }
   console.log('PASS native Excel initialization '+scenario);
  }
  await page.goto('about:blank');await page.goto('http://admin.ir.lib.sjtu.edu.cn/#/collectItem/batchManage');
  for(let i=1;i<=5;i++){
   const c={...candidate,title:'Synthetic paper '+i,doi:'10.1234/test'+i,
     fields:{...candidate.fields,'题名':'Synthetic paper '+i,'DOI':'10.1234/test'+i}};
   if(i%2===0){
    c.document_type='conference-paper';c.model_name='会议论文';
    delete c.fields['发表期刊'];delete c.fields['URL'];
    Object.assign(c.fields,{'出处':'Synthetic Proceedings','会议名称':'Synthetic Conference','相关网址':'https://doi.org/'+c.doi});
   }
   const params={...base(),sa_id:'continuous-'+i,instructions:'SA补充-continuous-'+i,candidate:c};
   if(i%2===0)params.model_id='conference-test';
   await page.evaluate(c=>testConfig.candidate=c,c);
   const probe=await page.evaluate(runExcelCommand,{...params,action:'import_excel_probe'});
   assert.equal(probe.ok,true,JSON.stringify(probe));
   assert.equal(probe.data.model_id,params.model_id,'correct model per paper, not leftover journal selection');
   const uploaded=await page.evaluate(runExcelCommand,{...params,action:'import_excel_upload'});
   assert.equal(uploaded.ok,true,JSON.stringify(uploaded));
   const submitted=await page.evaluate(runExcelCommand,{...params,action:'import_excel_submit',upload:uploaded.data});
   assert.equal(submitted.ok,true,JSON.stringify(submitted));
   const checked=await page.evaluate(runImportCommand,{...params,action:'import_check'});
   assert.equal(checked.ok,true,JSON.stringify(checked));
   assert.equal(await page.evaluate(()=>drawer.__saImport),undefined,'upload marker is cleared by native verification');
   const pushed=await page.evaluate(runImportCommand,{...params,action:'import_push',batch:checked.data.batch});
   assert.equal(pushed.ok,true,JSON.stringify(pushed));
   const verified=await page.evaluate(runImportCommand,{...params,action:'import_check',batch_id:checked.data.batch.id,expect_pushed:true});
   assert.equal(verified.ok,true,JSON.stringify(verified));assert.equal(verified.data.batch.increase,1);
   assert.equal(await page.evaluate(()=>drawer.__saCompleted.batch_id),checked.data.batch.id);
  }
  assert.deepEqual(await page.evaluate(()=>({uploads,imports,pushes,resets})),{uploads:5,imports:5,pushes:5,resets:4});
  console.log('PASS five consecutive mixed journal/conference papers: upload -> validation -> import -> push -> verified native reset');
  await page.goto('about:blank');await page.goto('http://admin.ir.lib.sjtu.edu.cn/#/collectItem/batchManage');
  await page.evaluate(c=>{testConfig.candidate=c;testConfig.importRejected=true;},candidate);
  const rejectedUpload=await page.evaluate(runExcelCommand,{...base(),action:'import_excel_upload'});
  assert.equal(rejectedUpload.ok,true,JSON.stringify(rejectedUpload));
  const rejectedSubmit=await page.evaluate(runExcelCommand,{...base(),action:'import_excel_submit',upload:rejectedUpload.data});
  assert.equal(rejectedSubmit.ok,false);assert.match(rejectedSubmit.error,/HTTP 400.*本次导入已拒绝/);
  const repeatedSubmit=await page.evaluate(runExcelCommand,{...base(),action:'import_excel_submit',upload:rejectedUpload.data});
  assert.equal(repeatedSubmit.ok,false);assert.equal(await page.evaluate(()=>imports),0);
  console.log('PASS structured native rejection preserves readable error and never automatically resubmits');
  activeFixture=saFixture;
  for(const scenario of ['valid','noBatch','wrongSource','notPushed','wrongDOI','noSJTU','unclaimed',
                         'pendingRoleChanged','pendingUnitChanged','pendingDuplicateBatch']){
   await page.goto('about:blank');await page.goto('http://admin.ir.lib.sjtu.edu.cn/#/dataCompare/list');
   await page.evaluate(()=>{Object.assign(synthetic,{doiValue:'10.1234/test',wosValue:''});
    Object.assign(testConfig,{modern:true,unitInfo:true,saDoi:'10.1234/test',libraryDoi:'10.1234/test',libraryWos:'',claim:'已认领'});});
   const read=await page.evaluate(runSACommand,{...base(),action:'search'});assert.equal(read.ok,true);
   const batch={id:'batch-metadata',modelId:'article',instructions:'SA补充-demo-001',source:'人工提交',
    status:2,total:1,actual:1,fail:0,increase:1,duplicateSkip:0,duplicateIncreaseUpdate:0,duplicateOverallCoverage:0};
   const c={...candidate};
   if(scenario==='wrongSource')batch.source='WOS';
   if(scenario==='notPushed')batch.status=1;
   if(scenario==='wrongDOI')c.doi='10.1234/wrong';
   if(scenario==='noSJTU')c.affiliation='Other University';
   if(scenario==='unclaimed')await page.evaluate(()=>testConfig.claim='未认领');
   if(scenario==='pendingRoleChanged')await page.evaluate(()=>testConfig.unknownModernRole='是');
   if(scenario==='pendingUnitChanged')await page.evaluate(()=>testConfig.libraryFirstUnit='否');
   if(scenario==='pendingDuplicateBatch')batch.increase=0;
   const latest=scenario==='unclaimed'?await page.evaluate(runSACommand,{...base(),action:'search'}):read;
   const result=await page.evaluate(runSACommand,{...base(),action:'complete',expected:read.data.row,
    expected_comparison:latest.data.comparison,reviewed:true,owner:'谭勋策',note:'已入库',
    metadata_imported_evidence:scenario==='noBatch'?null:{candidate:c,batch,item_id:'1234567890123456789',xlsx_sha256:'b'.repeat(64)}});
   assert.equal(result.ok,scenario==='valid',scenario+JSON.stringify(result));
   assert.equal(await page.evaluate(()=>writeCount),scenario==='valid'?1:0);
   assert.equal(await page.evaluate(()=>claimWrites),0);
   console.log('PASS metadata closure '+scenario);
  }
  for(const scenario of ['valid','lostAck','manualRemark','noProof','duplicateBatch','roleChanged','rejected']){
   await page.goto('about:blank');await page.goto('http://admin.ir.lib.sjtu.edu.cn/#/dataCompare/list');
   await page.evaluate(scenario=>{
    Object.assign(synthetic,{doiValue:'10.1234/test',wosValue:'',markStatus:'已处理',
      remark:scenario==='manualRemark'?'人工保留备注':''});
    Object.assign(testConfig,{modern:true,unitInfo:true,saDoi:'10.1234/test',libraryDoi:'10.1234/test',
      libraryWos:'',claim:'已认领',annotationLostAck:scenario==='lostAck',annotationRejected:scenario==='rejected'});
   },scenario);
   const read=await page.evaluate(runSACommand,{...base(),action:'search'});
   assert.equal(read.ok,true,JSON.stringify(read));
   const batch={id:'batch-metadata',modelId:'article',instructions:'SA补充-demo-001',source:'人工提交',
    status:2,total:1,actual:1,fail:0,increase:scenario==='duplicateBatch'?0:1,
    duplicateSkip:0,duplicateIncreaseUpdate:0,duplicateOverallCoverage:0};
   const proof={candidate,batch,item_id:'1234567890123456789',xlsx_sha256:'b'.repeat(64)};
   if(scenario==='roleChanged')await page.evaluate(()=>testConfig.unknownModernRole='是');
   const params={...base(),action:'complete',expected:read.data.row,expected_comparison:read.data.comparison,
    reviewed:true,owner:'谭勋策',note:'已入库',metadata_imported_evidence:scenario==='noProof'?null:proof};
   const result=await page.evaluate(runSACommand,params);
   assert.equal(result.ok,scenario==='valid',scenario+JSON.stringify(result));
   assert.equal(await page.evaluate(()=>synthetic.markStatus),'已处理','never reopen or toggle processed task');
   assert.equal(await page.evaluate(()=>annotationRequests),['valid','lostAck','rejected'].includes(scenario)?1:0,scenario);
   assert.equal(await page.evaluate(()=>writeCount),['valid','lostAck'].includes(scenario)?1:0,scenario);
   assert.equal(await page.evaluate(()=>document.getElementById('status').style.display),'none','never open toggle dialog');
   if(scenario==='manualRemark')assert.equal(await page.evaluate(()=>synthetic.remark),'人工保留备注');
   if(['valid','lostAck'].includes(scenario)){
    const again=await page.evaluate(runSACommand,{...base(),action:'search'});
    const resumed=await page.evaluate(runSACommand,{...params,expected:again.data.row,expected_comparison:again.data.comparison});
    assert.equal(resumed.ok,true,JSON.stringify(resumed));
    assert.equal(await page.evaluate(()=>annotationRequests),1,'existing intake note is a read-only success');
   }
   console.log('PASS already-processed intake annotation '+scenario);
  }
  for(const scenario of ['valid','wrongModernSa','wrongModernStaff','unknownModernRole']){
   await page.goto('about:blank');await page.goto('http://admin.ir.lib.sjtu.edu.cn/#/dataCompare/list');
   await page.evaluate(scenario=>{
    Object.assign(synthetic,{doiValue:'10.1234/test',wosValue:''});
    Object.assign(testConfig,{modern:true,saDoi:'10.1234/test',libraryDoi:'10.1234/test',libraryWos:''});
    if(scenario==='wrongModernSa')testConfig.wrongModernSa='another-row';
    if(scenario==='wrongModernStaff')testConfig.wrongModernStaff='00002';
    if(scenario==='unknownModernRole')testConfig.unknownModernRole='未知';
   },scenario);
   const read=await page.evaluate(runSACommand,{...base(),action:'search'});
   assert.equal(read.ok,scenario==='valid',scenario+JSON.stringify(read));
   if(scenario==='valid'){
    assert.equal(read.data.metadata_completion_protocol,4);
    const field=label=>read.data.comparison.find(row=>row.label===label);
    assert.equal(field('WOS记录号').sa,'');assert.equal(field('WOS记录号').library,'');
    assert.equal(field('认领状态').sa,'测试员(00001)');assert.equal(field('认领状态').library,'未认领');
    assert.match(field('作者信息').sa,/是否通讯作者：否/);
    const params={...base(),expected:read.data.row,sa_text:'测试员(00001)',staff_id:'00001',roster_staff_id:'00001'};
    const prepared=await page.evaluate(runSACommand,{...params,action:'prepare_claim'});
    assert.equal(prepared.ok,true,JSON.stringify(prepared));
    const claimed=await page.evaluate(runSACommand,{...params,action:'submit_claim',prepared:prepared.data.prepared,
      author_index:prepared.data.suggested_index,confirmed:true});
    assert.equal(claimed.ok,true,JSON.stringify(claimed));
    const after=await page.evaluate(runSACommand,{...base(),action:'search'});
    assert.equal(after.ok,true,JSON.stringify(after));
    assert.equal(after.data.comparison.find(row=>row.label==='认领状态').library,'已认领');
    assert.equal(await page.evaluate(()=>claimWrites),1);
   }
   console.log('PASS modern SA comparison '+scenario);
  }
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
