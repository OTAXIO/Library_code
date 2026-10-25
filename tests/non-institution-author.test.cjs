/* Isolated native fixtures: author task closure, without claiming the paper. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {test,before,after}=require('node:test');const {chromium}=require('playwright');
const {runSACommand}=require('../extension/adapter.js');let browser,page;
before(async()=>{browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
 const context=await browser.newContext();const fixture=fs.readFileSync(path.join(__dirname,'fixtures/compare.html'),'utf8');
 await context.route('**/*',r=>r.fulfill({status:200,contentType:'text/html',body:fixture}));page=await context.newPage();});
after(async()=>{await browser?.close();});
const command=(action,extra={})=>({action,sa_id:'demo-001',expires:Date.now()+60000,...extra});
async function setup(){
 await page.goto('about:blank');await page.goto('http://admin.ir.lib.sjtu.edu.cn/#/dataCompare/list');
 await page.evaluate(()=>{
  testConfig.authors=[{id:'a-1',order:1,fullname:'Demo',scholarId:null,institutionOrderNums:[1]}];
  const institutions=[{order:1,address:'Example Foreign University, Other City',topInstitutionId:'foreign'}];
  detail.itemData={id:synthetic.itemId,metadata:{author:copy(testConfig.authors),authorInstitution:copy(institutions)}};
  const nativeRead=claimWindow.getItemDetail;
  claimWindow.getItemDetail=async function(){await nativeRead.call(this);this.tableData.metadata.authorInstitution=copy(institutions);};
  claimWindow.getShows=()=>false;
 });
 const read=await page.evaluate(runSACommand,command('search'));assert.equal(read.ok,true,JSON.stringify(read));
 const params={expected:read.data.row,sa_text:read.data.comparison.find(x=>x.label==='认领状态').sa,
  staff_id:'00001',roster_staff_id:'00001'};
 const prep=await page.evaluate(runSACommand,command('prepare_claim',params));assert.equal(prep.ok,true,JSON.stringify(prep));
 const author=prep.data.prepared.authors[0];assert.equal(author.non_institution,true);assert.equal(author.eligible,false);
 return command('complete',{expected:read.data.row,expected_comparison:read.data.comparison,
  reviewed:true,owner:'谭勋策',note:'非交大作者',non_institution_author_evidence:{prepared:prep.data.prepared,author}});
}
test('all non-institution author rows can still resolve full staff and close the author task once',async()=>{
 const input=await setup();const r=await page.evaluate(runSACommand,input);assert.equal(r.ok,true,JSON.stringify(r));
 assert.equal(r.data.row.markStatus,'已处理');assert.equal(r.data.row.remark,'非交大作者');
 assert.equal(await page.evaluate(()=>claimWrites),0);assert.equal(await page.evaluate(()=>writeCount),1);
});
test('unrelated unit ordering and presentation state do not block the target author closure',async()=>{
 const input=await setup();await page.evaluate(()=>{
  detail.itemData.metadata.authorInstitution[0].claimStatus=20;
  detail.itemData.metadata.authorInstitution.push({order:2,address:'Shanghai Jiao Tong Univ',topInstitutionId:'sjtu'});
  detail.itemData.metadata.authorInstitution.reverse();
 });
 const r=await page.evaluate(runSACommand,input);assert.equal(r.ok,true,JSON.stringify(r));
 assert.equal(r.data.row.remark,'非交大作者');assert.equal(await page.evaluate(()=>claimWrites),0);
});
test('a changed affiliation or newly claimed author prevents the negative closure',async()=>{
 for(const change of ['unit','claim']){
  const input=await setup();await page.evaluate(change=>{
   if(change==='unit')detail.itemData.metadata.authorInstitution[0].address='Shanghai Jiao Tong Univ';
   else detail.itemData.metadata.author[0].scholarId='another-person';
  },change);
  const r=await page.evaluate(runSACommand,input);assert.equal(r.ok,false);assert.match(r.error,/资料已变化/);
  assert.equal(await page.evaluate(()=>writeCount),0);
 }
});
test('modern staff cards await the complete native asynchronous detail reader',async()=>{
 await page.goto('about:blank');await page.goto('http://admin.ir.lib.sjtu.edu.cn/#/dataCompare/list');
 await page.evaluate(()=>{
  testConfig.modern=true;testConfig.wrongModernStaff='00002';
  detail.refreshCompareData=async function(){
   await new Promise(r=>setTimeout(r,80));
   const row=this.currentRows.find(x=>x.label==='认领状态');
   row.compareLeftValue=row.compareLeftValue.replace('00002','00001');
  };
 });
 const r=await page.evaluate(runSACommand,command('search'));
 assert.equal(r.ok,true,JSON.stringify(r));
 assert.equal(r.data.comparison.find(x=>x.label==='认领状态').sa,'测试员(00001)');
 assert.equal(await page.evaluate(()=>writeCount),0);
});
