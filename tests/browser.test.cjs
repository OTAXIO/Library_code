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
  try {
    for (const [name, fn] of cases) { await reset(); await fn(); console.log('PASS',name); }
    console.log(`Browser adapter: ${cases.length} offline cases passed. No production requests.`);
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
