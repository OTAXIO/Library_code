/* Zero-match 需导入 closure: one native status write, nothing imported or claimed. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {test,before,after}=require('node:test');const {chromium}=require('playwright');
const {runSACommand}=require('../extension/adapter.js');let browser,page;
before(async()=>{browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
 const context=await browser.newContext();const fixture=fs.readFileSync(path.join(__dirname,'fixtures/compare.html'),'utf8');
 await context.route('**/*',r=>r.fulfill({status:200,contentType:'text/html',body:fixture}));page=await context.newPage();});
after(async()=>{await browser?.close();});
test('cross review sets handler and processed filters, reads the requested page and rejects mixed rows',async()=>{
 await page.goto('http://admin.ir.lib.sjtu.edu.cn/#/dataCompare/list');
 await page.evaluate(()=>{
  synthetic.markStatus='已处理'; synthetic.updateUsername='测试员';
  vm.getData=async function(){
   if(this.searchForm.markStatus!=='已处理'||this.searchForm.updateUsername!=='测试员'||
      this.searchForm.saLzkId!==''||this.page.currentPage!==2) throw Error('Wrong scope');
   this.tableData=[{...synthetic}];this.page.total=11;
  };
 });
 const request={action:'cross_review_page',owner:'测试员',page:2,expires:Date.now()+60000};
 const answer=await page.evaluate(runSACommand,request);
 assert.equal(answer.ok,true,JSON.stringify(answer));assert.equal(answer.data.page,2);
 await page.evaluate(()=>{synthetic.updateUsername='他人'});
 assert.equal((await page.evaluate(runSACommand,request)).ok,false);
 assert.equal(await page.evaluate(()=>writeCount),0);
});
const command=(action,extra={})=>({action,sa_id:'demo-001',expires:Date.now()+60000,...extra});
async function zeroRow(changes={}){
 await page.goto('about:blank');await page.goto('http://admin.ir.lib.sjtu.edu.cn/#/dataCompare/list');
 await page.evaluate(changes=>Object.assign(synthetic,{itemId:'',matchCount:0,reason:''},changes),changes);
 const read=await page.evaluate(runSACommand,command('status'));assert.equal(read.ok,true,JSON.stringify(read));
 assert.equal(read.data.need_import_protocol,1);
 return command('complete',{expected:read.data.row,reviewed:true,owner:'测试员',note:'需导入'});
}
test('a pending zero-match row is closed once with the 需导入 note for any named owner',async()=>{
 const r=await page.evaluate(runSACommand,await zeroRow());assert.equal(r.ok,true,JSON.stringify(r));
 assert.equal(r.data.row.markStatus,'已处理');assert.equal(r.data.row.remark,'需导入');
 assert.equal(await page.evaluate(()=>writeCount),1);assert.equal(await page.evaluate(()=>claimWrites),0);
});
test('matched, linked, noted or unowned rows are refused before any write',async()=>{
 for(const [changes,owner] of [[{matchCount:1,itemId:'1234567890123456789'},'测试员'],
     [{itemId:'1234567890123456789'},'测试员'],[{remark:'人工备注'},'测试员'],[{},'（未分配）'],[{},'']]){
  const input=await zeroRow(changes);input.owner=owner;
  const r=await page.evaluate(runSACommand,input);assert.equal(r.ok,false,JSON.stringify(changes));
  assert.match(r.error,/^【已暂停】/);assert.equal(await page.evaluate(()=>writeCount),0);
 }
});
test('the 需导入 note is not a shortcut for single-match claim rows',async()=>{
 await page.goto('about:blank');await page.goto('http://admin.ir.lib.sjtu.edu.cn/#/dataCompare/list');
 const read=await page.evaluate(runSACommand,command('status'));
 const r=await page.evaluate(runSACommand,command('complete',{expected:read.data.row,reviewed:true,owner:'测试员',note:'需导入'}));
 assert.equal(r.ok,false);assert.equal(await page.evaluate(()=>writeCount),0);
});
