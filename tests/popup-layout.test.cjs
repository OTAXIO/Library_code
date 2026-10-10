/* Render only the project's local popup, never a user's browser profile. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {pathToFileURL}=require('node:url');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'..');
(async()=>{
  const browser=await chromium.launch({headless:true,executablePath:process.env.SA_TEST_BROWSER||'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  try {
    const page=await browser.newPage({viewport:{width:400,height:600}});
    await page.addInitScript(()=>{
      window.calls=[];
      window.activeURL='http://admin.ir.lib.sjtu.edu.cn/#/dataCompare/list';
      window.chrome={tabs:{query:async()=>[{id:1,url:window.activeURL}]},runtime:{sendMessage:async message=>{
        window.calls.push(message);return {ok:true};}}};
    });
    await page.goto(pathToFileURL(path.join(root,'extension/popup.html')).href);
    const folder=path.join(root,'runtime/ui-preview');fs.mkdirSync(folder,{recursive:true});
    await page.screenshot({path:path.join(folder,'popup-0.4.0.png')});
    assert.equal(await page.locator('.workflow').evaluate(el=>el.open),false);
    const status=await page.locator('#status').boundingBox();
    assert.ok(status.y+status.height<=600,'connection status must be visible on opening');
    await page.locator('summary').click();
    const labels={pair:'连接 SA 比对页',wos:'绑定当前 WOS 页',import:'绑定当前导入页',
      'open-import':'打开数据导入与批次管理',inspect:'查看连接诊断',disconnect:'断开连接'};
    for (const [id,text] of Object.entries(labels)) {
      const button=page.locator('#'+id);
      assert.ok((await button.innerText()).includes(text));
      await button.scrollIntoViewIfNeeded();
      const dimensions=await button.evaluate(el=>({width:el.clientWidth,scroll:el.scrollWidth,rect:el.getBoundingClientRect().toJSON()}));
      assert.ok(dimensions.scroll<=dimensions.width+1,`${id} text overflows`);
      assert.ok(dimensions.rect.x>=0&&dimensions.rect.right<=400,`${id} outside popup`);
    }
    await page.locator('#token').fill('synthetic-pair-code');
    await page.locator('#pair').click();
    assert.equal(await page.locator('#token').inputValue(),'');
    await page.locator('#wos').click();
    assert.ok((await page.locator('#status').innerText()).includes('WOS 页已绑定'));
    await page.locator('#import').click();
    assert.ok((await page.locator('#status').innerText()).includes('导入页已绑定'));
    const calls=await page.evaluate(()=>window.calls);
    assert.deepEqual(calls.map(value=>value.type),['pair','bind_workflow','bind_workflow']);
    assert.deepEqual(calls.slice(1).map(value=>value.role),['wosTabId','importTabId']);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.locator('#status').evaluate(el=>{el.textContent='长连接诊断示例 '.repeat(100);});
    const card=await page.locator('.status-card').boundingBox();
    assert.ok(card.height<180,'long status must not squeeze the other controls');
    await page.locator('#diagnostics').evaluate(el=>{el.hidden=false;el.value='synthetic-diagnostic-'.repeat(100);});
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
    await page.locator('#status').evaluate(el=>{el.textContent='布局检查完成';});
    await page.locator('#diagnostics').evaluate(el=>{el.hidden=true;});
    await page.evaluate(()=>window.scrollTo(0,0));
    await page.screenshot({path:path.join(folder,'popup-0.4.0-expanded.png'),fullPage:true});
    assert.equal(await page.locator('#mute').count(),0);
    await page.evaluate(()=>window.activeURL='https://webofscience.clarivate.cn/wos/woscc/basic-search');
    await page.locator('#pair').click();
    assert.ok((await page.locator('#status').innerText()).includes('请先切换到 SA'));
    assert.equal(await page.evaluate(()=>window.calls.length),3,'wrong role must not replace pairing');
    console.log('PASS popup: minimal 6 controls, collapsed/expanded layout and long diagnostics fit; roles unchanged');
  } finally {await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
