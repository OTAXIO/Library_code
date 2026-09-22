const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const readline=require('node:readline');
const {spawn}=require('node:child_process');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'..');
const pythonBundle=path.join(os.homedir(),'.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe');
const python=process.env.SA_TEST_PYTHON || (fs.existsSync(pythonBundle)?pythonBundle:'python');
const edge='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
(async()=>{
  const backend=spawn(python,['-u','-m','tests.extension_backend'],{cwd:root,windowsHide:true,stdio:['pipe','pipe','pipe']});
  let stderr='';backend.stderr.on('data',x=>stderr+=x);
  const lines=[], waiters=[];
  readline.createInterface({input:backend.stdout}).on('line',line=>{
    const parsed=JSON.parse(line);if(waiters.length)waiters.shift()(parsed);else lines.push(parsed);
  });
  let stopped=false;
  backend.on('exit',()=>{stopped=true;while(waiters.length)waiters.shift()({ok:false,error:stderr||'test bridge exited'});});
  const next=()=>lines.length?Promise.resolve(lines.shift()):stopped?Promise.reject(new Error(stderr)):
    new Promise(resolve=>waiters.push(resolve));
  const invoke=async(action,payload)=>{backend.stdin.write(JSON.stringify({action,payload})+'\n');return next();};
  let context;
  try {
    const pair=await next();assert.ok(pair.token,stderr||'bridge failed');
    const extension=path.join(root,'extension');
    context=await chromium.launchPersistentContext('',{headless:true,
      ...(process.env.SA_TEST_BROWSER?{executablePath:process.env.SA_TEST_BROWSER}:fs.existsSync(edge)?{executablePath:edge}:{}),
      args:[`--disable-extensions-except=${extension}`,`--load-extension=${extension}`]});
    const fixture=fs.readFileSync(path.join(__dirname,'fixtures/compare.html'),'utf8');
    await context.route('**/*',route=>{
      const url=new URL(route.request().url());
      if(url.hostname==='admin.ir.lib.sjtu.edu.cn')return route.fulfill({status:200,contentType:'text/html',body:fixture});
      if(url.hostname==='127.0.0.1'||url.protocol==='chrome-extension:')return route.continue();
      return route.abort();
    });
    const worker=context.serviceWorkers()[0]||await context.waitForEvent('serviceworker',{timeout:10000});
    const extensionId=new URL(worker.url()).hostname;
    const site=await context.newPage();
    await site.goto('http://admin.ir.lib.sjtu.edu.cn/#/dataCompare/list');
    const popup=await context.newPage();
    await popup.goto(`chrome-extension://${extensionId}/popup.html`);
    const connection=await popup.evaluate(async token=>{
      const tabs=await chrome.tabs.query({url:'http://admin.ir.lib.sjtu.edu.cn/*'});
      return chrome.runtime.sendMessage({type:'pair',tabId:tabs[0].id,token});
    },pair.token);
    assert.equal(connection.ok,true,connection.error);
    console.log('PASS real extension pairs through authenticated loopback');
    await site.waitForTimeout(1800);
    let read=await invoke('search',{sa_id:'demo-001'});
    assert.equal(read.ok,true,JSON.stringify(read));
    assert.equal(read.data.row.saLzkId,'demo-001');
    console.log('PASS desktop bridge -> extension -> page -> result round trip');
    const changed=await invoke('link',{sa_id:'demo-001',expected:read.data.row,reviewed:true,
      note:'已核验测试文献与平台唯一号',item_id:'9876543210987654321'});
    assert.equal(changed.ok,true,JSON.stringify(changed));
    assert.equal(changed.data.row.itemId,'9876543210987654321');
    console.log('PASS platform edit round trip preserves ID');
    await site.evaluate(()=>testConfig.claim='已认领');
    read=await invoke('search',{sa_id:'demo-001'});
    assert.equal(read.ok,true,JSON.stringify(read));
    const completed=await invoke('complete',{sa_id:'demo-001',expected:read.data.row,expected_comparison:read.data.comparison,reviewed:true,note:'已认领'});
    assert.equal(completed.ok,true,JSON.stringify(completed));
    assert.equal(completed.data.row.markStatus,'已处理');
    assert.equal(completed.data.row.remark,'已认领');
    console.log('PASS completion round trip verifies status and remark');
    const duplicate=await invoke('complete',{sa_id:'demo-001',expected:completed.data.row,reviewed:true,note:'已核验测试原文，本库正确。'});
    assert.equal(duplicate.ok,false);
    assert.equal(await site.evaluate(()=>writeCount),2);
    console.log('PASS duplicate completion is refused across bridge');
    console.log('Extension integration: 5 cases passed. Only synthetic data; no production requests.');
  } finally {
    if(context)await context.close();
    backend.stdin.end(JSON.stringify({exit:true})+'\n');
    setTimeout(()=>{if(!stopped)backend.kill();},2000).unref();
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
