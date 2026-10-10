/* Single-purpose intake closure. Every page/request uses an offline fixture. */
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const {runSACommand} = require('../extension/adapter.js');
const fixture = fs.readFileSync(path.join(__dirname,'fixtures/compare.html'),'utf8');
const options={headless:true};
const edge='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
if(process.env.SA_TEST_BROWSER)options.executablePath=process.env.SA_TEST_BROWSER;
else if(fs.existsSync(edge))options.executablePath=edge;
(async()=>{
  const browser=await chromium.launch(options);
  try{
    const context=await browser.newContext();
    await context.route('**/*',route=>route.fulfill({status:200,contentType:'text/html',body:fixture}));
    const page=await context.newPage();
    const candidate={title:'Synthetic paper',doi:'10.1234/test',wos:'WOS:000123456789012',
      authors:'Test, Alice',affiliation:'[Test, Alice] Shanghai Jiao Tong Univ, China',sha256:'a'.repeat(64)};
    const batch={id:'batch-1',modelId:'article',source:'WOS',instructions:'SA补充-demo-001',
      status:2,total:1,actual:1,fail:0};
    const cases=[
      {name:'verified import closes with exact 已入库 and never claims',ok:true},
      {name:'author or first-unit reason does not block confirmed intake',row:{reason:'第一单位不一致；作者不一致'},ok:true},
      {name:'missing receipt cannot close',missing:true},
      {name:'other owner cannot close',owner:'其他人'},
      {name:'download-only cannot close',batch:{status:0,actual:0}},
      {name:'import submitted but unpushed cannot close',batch:{status:1,actual:0}},
      {name:'failed batch cannot close',batch:{fail:1}},
      {name:'multiple imported records cannot close',batch:{total:2,actual:2}},
      {name:'boolean counts cannot masquerade as one',batch:{total:true}},
      {name:'another batch instructions cannot close',batch:{instructions:'SA补充-other'}},
      {name:'another source cannot close',batch:{source:'CNKI'}},
      {name:'missing batch ID cannot close',batch:{id:''}},
      {name:'missing exact platform linkage cannot close',item:'999'},
      {name:'zero-match cannot close as already imported',row:{matchCount:0,itemId:''}},
      {name:'title-only cannot close',row:{doiValue:'',wosValue:''}},
      {name:'different DOI cannot close',candidate:{doi:'10.1234/other'}},
      {name:'non-SJTU cannot close as imported',candidate:{affiliation:'[Test, Alice] Other Univ, China'}},
      {name:'different library WOS cannot close',config:{libraryWos:'WOS:999999999999999'}},
      {name:'different SA comparison cannot close',config:{saDoi:'10.1234/changed'}},
      {name:'existing human remark cannot be overwritten',row:{remark:'人工待复核'}},
      {name:'missing expected comparison cannot close',noComparison:true},
      {name:'bad server readback is uncertain, not success',config:{badRemark:true},uncertain:true},
    ];
    for(const c of cases){
      await page.goto('about:blank');
      await page.goto('http://admin.ir.lib.sjtu.edu.cn/#/dataCompare/list');
      await page.evaluate(input=>{
        Object.assign(synthetic,{doiValue:'10.1234/test',wosValue:'',...input.row});
        Object.assign(testConfig,{saDoi:'10.1234/test',libraryDoi:'10.1234/test',
          libraryWos:'WOS:000123456789012',...input.config});
      },{row:c.row??{},config:c.config??{}});
      const base={id:'test-import-closure',sa_id:'demo-001',expires:Date.now()+60000};
      const read=await page.evaluate(runSACommand,{...base,action:'search'});
      assert.equal(read.ok,true,JSON.stringify(read));
      assert.equal(read.data.import_completion_protocol,1);
      const result=await page.evaluate(runSACommand,{...base,action:'complete',expected:read.data.row,
        expected_comparison:c.noComparison?null:read.data.comparison,
        note:'已入库',reviewed:true,owner:c.owner??'谭勋策',
        imported_evidence:c.missing?null:{candidate:{...candidate,...c.candidate},batch:{...batch,...c.batch},
          item_id:c.item??'1234567890123456789'}});
      assert.equal(result.ok,!!c.ok,c.name+': '+JSON.stringify(result));
      assert.equal(await page.evaluate(()=>writeCount),c.ok||c.uncertain?1:0,c.name);
      assert.equal(await page.evaluate(()=>claimWrites),0,c.name);
      if(c.ok){
        assert.equal(result.data.row.remark,'已入库');
        assert.equal(result.data.row.markStatus,'已处理');
        const repeat=await page.evaluate(runSACommand,{...base,action:'complete',expected:result.data.row,
          reviewed:true,note:'已入库',owner:'谭勋策',imported_evidence:{candidate,batch,item_id:'1234567890123456789'}});
        assert.equal(repeat.ok,false);
        assert.equal(await page.evaluate(()=>writeCount),1);
      }
      if(c.uncertain)assert.match(result.error,/已发出写入/);
      console.log('PASS imported completion: '+c.name);
    }
    console.log(`Imported completion: ${cases.length} offline checks passed. No production requests.`);
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
