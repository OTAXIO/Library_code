/* Dispatcher state machine only. No browser, user files or network. */
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../extension/workflow-background.js'),'utf8');
const origin='https://webofscience.clarivate.cn';
const record=origin+'/wos/woscc/full-record/WOS:000123456789012';
const command=action=>({action,sa_id:'offline-export',title:'Synthetic paper',record_url:record,expires:Date.now()+75000});

function setup(respond){
  const calls=[],tab={id:2,active:true,url:record},listeners=new Set();
  let reloads=0;
  const api={calls,tab,listeners,get reloads(){return reloads;},created:null};
  let clock=Date.now();
  const sandbox={URL,Date:{now:()=>clock},clearTimeout,
    setTimeout:(fn,ms)=>setTimeout(()=>{if(ms<8000)clock+=ms;fn();},ms>=8000?ms:0),
    runWOSCommand(){},inspectWorkPage(){},chrome:{
      tabs:{get:async()=>tab,reload:async()=>{reloads++;if(api.onReload)api.onReload();}},
      scripting:{executeScript:async injection=>{
        const payload=injection.args[0];calls.push(payload.action);
        const result=await respond(payload,api,injection);
        return [{result}];
      }},
      downloads:{onCreated:{addListener:fn=>listeners.add(fn),removeListener:fn=>listeners.delete(fn)},
        search:async()=>[{id:4,state:'complete',filename:'C:/offline/savedrecs.txt',fileSize:200}]}
    }};
  vm.runInNewContext(source,sandbox);
  api.dispatch=payload=>sandbox.dispatchWorkflow(payload,{tabId:1,wosTabId:2});
  return api;
}
const ok=data=>({ok:true,data});

test('blank canonical record reloads once, waits for hydration, prepares without final Export',async()=>{
  let hydrations=0;
  const api=setup((cmd,env,injection)=>{
    if(cmd.action==='wos_export_probe'){
      assert.equal(injection.world,'ISOLATED');
      return ok({state:env.reloads?(++hydrations>2?'ready':'loading'):'blank',record_url:record});
    }
    assert.equal(cmd.action,'wos_prepare_export');
    return ok({ready:true,record_url:record});
  });
  const result=await api.dispatch(command('wos_export_prepare'));
  assert.equal(result.ok,true);assert.equal(api.reloads,1);
  assert.equal(api.calls.filter(x=>x==='wos_prepare_export').length,1);
  assert.equal(api.calls.filter(x=>x==='wos_download').length,0);
});

test('format-preview white screen can recover before any final Export',async()=>{
  let opened=false;
  const api=setup((cmd,env)=>{
    if(cmd.action==='wos_export_probe')return ok({state:opened&&!env.reloads?'blank':'ready',record_url:record});
    assert.equal(cmd.action,'wos_prepare_export');
    if(!env.reloads){opened=true;return {ok:false,error:'导出设置超时，未自动重复操作'};}
    return ok({ready:true,record_url:record});
  });
  assert.equal((await api.dispatch(command('wos_export_prepare'))).ok,true);
  assert.equal(api.reloads,1);
  assert.equal(api.calls.filter(x=>x==='wos_download').length,0);
});

test('known prepared final Export captures one download without preparing or reloading',async()=>{
  const api=setup((cmd,env)=>{
    if(cmd.action==='wos_check_export')return ok({ready:true,record_url:record});
    assert.equal(cmd.action,'wos_download');
    for(const fn of env.listeners)fn({id:4,url:'blob:'+origin+'/offline',referrer:record});
    return ok({submitted:true,record_url:record});
  });
  const result=await api.dispatch({...command('wos_export'),prepared:true});
  assert.equal(result.ok,true);assert.equal(result.data.path,'C:/offline/savedrecs.txt');
  assert.deepEqual(api.calls,['wos_check_export','wos_download']);
  assert.equal(api.reloads,0);assert.equal(api.listeners.size,0);
});

test('context loss at final Export never refreshes or retries it',async()=>{
  const api=setup(cmd=>{
    if(cmd.action==='wos_check_export')return ok({ready:true,record_url:record});
    assert.equal(cmd.action,'wos_download');throw Error('Execution context was destroyed');
  });
  await assert.rejects(api.dispatch({...command('wos_export'),prepared:true}),/context/);
  assert.deepEqual(api.calls,['wos_check_export','wos_download']);
  assert.equal(api.reloads,0);assert.equal(api.listeners.size,0);
});

test('login/CAPTCHA, foreign popup, site error gates do not reload or submit',async()=>{
  for(const error of ['登录表单','人工验证','非制表符导出弹窗','Oops, something went wrong!']){
    const api=setup(()=>({ok:false,error}));
    const result=await api.dispatch(command('wos_export_prepare'));
    assert.equal(result.error,error);assert.deepEqual(api.calls,['wos_export_probe']);
    assert.equal(api.reloads,0);
  }
});

test('blank recovery rejects a changed UT or origin',async()=>{
  for(const next of [record.replace('000123456789012','000999999999999'),record.replace(origin,'https://www.webofscience.com')]){
    const api=setup(()=>ok({state:'blank',record_url:record}));
    // Change between the read probe and the pre-reload target check.
    let count=0;
    const get=api.tab;
    api.onReload=()=>{throw Error('must not reload');};
    // Mutate after one preparation probe so the next target check sees it.
    const mutator=setTimeout(()=>{get.url=next;},1);
    await assert.rejects(api.dispatch(command('wos_export_prepare')),/变化|不一致/);
    clearTimeout(mutator);assert.equal(api.reloads,0);
  }
});

test('only a searched canonical target can hand a blank record to export preparation',async()=>{
  let started=false,navigated=false;
  const calls=[];
  const sandbox={URL,Date,setTimeout,clearTimeout,runWOSCommand(){},inspectWorkPage(){},chrome:{
    tabs:{get:async()=>({id:2,active:true,url:navigated?record:origin+'/wos/woscc/basic-search'}),
      update:async(id,change)=>{assert.equal(change.url,record);navigated=true;}},
    scripting:{executeScript:async input=>{
      const cmd=input.args[0];calls.push(cmd.action);
      if(cmd.action==='wos_prepare_search')return [{result:ok({state:'ready'})}];
      if(cmd.action==='wos_start_search'){started=true;return [{result:ok({submitted:true})}];}
      return [{result:ok(navigated?{state:'loading',diagnostic:{record_route:true,blank_record:true,busy:false}}:
        {state:'single',navigate_url:record})}];
    }}
  }};
  vm.runInNewContext(source,sandbox);
  const result=await sandbox.dispatchWorkflow(command('wos_search'),{tabId:1,wosTabId:2});
  assert.equal(started,true);assert.equal(result.data.record_url,record);
  assert.equal(result.data.state,'record_needs_preparation');
  assert.equal(calls.filter(x=>x==='wos_start_search').length,1);
});

test('next-paper navigation does not discard an open modal on a record page',async()=>{
  for(const diagnostic of [{dialog_count:1,export_dialog:true},{dialog_count:2},
      {dialog_count:0,login_required:true},{dialog_count:0,site_timeout:true}]){
    const api=setup(cmd=>{assert.equal(cmd.action,'wos_diagnose');return {
      dialog_count:0,login_required:false,wos_error:false,site_timeout:false,...diagnostic};});
    await assert.rejects(api.dispatch(command('wos_search')),/弹窗|登录|报错/);
    assert.deepEqual(api.calls,['wos_diagnose']);assert.equal(api.reloads,0);
  }
});
