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
  test('status precheck reads exact row without opening lazy detail',async()=>{
    const result=await execute(command('status'));
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(result.data.row.saLzkId,'demo-001');
    assert.equal(result.data.row.markStatus,'待处理');
    assert.equal(await page.evaluate(()=>vm.$refs.compareDetailDrawer.dialogVisible),false);
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('status precheck works with unrendered detail but rejects visible windows',async()=>{
    await page.evaluate(()=>{
      const d=vm.$refs.compareDetailDrawer,u=d.$children[0];
      u.$el=document.createElement('div');u.$refs={};u.rendered=false;
      d.dialogVisible=true;
      document.getElementById('drawer-wrapper').style.display='none';
    });
    const result=await execute(command('status'));
    assert.equal(result.ok,true,JSON.stringify(result));
    await page.evaluate(()=>document.getElementById('status').style.display='block');
    assert.match((await execute(command('status'))).error,/未关闭的窗口/);
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('same-record readonly drawer is refreshed without a close cycle',async()=>{
    const first=await execute(command('search'));assert.equal(first.ok,true,JSON.stringify(first));
    await page.evaluate(()=>testConfig.drawerCloseDelay=20000);
    const result=await execute(command('search',{expires:Date.now()+4200}));
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(await page.evaluate(()=>window.closeCalls||0),0);
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('lazy drawer comment before first open is materialized and revalidated',async()=>{
    await page.evaluate(()=>{
      const d=vm.$refs.compareDetailDrawer,u=d.$children[0],root=u.$el,panel=u.$refs.drawer;
      const placeholder=document.createComment('lazy drawer');root.replaceWith(placeholder);
      u.$el=placeholder;u.$refs={};u.rendered=false;
      const show=d.show;
      d.show=function(row){placeholder.replaceWith(root);u.$el=root;u.$refs.drawer=panel;u.rendered=true;show.call(this,row);};
    });
    const result=await execute(command('search'));
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal((await execute(command('search'))).ok,true);
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('lazy DIV root with zero panels does not block first read',async()=>{
    await page.evaluate(()=>{
      const d=vm.$refs.compareDetailDrawer,u=d.$children[0],root=u.$el,panel=u.$refs.drawer;
      const placeholder=document.createElement('div');placeholder.textContent='closed lazy component';
      root.replaceWith(placeholder);u.$el=placeholder;u.$refs={};u.rendered=false;
      const show=d.show;
      d.show=function(row){placeholder.replaceWith(root);u.$el=root;u.$refs.drawer=panel;
        u.rendered=true;show.call(this,row);};
    });
    const result=await execute(command('search'));
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(result.data.row.saLzkId,'demo-001');
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('lazy DIV root with inert layout children does not block first read',async()=>{
    await page.evaluate(()=>{
      const d=vm.$refs.compareDetailDrawer,u=d.$children[0],root=u.$el,panel=u.$refs.drawer;
      const placeholder=document.createElement('div');
      placeholder.append(document.createElement('div'));
      root.replaceWith(placeholder);u.$el=placeholder;u.$refs={};u.rendered=false;
      const show=d.show;
      d.show=function(row){placeholder.replaceWith(root);u.$el=root;u.$refs.drawer=panel;
        u.rendered=true;show.call(this,row);};
    });
    const result=await execute(command('search'));
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('mounted lazy DIV with one visible text placeholder remains read-only',async()=>{
    await page.evaluate(()=>{
      const d=vm.$refs.compareDetailDrawer,u=d.$children[0],root=u.$el,panel=u.$refs.drawer;
      const placeholder=document.createElement('div');
      const child=document.createElement('div');child.textContent='lazy detail placeholder';
      placeholder.append(child);document.body.append(placeholder);
      root.remove();u.$el=placeholder;u.$refs={};u.rendered=false;
      const show=d.show;
      d.show=function(row){placeholder.replaceWith(root);u.$el=root;u.$refs.drawer=panel;
        u.rendered=true;show.call(this,row);};
    });
    const result=await execute(command('search'));
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(result.data.row.saLzkId,'demo-001');
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('mounted lazy DIV with one hidden leftover drawer is revalidated after opening',async()=>{
    await page.evaluate(()=>{
      const d=vm.$refs.compareDetailDrawer,u=d.$children[0],root=u.$el,panel=u.$refs.drawer;
      const placeholder=document.createElement('div');
      const hidden=document.createElement('div');hidden.className='el-drawer';hidden.style.display='none';
      placeholder.append(hidden);document.body.append(placeholder);
      root.remove();u.$el=placeholder;u.$refs={};u.rendered=false;
      const show=d.show;
      d.show=function(row){placeholder.replaceWith(root);u.$el=root;u.$refs.drawer=panel;
        u.rendered=true;show.call(this,row);};
    });
    const result=await execute(command('search'));
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(result.data.row.saLzkId,'demo-001');
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('lazy DIV root with visible controls is not treated as empty',async()=>{
    await page.evaluate(()=>{
      const d=vm.$refs.compareDetailDrawer,u=d.$children[0];
      const placeholder=document.createElement('div');
      placeholder.innerHTML='<div><button>unsaved action</button></div>';
      document.body.append(placeholder);u.$el=placeholder;u.$refs={};u.rendered=false;
    });
    const result=await execute(command('search'));
    assert.match(result.error,/窗口结构不兼容/);
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('stale logically-open but unrendered DIV is reset only for a readonly search',async()=>{
    await page.evaluate(()=>{
      const d=vm.$refs.compareDetailDrawer,u=d.$children[0],root=u.$el,panel=u.$refs.drawer;
      const placeholder=document.createElement('div');
      root.replaceWith(placeholder);u.$el=placeholder;u.$refs={};u.rendered=false;
      d.dialogVisible=true;
      const show=d.show;
      d.show=function(row){placeholder.replaceWith(root);u.$el=root;u.$refs.drawer=panel;
        u.rendered=true;show.call(this,row);};
    });
    const result=await execute(command('search'));
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(result.data.row.saLzkId,'demo-001');
    assert.equal(await page.evaluate(()=>vm.$refs.compareDetailDrawer.dialogVisible),true);
    assert.equal(await page.evaluate(()=>window.closeCalls||0),0);
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('stale unrendered detail never dismisses a visible foreign dialog',async()=>{
    await page.evaluate(()=>{
      const d=vm.$refs.compareDetailDrawer,u=d.$children[0];
      u.$el=document.createElement('div');u.$refs={};u.rendered=false;
      d.dialogVisible=true;
      document.getElementById('drawer-wrapper').style.display='none';
      document.getElementById('status').style.display='block';
    });
    const result=await execute(command('search'));
    assert.match(result.error,/未关闭的编辑/);
    assert.equal(await page.evaluate(()=>vm.$refs.compareDetailDrawer.dialogVisible),true);
    assert.equal(await page.evaluate(()=>window.queryCount||0),0);
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('owned wrapper nested under a component root remains uniquely scoped',async()=>{
    await page.evaluate(()=>{
      const u=vm.$refs.compareDetailDrawer.$children[0],wrapper=u.$el;
      const outer=document.createElement('div');wrapper.replaceWith(outer);outer.append(wrapper);u.$el=outer;
    });
    const read=await execute(command('search'));
    assert.equal(read.ok,true,JSON.stringify(read));
    await page.evaluate(()=>synthetic.saLzkId='demo-002');
    const next=await execute(command('search',{sa_id:'demo-002'}));
    assert.equal(next.ok,true,JSON.stringify(next));
    assert.equal(await page.evaluate(()=>closeCalls),1);
  });
  test('drawer missing private panel ref uses only its own unique panel',async()=>{
    await page.evaluate(()=>{
      delete vm.$refs.compareDetailDrawer.$children[0].$refs.drawer;
      delete claimWindow.$children[0].$refs.drawer;
    });
    const read=await execute(command('search'));assert.equal(read.ok,true,JSON.stringify(read));
    await page.evaluate(()=>synthetic.saLzkId='demo-002');
    const result=await execute(command('search',{sa_id:'demo-002'}));
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(await page.evaluate(()=>closeCalls),1);
  });
  test('lazy drawer state cannot hide an open window or malformed rendered DOM',async()=>{
    for(const active of [true,false]){
      await reset();
      await page.evaluate(active=>{const d=vm.$refs.compareDetailDrawer,u=d.$children[0];
        d.dialogVisible=active;u.rendered=!active;u.$refs={};u.$el=document.createComment('unknown');},active);
      assert.match((await execute(command('search'))).error,/窗口结构不兼容/);
      assert.equal(await page.evaluate(()=>writeCount),0);
    }
  });
  test('missing drawer ref never selects one of several owned panels',async()=>{
    await page.evaluate(()=>{const u=vm.$refs.compareDetailDrawer.$children[0];delete u.$refs.drawer;
      const p=document.createElement('div');p.className='el-drawer';u.$el.append(p);});
    assert.match((await execute(command('search'))).error,/直属面板 2 个/);
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('same-record drawer refresh still fetches changed comparison data',async()=>{
    await execute(command('search'));
    await page.evaluate(()=>testConfig.saDoi='10.example/new');
    const result=await execute(command('search'));
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(result.data.comparison.find(x=>x.label==='DOI').sa,'10.example/new');
    assert.equal(await page.evaluate(()=>queryCount),2);
    assert.equal(await page.evaluate(()=>window.closeCalls||0),0);
  });
  test('different-record drawer waits for closed cleanup before opening new detail',async()=>{
    await execute(command('search'));
    await page.evaluate(()=>{synthetic.saLzkId='demo-002';testConfig.drawerCloseDelay=350;});
    const result=await execute(command('search',{sa_id:'demo-002'}));
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(result.data.row.saLzkId,'demo-002');
    await page.waitForTimeout(400);
    assert.equal(await page.evaluate(()=>vm.$refs.compareDetailDrawer.currentSaLzkId),'demo-002');
    assert.equal(await page.evaluate(()=>closeCalls),1);
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('foreign visible drawer blocks before closing or querying and is preserved',async()=>{
    await execute(command('search'));
    await page.evaluate(()=>{const el=document.createElement('div');el.className='el-drawer';el.id='foreign';
      el.innerHTML='<input value="unsaved">';document.body.append(el);});
    const result=await execute(command('search'));
    assert.match(result.error,/另有 1 个.*抽屉/);
    assert.equal(await page.evaluate(()=>vm.$refs.compareDetailDrawer.dialogVisible),true);
    assert.equal(await page.evaluate(()=>queryCount),1);
    assert.equal(await page.locator('#foreign input').inputValue(),'unsaved');
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('hidden foreign drawer does not block repeated reads',async()=>{
    await execute(command('search'));
    await page.evaluate(()=>{const el=document.createElement('div');el.className='el-drawer';
      el.style.display='none';document.body.append(el);});
    assert.equal((await execute(command('search'))).ok,true);
  });
  test('readonly drawer with ambiguous component mapping stops without closing',async()=>{
    await execute(command('search'));
    await page.evaluate(()=>{const d=vm.$refs.compareDetailDrawer;d.$children.push(d.$children[0]);});
    const result=await execute(command('search'));
    assert.match(result.error,/窗口结构未唯一识别/);
    assert.equal(await page.evaluate(()=>vm.$refs.compareDetailDrawer.dialogVisible),true);
    assert.equal(await page.evaluate(()=>window.closeCalls||0),0);
  });
  test('readonly drawer cannot borrow another wrapper or panel',async()=>{
    await execute(command('search'));
    await page.evaluate(()=>vm.$refs.compareDetailDrawer.$children[0].$refs.drawer=document.getElementById('claim'));
    assert.match((await execute(command('search'))).error,/窗口结构不兼容/);
    assert.equal(await page.evaluate(()=>vm.$refs.compareDetailDrawer.dialogVisible),true);
  });
  test('drawer close timeout reports no write and does not retry the close',async()=>{
    await execute(command('search'));
    await page.evaluate(()=>{synthetic.saLzkId='demo-002';testConfig.drawerCloseDelay=20000;});
    const result=await execute(command('search',{sa_id:'demo-002',expires:Date.now()+4000}));
    assert.match(result.error,/关闭只读详情超时.*本次命令未提交写入/);
    assert.equal(await page.evaluate(()=>closeCalls),1);
    assert.equal(await page.evaluate(()=>queryCount),1);
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('drawer DOM hidden without closed event still blocks next-record query',async()=>{
    await execute(command('search'));
    await page.evaluate(()=>{synthetic.saLzkId='demo-002';vm.$refs.compareDetailDrawer.$children[0].$on=()=>{};});
    const result=await execute(command('search',{sa_id:'demo-002',expires:Date.now()+4000}));
    assert.match(result.error,/关闭只读详情超时/);
    assert.equal(await page.evaluate(()=>queryCount),1);
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('drawer closed event without disappearance does not permit next query',async()=>{
    await execute(command('search'));
    await page.evaluate(()=>{synthetic.saLzkId='demo-002';const ui=vm.$refs.compareDetailDrawer.$children[0];
      ui.$on('closed',()=>ui.$el.style.display='block');});
    const result=await execute(command('search',{sa_id:'demo-002',expires:Date.now()+4000}));
    assert.match(result.error,/关闭只读详情超时/);
    assert.equal(await page.evaluate(()=>queryCount),1);
  });
  test('drawer manual close already in progress is awaited without another close',async()=>{
    await execute(command('search'));
    await page.evaluate(()=>{testConfig.drawerCloseDelay=250;vm.$refs.compareDetailDrawer.dialogVisible=false;});
    const result=await execute(command('search'));
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(await page.evaluate(()=>window.closeCalls||0),0);
    assert.equal(await page.evaluate(()=>vm.$refs.compareDetailDrawer.currentSaLzkId),'demo-001');
  });
  test('drawer reopening during close stops and is not closed again',async()=>{
    await execute(command('search'));
    await page.evaluate(()=>{synthetic.saLzkId='demo-002';testConfig.drawerCloseDelay=500;
      const d=vm.$refs.compareDetailDrawer,u=d.$children[0],original=u.closeDrawer;
      u.closeDrawer=()=>{original();setTimeout(()=>d.dialogVisible=true,60);};});
    assert.match((await execute(command('search',{sa_id:'demo-002'}))).error,/被重新打开/);
    assert.equal(await page.evaluate(()=>closeCalls),1);
    assert.equal(await page.evaluate(()=>queryCount),1);
  });
  test('drawer close never dismisses a newly appearing foreign dialog',async()=>{
    await execute(command('search'));
    await page.evaluate(()=>{synthetic.saLzkId='demo-002';testConfig.drawerCloseDelay=500;
      const u=vm.$refs.compareDetailDrawer.$children[0],original=u.closeDrawer;
      u.closeDrawer=()=>{original();document.getElementById('status').style.display='block';};});
    assert.match((await execute(command('search',{sa_id:'demo-002'}))).error,/未关闭的编辑/);
    assert.equal(await page.locator('#status').isVisible(),true);
    assert.equal(await page.evaluate(()=>queryCount),1);
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('drawer requiring an unknown beforeClose hook is left for human',async()=>{
    await execute(command('search'));
    await page.evaluate(()=>{synthetic.saLzkId='demo-002';vm.$refs.compareDetailDrawer.$children[0].beforeClose=()=>{writeCount++;};});
    assert.match((await execute(command('search',{sa_id:'demo-002'}))).error,/额外关闭确认/);
    assert.equal(await page.evaluate(()=>vm.$refs.compareDetailDrawer.dialogVisible),true);
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('drawer reuse never hides an active metadata editor state',async()=>{
    await execute(command('search'));
    await page.evaluate(()=>vm.$refs.compareDetailDrawer.$refs.itemEdit={drawer:true,unsaved:'keep'});
    assert.match((await execute(command('search'))).error,/元数据编辑窗口尚未关闭/);
    assert.equal(await page.evaluate(()=>vm.$refs.compareDetailDrawer.$refs.itemEdit.unsaved),'keep');
  });
  test('manual claim drawer closing animation is awaited without auto-dismiss',async()=>{
    const read=await execute(command('search'));
    assert.equal((await execute(command('open_claim',{expected:read.data.row}))).ok,true);
    await page.waitForFunction(()=>claimWindow.drawer && !claimWindow.loading);
    await page.evaluate(()=>{testConfig.claimCloseDelay=300;claimWindow.drawer=false;});
    const result=await execute(command('search'));
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(await page.evaluate(()=>window.closeCalls||0),0);
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
  test('claim tolerates its own closing selection-dialog animation',async()=>{
    await page.evaluate(()=>testConfig.peopleCloseDelay=220);
    const prepared=await prepare();
    assert.equal(prepared.ok,true,JSON.stringify(prepared));
    await page.waitForFunction(()=>document.getElementById('people').style.display==='none');
    const result=await submit(prepared);
    assert.equal(result.ok,true,JSON.stringify(result));
    assert.equal(await page.evaluate(()=>writeCount),1);
  });
  test('claim still stops for a foreign modal during its own closing animation',async()=>{
    await page.evaluate(()=>testConfig.peopleCloseDelay=220);
    const prepared=await prepare();
    assert.equal(prepared.ok,true,JSON.stringify(prepared));
    await page.evaluate(()=>testConfig.foreignAfterSelect=true);
    const result=await submit(prepared);
    assert.match(result.error,/网页出现其他操作窗口/);
    assert.equal(await page.evaluate(()=>writeCount),0);
    assert.equal(await page.locator('#status').isVisible(),true);
  });
  test('lookup only returns after the owned selection dialog has closed',async()=>{
    await page.evaluate(()=>testConfig.peopleCloseDelay=250);
    const prepared=await prepare();
    assert.equal(prepared.ok,true,JSON.stringify(prepared));
    assert.equal(await page.locator('#people').isVisible(),false);
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('a closing picker that never disappears times out without submitting',async()=>{
    const prepared=await prepare();
    assert.equal(prepared.ok,true,JSON.stringify(prepared));
    await page.evaluate(()=>testConfig.peopleCloseDelay=20000);
    const result=await submit(prepared,{expires:Date.now()+4200});
    assert.match(result.error,/等待选择作者窗口关闭超时/);
    assert.equal(await page.evaluate(()=>writeCount),0);
  });
  test('unrecognized picker DOM cannot be ignored or auto-closed',async()=>{
    await page.evaluate(()=>people.$el=document.getElementById('status'));
    const result=await prepare();
    assert.match(result.error,/无法唯一识别/);
    assert.equal(await page.evaluate(()=>writeCount),0);
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
    const selected=process.env.SA_TEST_CASE?cases.filter(([name])=>name.includes(process.env.SA_TEST_CASE)):cases;
    assert.ok(selected.length,'No matching test');
    for (const [name, fn] of selected) { await reset(); await fn(); console.log('PASS',name); }
    console.log(`Browser adapter: ${selected.length} offline cases passed. No production requests.`);
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1;});
