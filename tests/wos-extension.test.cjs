/* Real unpacked extension + ephemeral desktop bridge, WOS-only. All site
 * requests are fulfilled by a local fixture; never uses the user's profile. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const os=require('node:os');
const path=require('node:path');
const readline=require('node:readline');
const {spawn}=require('node:child_process');
const {chromium}=require('playwright');
const {runWOSCommand}=require('../extension/wos-adapter.js');
const root=path.resolve(__dirname,'..');
const pythonBundle=path.join(os.homedir(),'.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe');
const python=process.env.SA_TEST_PYTHON||(fs.existsSync(pythonBundle)?pythonBundle:'python');
const edge='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const fixture=fs.readFileSync(path.join(__dirname,'fixtures/wos-navigation.html'),'utf8');
(async()=>{
  const backend=spawn(python,['-u','-m','tests.extension_backend'],{cwd:root,windowsHide:true,stdio:['pipe','pipe','pipe']});
  const lines=[],waiters=[];let stderr='',stopped=false;
  backend.stderr.on('data',chunk=>stderr+=chunk);
  readline.createInterface({input:backend.stdout}).on('line',line=>{
    const data=JSON.parse(line);if(waiters.length)waiters.shift()(data);else lines.push(data);
  });
  backend.on('exit',()=>{stopped=true;while(waiters.length)waiters.shift()({ok:false,error:stderr||'test bridge exited'});});
  const next=()=>lines.length?Promise.resolve(lines.shift()):stopped?Promise.reject(new Error(stderr||'test bridge exited')):new Promise(resolve=>waiters.push(resolve));
  const invoke=async(action,payload)=>{backend.stdin.write(JSON.stringify({action,payload})+'\n');return next();};
  const tempRoot=fs.mkdtempSync(path.join(os.tmpdir(),'sa-wos-integration-'));
  let context;
  try{
    const pair=await next();assert.ok(pair.token,stderr||'bridge failed');
    const staged=path.join(tempRoot,'extension');fs.mkdirSync(staged);
    for(const name of fs.readdirSync(path.join(root,'extension')))fs.copyFileSync(path.join(root,'extension',name),path.join(staged,name));
    for(const name of ['background.js','manifest.json']){
      const file=path.join(staged,name);fs.writeFileSync(file,fs.readFileSync(file,'utf8').replaceAll('127.0.0.1:8765',`127.0.0.1:${pair.port}`));
    }
    const profile=path.join(tempRoot,'profile'),downloads=path.join(tempRoot,'downloads');
    fs.mkdirSync(path.join(profile,'Default'),{recursive:true});fs.mkdirSync(downloads);
    // Save only these disposable synthetic downloads, never the user's Downloads.
    fs.writeFileSync(path.join(profile,'Default','Preferences'),JSON.stringify({download:{default_directory:downloads,prompt_for_download:false}}));
    context=await chromium.launchPersistentContext(profile,{headless:true,acceptDownloads:true,downloadsPath:downloads,
      ...(process.env.SA_TEST_BROWSER?{executablePath:process.env.SA_TEST_BROWSER}:fs.existsSync(edge)?{executablePath:edge}:{}),
      args:[`--disable-extensions-except=${staged}`,`--load-extension=${staged}`]});
    const searches=[],queries=[],searchPageVisits=new Map();
    await context.route('**/*',route=>{
      const url=new URL(route.request().url());
      if(['www.webofscience.com','webofscience.clarivate.cn'].includes(url.hostname)){
        if(url.pathname==='/offline-query'){
          queries.push({origin:url.origin,query:route.request().postData()});
          return route.fulfill({status:200,body:'ok'});
        }
        if(url.pathname.includes('/summary/'))searches.push(url.origin);
        let body=fixture;
        if(url.pathname==='/wos/woscc/basic-search'){
          const visits=(searchPageVisits.get(url.origin)||0)+1;searchPageVisits.set(url.origin,visits);
          body=body.replace('const main=document.getElementById',visits>1?
            'window.__offlineSearchMountDelay=1300;const main=document.getElementById':
            'window.__offlineStartWithNavigation=true;const main=document.getElementById');
        }
        return route.fulfill({status:200,contentType:'text/html',body});
      }
      if(url.hostname==='127.0.0.1'||url.protocol==='chrome-extension:')return route.continue();
      return route.abort();
    });
    const worker=context.serviceWorkers()[0]||await context.waitForEvent('serviceworker',{timeout:10000});
    const extensionId=new URL(worker.url()).hostname;
    const popup=await context.newPage();await popup.goto(`chrome-extension://${extensionId}/popup.html`);
    const site=await context.newPage();
    // Playwright normally renames downloads to GUIDs. This isolated browser
    // must preserve normal filenames so Chrome's downloads API is tested with
    // the same .txt evidence that the desktop bridge reads in production.
    const devtools=await context.newCDPSession(site);
    await devtools.send('Browser.setDownloadBehavior',{behavior:'allow',downloadPath:downloads});
    for(const origin of ['https://www.webofscience.com','https://webofscience.clarivate.cn']){
      await site.goto(origin+'/wos/woscc/basic-search');
      const paired=await popup.evaluate(async({origin,token})=>{
        const tabs=await chrome.tabs.query({url:origin+'/*'});
        if(tabs.length!==1)throw new Error('Expected exactly one isolated WOS tab');
        return chrome.runtime.sendMessage({type:'pair',tabId:tabs[0].id,token});
      },{origin,token:pair.token});
      assert.equal(paired.ok,true,JSON.stringify(paired));
      const ready=await invoke('wos_diagnose',{});
      assert.equal(ready.ok,true,JSON.stringify(ready));
      assert.equal(ready.data.extension_version,JSON.parse(fs.readFileSync(path.join(root,'extension/manifest.json'),'utf8')).version);
      assert.equal(ready.data.result_reader,'shared-diagnostic');
      assert.equal(ready.data.page.core_search_route,true);
      assert.equal(ready.data.page.query_input_count,0);
      assert.equal(ready.data.search_prepare_protocol,1);
      assert.equal(ready.data.page.site_timeout,false);
      assert.equal(ready.data.page.login_required,false);
      assert.equal(ready.data.page.dialog_count,0);
      const preflight=await invoke('test_preflight',{});
      assert.equal(preflight.ok,true,JSON.stringify(preflight));
      assert.equal(queries.filter(item=>item.origin===origin).length,0);
      console.log(`PASS ${origin} actual Python preflight with no input -> navigation preparation permitted, no Search yet`);
      const missing=await invoke('wos_search',{sa_id:'offline-missing',title:'Missing synthetic paper',doi:'',wos:''});
      assert.equal(missing.ok,false);assert.match(missing.error,/WOS 未找到记录/);
      console.log(`PASS ${origin} zero results return through real extension without losing pairing`);
      const nextMissing=await invoke('wos_search',{sa_id:'offline-missing-2',title:'Missing synthetic paper',doi:'',wos:''});
      assert.equal(nextMissing.ok,false);assert.match(nextMissing.error,/WOS 未找到记录/);
      assert.equal(searchPageVisits.get(origin),2,'one clean reload after the first zero, no reload storm');
      console.log(`PASS ${origin} second zero finishes after slow stale-banner reset without stopping the queue`);
      const query={sa_id:'offline-one',title:'Synthetic paper',doi:'10.1234/test',wos:''};
      const searched=await invoke('wos_search',query);
      assert.equal(searched.ok,true,JSON.stringify(searched));
      assert.equal(site.url(),origin+'/wos/woscc/full-record/WOS:000123456789012');
      assert.equal(searches.filter(value=>value===origin).length,1,'Search is not repeated across real navigations');
      assert.deepEqual(queries.filter(item=>item.origin===origin).map(item=>item.query),
        ['Missing synthetic paper','Missing synthetic paper','10.1234/test'],'exactly one Search per paper despite full-navigation preparation and slow hydration');
      assert.equal(searchPageVisits.get(origin),3,'one clean reload after each zero');
      console.log(`PASS ${origin} real navigation -> isolated record probe despite page DOM hook -> full record`);
      query.record_url=searched.data.record_url;
      const preview=await invoke('wos_export_prepare',query);
      assert.equal(preview.ok,true,JSON.stringify(preview));assert.equal(preview.data.ready,true);
      assert.equal(await site.evaluate(()=>exportsMade),0,'preparing Full Record never clicks final Export');
      const snapshot=await invoke('wos_diagnose',{});
      assert.equal(snapshot.data.page.export_dialog,true,'a known export modal can pass resume preflight');
      const pending=await invoke('wos_export_status',query);
      assert.equal(pending.ok,true,JSON.stringify(pending));assert.equal(pending.data.state,'unsubmitted');
      console.log(`PASS ${origin} separate prepared Full Record preview + read-only unsubmitted receipt`);
      const exported=await invoke('wos_export',{...query,prepared:true});
      if(!exported.ok)console.log('Offline download diagnosis',await worker.evaluate(async()=>
        (await chrome.downloads.search({})).map(item=>({state:item.state,error:item.error,size:item.fileSize,txt:/\.txt$/i.test(item.filename)}))));
      assert.equal(exported.ok,true,JSON.stringify(exported));
      assert.equal(exported.data.sa_id,query.sa_id);
      const saved=path.resolve(exported.data.path);
      assert.ok(saved.startsWith(path.resolve(downloads)+path.sep),'synthetic downloads must stay inside the disposable test directory');
      const raw=fs.readFileSync(saved,'utf8');assert.match(raw,/WOS:000123456789012/);assert.match(raw,/TI\tAU\tAF/);
      assert.equal(await site.evaluate(()=>exportsMade),1);
      const duplicate=await site.evaluate(runWOSCommand,{...query,action:'wos_download',expires:Date.now()+30000});
      assert.equal(duplicate.ok,false);assert.match(duplicate.error,/已提交/);
      assert.equal(await site.evaluate(()=>exportsMade),1,'a submitted export is not clicked twice');
      console.log(`PASS ${origin} one Full Record TXT download correlated and verified on disk; repeat submission refused`);
    }
    console.log('WOS extension: 16 end-to-end checks passed across both origins. Separate prepare/final Export, real Python preflight, synthetic pages/files only.');
  }finally{
    if(context)await context.close();
    backend.stdin.end(JSON.stringify({exit:true})+'\n');
    setTimeout(()=>{if(!stopped)backend.kill();},2000).unref();
    // tempRoot is an exact mkdtemp result below the OS temporary directory.
    if(path.dirname(path.resolve(tempRoot))===path.resolve(os.tmpdir())&&path.basename(tempRoot).startsWith('sa-wos-integration-'))
      fs.rmSync(tempRoot,{recursive:true,force:true});
  }
})().catch(error=>{console.error(error);process.exitCode=1;});
