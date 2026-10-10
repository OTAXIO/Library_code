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
      authors:'Test, Alice; Other, Bob',affiliation:'[Test, Alice] Other University, China; [Other, Bob] Another Institute, Country',
      sjtu:true,sha256:'a'.repeat(64)}; // Cached boolean must not replace the C1 evidence.
    const cases=[
      {name:'complete negative writes exact remark and processed state once',ok:true},
      {name:'missing Full Record proof never authorizes the short negative',evidence:null},
      {name:'missing strong identifiers cannot close a title-only record',row:{doiValue:'',wosValue:''}},
      {name:'conflicting DOI cannot close',evidence:{doi:'10.1234/other'}},
      {name:'another paper title cannot close',evidence:{title:'Another paper'}},
      {name:'SJTU address cannot close as non-SJTU',evidence:{affiliation:'[Test, Alice; Other, Bob] Shanghai Jiao Tong Univ, China'}},
      {name:'an uncovered coauthor cannot close',evidence:{affiliation:'[Test, Alice] Other Univ, China'}},
      {name:'truncated C1 cannot close',evidence:{affiliation:'[Test, Alice; Other, Bob] Other Univ, China; …'}},
      {name:'existing human remark cannot be overwritten',row:{remark:'人工已有说明'}},
      {name:'first-author judgement remains deferred',row:{reason:'第一作者不一致'}},
      {name:'matched platform record cannot use zero-match closure',row:{matchCount:1,itemId:'1234567890123456789'}},
      {name:'other owner cannot authorize a negative',owner:'其他人'},
      {name:'bad server readback remains uncertain',config:{badRemark:true},uncertain:true},
      {name:'an identifier-only strong match with complete addresses can close',row:{doiValue:'',wosValue:'000123456789012'},ok:true},
    ];
    for(const c of cases){
      await page.goto('about:blank'); // Same-URL goto alone can retain the previous fixture's writes.
      await page.goto('http://admin.ir.lib.sjtu.edu.cn/#/dataCompare/list');
      await page.evaluate(input=>{
        Object.assign(synthetic,{itemId:'',matchCount:0,reason:'',doiValue:'10.1234/test',wosValue:'',...input.row});
        Object.assign(testConfig,input.config);
      },{row:c.row??{},config:c.config??{}});
      const base={id:'test-negative',sa_id:'demo-001',expires:Date.now()+60000};
      const read=await page.evaluate(runSACommand,{...base,action:'status'});
      assert.equal(read.ok,true,JSON.stringify(read));
      assert.equal(read.data.non_sjtu_completion_protocol,1);
      const result=await page.evaluate(runSACommand,{...base,action:'complete',expected:read.data.row,
        note:'非交大',reviewed:true,owner:c.owner??'谭勋策',
        non_sjtu_evidence:c.evidence===null?null:{...candidate,...c.evidence}});
      assert.equal(result.ok,!!c.ok,c.name+': '+JSON.stringify(result));
      assert.equal(await page.evaluate(()=>writeCount),c.ok||c.uncertain?1:0,c.name);
      if(c.ok){
        assert.equal(result.data.row.remark,'非交大');
        assert.equal(result.data.row.markStatus,'已处理');
        assert.equal(result.data.row.itemId,'');
        assert.equal(await page.evaluate(()=>claimWrites),0);
        const repeat=await page.evaluate(runSACommand,{...base,action:'complete',expected:result.data.row,
          reviewed:true,note:'非交大',owner:'谭勋策',non_sjtu_evidence:candidate});
        assert.equal(repeat.ok,false);
        assert.equal(await page.evaluate(()=>writeCount),1);
      }
      if(c.uncertain)assert.match(result.error,/已发出写入/);
      console.log('PASS non-SJTU: '+c.name);
    }
    console.log(`Non-SJTU completion: ${cases.length} offline checks passed. No production requests.`);
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
