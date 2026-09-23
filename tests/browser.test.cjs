const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const {runSACommand} = require('../extension/adapter.js');
const fixture = fs.readFileSync(path.join(__dirname, 'fixtures/compare.html'), 'utf8');
const windowsEdge = 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const launchOptions = {headless: true};
if (process.env.SA_TEST_BROWSER) launchOptions.executablePath = process.env.SA_TEST_BROWSER;
else if (fs.existsSync(windowsEdge)) launchOptions.executablePath = windowsEdge;

(async () => {
  const browser = await chromium.launch(launchOptions);
  const context = await browser.newContext();
  // Every HTTP request is intercepted. This suite never contacts production.
  await context.route('**/*', route => route.fulfill({status:200,contentType:'text/html',body:fixture}));
  const page = await context.newPage();
  const reset = async () => {
    await page.goto('http://admin.ir.lib.sjtu.edu.cn/#/dataCompare/list');
    await page.reload();
  };
  const command = (action, extra={}) => ({id:'test-command', action, sa_id:'demo-001',expires:Date.now()+60000,...extra});
  const execute = value => page.evaluate(runSACommand, value);
  const cases = [];
  const test = (name, fn) => cases.push([name,fn]);
  test('search returns exact record and plaintext comparison',async()=>{
    const result=await execute(command('search'));
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(result.data.row.itemId,'1234567890123456789');
    assert.ok(result.data.comparison[1].sa.includes('\n'));
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('wrong route stops',async()=>{
    await page.evaluate(()=>location.hash='#/login');
    assert.equal((await execute(command('search'))).ok,false);
  });
  test('nonunique result stops',async()=>{
    await page.evaluate(()=>testConfig.total=2);
    assert.match((await execute(command('search'))).error,/唯一/);
  });
  test('zero results stop',async()=>{
    await page.evaluate(()=>testConfig.noRows=true);
    assert.equal((await execute(command('search'))).ok,false);
  });
  test('exact ID is required',async()=>{
    await page.evaluate(()=>synthetic.saLzkId='demo-001-other');
    assert.match((await execute(command('search'))).error,/不完全相同/);
  });
  test('stale query data stops',async()=>{
    await page.evaluate(()=>testConfig.stale=true);
    assert.match((await execute(command('search'))).error,/未取得新数据/);
  });
  test('long numeric ID from server stops',async()=>{
    await page.evaluate(()=>synthetic.itemId=1234567890123456789);
    assert.match((await execute(command('search'))).error,/不是文本/);
  });
  test('unknown search schema stops',async()=>{
    await page.evaluate(()=>vm.searchForm.newField='unknown');
    assert.match((await execute(command('search'))).error,/字段发生变化/);
  });
  test('open user form is never dismissed',async()=>{
    await page.evaluate(()=>document.getElementById('status').style.display='block');
    assert.match((await execute(command('search'))).error,/未关闭/);
    assert.equal(await page.locator('#status').isVisible(),true);
  });
  test('writes require human evidence',async()=>{
    const read=await execute(command('search'));
    const result=await execute(command('complete',{expected:read.data.row,note:'已核验原文证据'}));
    assert.match(result.error,/缺少人工核验/);
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('optimistic concurrency rejects changed record',async()=>{
    const read=await execute(command('search'));
    await page.evaluate(()=>synthetic.updateTime='someone-edited');
    const result=await execute(command('complete',{expected:read.data.row,note:'已核验原文证据',reviewed:true}));
    assert.match(result.error,/数据在核验后已变化/);
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('snapshot key ordering does not trigger false conflicts',async()=>{
    const read=await execute(command('search'));
    const expected=Object.fromEntries(Object.entries(read.data.row).reverse());
    const result=await execute(command('open_metadata',{expected}));
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('complete writes once and verifies status plus remark',async()=>{
    const read=await execute(command('search'));
    const result=await execute(command('complete',{expected:read.data.row,note:'已核验原文，本库正确。',reviewed:true}));
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(result.data.row.markStatus,'已处理');
    assert.equal(await page.evaluate(()=>writeCount),1);
    const again=await execute(command('complete',{expected:result.data.row,note:'已核验原文，本库正确。',reviewed:true}));
    assert.match(again.error,/禁止再次切换/);
    assert.equal(await page.evaluate(()=>writeCount),1);
  });
  test('bad readback becomes uncertain, not success',async()=>{
    const read=await execute(command('search'));
    await page.evaluate(()=>testConfig.badRemark=true);
    const result=await execute(command('complete',{expected:read.data.row,note:'已核验原文，本库正确。',reviewed:true}));
    assert.equal(result.ok,false);
    assert.match(result.error,/已发出写入/);
    assert.equal(await page.evaluate(()=>writeCount),1);
  });
  test('link platform ID preserves all 19 digits',async()=>{
    const read=await execute(command('search'));
    const result=await execute(command('link',{expected:read.data.row,note:'已核验同一文献与平台号。',reviewed:true,item_id:'9876543210987654321'}));
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(result.data.row.itemId,'9876543210987654321');
    assert.equal(result.data.row.markStatus,'待处理');
  });
  test('metadata editing is a manual handoff',async()=>{
    const read=await execute(command('search'));
    const result=await execute(command('open_metadata',{expected:read.data.row}));
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(await page.evaluate(()=>openedEditor),'1234567890123456789');
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('multiple matches cannot open unqualified editor',async()=>{
    await page.evaluate(()=>{synthetic.itemId='1,2';synthetic.matchCount=2;});
    const read=await execute(command('search'));
    const result=await execute(command('open_metadata',{expected:read.data.row}));
    assert.equal(result.ok,false);
  });
  test('expired command never writes',async()=>{
    const result=await execute(command('complete',{expires:Date.now()-1,reviewed:true,note:'已核验原文'}));
    assert.match(result.error,/时限已到/);
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('unrecognized operation cannot call backend jobs',async()=>{
    assert.equal((await execute(command('runIncrement'))).ok,false);
  });
  const completeWithNote = async note => {
    const read = await execute(command('search'));
    assert.equal(read.ok,true,JSON.stringify(read));
    return execute(command('complete',{expected:read.data.row,expected_comparison:read.data.comparison,reviewed:true,note}));
  };
  test('exact short claimed note is accepted after readback',async()=>{
    await page.evaluate(()=>testConfig.claim='已认领');
    const result=await completeWithNote('已认领');
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(result.data.row.remark,'已认领');
  });
  test('unclaimed record cannot use claimed note',async()=>{
    const result=await completeWithNote('已认领');
    assert.equal(result.ok,false);
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('both SA identifiers blank use exact requested note',async()=>{
    const result=await completeWithNote('DOI和WOSID SA未提交');
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(result.data.row.remark,'DOI和WOSID SA未提交');
  });
  test('only one missing identifier cannot use combined note',async()=>{
    await page.evaluate(()=>testConfig.saDoi='10.example/present');
    const result=await completeWithNote('DOI和WOSID SA未提交');
    assert.equal(result.ok,false);
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('missing detail field is not a blank identifier',async()=>{
    await page.evaluate(()=>testConfig.omitWos=true);
    const result=await completeWithNote('DOI和WOSID SA未提交');
    assert.equal(result.ok,false);
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('remark-related detail change stops despite unchanged list row',async()=>{
    const read=await execute(command('search'));
    await page.evaluate(()=>testConfig.libraryWos='WOS:CHANGED');
    const result=await execute(command('complete',{expected:read.data.row,expected_comparison:read.data.comparison,reviewed:true,note:'DOI和WOSID SA未提交'}));
    assert.match(result.error,/详情在确认后发生变化/);
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('confirmed correspondent correction uses exact remark',async()=>{
    const result=await completeWithNote('通讯作者修正');
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(result.data.row.remark,'通讯作者修正');
  });
  test('unknown correspondent flag blocks correction remark',async()=>{
    await page.evaluate(()=>testConfig.authorInfo='是否通讯作者：未知');
    const result=await completeWithNote('通讯作者修正');
    assert.equal(result.ok,false);
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('combined remarks preserve wording',async()=>{
    await page.evaluate(()=>testConfig.claim='已认领');
    const note='已认领；DOI和WOSID SA未提交；通讯作者修正';
    const result=await completeWithNote(note);
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(result.data.row.remark,note);
  });
  const prepare = async (extra={}) => {
    const read=await execute(command('search'));
    assert.equal(read.ok,true,JSON.stringify(read));
    return execute(command('prepare_claim',{expected:read.data.row,sa_text:'测试员(00001)①',staff_id:'00001',roster_staff_id:'00001',...extra}));
  };
  const submit = (read,extra={}) => execute(command('submit_claim',{
    expected:read.data.row,sa_text:read.data.prepared.sa_text,staff_id:read.data.prepared.staff_id,
    prepared:read.data.prepared,author_index:read.data.suggested_index,confirmed:true,...extra}));
  test('claim lookup returns exact person and alias-matched author without writing',async()=>{
    const result=await prepare();
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(result.data.prepared.staff_id,'00001');
    assert.equal(result.data.suggested_index,0);
    assert.equal(result.data.prepared.person.id,'scholar-001');
    assert.equal(await page.evaluate(()=>writeCount),0);
    assert.equal(await page.evaluate(()=>people.queryForm.wno),'00001');
    assert.equal(await page.evaluate(()=>claimWindow.activeName),'author');
  });
  test('confirmed claim submits once and verifies exact author and scholar',async()=>{
    const prepared=await prepare();
    const result=await submit(prepared);
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(result.data.verified,true);
    assert.equal(result.data.scholar_id,'scholar-001');
    assert.equal(result.data.author,'Demo');
    assert.equal(await page.evaluate(()=>writeCount),1);
    assert.equal(await page.evaluate(()=>synthetic.markStatus),'待处理');
  });
  test('claim snapshot accepts reordered object keys from extension messaging',async()=>{
    const prepared=await prepare();
    const reorder=value=>Array.isArray(value)?value.map(reorder):value&&typeof value==='object'?
      Object.fromEntries(Object.entries(value).reverse().map(([key,item])=>[key,reorder(item)])):value;
    const result=await submit(prepared,{prepared:reorder(prepared.data.prepared)});
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(await page.evaluate(()=>writeCount),1);
  });
  test('ambiguous author alias is not auto-selected; human choice can resolve',async()=>{
    await page.evaluate(()=>testConfig.people=[{id:'scholar-001',wno:'00001',nameCn:'测试员',aliases:[{nameAlias:'Demo'},{nameAlias:'Coauthor'}]}]);
    const prepared=await prepare();
    assert.equal(prepared.ok,true,JSON.stringify(prepared));
    assert.equal(prepared.data.suggested_index,null);
    const result=await submit(prepared,{author_index:1});
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(result.data.author,'Coauthor');
  });
  test('nonunique, missing, fuzzy and numeric person identifiers stop without writes',async()=>{
    for(const config of [
      {peopleTotal:2}, {people:[]}, {people:[{id:'s',wno:'100001',nameCn:'测试员'}]},
      {people:[{id:'s',wno:1,nameCn:'测试员'}]}, {stalePeople:true}
    ]){
      await reset();await page.evaluate(value=>Object.assign(testConfig,value),config);
      const result=await prepare();assert.equal(result.ok,false,JSON.stringify(result));
      assert.equal(await page.evaluate(()=>writeCount),0);
    }
  });
  test('SA ambiguity, roster conflict, changed SA source and multiple items stop',async()=>{
    for(const config of [{saClaim:'甲(1);乙(2)'},{saClaim:'测试员(00002)'},{saClaim:'测试员(1e8)'}]){
      await reset();await page.evaluate(value=>Object.assign(testConfig,value),config);
      assert.equal((await prepare()).ok,false);assert.equal(await page.evaluate(()=>writeCount),0);
    }
    await reset();assert.equal((await prepare({roster_staff_id:'00002'})).ok,false);
    await reset();await page.evaluate(()=>{synthetic.itemId='1,2';synthetic.matchCount=2;});
    assert.equal((await prepare()).ok,false);
  });
  test('fullwidth SA parentheses preserve exact identifier',async()=>{
    await page.evaluate(()=>testConfig.saClaim='测试员（00001）');
    const result=await prepare({sa_text:'测试员（00001）'});
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(result.data.prepared.staff_id,'00001');
  });
  test('claim requires explicit confirmation and author selection',async()=>{
    let prepared=await prepare();
    assert.equal((await submit(prepared,{confirmed:false})).ok,false);
    prepared=await prepare();
    assert.equal((await submit(prepared,{author_index:null})).ok,false);
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('person or author change after preparation prevents submit',async()=>{
    for(const config of [
      {people:[{id:'changed-scholar',wno:'00001',nameCn:'测试员',aliases:[{nameAlias:'Demo'}]}]},
      {authors:[{id:'a',order:1,fullname:'Someone else',scholarId:null}]}
    ]){
      await reset();const prepared=await prepare();
      await page.evaluate(value=>Object.assign(testConfig,value),config);
      assert.equal((await submit(prepared)).ok,false);assert.equal(await page.evaluate(()=>writeCount),0);
    }
  });
  test('existing claim and explicitly denied relationship cannot be overwritten',async()=>{
    await page.evaluate(()=>claimedUsers[1]='scholar-001');
    assert.equal((await prepare()).ok,false);
    await reset();await page.evaluate(()=>testConfig.relations={'1':[{scholarId:'scholar-001',status:2}]});
    const prepared=await prepare();
    assert.equal(prepared.ok,true,JSON.stringify(prepared));
    assert.match((await submit(prepared)).error,/非本人作品/);
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('different existing claimant, forbidden institution and wrong item stop',async()=>{
    let prepared=await prepare();
    await page.evaluate(()=>claimedUsers[1]='another-scholar');
    assert.equal((await submit(prepared)).ok,false);
    for(const config of [{nonInstitution:true},{wrongClaimItem:'wrong'}]){
      await reset();await page.evaluate(value=>Object.assign(testConfig,value),config);
      assert.equal((await prepare()).ok,false);assert.equal(await page.evaluate(()=>writeCount),0);
    }
  });
  test('another active relationship cannot be replaced even if author scholarId is empty',async()=>{
    await page.evaluate(()=>testConfig.relations={'1':[{scholarId:'another-scholar',status:6}]});
    const prepared=await prepare();
    assert.equal(prepared.ok,true,JSON.stringify(prepared));
    assert.match((await submit(prepared)).error,/其他有效认领/);
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('bad claim readback is uncertain, no retry or completed marker',async()=>{
    const prepared=await prepare();await page.evaluate(()=>testConfig.badClaimReadback=true);
    const result=await submit(prepared);
    assert.match(result.error,/已发出写入/);
    assert.equal(await page.evaluate(()=>writeCount),1);
    assert.equal(await page.evaluate(()=>synthetic.markStatus),'待处理');
  });
  test('claim timeout submits at most once and prohibits retry',async()=>{
    const prepared=await prepare();await page.evaluate(()=>testConfig.hangClaim=true);
    const result=await submit(prepared,{expires:Date.now()+4000});
    assert.match(result.error,/已发出写入/);
    assert.equal(await page.evaluate(()=>writeCount),1);
  });
  test('user pending selection is never discarded',async()=>{
    const prepared=await prepare();
    await page.evaluate(()=>claimWindow.tableData.metadata.author[0].data={name:'manual choice'});
    const result=await submit(prepared);
    assert.match(result.error,/人工操作或未保存/);
    assert.equal(await page.evaluate(()=>writeCount),0);
    assert.equal(await page.evaluate(()=>claimWindow.tableData.metadata.author[0].data.name),'manual choice');
  });
  test('already processed record cannot prepare claim',async()=>{
    await page.evaluate(()=>synthetic.markStatus='已处理');
    assert.equal((await prepare()).ok,false);
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  try {
    for (const [name, fn] of cases) { await reset(); await fn(); console.log('PASS',name); }
    console.log(`Browser adapter: ${cases.length} offline cases passed. No production requests.`);
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
