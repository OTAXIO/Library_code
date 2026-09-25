const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('playwright');
const {runImportCommand}=require('../extension/import-adapter.js');
const {runWOSCommand}=require('../extension/wos-adapter.js');
const {inspectWorkPage}=require('../extension/page-diagnostics.js');
const fixture=fs.readFileSync(path.join(__dirname,'fixtures/import.html'),'utf8');
const wosFixture=fs.readFileSync(path.join(__dirname,'fixtures/wos.html'),'utf8');
const edge='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const candidate={title:'Synthetic paper',doi:'10.1234/test',wos:'WOS:000123456789012',sjtu:true,sha256:'a'.repeat(64)};
const cmd=(action,more={})=>({action,sa_id:'demo-001',instructions:'SA补充-demo-001',candidate,expires:Date.now()+15000,...more});
(async()=>{
  const browser=await chromium.launch({headless:true,...(fs.existsSync(edge)?{executablePath:edge}:{})});
  const context=await browser.newContext();
  await context.route('**/*',r=>r.fulfill({status:200,contentType:'text/html',body:new URL(r.request().url()).hostname==='admin.ir.lib.sjtu.edu.cn'?fixture:wosFixture}));
  const page=await context.newPage();
  const reset=async()=>{await page.goto('http://admin.ir.lib.sjtu.edu.cn/#/collectItem/batchManage');await page.reload();};
  const execute=c=>page.evaluate(runImportCommand,c);
  const upload=async()=>{
    const r=await execute(cmd('import_upload',{content:Buffer.from('synthetic TXT').toString('base64'),contentSha:candidate.sha256}));
    assert.equal(r.ok,true,JSON.stringify(r));return r.data;
  };
  const submit=async()=>{const data=await upload();const r=await execute(cmd('import_submit',{upload:data}));assert.equal(r.ok,true,JSON.stringify(r));};
  const tests=[];const test=(name,fn)=>tests.push([name,fn]);
  test('empty scan performs no writes',async()=>{
    assert.deepEqual((await execute(cmd('import_scan'))).data.batches,[]);
    assert.deepEqual(await page.evaluate(()=>writes),{upload:0,import:0,push:0});
  });
  test('complete import and priority-merge push verify original title and identifiers',async()=>{
    await submit();
    const checked=await execute(cmd('import_check'));
    assert.equal(checked.ok,true,JSON.stringify(checked));
    assert.equal(checked.data.batch.actual,0);
    const pushed=await execute(cmd('import_push',{batch:checked.data.batch}));assert.equal(pushed.ok,true,JSON.stringify(pushed));
    const verified=await execute(cmd('import_check',{batch_id:'batch-001',expect_pushed:true}));
    assert.equal(verified.data.batch.status,2);
    assert.equal(await page.evaluate(()=>push.form.duplicateItemProcessingType),'4');
    assert.equal(await page.evaluate(()=>push.form.duplicateQueryType),'ppt-composite');
    assert.deepEqual(await page.evaluate(()=>writes),{upload:1,import:1,push:1});
  });
  test('wrong route and foreign drawer never mutate',async()=>{
    await page.evaluate(()=>location.hash='#/wel/index');assert.equal((await execute(cmd('import_scan'))).ok,false);
    await reset();await page.evaluate(()=>drawer.drawer=true);
    assert.match((await execute(cmd('import_upload'))).error,/人工打开/);
    assert.equal(await page.evaluate(()=>writes.upload),0);
  });
  test('double upload and double import are refused',async()=>{
    const u=await upload();assert.equal((await execute(cmd('import_upload'))).ok,false);
    assert.equal((await execute(cmd('import_submit',{upload:u}))).ok,true);
    assert.equal((await execute(cmd('import_submit',{upload:u}))).ok,false);
    assert.equal(await page.evaluate(()=>writes.import),1);
  });
  test('wrong instructions and unhashed upload are refused',async()=>{
    assert.equal((await execute(cmd('import_upload',{instructions:'SA补充-other'}))).ok,false);
    assert.equal((await execute(cmd('import_upload',{content:'eA==',contentSha:'b'.repeat(64)}))).ok,false);
    assert.equal(await page.evaluate(()=>writes.upload),0);
  });
  test('changed institution or description after upload stops submission',async()=>{
    const u=await upload();await page.evaluate(()=>drawer.form.datasetId='other');
    assert.equal((await execute(cmd('import_submit',{upload:u}))).ok,false);
    assert.equal(await page.evaluate(()=>writes.import),0);
  });
  test('pre-existing same-description batch never uploads again',async()=>{
    await submit();await page.evaluate(()=>delete drawer.__saImport);
    assert.equal((await execute(cmd('import_upload'))).ok,false);assert.equal(await page.evaluate(()=>writes.upload),1);
  });
  test('wrong imported identifiers prevent push',async()=>{
    await submit();await page.evaluate(()=>testConfig.wrongUT=true);
    assert.match((await execute(cmd('import_check'))).error,/入藏号/);
    assert.equal(await page.evaluate(()=>writes.push),0);
  });
  test('duplicate instructions cannot select first batch',async()=>{
    await submit();await execute(cmd('import_scan'));
    await page.evaluate(()=>batches.push({...batches[0],id:'batch-002'}));
    assert.match((await execute(cmd('import_check'))).error,/唯一/);
  });
  test('missing PPT dedup option stops, never falls back to defaults',async()=>{
    await submit();const r=await execute(cmd('import_check'));
    await page.evaluate(()=>push.duplicateQueryTypes=[{value:'default',label:'唯一标识'}]);
    assert.match((await execute(cmd('import_push',{batch:r.data.batch}))).error,/PPT/);
    assert.equal(await page.evaluate(()=>writes.push),0);
  });
  test('WOS author search and foreign hosts are rejected',async()=>{
    await page.goto('https://www.webofscience.com/wos/author/author-search');
    assert.equal((await page.evaluate(runWOSCommand,{...cmd('wos_search'),title:'Synthetic paper'})).ok,false);
    for(const origin of ['http://webofscience.clarivate.cn','https://webofscience.clarivate.cn.example.invalid','https://www.webofscience.com.example.invalid']) {
      await page.goto(origin+'/wos/woscc/basic-search');
      assert.equal((await page.evaluate(runWOSCommand,{...cmd('wos_search'),title:'Synthetic paper'})).ok,false);
      assert.equal(await page.evaluate(()=>searches),0);
      assert.equal((await page.evaluate(inspectWorkPage)).error,'非工作网站');
    }
  });
  test('expired commands are rejected before scanning',async()=>{
    assert.equal((await execute(cmd('import_scan',{expires:Date.now()-1}))).ok,false);
  });
  test('WOS title query opens the single record and selects Full Record export',async()=>{
    await page.goto('https://www.webofscience.com/wos/woscc/basic-search');
    const base={...cmd('wos_search'),title:'Synthetic paper',doi:'10.1234/test',wos:''};
    const found=await page.evaluate(runWOSCommand,base);assert.equal(found.ok,true,JSON.stringify(found));
    const prep=await page.evaluate(runWOSCommand,{...base,action:'wos_prepare_export'});
    assert.equal(prep.ok,true,JSON.stringify(prep));
    assert.equal(await page.evaluate(()=>document.querySelector('select').value),'Full Record');
    const download=page.waitForEvent('download');
    const done=await page.evaluate(runWOSCommand,{...base,action:'wos_download'});
    assert.equal(done.ok,true,JSON.stringify(done));await download;
    assert.equal((await page.evaluate(runWOSCommand,{...base,action:'wos_download'})).ok,false);
    assert.equal(await page.evaluate(()=>exportsMade),1);
  });
  test('WOS multiple matches pause without opening the first record',async()=>{
    await page.goto('https://www.webofscience.com/wos/woscc/basic-search');
    await page.evaluate(()=>wosMany=true);
    const r=await page.evaluate(runWOSCommand,{...cmd('wos_search'),title:'Synthetic paper'});
    assert.equal(r.ok,false);assert.match(r.error,/唯一/);assert.ok(page.url().includes('/summary/'));
  });
  test('WOS Oops page reports site failure before field selection or any search',async()=>{
    await page.goto('https://www.webofscience.com/wos/woscc/basic-search');
    await page.evaluate(()=>document.getElementById('main').innerHTML="<h1>Oops, something went wrong!</h1><p>Please click on 'Search' at the top of the screen.</p>");
    const r=await page.evaluate(runWOSCommand,{...cmd('wos_search'),title:'Synthetic paper'});
    assert.match(r.error,/WOS 网站自身报错/);assert.doesNotMatch(r.error,/选择器未唯一/);
    assert.equal(await page.evaluate(()=>searches),0);
  });
  test('WOS Smart Search follows visible Advanced and Fielded tabs, without changing preferences',async()=>{
    await page.goto('https://www.webofscience.com/wos/woscc/basic-search');
    await page.evaluate(()=>smartPage());
    const r=await page.evaluate(runWOSCommand,{...cmd('wos_search'),title:'Synthetic paper'});
    assert.equal(r.ok,true,JSON.stringify(r));assert.equal(await page.evaluate(()=>searches),1);
  });
  test('WOS multiple field rows are still refused',async()=>{
    await page.goto('https://www.webofscience.com/wos/woscc/basic-search');
    await page.evaluate(()=>document.getElementById('main').appendChild(document.querySelector('select').cloneNode(true)));
    const r=await page.evaluate(runWOSCommand,{...cmd('wos_search'),title:'Synthetic paper'});
    assert.match(r.error,/识别到 2 个/);assert.equal(await page.evaluate(()=>searches),0);
  });
  test('WOS Chinese all-fields selector is supported',async()=>{
    await page.goto('https://www.webofscience.com/wos/woscc/basic-search');
    await page.evaluate(()=>document.querySelector('select').options[0].textContent='所有字段');
    const r=await page.evaluate(runWOSCommand,{...cmd('wos_search'),title:'Synthetic paper'});
    assert.equal(r.ok,true,JSON.stringify(r));
  });
  test('WOS Oops arising during search is not retried',async()=>{
    await page.goto('https://www.webofscience.com/wos/woscc/basic-search');
    await page.evaluate(()=>document.querySelector('button').onclick=()=>{searches++;document.getElementById('main').innerHTML='Oops, something went wrong!';});
    const r=await page.evaluate(runWOSCommand,{...cmd('wos_search'),title:'Synthetic paper'});
    assert.match(r.error,/WOS 网站自身报错/);assert.equal(await page.evaluate(()=>searches),1);
  });
  test('read-only diagnosis detects error page and excludes entered input text',async()=>{
    await page.goto('https://www.webofscience.com/wos/woscc/basic-search');
    await page.evaluate(()=>{document.querySelector('input').value='DO_NOT_DISCLOSE_QUERY';document.querySelector('input').setAttribute('role','combobox');
      const p=document.createElement('p');p.textContent='Oops, something went wrong!';document.body.appendChild(p);});
    const d=await page.evaluate(inspectWorkPage);assert.equal(d.wos_error,true);
    assert.ok(!JSON.stringify(d).includes('DO_NOT_DISCLOSE_QUERY'));assert.equal(await page.evaluate(()=>searches),0);
  });
  test('Clarivate CN supports diagnosis, title search, and one Full Record export on the same origin',async()=>{
    const origin='https://webofscience.clarivate.cn';
    await page.goto(origin+'/wos/woscc/basic-search');
    const diagnostic=await page.evaluate(inspectWorkPage);
    assert.equal(diagnostic.site,'webofscience.clarivate.cn');assert.equal(diagnostic.controls.length,1);
    const base={...cmd('wos_search'),title:'Synthetic paper'};
    const found=await page.evaluate(runWOSCommand,base);assert.equal(found.ok,true,JSON.stringify(found));
    assert.ok(found.data.record_url.startsWith(origin+'/wos/woscc/full-record/'));
    const prepared=await page.evaluate(runWOSCommand,{...base,action:'wos_prepare_export'});
    assert.equal(prepared.ok,true,JSON.stringify(prepared));assert.equal(await page.evaluate(()=>document.querySelector('select').value),'Full Record');
    const downloaded=page.waitForEvent('download');
    assert.equal((await page.evaluate(runWOSCommand,{...base,action:'wos_download'})).ok,true);
    await downloaded;assert.equal(await page.evaluate(()=>exportsMade),1);
    assert.equal((await page.evaluate(runWOSCommand,{...base,action:'wos_download'})).ok,false);
  });
  try{for(const [name,fn] of tests){await reset();await fn();console.log('PASS '+name);}console.log(`Workflow adapters: ${tests.length} offline cases passed.`);}
  finally{await context.close();await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
