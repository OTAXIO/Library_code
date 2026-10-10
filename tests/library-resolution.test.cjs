/* Disposable browser, every HTTP request intercepted; no production traffic. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require('playwright');
const {resolveLibraryRecord}=require('../extension/library-adapter.js');
const edge='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const origin='http://admin.ir.lib.sjtu.edu.cn';
const candidate={title:'Synthetic paper',doi:'10.1234/test',wos:'WOS:000123456789012',sjtu:true,sha256:'a'.repeat(64)};
const item={id:'1234567890123456789',metadata:{title:[candidate.title],doi:[candidate.doi],wosId:[candidate.wos]}};
const fixture=`<div id="app">Disposable library fixture</div><script>
window.config={rows:[],failure:false,incomplete:false};window.reads=[];window.routes=[];
const root=document.getElementById('app');
const library={$el:root,$options:{name:'EntryManage',components:{ItemEdit:{}}},$children:[],
  $refs:{itemDetail:{}},page:{current:1,size:10},queryForm:{},tableData:[],total:0,loading:false,
  getData(page,query){reads.push({...query});this.loading=true;this.tableData=[];
    setTimeout(()=>{if(!config.failure){this.tableData=config.rows.map(x=>({...x}));this.total=this.tableData.length+(config.incomplete?1:0);}this.loading=false;},10);}};
const batch={$el:root,$options:{name:'BatchManage'},$children:[],
  $refs:{wosDataText:{},batchPushModal:{}},$router:{async push(path){routes.push(path);location.hash='#'+path;root.__vue__=path==='/item/entryManage'?library:batch;}}};
root.__vue__=batch;
</script>`;
(async()=>{
  const browser=await chromium.launch({headless:true,...(fs.existsSync(edge)?{executablePath:edge}:{})});
  try{
    const context=await browser.newContext();
    await context.route('**/*',route=>route.fulfill({status:200,contentType:'text/html',body:fixture}));
    const page=await context.newPage();
    const cases=[
      {name:'empty verified DOI and title queries prove absence',rows:[],ok:true,count:0},
      {name:'exact existing paper returns all 19 ID digits',rows:[item],ok:true,count:1},
      {name:'numeric ID is never rounded into a platform match',rows:[{...item,id:1234567890123456789}],ok:false,error:/精确数字文本/},
      {name:'same DOI with different WOS ID blocks duplicate import',rows:[{...item,metadata:{...item.metadata,wosId:['WOS:999999999999999']}}],ok:false,error:/冲突/},
      {name:'same title with missing identifiers needs dedupe',rows:[{...item,metadata:{title:[candidate.title],doi:[],wosId:[]}}],ok:false,error:/人工查重/},
      {name:'HTTP failure is not a zero result',rows:[],failure:true,ok:false,error:/未确认成功/},
      {name:'truncated results do not prove absence',rows:[item],incomplete:true,ok:false,error:/不完整/},
      {name:'duplicate papers remain separate, never choose one',rows:[item,{...item,id:'9876543210987654321'}],ok:true,count:2},
    ];
    for(const c of cases){
      await page.goto(origin+'/#/collectItem/batchManage');
      await page.reload();
      await page.evaluate(config=>window.config={failure:false,incomplete:false,...config},c);
      const result=await page.evaluate(resolveLibraryRecord,{action:'import_resolve',sa_id:'demo-001',candidate,expires:Date.now()+30000});
      assert.equal(result.ok,c.ok,JSON.stringify(result)+' '+c.name);
      if(c.ok){assert.equal(result.data.items.length,c.count);if(c.count)assert.equal(result.data.items[0].id,item.id);}
      else assert.match(result.error,c.error);
      assert.equal(new URL(page.url()).hash,'#/collectItem/batchManage',c.name);
      const reads=await page.evaluate(()=>window.reads);
      assert.equal(reads[0].doi,candidate.doi,c.name);
      if(c.ok)assert.equal(reads[1].title,candidate.title,c.name);
      console.log('PASS '+c.name);
    }
    await page.goto(origin+'/#/collectItem/batchManage');
    await page.reload();
    await page.evaluate(()=>document.body.insertAdjacentHTML('beforeend','<div class="el-message-box">Human operation</div>'));
    const result=await page.evaluate(resolveLibraryRecord,{action:'import_resolve',candidate,expires:Date.now()+30000});
    assert.equal(result.ok,false);assert.match(result.error,/人工操作/);
    assert.deepEqual(await page.evaluate(()=>window.reads),[]);
    console.log('PASS human operation window never closed or queried');
    console.log('Library resolution: 9 offline cases passed.');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
