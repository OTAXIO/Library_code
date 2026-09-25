const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');
const source = fs.readFileSync(path.join(__dirname, '../extension/workflow-background.js'), 'utf8');
const policy = require('../extension/workflow-background.js');
const origins = ['https://www.webofscience.com', 'https://webofscience.clarivate.cn'];
const recordPath = '/wos/woscc/full-record/WOS:000123456789012';

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
  });
  test(`${origin}: navigation retains the bound regional origin`, async () => {
    const navigations = [], executions = [];
    let tab = {id:2, url:origin + '/wos/author/author-search', status:'complete'};
    const sandbox = {URL, Date, setTimeout, runWOSCommand(){}, chrome:{
      tabs:{get:async()=>tab, update:async(id, change)=>{navigations.push(change.url);tab={...tab,...change};}},
      scripting:{executeScript:async input=>{executions.push(input);return [{result:{ok:true}}];}},
    }};
    vm.createContext(sandbox);vm.runInContext(source, sandbox);
    const result = await sandbox.dispatchWorkflow({action:'wos_search', expires:Date.now()+30000}, {tabId:1,wosTabId:2});
    assert.equal(result.ok, true);
    assert.deepEqual(navigations, [origin + '/wos/woscc/basic-search']);
    assert.equal(executions.length, 1);
    assert.equal(executions[0].target.tabId, 2);
  });
}

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
  vm.createContext(sandbox);vm.runInContext(source,sandbox);
  await assert.rejects(sandbox.dispatchWorkflow({action:'wos_search',expires:Date.now()+30000},{tabId:1,wosTabId:2}), /域名/);
  assert.equal(executed,false);
});

test('manifest grants both exact WOS hosts, not broad wildcard hosts', () => {
  const manifest=require('../extension/manifest.json');
  assert.equal(manifest.version,'0.3.3');
  for(const origin of origins)assert.ok(manifest.host_permissions.includes(origin+'/*'));
  assert.ok(!manifest.host_permissions.some(x=>x.includes('*://')||x.includes('://*.')||x==='<all_urls>'));
});
