/* Read-only recovery tests: isolated Edge profile and intercepted fixtures only. */
const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('playwright');
const {runImportCommand}=require('../extension/import-adapter.js');
const fixture=fs.readFileSync(path.join(__dirname,'fixtures/import.html'),'utf8');
const candidate={title:'Synthetic paper',doi:'10.1234/test',wos:'WOS:000123456789012',sjtu:true,sha256:'a'.repeat(64)};
const batch={id:'saved-batch',modelId:'article-model',batchNumber:'SYNTHETIC',source:'WOS',instructions:'SA补充-demo-001',
  total:1,actual:1,fail:0,status:2,increase:1,duplicateSkip:0,duplicateIncreaseUpdate:0,duplicateOverallCoverage:0};
let browser,context,page;
test.before(async()=>{
  browser=await chromium.launch({headless:true,executablePath:process.env.SA_TEST_BROWSER||'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  context=await browser.newContext();
  await context.route('**/*',r=>r.fulfill({status:200,contentType:'text/html',body:fixture}));
  page=await context.newPage();
});
test.after(async()=>{await context?.close();await browser?.close();});
test.beforeEach(async()=>{await page.goto('about:blank');await page.goto('http://admin.ir.lib.sjtu.edu.cn/#/collectItem/batchManage');});
const run=extra=>page.evaluate(runImportCommand,{action:'import_check',sa_id:'demo-001',instructions:'SA补充-demo-001',
  candidate,expires:Date.now()+7500,...extra});
const noWrites=async()=>assert.deepEqual(await page.evaluate(()=>writes),{upload:0,import:0,push:0});

test('no matching batch reports zero and preserves the uncertain submission',async()=>{
  const r=await run({});assert.equal(r.ok,false);assert.match(r.error,/未找到本条导入批次/);
  assert.match(r.error,/完整扫描 0 条，同说明 0 条/);assert.match(r.error,/保留断点/);await noWrites();
});
test('several same-description rows are explicit ambiguity without a saved ID',async()=>{
  await page.evaluate(b=>batches.push(b,{...b,id:'other-batch'}),batch);
  const r=await run({});assert.equal(r.ok,false);assert.match(r.error,/找到 2 个同说明批次/);
  assert.match(r.error,/无法唯一/);await noWrites();
});
test('saved exact batch ID can be recovered read-only among duplicate descriptions',async()=>{
  await page.evaluate(b=>batches.push({...b,id:'other-batch'},b),batch);
  const r=await run({batch_id:batch.id,model_id:batch.modelId,expect_pushed:true});
  assert.equal(r.ok,true,JSON.stringify(r));assert.equal(r.data.batch.id,batch.id);await noWrites();
});
test('legacy WOS null and empty IDs still require a unique exact candidate',async()=>{
  await page.evaluate(b=>batches.push(b),batch);
  for(const batch_id of [null,'']){
    const r=await run({batch_id});assert.equal(r.ok,true,JSON.stringify(r));assert.equal(r.data.batch.id,batch.id);
  }
  await page.evaluate(b=>batches.push({...b,id:'other-batch'}),batch);
  assert.match((await run({batch_id:null})).error,/找到 2 个同说明批次/);await noWrites();
});
test('missing saved ID never substitutes the other same-description row',async()=>{
  await page.evaluate(b=>batches.push({...b,id:'other-batch'}),batch);
  const r=await run({batch_id:batch.id});assert.equal(r.ok,false);assert.match(r.error,/未找到原批次 ID/);
  assert.match(r.error,/同说明 1 条/);await noWrites();
});
test('saved model mismatch is rejected before opening batch details',async()=>{
  await page.evaluate(b=>batches.push(b),batch);
  const r=await run({batch_id:batch.id,model_id:'wrong-model'});assert.equal(r.ok,false);assert.match(r.error,/模型与原入库断点不一致/);
  assert.equal(await page.evaluate(()=>view.drawer),false);await noWrites();
});
test('exact read recovery does not authorize pushing an ambiguous description',async()=>{
  await page.evaluate(b=>batches.push(b,{...b,id:'other-batch'}),batch);
  const r=await run({action:'import_push',batch_id:batch.id,batch});assert.equal(r.ok,false);
  assert.match(r.error,/找到 2 个同说明批次/);await noWrites();
});
test('ambiguous-description Excel read cannot close an upload or authorize a next-form reset',async()=>{
  const c={...candidate,kind:'crossref-openalex-v1',source:'Crossref+OpenAlex',wos:'',author_names:['Alice Test']};
  for(const open of [true,false]){
    await page.evaluate(({b,c,open})=>{
      batches.splice(0,batches.length,{...b,source:'人工提交'},{...b,id:'other-batch',source:'人工提交'});
      vm.$refs.batch={drawer:open,form:{instructions:b.instructions,modelId:b.modelId,datasource:'人工提交'},
        $children:[],__saImport:{sa_id:'demo-001',submitted:true}};
      view.showDrawer=function(id){this.id=id;this.drawer=true;this.loading=false;this.page.total=1;
        this.tableData=[{metadata:{title:[c.title],doi:[c.doi],author:[{fullname:'Alice Test'}],
          authorInstitution:['Shanghai Jiao Tong University']}}];};
    },{b:batch,c,open});
    const r=await run({candidate:c,batch_id:batch.id,model_id:batch.modelId});
    assert.equal(r.ok,true,JSON.stringify(r));assert.equal(await page.evaluate(()=>vm.$refs.batch.drawer),open);
    assert.equal(await page.evaluate(()=>vm.$refs.batch.__saCompleted),undefined);await noWrites();
  }
});
test('null boolean and empty totals cannot prove a complete empty scan',async()=>{
  for(const value of [null,true,'']){
    await page.evaluate(value=>{const getData=vm.getData;vm.getData=function(...args){getData.apply(this,args);
      setTimeout(()=>this.page.total=value,35);};},value);
    const r=await run({action:'import_scan'});assert.equal(r.ok,false);assert.match(r.error,/批次数量未知/);await noWrites();
  }
});
test('all-source scan clears only the query filter and proves a complete empty match set',async()=>{
  await page.evaluate(b=>batches.push({...b,id:'unrelated-one',instructions:'another-task',source:'人工提交'},
    {...b,id:'unrelated-two',instructions:'another-task-2',source:'WOS'}),batch);
  const r=await run({action:'import_scan',all_sources:true});
  assert.equal(r.ok,true,JSON.stringify(r));assert.equal(r.data.all_sources,true);
  assert.equal(r.data.scan_protocol,1);assert.equal(r.data.scanned_count,2);
  assert.deepEqual(r.data.batches,[]);assert.equal(await page.evaluate(()=>vm.queryForm.source),'');
  await noWrites();
});
test('all-source scan cannot hide a same-description batch with a different source',async()=>{
  await page.evaluate(b=>batches.push({...b,source:'人工提交'}),batch);
  const r=await run({action:'import_scan',all_sources:true});assert.equal(r.ok,false);
  assert.match(r.error,/批次来源/);await noWrites();
});
test('all-source option never changes the source of an upload submit or push',async()=>{
  for(const action of ['import_upload','import_submit','import_push']){
    const r=await run({action,all_sources:true});assert.equal(r.ok,false,JSON.stringify(r));
    assert.match(r.error,/只读扫描/);
  }
  await noWrites();
});
