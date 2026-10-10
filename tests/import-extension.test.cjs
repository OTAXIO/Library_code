/* Real WOS download + WOSFlow journal + unpacked extension + asynchronous
 * intercepted backend. Never attaches to the user's browser or sends a request
 * to a production site. All state and TXT files are disposable. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const readline=require('node:readline');
const {spawn}=require('node:child_process');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'..');
const pythonBundle=path.join(os.homedir(),'.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe');
const python=process.env.SA_TEST_PYTHON||(fs.existsSync(pythonBundle)?pythonBundle:'python');
const edge='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const readFixture=name=>fs.readFileSync(path.join(__dirname,'fixtures',name),'utf8');
const importFixture=readFixture('import-service.html'),saFixture=readFixture('compare.html'),wosFixture=readFixture('wos-navigation.html');
const tempRoot=fs.mkdtempSync(path.join(os.tmpdir(),'sa-import-integration-'));
const saURL='http://admin.ir.lib.sjtu.edu.cn/#/dataCompare/list';
const importURL='http://admin.ir.lib.sjtu.edu.cn/#/collectItem/batchManage';
const wosURL='https://webofscience.clarivate.cn/wos/woscc/basic-search';
const clone=value=>JSON.parse(JSON.stringify(value));
(async()=>{
  const backend=spawn(python,['-u','-m','tests.import_flow_backend',tempRoot],{cwd:root,windowsHide:true,
    env:{...process.env,PYTHONUTF8:'1'},stdio:['pipe','pipe','pipe']});
  const lines=[],waiters=[];let stderr='',stopped=false,context;
  backend.stderr.on('data',chunk=>stderr+=chunk);
  readline.createInterface({input:backend.stdout}).on('line',line=>{
    const data=JSON.parse(line);if(waiters.length)waiters.shift()(data);else lines.push(data);
  });
  backend.on('exit',()=>{stopped=true;while(waiters.length)waiters.shift()({ok:false,error:stderr||'test bridge exited'});});
  const next=()=>lines.length?Promise.resolve(lines.shift()):stopped?Promise.reject(new Error(stderr||'test bridge exited')):new Promise(resolve=>waiters.push(resolve));
  const invoke=async(action,payload={})=>{backend.stdin.write(JSON.stringify({action,payload})+'\n');return next();};
  const expectOK=async(action,payload={})=>{const result=await invoke(action,payload);assert.equal(result.ok,true,JSON.stringify(result));return result.data;};
  let service,exportedRaw;
  const resetService=()=>service={batches:[],files:new Map(),writes:{upload:0,import:0,push:0},reads:0,wrongUT:false,errors:[]};
  resetService();
  try{
    const pair=await next();assert.ok(pair.token,stderr||'bridge failed');
    const staged=path.join(tempRoot,'extension');fs.mkdirSync(staged);
    for(const name of fs.readdirSync(path.join(root,'extension')))fs.copyFileSync(path.join(root,'extension',name),path.join(staged,name));
    for(const name of ['background.js','manifest.json']){
      const file=path.join(staged,name);fs.writeFileSync(file,fs.readFileSync(file,'utf8').replaceAll('127.0.0.1:8765',`127.0.0.1:${pair.port}`));
    }
    const downloads=path.join(tempRoot,'downloads');fs.mkdirSync(downloads);
    context=await chromium.launchPersistentContext(path.join(tempRoot,'profile'),{headless:true,acceptDownloads:true,downloadsPath:downloads,
      ...(process.env.SA_TEST_BROWSER?{executablePath:process.env.SA_TEST_BROWSER}:fs.existsSync(edge)?{executablePath:edge}:{}),
      args:[`--disable-extensions-except=${staged}`,`--load-extension=${staged}`]});
    await context.route('**/*',async route=>{
      const request=route.request(),url=new URL(request.url());
      if(url.hostname==='127.0.0.1'||url.protocol==='chrome-extension:')return route.continue();
      if(url.hostname==='webofscience.clarivate.cn')return route.fulfill({status:200,contentType:'text/html',body:wosFixture});
      if(url.hostname!=='admin.ir.lib.sjtu.edu.cn')return route.abort();
      if(!url.pathname.startsWith('/__offline/'))return route.fulfill({status:200,contentType:'text/html',body:'<h1>Offline fixture: select a dedicated page route</h1>'});
      const state=service;
      try{
        let data;
        const payload=request.method()==='POST'?request.postDataJSON():null;
        const endpoint=url.pathname.slice('/__offline/'.length);
        if(endpoint==='upload'){
          assert.equal(payload.content,exportedRaw.toString('utf8'),'uploaded bytes must be the actual captured Full Record TXT');
          assert.equal(payload.size,exportedRaw.length);assert.equal(payload.name,'SA-WOS-demo-001.txt');
          state.writes.upload++;state.files.set('offline-object.txt',payload.content);
          data={code:200,success:true,data:{name:'offline-object.txt'}};
        }else if(endpoint==='import'){
          assert.equal(payload.instructions,'SA补充-demo-001');assert.equal(payload.datasetId,'sjtu-offline');
          assert.ok(state.files.has(payload.filename));assert.equal(state.batches.length,0);
          state.writes.import++;
          const batch={id:'batch-001',modelId:'article-model',batchNumber:'OFFLINE-1',source:'WOS',instructions:payload.instructions,
            total:0,actual:0,fail:0,status:1,increase:0,duplicateSkip:0,duplicateIncreaseUpdate:0,duplicateOverallCoverage:0};
          state.batches.push(batch);setTimeout(()=>batch.total=1,350);
          data={success:true};
        }else if(endpoint==='push'){
          assert.deepEqual(payload,{batchId:'batch-001',modelId:'article-model',duplicateChecking:true,
            duplicateQueryType:'ppt-composite',duplicateItemProcessingType:'4',newItemProcessingType:'1',owner:true,updateFields:[]});
          assert.equal(state.batches[0].status,1);state.writes.push++;
          setTimeout(()=>{state.batches[0].status=2;state.batches[0].actual=1;state.batches[0].increase=1;},350);
          data={success:true};
        }else if(endpoint==='batches'){
          state.reads++;const start=(Number(url.searchParams.get('page'))-1)*Number(url.searchParams.get('size'));
          data={items:clone(state.batches.slice(start,start+Number(url.searchParams.get('size')))),total:state.batches.length};
        }else if(endpoint==='metadata'){
          assert.equal(url.searchParams.get('id'),'batch-001');state.reads++;
          data={total:1,items:[{metadata:{title:['Synthetic paper'],doi:['10.1234/test'],wosId:[state.wrongUT?'WOS:999999999999999':'WOS:000123456789012']}}]};
        }else if(endpoint==='library'){
          assert.equal(payload.containsChild,true);assert.equal(payload.emptyYear,false);
          assert.ok(payload.doi==='10.1234/test'||payload.title==='Synthetic paper');
          state.reads++;
          const present=state.batches.some(batch=>batch.status===2);
          data={total:present?1:0,items:present?[{id:'1234567890123456789',metadata:{title:['Synthetic paper'],doi:['10.1234/test'],wosId:['WOS:000123456789012']}}]:[]};
        }else throw new Error('Unrecognized offline endpoint');
        await new Promise(resolve=>setTimeout(resolve,50));
        return route.fulfill({status:200,contentType:'application/json',body:JSON.stringify(data)});
      }catch(error){state.errors.push(error.message);return route.fulfill({status:500,body:'Synthetic request rejected'});}
    });
    const worker=context.serviceWorkers()[0]||await context.waitForEvent('serviceworker',{timeout:10000});
    const popup=await context.newPage();await popup.goto(`chrome-extension://${new URL(worker.url()).hostname}/popup.html`);
    const sa=await context.newPage();await sa.route('http://admin.ir.lib.sjtu.edu.cn/',route=>route.fulfill({status:200,contentType:'text/html',body:saFixture}));await sa.goto(saURL);
    await sa.evaluate(()=>Object.assign(synthetic,{itemId:'',matchCount:0,reason:'无匹配',doiValue:'10.1234/test',wosValue:''}));
    const imports=await context.newPage();await imports.route('http://admin.ir.lib.sjtu.edu.cn/',route=>route.fulfill({status:200,contentType:'text/html',body:importFixture}));await imports.goto(importURL);
    const wos=await context.newPage();await wos.goto(wosURL);
    const devtools=await context.newCDPSession(wos);await devtools.send('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:downloads});
    const pairResult=await popup.evaluate(async({token,url})=>{
      const tabs=await chrome.tabs.query({});const tab=tabs.find(item=>item.url===url);
      return chrome.runtime.sendMessage({type:'pair',tabId:tab.id,token});
    },{token:pair.token,url:saURL});assert.equal(pairResult.ok,true,JSON.stringify(pairResult));
    for(const [role,url] of [['wosTabId',wosURL],['importTabId',importURL]]){
      const bound=await popup.evaluate(async({role,url})=>{const tabs=await chrome.tabs.query({});const tab=tabs.find(item=>item.url===url);
        return chrome.runtime.sendMessage({type:'bind_workflow',role,tabId:tab.id});},{role,url});
      assert.equal(bound.ok,true,JSON.stringify(bound));
    }
    const query={sa_id:'demo-001',title:'Synthetic paper',doi:'10.1234/test',wos:''};
    await expectOK('wos_search',query);const downloaded=await expectOK('wos_export',query);
    assert.ok(path.resolve(downloaded.path).startsWith(downloads+path.sep));exportedRaw=fs.readFileSync(downloaded.path);
    console.log('PASS real extension downloads one Full Record TXT into the isolated directory');

    const prepare=async(scenario,failures={})=>{
      await expectOK('reset_flow',{scenario,...failures});const prepared=await expectOK('prepare_file',{path:downloaded.path});
      assert.equal(prepared.phase,'exported');assert.equal(prepared.identity_confirmed,true);return prepared;
    };
    const normal=await prepare('normal');
    const capability=await expectOK('import_capabilities',{sa_id:'demo-001'});
    assert.equal(capability.zero_match_protocol,1);
    const absent=await expectOK('import_resolve',{sa_id:'demo-001',candidate:normal.candidate});
    assert.deepEqual(absent,{verified:true,items:[]});
    assert.equal(new URL(imports.url()).hash,'#/collectItem/batchManage');
    assert.deepEqual(service.writes,{upload:0,import:0,push:0});
    console.log('PASS real extension capability and library resolution prove absence before upload without any writes');
    const badHash=await invoke('import_upload',{sa_id:'demo-001',instructions:'SA补充-demo-001',candidate:normal.candidate,content:Buffer.from('wrong bytes').toString('base64')});
    assert.equal(badHash.ok,false);assert.match(badHash.error,/哈希/);assert.equal(service.writes.upload,0);
    console.log('PASS independent extension SHA-256 validation rejects tampered TXT before any upload');
    const completed=await expectOK('proceed');assert.equal(completed.phase,'pushed');assert.equal(completed.batch.actual,1);
    assert.deepEqual(service.writes,{upload:1,import:1,push:1});assert.deepEqual(service.errors,[]);
    assert.equal(await sa.evaluate(()=>writeCount),0,'import must not mark the SA row complete');
    console.log('PASS real WOSFlow: fresh SA check -> upload -> asynchronous import -> exact PPT merge settings -> verified push');
    const resolved=await expectOK('import_resolve',{sa_id:'demo-001',candidate:normal.candidate});
    assert.equal(resolved.verified,true);assert.equal(resolved.items.length,1);
    assert.equal(resolved.items[0].id,'1234567890123456789');
    assert.equal(new URL(imports.url()).hash,'#/collectItem/batchManage');
    assert.deepEqual(service.writes,{upload:1,import:1,push:1});
    console.log('PASS real extension resolves the final 19-digit library ID after push and restores the import tab');
    const before=clone(service.writes);await expectOK('proceed');assert.deepEqual(service.writes,before);
    await imports.reload();
    const checked=await expectOK('import_check',{sa_id:'demo-001',instructions:'SA补充-demo-001',candidate:normal.candidate,batch_id:'batch-001',expect_pushed:true});
    assert.equal(checked.batch.status,2);assert.deepEqual(service.writes,before);
    const replay=await invoke('import_upload',{sa_id:'demo-001',instructions:'SA补充-demo-001',candidate:normal.candidate,content:exportedRaw.toString('base64')});
    assert.equal(replay.ok,false);assert.match(replay.error,/相同说明批次/);assert.deepEqual(service.writes,before);
    console.log('PASS page reload recovers batch from the service; repeat run and same-description reupload do not duplicate writes');

    resetService();await imports.reload();await prepare('lost-ack',{lose_import_ack:true});
    const lost=await invoke('proceed');assert.equal(lost.ok,false);assert.match(lost.error,/lost import acknowledgement/);
    assert.equal((await expectOK('state')).state.phase,'import_intent');assert.deepEqual(service.writes,{upload:1,import:1,push:0});
    await imports.reload();const resumed=await expectOK('proceed');assert.equal(resumed.phase,'pushed');
    assert.deepEqual(service.writes,{upload:1,import:1,push:1});assert.deepEqual(service.errors,[]);
    assert.deepEqual((await expectOK('state')).actions,['search','import_scan','import_upload','import_submit','import_check','import_push','import_check']);
    console.log('PASS lost import acknowledgement + recreated journal + page reload resumes readback, never reimports');

    resetService();await imports.reload();await prepare('lost-push-ack',{lose_push_ack:true});
    const lostPush=await invoke('proceed');assert.equal(lostPush.ok,false);assert.match(lostPush.error,/lost push acknowledgement/);
    assert.equal((await expectOK('state')).state.phase,'push_intent');assert.deepEqual(service.writes,{upload:1,import:1,push:1});
    await imports.reload();assert.equal((await expectOK('proceed')).phase,'pushed');
    assert.deepEqual(service.writes,{upload:1,import:1,push:1});assert.deepEqual(service.errors,[]);
    assert.deepEqual((await expectOK('state')).actions,['search','import_scan','import_upload','import_submit','import_check','import_push','import_check']);
    console.log('PASS lost push acknowledgement resumes only verification and never issues a second push');

    resetService();service.wrongUT=true;await imports.reload();await prepare('wrong-identity');
    const mismatch=await invoke('proceed');assert.equal(mismatch.ok,false);assert.match(mismatch.error,/入藏号/);
    assert.deepEqual(service.writes,{upload:1,import:1,push:0});assert.deepEqual(service.errors,[]);
    console.log('PASS imported identifier mismatch stops before push and leaves an auditable import intent');
    console.log('Import extension: 9 end-to-end checks passed. Synthetic data, no production requests or roster modifications.');
  }finally{
    if(context)await context.close();backend.stdin.end(JSON.stringify({exit:true})+'\n');
    await new Promise(resolve=>{if(stopped)return resolve();const timer=setTimeout(()=>{backend.kill();resolve();},3000);
      backend.once('exit',()=>{clearTimeout(timer);resolve();});});
    if(path.dirname(path.resolve(tempRoot))===path.resolve(os.tmpdir())&&path.basename(tempRoot).startsWith('sa-import-integration-'))
      fs.rmSync(tempRoot,{recursive:true,force:true});
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
