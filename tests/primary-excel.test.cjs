/* No production traffic: all requests fulfilled from isolated local fixtures. */
const assert=require('node:assert/strict');
const fs=require('node:fs');const path=require('node:path');
const {chromium}=require('playwright');
const {runExcelCommand}=require('../extension/excel-adapter.js');
const {runImportCommand}=require('../extension/import-adapter.js');
const {runSACommand}=require('../extension/adapter.js');
const fixture=fs.readFileSync(path.join(__dirname,'fixtures/excel.html'),'utf8');
const saFixture=fs.readFileSync(path.join(__dirname,'fixtures/compare.html'),'utf8');
const primaryFields={'相关网址':'url','年份':'year','会议名称':'conferenceName'};
const candidate={kind:'publisher-verified-v1',source:'PMLR官方论文集',primary_url:'https://proceedings.mlr.press/v306/test26a.html',
  primary_evidence_sha256:'c'.repeat(64),title:'Synthetic paper',doi:'',wos:'',sjtu:true,sha256:'a'.repeat(64),
  model_name:'会议论文',document_type:'conference-paper',authors:'Alice Test',author_names:['Alice Test'],
  addresses:['Shanghai Jiao Tong University'],author_mapping:[[1]],affiliation:'Shanghai Jiao Tong University',fields:{
    '题名':'Synthetic paper','作者':'Alice Test(1)','作者单位':'(1)Shanghai Jiao Tong University','发表日期':'2026-09-29',
    '年份':'2026','出处':'International Conference on Machine Learning','会议名称':'International Conference on Machine Learning',
    '相关网址':'https://proceedings.mlr.press/v306/test26a.html'}};
const base=()=>({expires:Date.now()+60000,sa_id:'demo-001',instructions:'SA补充-demo-001',candidate,
  primary_fields:primaryFields,model_id:'conference-test',xlsx_sha256:'b'.repeat(64),contentSha:'b'.repeat(64),
  content:Buffer.from('PK'+'x'.repeat(110)).toString('base64')});
(async()=>{
 const browser=await chromium.launch({headless:true,executablePath:process.env.SA_TEST_BROWSER||'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
 try{
  const ctx=await browser.newContext();
  await ctx.route('**/*',r=>r.fulfill({status:200,contentType:'text/html',body:r.request().url().includes('dataCompare')?saFixture:fixture}));
  const page=await ctx.newPage(),sa=await ctx.newPage();
  await page.goto('http://admin.ir.lib.sjtu.edu.cn/#/collectItem/batchManage');
  // Fragments are not sent in HTTP; this explicit local fixture route owns SA.
  await sa.route('**/*',r=>r.fulfill({status:200,contentType:'text/html',body:saFixture}));
  await sa.goto('http://admin.ir.lib.sjtu.edu.cn/#/dataCompare/list');
  for(let n=1;n<=7;n++){
   const c=structuredClone(candidate);c.title+=' '+n;c.fields['题名']=c.title;
   c.primary_url=c.fields['相关网址']='https://proceedings.mlr.press/v306/test26'+String.fromCharCode(96+n)+'.html';
   if(n>5){
    c.source=n===6?'Nature出版社':'SciOpen出版社';c.model_name='期刊论文';c.document_type='journal-article';
    c.doi=n===6?'10.1038/s99999-025-00001-1':'10.26599/test.2025.1';
    c.primary_url=n===6?'https://www.nature.com/articles/s99999-025-00001-1':'https://www.sciopen.com/article/10.26599/TEST.2025.1';
    delete c.fields['出处'];delete c.fields['会议名称'];delete c.fields['相关网址'];
    Object.assign(c.fields,{'URL':c.primary_url,'发表期刊':'Synthetic Journal','DOI':c.doi,
      '卷号':'2','期号':'5','页码':'10-19','ISSN':'1234-5678'});
   }
   const p={...base(),candidate:c,sa_id:'primary-'+n,instructions:'SA补充-primary-'+n,
    ...(n>5?{model_id:'journal-test',primary_fields:{'URL':'url','年份':'year','发表期刊':'source'}}:{})};
   await page.evaluate(c=>testConfig.candidate=c,c);
   const probe=await page.evaluate(runExcelCommand,{...p,action:'import_excel_probe'});
   assert.equal(probe.ok,true,JSON.stringify(probe));assert.equal(probe.data.model_id,p.model_id);
   const upload=await page.evaluate(runExcelCommand,{...p,action:'import_excel_upload'});
   assert.equal(upload.ok,true,JSON.stringify(upload));
   const submitted=await page.evaluate(runExcelCommand,{...p,action:'import_excel_submit',upload:upload.data});
   assert.equal(submitted.ok,true,JSON.stringify(submitted));
   const checked=await page.evaluate(runImportCommand,{...p,action:'import_check'});
   assert.equal(checked.ok,true,JSON.stringify(checked));
   if(n===6){
    await page.evaluate(()=>{window.originalJournalView=vm.$refs.batchViewModal.showDrawer;});
    const malformed=[['source',[{name:'Different Journal'}]],['source',[{}]],['source',[{name:['Synthetic Journal']}]],
      ['source',[['Synthetic Journal']]],['source',[{name:'Synthetic Journal'},{name:'Other Journal'}]],
      ['year',[[2026]]],['issn',['9999-9999']],['pages',['20-29']],['issued',['2025-09-29']],
      ['author',[{fullname:'Alice Test',institutionOrderNums:[2]}]],
      ['authorInstitution',[{address:'Different department, Shanghai Jiao Tong University',order:1}]]];
    for(const [field,value] of malformed){
     await page.evaluate(({field,value})=>{vm.$refs.batchViewModal.showDrawer=function(id){
      originalJournalView.call(this,id);this.tableData[0].metadata[field]=value;};},{field,value});
     const refused=await page.evaluate(runImportCommand,{...p,action:'import_push',batch:checked.data.batch});
     assert.equal(refused.ok,false,JSON.stringify({field,value,refused}));assert.match(refused.error,/未确认/);
     assert.equal(await page.evaluate(()=>pushes),5,'malformed journal must never push');
    }
    await page.evaluate(()=>{vm.$refs.batchViewModal.showDrawer=originalJournalView;delete window.originalJournalView;});
    console.log('PASS native journal object identity and malformed field push guards');
   }
   const push=await page.evaluate(runImportCommand,{...p,action:'import_push',batch:checked.data.batch});
   assert.equal(push.ok,true,JSON.stringify(push));
   const after=await page.evaluate(runImportCommand,{...p,action:'import_check',batch_id:checked.data.batch.id,expect_pushed:true});
   assert.equal(after.ok,true,JSON.stringify(after));assert.equal(after.data.batch.increase,1);
   await sa.evaluate(({c,id,n})=>{
    Object.assign(synthetic,{saLzkId:id,titleValue:c.title,doiValue:c.doi,wosValue:'',itemId:String(123000+n),
      matchCount:1,markStatus:'待处理',reason:'',remark:''});
    Object.assign(testConfig,{modern:true,unitInfo:true,saDoi:c.doi,libraryDoi:c.doi,libraryWos:'',claim:'已认领'});
    const show=vm.showItemId;
    vm.showItemId=function(row){show.call(this,row);for(const f of detail.currentRows||[])
      if(f.label==='题名')f.compareLeftValue=f.compareRightValue=row.titleValue;};
   },{c,id:p.sa_id,n});
   const read=await sa.evaluate(runSACommand,{...p,action:'search',close_detail:true});
   assert.equal(read.ok,true,JSON.stringify(read));
   const proof={candidate:c,batch:after.data.batch,item_id:String(123000+n),xlsx_sha256:p.xlsx_sha256};
   const done=await sa.evaluate(runSACommand,{...p,action:'complete',expected:read.data.row,expected_comparison:read.data.comparison,
    owner:'谭勋策',reviewed:true,note:'已入库',metadata_imported_evidence:proof});
   assert.equal(done.ok,true,JSON.stringify(done));assert.equal(done.data.row.markStatus,'已处理');assert.equal(done.data.row.remark,'已入库');
   console.log('PASS isolated official proceedings/journal pipeline '+n);
  }
  assert.deepEqual(await page.evaluate(()=>({uploads,imports,pushes})),{uploads:7,imports:7,pushes:7});
  await page.goto('about:blank');await page.goto('http://admin.ir.lib.sjtu.edu.cn/#/collectItem/batchManage');
  await page.evaluate(c=>testConfig.candidate=c,candidate);
  const bad=structuredClone(candidate);bad.primary_url=bad.fields['相关网址']='https://evil.example/paper';
  const refused=await page.evaluate(runExcelCommand,{...base(),candidate:bad,action:'import_excel_upload'});
  assert.equal(refused.ok,false);assert.equal(await page.evaluate(()=>uploads),0);
  const upload=await page.evaluate(runExcelCommand,{...base(),action:'import_excel_upload'});
  assert.equal(upload.ok,true,JSON.stringify(upload));
  const submit=await page.evaluate(runExcelCommand,{...base(),action:'import_excel_submit',upload:upload.data});
  assert.equal(submit.ok,true,JSON.stringify(submit));
  await page.evaluate(()=>{vm.$refs.batchViewModal.showDrawer=function(id){this.id=id;this.drawer=true;this.page.total=1;
    this.tableData=[{metadata:{title:['Synthetic paper'],doi:[''],author:[{fullname:'Alice Test'}],
    authorInstitution:[{address:'Shanghai Jiao Tong University'}],url:['https://wrong.example'],year:['2026'],conferenceName:['International Conference on Machine Learning']}}];};});
  const wrong=await page.evaluate(runImportCommand,{...base(),action:'import_check'});
  assert.equal(wrong.ok,false);assert.match(wrong.error,/官方网址/);assert.equal(await page.evaluate(()=>pushes),0);
  console.log('PASS invalid official source cannot upload; native URL mismatch cannot push');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
