/* No production traffic. Test the observed native encrypted-client contract. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {test,before,after}=require('node:test');const {chromium}=require('playwright');
const {runSACommand}=require('../extension/adapter.js');let browser,page;
before(async()=>{browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
 const context=await browser.newContext();const fixture=fs.readFileSync(path.join(__dirname,'fixtures/compare.html'),'utf8');
 await context.route('**/*',r=>r.fulfill({status:200,contentType:'text/html',body:fixture}));page=await context.newPage();});
after(async()=>{await browser?.close();});
const command=(action,extra={})=>({action,sa_id:'demo-001',expires:Date.now()+60000,...extra});
async function setup(mode){
 await page.goto('about:blank');await page.goto('http://admin.ir.lib.sjtu.edu.cn/#/dataCompare/list');
 const read=await page.evaluate(runSACommand,command('search'));assert.equal(read.ok,true,JSON.stringify(read));
 const params={expected:read.data.row,sa_text:read.data.comparison.find(x=>x.label==='认领状态').sa,
  staff_id:'00001',roster_staff_id:'00001'};
 const prep=await page.evaluate(runSACommand,command('prepare_claim',params));assert.equal(prep.ok,true,JSON.stringify(prep));
 await page.evaluate(mode=>{
  window.nativeClaimRequests=[];
  vm.$http.request=async config=>{
   nativeClaimRequests.push({url:config.url,method:config.method,params:config.params,data:config.data,
     accepts400:config.validateStatus(400),rejects401:!config.validateStatus(401),rejects500:!config.validateStatus(500)});
   if(mode==='ok'){claimedUsers[config.params.order]=config.params.scholarId;return {status:200,data:{success:true,code:200}};}
   if(mode==='false-success')return {status:200,data:{success:true,code:200}};
   if(mode==='cipher')throw {status:400,data:{data:'unparsed encrypted error'}};
   if(mode==='changed')claimedUsers[config.params.order]='different-scholar';
   throw {status:400,data:{success:false,code:400,msg:'Synthetic required body validation error'}};
  };
 },mode);
 return command('submit_claim',{...params,prepared:prep.data.prepared,author_index:prep.data.suggested_index,confirmed:true});
}
test('one native-client request carries identical body and query and verifies actual author relation',async()=>{
 const input=await setup('ok');const r=await page.evaluate(runSACommand,input);assert.equal(r.ok,true,JSON.stringify(r));
 assert.equal(r.data.claimed,true);assert.equal(r.data.staff_id,'00001');
 const requests=await page.evaluate(()=>nativeClaimRequests);assert.equal(requests.length,1);
 assert.deepEqual(requests[0].data,requests[0].params);assert.equal(requests[0].params.scholarId,'scholar-001');
 assert.equal(requests[0].accepts400,true);assert.equal(requests[0].rejects401,true);assert.equal(requests[0].rejects500,true);
});
test('decoded explicit rejection returns immediately after unchanged native relationship read, no retry',async()=>{
 const input=await setup('reject');const r=await page.evaluate(runSACommand,input);assert.equal(r.ok,true,JSON.stringify(r));
 assert.equal(r.data.rejected,true);assert.equal(r.data.unchanged,true);assert.equal(r.data.verified,false);
 assert.equal(r.data.rejection.status,400);assert.equal(await page.evaluate(()=>nativeClaimRequests.length),1);
});
test('a rejection with changed authors is uncertain and cannot advance the queue',async()=>{
 const input=await setup('changed');const r=await page.evaluate(runSACommand,input);
 assert.equal(r.ok,false);assert.match(r.error,/关系已变化/);assert.match(r.error,/禁止自动重试/);
 assert.equal(await page.evaluate(()=>nativeClaimRequests.length),1);
});
test('unparsed encrypted errors and a false successful transport never become verified claims',async()=>{
 for(const mode of ['cipher','false-success']){
  const input=await setup(mode);const r=await page.evaluate(runSACommand,input);assert.equal(r.ok,false);
  assert.match(r.error,/已发出写入/);assert.equal(await page.evaluate(()=>nativeClaimRequests.length),1);
 }
});
