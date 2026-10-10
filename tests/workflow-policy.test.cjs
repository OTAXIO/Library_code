const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../extension/page-diagnostics.js'), 'utf8')+'\n'+
  fs.readFileSync(path.join(__dirname, '../extension/workflow-background.js'), 'utf8');
const policy = require('../extension/workflow-background.js');
const origins = ['https://www.webofscience.com', 'https://webofscience.clarivate.cn'];
const recordPath = '/wos/woscc/full-record/WOS:000123456789012';
function loadPolicy(sandbox) {
  sandbox.clearTimeout=clearTimeout;
  vm.createContext(sandbox);
  vm.runInContext(source,sandbox);
}

test('library resolution dispatches only to the separately bound import tab', async () => {
  const calls=[];
  const sandbox={URL,Date,setTimeout,resolveLibraryRecord(){},runImportCommand(){},chrome:{
    tabs:{get:async()=>({id:3,url:'http://admin.ir.lib.sjtu.edu.cn/#/collectItem/batchManage'})},
    scripting:{executeScript:async input=>{calls.push(input);return [{result:{ok:true,data:{verified:true,items:[]}}}];}},
  }};
  loadPolicy(sandbox);
  const result=await sandbox.dispatchWorkflow({action:'import_resolve',expires:Date.now()+30000},{tabId:1,importTabId:3});
  assert.equal(result.data.verified,true);
  assert.equal(calls.length,1);
  assert.equal(calls[0].target.tabId,3);
  assert.equal(calls[0].func,sandbox.resolveLibraryRecord);
  assert.equal(calls[0].world,'MAIN');
});

test('invalid import binding reports a page error without accessing command globals', () => {
  assert.match(policy.workflowBindingError('http://admin.ir.lib.sjtu.edu.cn/#/item/entryManage','importTabId',false),/普通/);
  assert.match(policy.workflowBindingError('http://admin.ir.lib.sjtu.edu.cn/#/wel/index','importTabId',false),/不是后台/);
});

test('validated background import tab is activated before its async public methods', async()=>{
  const order=[],tab={id:3,active:false,url:'http://admin.ir.lib.sjtu.edu.cn/#/collectItem/batchManage'};
  const sandbox={URL,Date,setTimeout,resolveLibraryRecord(){},runImportCommand(){},chrome:{
    tabs:{get:async()=>tab,update:async(id,options)=>{assert.equal(id,3);assert.equal(options.active,true);assert.deepEqual(Object.keys(options),['active']);tab.active=true;order.push('activate');}},
    scripting:{executeScript:async()=>{order.push('execute');return [{result:{ok:true,data:{verified:true,items:[]}}}];}}}};
  loadPolicy(sandbox);
  const result=await sandbox.dispatchWorkflow({action:'import_resolve',expires:Date.now()+30000},{tabId:1,importTabId:3});
  assert.equal(result.ok,true);
  assert.deepEqual(order,['activate','execute']);
});

test('a changed import binding is rejected before tab activation or page execution',async()=>{
  const sandbox={URL,Date,setTimeout,chrome:{
    tabs:{get:async()=>({id:3,active:false,url:'http://admin.ir.lib.sjtu.edu.cn/#/item/entryManage'}),
      update:async()=>{throw Error('must not activate a changed tab');}},
    scripting:{executeScript:async()=>{throw Error('must not execute on a changed tab');}}}};
  loadPolicy(sandbox);
  await assert.rejects(sandbox.dispatchWorkflow({action:'import_resolve',expires:Date.now()+30000},
    {tabId:1,importTabId:3}),/已切换/);
});

for (const origin of origins) {
  test(`${origin}: binding accepts only the exact HTTPS WOS origin`, () => {
    assert.equal(policy.validRolePage(origin + '/wos/woscc/basic-search', 'wosTabId'), true);
    assert.equal(policy.validRolePage(origin + '/wos/author/author-search', 'wosTabId'), true);
    assert.equal(policy.validRolePage(origin + '/', 'wosTabId'), false);
    assert.equal(policy.validRolePage(origin.replace('https:', 'http:') + '/wos/', 'wosTabId'), false);
    assert.equal(policy.validRolePage(origin + ':8443/wos/', 'wosTabId'), false);
    assert.equal(policy.validRolePage(origin.replace('https://', 'https://user:secret@') + '/wos/', 'wosTabId'), false);
    assert.equal(policy.validRolePage(origin + '.example.invalid/wos/', 'wosTabId'), false);
    assert.equal(policy.validRolePage(origin + '/wos/', 'importTabId'), false);
  });
  test(`${origin}: downloads must correlate to this record and this origin`, () => {
    const record = origin + recordPath;
    const blob = 'blob:' + origin + '/synthetic-id';
    assert.equal(policy.isExpectedWOSDownload({url:blob, referrer:''}, record), true);
    assert.equal(policy.isExpectedWOSDownload({url:blob, referrer:record + '?view=1#section'}, record), true);
    const encodedRecord=origin+recordPath.replace('WOS:','WOS%3A');
    assert.equal(policy.isExpectedWOSDownload({url:blob, referrer:encodedRecord}, encodedRecord), true);
    assert.equal(policy.isExpectedWOSDownload({url:blob, referrer:record}, encodedRecord), true);
    assert.equal(policy.isExpectedWOSDownload({url:blob, referrer:encodedRecord}, record), true);
    assert.equal(policy.isExpectedWOSDownload({url:origin + '/export.txt', referrer:record}, record), true);
    assert.equal(policy.isExpectedWOSDownload({url:origin + '/export.txt', referrer:''}, record), false);
    assert.equal(policy.isExpectedWOSDownload({url:blob, referrer:origin + '/wos/woscc/basic-search'}, record), false);
    assert.equal(policy.isExpectedWOSDownload({url:blob, referrer:record.replace('789012', '789013')}, record), false);
    const other = origins.find(x => x !== origin);
    assert.equal(policy.isExpectedWOSDownload({url:'blob:' + other + '/synthetic-id', referrer:''}, record), false);
    assert.equal(policy.isExpectedWOSDownload({url:'blob:' + other + '/synthetic-id', referrer:record}, record), false);
    assert.equal(policy.isExpectedWOSDownload({url:blob, referrer:other + recordPath}, record), false);
    assert.equal(policy.isExpectedWOSDownload({url:blob}, origin + '/wos/woscc/basic-search'), false);
    assert.equal(policy.isExpectedWOSDownload({url:'invalid'}, record), false);
    assert.equal(policy.isExpectedWOSDownload({url:blob,referrer:origin+'/wos%2Fwoscc%2Ffull-record%2FWOS%3A000123456789012'},
      origin+'/wos%2Fwoscc%2Ffull-record%2FWOS%3A000123456789012'),false);
  });
  test(`${origin}: navigation retains the bound regional origin`, async () => {
    const navigations = [], executions = [];
    let tab = {id:2, url:origin + '/wos/author/author-search', status:'complete'};
    const sandbox = {URL, Date, setTimeout, runWOSCommand(){}, chrome:{
      tabs:{get:async()=>tab, update:async(id, change)=>{navigations.push(change.url);tab={...tab,...change};}},
      scripting:{executeScript:async input=>{executions.push(input);const action=input.args[0].action;
        if(action==='wos_start_search'){tab={...tab,url:origin+recordPath,status:'complete'};return [{result:{ok:true,data:{submitted:true}}}];}
        return [{result:{ok:true,data:{state:'record',record_url:origin+recordPath}}}];}},
    }};
    loadPolicy(sandbox);
    const result = await sandbox.dispatchWorkflow({action:'wos_search', expires:Date.now()+30000}, {tabId:1,wosTabId:2});
    assert.equal(result.ok, true);
    assert.deepEqual(navigations, [origin + '/wos/woscc/basic-search']);
    assert.equal(executions.length, 2);
    assert.equal(executions[0].target.tabId, 2);
    assert.equal(executions[0].args[0].action, 'wos_start_search');
    assert.equal(executions[1].args[0].action, 'wos_read_results');
    assert.equal(executions[0].world, 'MAIN');
    assert.equal(executions[1].world, 'ISOLATED');
  });
}

test('only the pre-click stale-zero marker requests one clean WOS reload', async()=>{
  const stale='[WOS 已暂停] WOS 检索页保留上一条零结果，需刷新检索页';
  assert.equal(policy.shouldReloadWOSSearch({ok:false,error:stale}),true);
  assert.equal(policy.shouldReloadWOSSearch({ok:false,error:'WOS 检索页保留上一条零结果，需刷新检索页'}),false);
  assert.equal(policy.shouldReloadWOSSearch({ok:false,error:'加载结果超时，未自动重复操作'}),false);
  let tab={id:2,url:origins[0]+'/wos/woscc/basic-search',status:'complete'}, reloads=0, executions=0;
  const sandbox={URL,Date,setTimeout,runWOSCommand(){},chrome:{
    tabs:{get:async()=>tab,update:async()=>{},reload:async()=>{reloads++;tab={...tab,status:'complete'};}},
    scripting:{executeScript:async input=>{executions++;const action=input.args[0].action;
      if(action==='wos_start_search'&&executions===1)return [{result:{ok:false,error:stale}}];
      if(action==='wos_diagnose')return [{result:{core_search_route:true,query_input_count:1,
        zero_result:false,busy:false,wos_error:false,site_timeout:false,login_required:false,dialog_count:0}}];
      if(action==='wos_start_search'){tab={...tab,url:origins[0]+recordPath};return [{result:{ok:true,data:{submitted:true}}}];}
      return [{result:{ok:true,data:{state:'record',record_url:origins[0]+recordPath}}}];}},
  }};
  loadPolicy(sandbox);
  const result=await sandbox.dispatchWorkflow({action:'wos_search',expires:Date.now()+30000},{tabId:1,wosTabId:2});
  assert.equal(result.ok,true);assert.equal(reloads,1);assert.equal(executions,4);
});

test('one canonical result is navigated by the extension background, never clicked in-page', async()=>{
  const origin=origins[0], navigations=[];
  let tab={id:2,url:origin+'/wos/woscc/basic-search',status:'complete'}, reads=0;
  const sandbox={URL,Date,setTimeout,runWOSCommand(){},chrome:{
    tabs:{get:async()=>tab,update:async(id,change)=>{navigations.push(change.url);tab={...tab,...change,status:'complete'};}},
    scripting:{executeScript:async input=>{const action=input.args[0].action;
      if(action==='wos_start_search')return [{result:{ok:true,data:{submitted:true}}}];
      if(action==='wos_read_results'&&reads++===0)return [{result:{ok:true,data:{state:'single',navigate_url:origin+recordPath}}}];
      return [{result:{ok:true,data:{state:'record',record_url:origin+recordPath}}}];}},
  }};
  loadPolicy(sandbox);
  const result=await sandbox.dispatchWorkflow({action:'wos_search',title:'Synthetic',expires:Date.now()+30000},{tabId:1,wosTabId:2});
  assert.equal(result.ok,true);assert.deepEqual(navigations,[origin+recordPath]);
});

test('slow zero-result reset waits for fresh search DOM before the next Search',async()=>{
  const origin=origins[1],stale='[WOS 已暂停] WOS 检索页保留上一条零结果，需刷新检索页';
  let tab={id:2,url:origin+'/wos/woscc/basic-search',status:'complete'};
  let reloads=0,starts=0,refreshReads=0,searchClicks=0,clean=false;
  const sandbox={URL,Date,setTimeout,runWOSCommand(){},chrome:{
    tabs:{get:async()=>tab,update:async()=>{},reload:async()=>{reloads++;}},
    scripting:{executeScript:async input=>{
      const action=input.args[0].action;
      if(action==='wos_diagnose'){
        assert.equal(input.world,'ISOLATED');
        if(++refreshReads>=4)clean=true;
        return [{result:{core_search_route:true,query_input_count:clean?1:0,
          zero_result:!clean,busy:false,wos_error:false,site_timeout:false,login_required:false,dialog_count:0}}];
      }
      if(action==='wos_start_search'){
        starts++;
        if(!clean)return [{result:{ok:false,error:stale}}];
        searchClicks++;tab={...tab,url:origin+recordPath};
        return [{result:{ok:true,data:{submitted:true}}}];
      }
      return [{result:{ok:true,data:{state:'record',record_url:origin+recordPath}}}];
    }}
  }};
  loadPolicy(sandbox);
  const result=await sandbox.dispatchWorkflow({action:'wos_search',title:'Next paper',expires:Date.now()+30000},
    {tabId:1,wosTabId:2});
  assert.equal(result.ok,true,JSON.stringify(result));
  assert.equal(reloads,1);assert.equal(starts,2);assert.equal(searchClicks,1);assert.equal(refreshReads,4);
});

test('an uncleared zero banner after reload pauses without searching or claiming a new zero result',async()=>{
  let now=Date.now(),reloads=0,starts=0;
  const sandbox={URL,Date:{now:()=>now},setTimeout:(fn,ms)=>setTimeout(()=>{now+=ms;fn();},Math.min(ms,2)),
    runWOSCommand(){},chrome:{
    tabs:{get:async()=>({id:2,url:origins[0]+'/wos/woscc/basic-search'}),update:async()=>{},reload:async()=>{reloads++;}},
    scripting:{executeScript:async input=>{
      if(input.args[0].action==='wos_start_search'){
        starts++;return [{result:{ok:false,error:'[WOS 已暂停] WOS 检索页保留上一条零结果，需刷新检索页'}}];
      }
      return [{result:{core_search_route:true,query_input_count:1,zero_result:true,busy:false,
        wos_error:false,site_timeout:false,login_required:false,dialog_count:0}}];
    }}
  }};
  loadPolicy(sandbox);
  await assert.rejects(sandbox.dispatchWorkflow({action:'wos_search',title:'Next paper',expires:now+14000},
    {tabId:1,wosTabId:2}),/刷新后的检索页尚未就绪.*未提交当前论文/);
  assert.equal(reloads,1);assert.equal(starts,1);
});

test('login, site error and foreign dialogs after a zero-result reload never submit Search',async()=>{
  for(const [flag,value,error] of [['login_required',true,/登录或人工验证/],['wos_error',true,/网站报错/],
      ['site_timeout',true,/5xx\/连接超时/],['dialog_count',1,/操作弹窗/]]){
    let starts=0,reloads=0;
    const sandbox={URL,Date,setTimeout,runWOSCommand(){},chrome:{
      tabs:{get:async()=>({id:2,url:origins[0]+'/wos/woscc/basic-search'}),update:async()=>{},reload:async()=>{reloads++;}},
      scripting:{executeScript:async input=>{
        if(input.args[0].action==='wos_start_search'){
          starts++;return [{result:{ok:false,error:'[WOS 已暂停] WOS 检索页保留上一条零结果，需刷新检索页'}}];
        }
        return [{result:{core_search_route:true,query_input_count:1,zero_result:false,busy:false,
          wos_error:false,site_timeout:false,login_required:false,dialog_count:0,[flag]:value}}];
      }}
    }};
    loadPolicy(sandbox);
    await assert.rejects(sandbox.dispatchWorkflow({action:'wos_search',expires:Date.now()+30000},
      {tabId:1,wosTabId:2}),error);
    assert.equal(reloads,1);assert.equal(starts,1);
  }
});

test('binding errors distinguish SA reuse, wrong role, and wrong backend menu without echoing credentials', () => {
  assert.match(policy.workflowBindingError(origins[0] + '/wos/', 'wosTabId', true), /SA 比对.*独立标签页/);
  assert.equal(policy.workflowBindingError(origins[1] + '/wos/woscc/basic-search', 'wosTabId', false), '');
  assert.match(policy.workflowBindingError('http://admin.ir.lib.sjtu.edu.cn/#/item/entryManage', 'importTabId', false), /普通.*数据管理.*数据导入与批次管理/);
  assert.match(policy.workflowBindingError('http://admin.ir.lib.sjtu.edu.cn/#/wel/index', 'importTabId', false), /数据导入与批次管理/);
  assert.equal(policy.workflowBindingError('http://admin.ir.lib.sjtu.edu.cn/#/collectItem/batchManage', 'importTabId', false), '');
  const error = policy.workflowBindingError('https://example.invalid/login?ticket=DO_NOT_ECHO', 'wosTabId', false);
  assert.match(error, /网址不受支持/);assert.ok(!error.includes('DO_NOT_ECHO'));
  assert.equal(policy.validRolePage('not a URL', 'wosTabId'), false);
});

test('a redirect to the other WOS origin stops before page execution', async () => {
  let tab={id:2,url:origins[1]+'/wos/author/author-search',status:'complete'}, executed=false;
  const sandbox={URL,Date,setTimeout,runWOSCommand(){},chrome:{
    tabs:{get:async()=>tab,update:async()=>{tab={...tab,url:origins[0]+'/wos/woscc/basic-search'};}},
    scripting:{executeScript:async()=>{executed=true;return [];}}
  }};
  loadPolicy(sandbox);
  await assert.rejects(sandbox.dispatchWorkflow({action:'wos_search',expires:Date.now()+30000},{tabId:1,wosTabId:2}), /域名/);
  assert.equal(executed,false);
});

test('manifest grants both exact WOS hosts, not broad wildcard hosts', () => {
  const manifest=require('../extension/manifest.json');
  assert.match(manifest.version,/^0\.4\.\d+$/);
  for(const origin of origins)assert.ok(manifest.host_permissions.includes(origin+'/*'));
  assert.ok(!manifest.host_permissions.some(x=>x.includes('*://')||x.includes('://*.')||x==='<all_urls>'));
});

test('WOS probes read a rendered loading tab immediately instead of waiting for document_idle', async()=>{
  const origin=origins[1],calls=[],navigations=[];
  let tab={id:2,url:origin+'/wos/woscc/summary/previous',status:'loading'};
  const sandbox={URL,Date,setTimeout,runWOSCommand(){},chrome:{
    tabs:{get:async()=>tab,update:async(id,change)=>{navigations.push(change.url);tab={...tab,...change,status:'loading'};}},
    scripting:{executeScript:async options=>{
      calls.push(options);
      if(options.args[0].action==='wos_start_search'){
        tab={...tab,url:origin+'/wos/woscc/summary/new',status:'loading'};
        return [{result:{ok:true,data:{submitted:true}}}];
      }
      return [{result:{ok:true,data:tab.url.includes('/summary/')?
        {state:'single',navigate_url:origin+recordPath}:{state:'record',record_url:origin+recordPath}}}];
    }}
  }};
  loadPolicy(sandbox);
  const result=await sandbox.dispatchWorkflow({action:'wos_search',title:'Synthetic',expires:Date.now()+30000},{tabId:1,wosTabId:2});
  assert.equal(result.ok,true);assert.equal(calls.length,3);
  assert.ok(calls.every(call=>call.injectImmediately===true));
  assert.deepEqual(navigations,[origin+'/wos/woscc/basic-search',origin+recordPath]);
});

test('a list still mounted after tabs.update is polled, not navigated twice', async()=>{
  const origin=origins[0],navigations=[];
  let tab={id:2,url:origin+'/wos/woscc/basic-search',status:'complete'},reads=0,starts=0;
  const sandbox={URL,Date,setTimeout,runWOSCommand(){},chrome:{
    tabs:{get:async()=>tab,update:async(id,change)=>{navigations.push(change.url);tab={...tab,...change};}},
    scripting:{executeScript:async options=>{
      if(options.args[0].action==='wos_start_search'){starts++;return [{result:{ok:true,data:{submitted:true}}}];}
      reads++;
      return [{result:{ok:true,data:reads<=2?{state:'single',navigate_url:origin+recordPath}:
        {state:'record',record_url:origin+recordPath}}}];
    }}
  }};
  loadPolicy(sandbox);
  const result=await sandbox.dispatchWorkflow({action:'wos_search',title:'Synthetic',expires:Date.now()+30000},{tabId:1,wosTabId:2});
  assert.equal(result.ok,true);assert.equal(starts,1);assert.equal(reads,3);
  assert.deepEqual(navigations,[origin+recordPath]);
});

test('navigation probes may transiently lose a frame, but never retry Search', async()=>{
  const origin=origins[0];let reads=0,starts=0;
  const sandbox={URL,Date,setTimeout,runWOSCommand(){},chrome:{
    tabs:{get:async()=>({id:2,url:origin+'/wos/woscc/basic-search',status:'loading'})},
    scripting:{executeScript:async options=>{
      if(options.args[0].action==='wos_start_search'){starts++;return [{result:{ok:true,data:{submitted:true}}}];}
      reads++;
      if(reads===1)throw new Error('Execution context was destroyed');
      if(reads===2)return [];
      return [{result:{ok:true,data:{state:'zero'}}}];
    }}
  }};
  loadPolicy(sandbox);
  const result=await sandbox.dispatchWorkflow({action:'wos_search',title:'Synthetic',expires:Date.now()+30000},{tabId:1,wosTabId:2});
  assert.equal(result.ok,false);assert.match(result.error,/WOS 未找到记录/);
  assert.equal(starts,1);assert.equal(reads,3);
});

test('a previous full-record DOM is not accepted for the new navigation target', async()=>{
  const origin=origins[0],other=origin+recordPath.replace('789012','789013');
  let tab={id:2,url:origin+'/wos/woscc/basic-search',status:'complete'},reads=0,navigations=0;
  const sandbox={URL,Date,setTimeout,runWOSCommand(){},chrome:{
    tabs:{get:async()=>tab,update:async(id,change)=>{navigations++;tab={...tab,...change};}},
    scripting:{executeScript:async options=>{
      if(options.args[0].action==='wos_start_search')return [{result:{ok:true,data:{submitted:true}}}];
      reads++;
      return [{result:{ok:true,data:reads===1?{state:'single',navigate_url:origin+recordPath}:
        {state:'record',record_url:reads===2?other:origin+recordPath}}}];
    }}
  }};
  loadPolicy(sandbox);
  const result=await sandbox.dispatchWorkflow({action:'wos_search',title:'Synthetic',expires:Date.now()+30000},{tabId:1,wosTabId:2});
  assert.equal(result.ok,true);assert.equal(result.data.record_url,origin+recordPath);
  assert.equal(reads,3);assert.equal(navigations,1);
});

test('timeout diagnostics distinguish missing totals/links without exposing arbitrary page data',()=>{
  const message=policy.wosSearchTimeout({summary_route:true,result_total:1,canonical_record_link_count:0,busy:false,
    title:'PRIVATE_TITLE',url:'https://example.invalid/?token=PRIVATE_KEY'});
  assert.match(message,/结果页；文献总数 1；安全单篇链接 0；加载中 否/);
  assert.ok(!message.includes('PRIVATE_'));
  assert.match(policy.wosSearchTimeout({result_total:'PRIVATE_QUERY',canonical_record_link_count:'PRIVATE_ID'}),/文献总数 未确认；安全单篇链接 未确认/);
  assert.equal(policy.isWOSRecordPage('https://user:secret@www.webofscience.com'+recordPath,origins[0]),false);
});

test('an unreturned read probe is bounded; observation retries never repeat Search',async()=>{
  let starts=0,reads=0;
  const sandbox={URL,Date,setTimeout:(fn,ms)=>setTimeout(fn,Math.min(ms,5)),runWOSCommand(){},chrome:{
    tabs:{get:async()=>({id:2,url:origins[0]+'/wos/woscc/basic-search'})},
    scripting:{executeScript:async options=>{
      if(options.args[0].action==='wos_start_search'){starts++;return [{result:{ok:true,data:{submitted:true}}}];}
      assert.equal(options.func.name,'inspectWorkPage');
      assert.equal(options.world,'ISOLATED');
      if(++reads===1)return new Promise(()=>{});
      return [{result:{ok:true,data:{state:'zero'}}}];
    }}
  }};
  loadPolicy(sandbox);
  const result=await sandbox.dispatchWorkflow({action:'wos_search',title:'Synthetic',expires:Date.now()+30000},{tabId:1,wosTabId:2});
  assert.equal(result.ok,false);assert.match(result.error,/WOS 未找到记录/);
  assert.equal(starts,1);assert.equal(reads,2);
});

test('download capability check is read-only and reports the actual running extension',async()=>{
  const injections=[];
  const sandbox={URL,Date,setTimeout,chrome:{
    runtime:{getManifest:()=>({version:'0.3.29'})},
    tabs:{get:async()=>({id:2,url:origins[1]+'/wos/woscc/summary/existing'})},
    scripting:{executeScript:async input=>{
      injections.push(input);
      return [{result:{core_search_route:false,query_input_count:0,wos_error:false,
        login_required:false,dialog_count:0,busy:false,private_text:'PRIVATE_QUERY'}}];
    }}
  }};
  loadPolicy(sandbox);
  const result=await sandbox.dispatchWorkflow({action:'wos_diagnose',expires:Date.now()+25000},{tabId:1,wosTabId:2});
  assert.equal(result.data.extension_version,'0.3.29');
  assert.equal(result.data.wos_download_protocol,1);
  assert.equal(result.data.result_reader,'shared-diagnostic');
  assert.equal(injections.length,1);
  assert.equal(injections[0].world,'ISOLATED');
  assert.equal(injections[0].func,sandbox.inspectWorkPage);
  assert.equal(injections[0].args[0].action,'wos_diagnose');
  assert.equal(JSON.stringify(result).includes('PRIVATE_QUERY'),false);
});

test('download preflight waits for delayed search inputs without a Search or navigation',async()=>{
  let reads=0;
  const sandbox={URL,Date,setTimeout,chrome:{
    runtime:{getManifest:()=>({version:'0.3.29'})},
    tabs:{get:async()=>({id:2,url:origins[1]+'/wos/woscc/basic-search'})},
    scripting:{executeScript:async input=>{
      assert.equal(input.world,'ISOLATED');
      assert.equal(input.args[0].action,'wos_diagnose');
      return [{result:{core_search_route:true,query_input_count:++reads<2?0:1,
        wos_error:false,login_required:false,dialog_count:0,busy:false}}];
    }}
  }};
  loadPolicy(sandbox);
  const result=await sandbox.dispatchWorkflow({action:'wos_diagnose',expires:Date.now()+25000},{tabId:1,wosTabId:2});
  assert.equal(reads,2);
  assert.equal(result.data.page.query_input_count,1);
});

test('empty initialized route returns an unavailable page, not a fabricated zero result',async()=>{
  let reads=0;
  const sandbox={URL,Date,setTimeout,chrome:{
    runtime:{getManifest:()=>({version:'0.3.29'})},
    tabs:{get:async()=>({id:2,url:origins[1]+'/wos/woscc/basic-search'})},
    scripting:{executeScript:async()=>{
      reads++;
      return [{result:{core_search_route:true,query_input_count:0,
        wos_error:false,login_required:false,dialog_count:0,busy:false}}];
    }}
  }};
  loadPolicy(sandbox);
  const result=await sandbox.dispatchWorkflow({action:'wos_diagnose',expires:Date.now()+13000},{tabId:1,wosTabId:2});
  assert.ok(reads>=1);
  assert.equal(result.data.page.query_input_count,0);
  assert.equal('zero_result' in result.data.page,false);
});

test('preflight read failure is not retried or used as permission to search',async()=>{
  let reads=0;
  const sandbox={URL,Date,setTimeout,chrome:{
    runtime:{getManifest:()=>({version:'0.3.29'})},
    tabs:{get:async()=>({id:2,url:origins[1]+'/wos/woscc/basic-search'})},
    scripting:{executeScript:async()=>{reads++;throw new Error('renderer unavailable');}}
  }};
  loadPolicy(sandbox);
  await assert.rejects(()=>sandbox.dispatchWorkflow({action:'wos_diagnose',expires:Date.now()+25000},{tabId:1,wosTabId:2}),/renderer unavailable/);
  assert.equal(reads,1);
});
