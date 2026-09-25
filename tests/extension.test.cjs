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
const wosOrigin=process.env.SA_TEST_WOS_ORIGIN || 'https://www.webofscience.com';
assert.ok(['https://www.webofscience.com','https://webofscience.clarivate.cn'].includes(wosOrigin));
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
  let context, stagedExtension;
  try {
    const pair=await next();assert.ok(pair.token,stderr||'bridge failed');
    const source=path.join(root,'extension');
    stagedExtension=fs.mkdtempSync(path.join(os.tmpdir(),'sa-extension-test-'));
    for(const name of fs.readdirSync(source))fs.copyFileSync(path.join(source,name),path.join(stagedExtension,name));
    // Only the test copy's loopback port differs. Production files stay intact.
    for(const name of ['background.js','manifest.json']){
      const file=path.join(stagedExtension,name);
      fs.writeFileSync(file,fs.readFileSync(file,'utf8').replaceAll('127.0.0.1:8765',`127.0.0.1:${pair.port}`));
    }
    const extension=stagedExtension;
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
    const editor=await invoke('open_metadata',{sa_id:'demo-001',expected:read.data.row});
    assert.equal(editor.ok,true,JSON.stringify(editor));
    assert.equal(await site.evaluate(()=>openedEditor),'1234567890123456789');
    assert.equal(await site.evaluate(()=>writeCount),0);
    console.log('PASS manual editor opens the exact item without saving');
    read=await invoke('search',{sa_id:'demo-001'});
    assert.equal(read.ok,true,JSON.stringify(read));
    const claim=await invoke('open_claim',{sa_id:'demo-001',expected:read.data.row});
    assert.equal(claim.ok,true,JSON.stringify(claim));
    assert.equal(await site.evaluate(()=>openedClaim),true);
    assert.equal(await site.evaluate(()=>writeCount),0);
    console.log('PASS manual claim window opens without submitting');
    // Synthetic user closes their manual drawer before starting a new command.
    await site.evaluate(()=>claimWindow.drawer=false);
    await site.evaluate(()=>testConfig.peopleCloseDelay=220);
    const prepared=await invoke('prepare_claim',{sa_id:'demo-001',expected:read.data.row,
      sa_text:'测试员(00001)①',staff_id:'00001',roster_staff_id:'00001'});
    assert.equal(prepared.ok,true,JSON.stringify(prepared));
    assert.equal(prepared.data.prepared.person.wno,'00001');
    assert.equal(await site.evaluate(()=>writeCount),0);
    console.log('PASS exact scholar lookup crosses the real extension without writing');
    const claimed=await invoke('submit_claim',{sa_id:'demo-001',expected:read.data.row,
      sa_text:'测试员(00001)①',staff_id:'00001',prepared:prepared.data.prepared,
      author_index:prepared.data.suggested_index,confirmed:true});
    assert.equal(claimed.ok,true,JSON.stringify(claimed));
    assert.equal(claimed.data.verified,true);
    assert.equal(claimed.data.scholar_id,'scholar-001');
    assert.equal(await site.evaluate(()=>claimWrites),1);
    console.log('PASS confirmed single-author claim submits once and verifies through bridge');
    await site.evaluate(()=>claimWindow.drawer=false);
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
    assert.equal(await site.evaluate(()=>writeCount),3);
    console.log('PASS duplicate completion is refused across bridge');
    const importPage=await context.newPage();
    const importFixture=fs.readFileSync(path.join(__dirname,'fixtures/import.html'),'utf8');
    await importPage.route('**/*',r=>r.fulfill({status:200,contentType:'text/html',body:importFixture}));
    await importPage.goto('http://admin.ir.lib.sjtu.edu.cn/#/collectItem/batchManage');
    const wosPage=await context.newPage();
    const wosFixture=fs.readFileSync(path.join(__dirname,'fixtures/wos.html'),'utf8');
    await wosPage.route('**/*',r=>r.fulfill({status:200,contentType:'text/html',body:wosFixture}));
    // Start outside document search to verify that navigation preserves the
    // selected regional host instead of silently switching CN back to .com.
    await wosPage.goto(wosOrigin+'/wos/author/author-search');
    // Playwright normally stores downloads under extensionless GUIDs. Give this
    // isolated test browser a normal dedicated download directory; production
    // extension behavior remains strict about .txt and is not relaxed for tests.
    const downloadSession=await context.newCDPSession(wosPage);
    const downloadDir=path.join(stagedExtension,'test-downloads');fs.mkdirSync(downloadDir);
    await downloadSession.send('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:downloadDir});
    const bind=async(role,url)=>popup.evaluate(async({role,url})=>{
      const tabs=await chrome.tabs.query({});const tab=tabs.find(t=>t.url===url);
      return chrome.runtime.sendMessage({type:'bind_workflow',role,tabId:tab.id});
    },{role,url});
    assert.equal((await bind('importTabId',importPage.url())).ok,true);
    assert.equal((await bind('wosTabId',wosPage.url())).ok,true);
    const before=await popup.evaluate(()=>chrome.storage.session.get(['tabId','wosTabId','importTabId']));
    assert.match((await bind('wosTabId',site.url())).error,/SA 比对.*独立标签页/);
    assert.match((await bind('importTabId',wosPage.url())).error,/不是后台/);
    assert.match((await bind('wosTabId',importPage.url())).error,/网址不受支持/);
    await importPage.evaluate(()=>location.hash='#/item/entryManage');
    assert.match((await bind('importTabId',importPage.url())).error,/普通.*数据管理.*数据导入与批次管理/);
    assert.deepEqual(await popup.evaluate(()=>chrome.storage.session.get(['tabId','wosTabId','importTabId'])),before);
    await importPage.evaluate(()=>location.hash='#/collectItem/batchManage');
    console.log('PASS distinct binding errors preserve existing valid bindings');
    const diagnostic=await popup.evaluate(async origin=>{
      const tabs=await chrome.tabs.query({url:origin+'/*'});
      return chrome.runtime.sendMessage({type:'inspect_workflow',tabId:tabs[0].id});
    },wosOrigin);
    assert.equal(diagnostic.ok,true);assert.equal(diagnostic.data.bindings.wos,true);assert.equal(diagnostic.data.version,'0.3.2');
    assert.equal(diagnostic.data.site,new URL(wosOrigin).hostname);
    console.log('PASS popup read-only diagnostics report role and version without searching');
    const muted=await popup.evaluate(()=>chrome.runtime.sendMessage({type:'toggle_wos_mute'}));assert.equal(muted.ok,true);
    const muteState=await popup.evaluate(async origin=>{
      const tabs=await chrome.tabs.query({url:origin+'/*'});return tabs[0].mutedInfo.muted;
    },wosOrigin);assert.equal(muteState,true);
    assert.equal((await popup.evaluate(()=>chrome.runtime.sendMessage({type:'toggle_wos_mute'}))).ok,true);
    console.log('PASS user-requested mute toggles only the explicitly bound WOS tab');
    const newImportPage=context.waitForEvent('page');
    const openedImport=await popup.evaluate(()=>chrome.runtime.sendMessage({type:'open_import'}));
    assert.equal(openedImport.ok,true,JSON.stringify(openedImport));
    const newPage=await newImportPage;await newPage.waitForURL('**/#/collectItem/batchManage');
    assert.equal(new URL(newPage.url()).hostname,'admin.ir.lib.sjtu.edu.cn');
    assert.ok(site.url().includes('/dataCompare/list'));await newPage.close();
    console.log('PASS import shortcut opens a separate same-backend tab and leaves SA page untouched');
    const c={title:'Synthetic paper',doi:'10.1234/test',wos:'WOS:000123456789012',sjtu:true,sha256:'a'.repeat(64)};
    const checked=await invoke('import_scan',{sa_id:'demo-001',instructions:'SA补充-demo-001',candidate:c});
    assert.equal(checked.ok,true,JSON.stringify(checked));assert.deepEqual(checked.data.batches,[]);
    console.log('PASS import command routes only to explicitly bound batch tab');
    const badFile=await invoke('import_upload',{sa_id:'demo-001',instructions:'SA补充-demo-001',candidate:c,content:'eA=='});
    assert.equal(badFile.ok,false);assert.match(badFile.error,/哈希/);
    assert.equal(await importPage.evaluate(()=>writes.upload),0);
    console.log('PASS extension independently hashes file before any upload');
    const wosQuery={sa_id:'demo-001',title:'Synthetic paper',doi:'10.1234/test',wos:'WOS:000123456789012'};
    const searched=await invoke('wos_search',wosQuery);assert.equal(searched.ok,true,JSON.stringify(searched));
    assert.equal(new URL(wosPage.url()).origin,wosOrigin);
    const exported=await invoke('wos_export',wosQuery);
    if(!exported.ok)console.log('Synthetic download diagnostics',await worker.evaluate(async()=>
      (await chrome.downloads.search({})).map(x=>({id:x.id,state:x.state,size:x.fileSize,bytes:x.bytesReceived,filename:x.filename,referrer:x.referrer,url:x.url,error:x.error}))));
    assert.equal(exported.ok,true,JSON.stringify(exported));
    assert.equal(new URL(exported.data.record_url).origin,wosOrigin);
    assert.equal(exported.data.sa_id,'demo-001');assert.match(exported.data.path,/\.txt$/i);
    console.log('PASS WOS search and correlated TXT download cross authenticated bridge');
    const raw=fs.readFileSync(exported.data.path);
    const importCandidate={...c,sha256:require('node:crypto').createHash('sha256').update(raw).digest('hex')};
    const target={sa_id:'demo-001',instructions:'SA补充-demo-001',candidate:importCandidate};
    const uploaded=await invoke('import_upload',{...target,content:raw.toString('base64')});assert.equal(uploaded.ok,true,JSON.stringify(uploaded));
    const submitted=await invoke('import_submit',{...target,upload:uploaded.data});assert.equal(submitted.ok,true,JSON.stringify(submitted));
    const imported=await invoke('import_check',target);assert.equal(imported.ok,true,JSON.stringify(imported));
    const pushed=await invoke('import_push',{...target,batch:imported.data.batch});assert.equal(pushed.ok,true,JSON.stringify(pushed));
    const verified=await invoke('import_check',{...target,batch_id:imported.data.batch.id,expect_pushed:true});
    assert.equal(verified.ok,true,JSON.stringify(verified));assert.equal(verified.data.batch.status,2);
    assert.deepEqual(await importPage.evaluate(()=>writes),{upload:1,import:1,push:1});
    console.log('PASS captured TXT -> hash check -> one upload -> one import -> priority merge -> verified push');
    assert.ok(site.url().includes('/dataCompare/list'));
    await importPage.evaluate(()=>location.hash='#/wel/index');
    const changedTab=await invoke('import_scan',{sa_id:'demo-001',instructions:'SA补充-demo-001',candidate:c});
    assert.equal(changedTab.ok,false);
    console.log('PASS changed workflow tab stops before executing commands');
    console.log(`Extension integration: 18 cases passed (${wosOrigin}). Only synthetic data; no production requests.`);
  } finally {
    if(context)await context.close();
    if(stagedExtension && path.dirname(path.resolve(stagedExtension))===path.resolve(os.tmpdir()) &&
        path.basename(stagedExtension).startsWith('sa-extension-test-'))fs.rmSync(stagedExtension,{recursive:true,force:true});
    backend.stdin.end(JSON.stringify({exit:true})+'\n');
    setTimeout(()=>{if(!stopped)backend.kill();},2000).unref();
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
