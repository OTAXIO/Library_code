const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const root=path.join(__dirname,'../extension');

test('validated background SA tab is activated before executing page transitions',async()=>{
  const order=[],tab={id:7,active:false,url:'http://admin.ir.lib.sjtu.edu.cn/#/dataCompare/list'};
  const state={token:'a'.repeat(43),tabId:7,mode:'sa'};
  let listener;
  const chrome={runtime:{id:'test',getURL:p=>'chrome-extension://test/'+p,getManifest:()=>({version:'test'}),
    onMessage:{addListener:fn=>listener=fn}},storage:{session:{get:async()=>state}},
    tabs:{get:async()=>tab,update:async(id,options)=>{assert.equal(id,7);assert.equal(options.active,true);assert.deepEqual(Object.keys(options),['active']);tab.active=true;order.push('activate');}},
    scripting:{executeScript:async()=>{order.push('execute');return [{result:{ok:true,data:{verified:true}}}];}}};
  const command={id:'command-1',action:'search',sa_id:'demo-001',expires:Date.now()+30000};
  const context=vm.createContext({chrome,URL,Date,AbortSignal,importScripts:()=>{},runSACommand(){},setTimeout,
    fetch:async(url)=>({ok:true,json:async()=>url.endsWith('/poll')?{command}:{}})});
  vm.runInContext(fs.readFileSync(path.join(root,'workflow-background.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(root,'background.js'),'utf8'),context);
  const reply=await new Promise(resolve=>listener({type:'tick'},{tab},resolve));
  assert.equal(reply.ok,true);
  assert.deepEqual(order,['activate','execute']);
});

test('WOS pairs and executes without an SA or import tab; rejects backend operations',async()=>{
  const state={}, requests=[], injections=[];
  let listener,command;
  const tab={id:7,url:'https://www.webofscience.com/wos/woscc/basic-search'};
  const chrome={
    runtime:{id:'test',getURL:p=>'chrome-extension://test/'+p,getManifest:()=>({version:'test'}),
      onMessage:{addListener:fn=>listener=fn}},
    storage:{session:{get:async()=>({...state}),clear:async()=>{for(const k of Object.keys(state))delete state[k];},
      set:async values=>Object.assign(state,values)}},
    tabs:{get:async id=>{assert.equal(id,7);return tab;}},
    scripting:{executeScript:async options=>{
      injections.push(options);
      // Pairing is tested through the production split-search dispatcher. A
      // bare data:{} stub no longer implements that read-only state contract.
      if(options.args?.[0]?.action==='wos_prepare_search')return [{result:{ok:true,data:{state:'ready'}}}];
      if(options.args?.[0]?.action==='wos_start_search'){
        tab.url='https://www.webofscience.com/wos/woscc/full-record/WOS:000123456789012';
        return [{result:{ok:true,data:{submitted:true}}}];
      }
      return [{result:{ok:true,data:{state:'record',record_url:tab.url}}}];
    }}
  };
  const context=vm.createContext({chrome,URL,Date,AbortSignal,importScripts:()=>{},
    runSACommand:()=>{throw Error('must not call SA');},
    fetch:async(url,opts)=>{
      requests.push({url,body:JSON.parse(opts.body)});
      return {ok:true,json:async()=>({command:JSON.parse(opts.body).claimOnly?null:command})};
    },runWOSCommand:()=>{},inspectWorkPage:()=>{},setTimeout});
  vm.runInContext(fs.readFileSync(path.join(root,'workflow-background.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(root,'background.js'),'utf8'),context);
  const send=(message,sender)=>new Promise(resolve=>listener(message,sender,resolve));
  const paired=await send({type:'pair',tabId:7,token:'a'.repeat(43)},
    {id:'test',url:'chrome-extension://test/popup.html'});
  assert.equal(paired.ok,true);
  assert.equal(state.mode,'wos');
  assert.equal(state.wosTabId,7);
  assert.equal(state.importTabId,undefined);
  command={id:'one',action:'wos_search',expires:Date.now()+60000};
  await send({type:'tick'},{tab});
  assert.equal(requests.at(-1).body.result.ok,true);
  assert.equal(injections.at(-1).target.tabId,7);
  const count=injections.length;
  command={id:'two',action:'import_submit',expires:Date.now()+60000};
  await send({type:'tick'},{tab});
  assert.equal(requests.at(-1).body.result.ok,false);
  assert.equal(injections.length,count);
});

test('bound WOS heartbeats keep the primary client and cannot claim while busy',async()=>{
  const primary={id:7,url:'http://admin.ir.lib.sjtu.edu.cn/#/dataCompare/list'};
  const wos={id:8,url:'https://webofscience.clarivate.cn/wos/woscc/basic-search'};
  const state={token:'a'.repeat(43),tabId:7,wosTabId:8,mode:'sa'};
  const requests=[],injections=[];
  let listener,finish,start;
  const started=new Promise(resolve=>start=resolve);
  const executing=new Promise(resolve=>finish=resolve);
  const chrome={
    runtime:{id:'test',getURL:p=>'chrome-extension://test/'+p,getManifest:()=>({version:'test'}),onMessage:{addListener:fn=>listener=fn}},
    storage:{session:{get:async()=>({...state})}},
    tabs:{get:async id=>{assert.equal(id,7);return primary;}},
    scripting:{executeScript:async options=>{injections.push(options);start();await executing;return [{result:{ok:true,data:{}}}];}}
  };
  const context=vm.createContext({chrome,URL,Date,AbortSignal,importScripts:()=>{},runSACommand:()=>{},fetch:async(url,opts)=>{
    requests.push({url,body:JSON.parse(opts.body)});
    return {ok:true,json:async()=>url.endsWith('/poll')?{command:{id:'one',action:'observe',expires:Date.now()+60000}}:{}};
  },setTimeout});
  vm.runInContext(fs.readFileSync(path.join(root,'workflow-background.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(root,'background.js'),'utf8'),context);
  const send=(message,sender)=>new Promise(resolve=>listener(message,sender,resolve));
  const first=send({type:'tick'},{tab:wos});
  await started;
  assert.equal(requests[0].body.client,'7');
  assert.equal(injections[0].target.tabId,7);
  await send({type:'tick'},{tab:wos});
  assert.equal(requests.length,1);
  const blocked=await send({type:'bind_workflow',tabId:8,role:'wosTabId'},
    {id:'test',url:'chrome-extension://test/popup.html'});
  assert.equal(blocked.ok,false);assert.match(blocked.error,/正在执行命令/);
  finish();assert.equal((await first).ok,true);
  assert.equal(requests.at(-1).body.client,'7');
  const count=requests.length;
  await send({type:'tick'},{tab:{...wos,id:9}});
  await send({type:'tick'},{tab:{...wos,url:'https://example.invalid/'}});
  assert.equal(requests.length,count);
  primary.url='http://admin.ir.lib.sjtu.edu.cn/#/wel/index';
  await send({type:'tick'},{tab:wos});
  assert.equal(requests.length,count);
});

test('refreshed backend pages retain heartbeat injection without wider permissions',()=>{
  const manifest=JSON.parse(fs.readFileSync(path.join(root,'manifest.json'),'utf8'));
  const matches=manifest.content_scripts.flatMap(item=>item.matches);
  assert.ok(matches.includes('http://admin.ir.lib.sjtu.edu.cn/*'));
  assert.ok(matches.includes('https://admin.ir.lib.sjtu.edu.cn/*'));
  assert.ok(!manifest.host_permissions.includes('<all_urls>'));
});

test('binding waits for an idle heartbeat but never overlaps a claimed command',async()=>{
  const primary={id:7,url:'http://admin.ir.lib.sjtu.edu.cn/#/dataCompare/list'};
  const wos={id:8,url:'https://webofscience.clarivate.cn/wos/woscc/basic-search'};
  const state={token:'a'.repeat(43),tabId:7,mode:'sa'};
  let listener,finishPoll,startPoll,polls=0;
  const started=new Promise(resolve=>startPoll=resolve);
  const waiting=new Promise(resolve=>finishPoll=resolve);
  const chrome={
    runtime:{id:'test',getURL:p=>'chrome-extension://test/'+p,getManifest:()=>({version:'test'}),onMessage:{addListener:fn=>listener=fn}},
    storage:{session:{get:async()=>({...state}),set:async values=>Object.assign(state,values)}},
    tabs:{get:async id=>id===7?primary:wos}
  };
  const context=vm.createContext({chrome,URL,Date,AbortSignal,importScripts:()=>{},fetch:async()=>{
    polls++;startPoll();await waiting;return {ok:true,json:async()=>({command:null})};
  },setTimeout});
  vm.runInContext(fs.readFileSync(path.join(root,'workflow-background.js'),'utf8'),context);
  vm.runInContext(fs.readFileSync(path.join(root,'background.js'),'utf8'),context);
  const send=(message,sender)=>new Promise(resolve=>listener(message,sender,resolve));
  const tick=send({type:'tick'},{tab:primary});await started;
  const bind=send({type:'bind_workflow',tabId:8,role:'wosTabId'},
    {id:'test',url:'chrome-extension://test/popup.html'});
  await send({type:'tick'},{tab:primary});assert.equal(polls,1);
  assert.equal(state.wosTabId,undefined);
  finishPoll();await tick;
  assert.equal((await bind).ok,true);assert.equal(state.wosTabId,8);
});
