/* Synthetic browser pages only. No production requests or user browser state. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('playwright');
const {runWOSCommand}=require('../extension/wos-adapter.js');
const {inspectWorkPage}=require('../extension/page-diagnostics.js');
const fixture=fs.readFileSync(path.join(__dirname,'fixtures/wos.html'),'utf8');
const edge='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
(async()=>{
  const browser=await chromium.launch({headless:true,...(fs.existsSync(edge)?{executablePath:edge}:{})});
  const context=await browser.newContext();
  await context.route('**/*',r=>r.fulfill({status:200,contentType:'text/html',body:fixture}));
  const page=await context.newPage();
  const run=()=>page.evaluate(runWOSCommand,{action:'wos_search',sa_id:'synthetic',title:'Synthetic paper',expires:Date.now()+12000});
  const success=async()=>{const result=await run();assert.equal(result.ok,true,JSON.stringify(result));assert.equal(await page.evaluate(()=>searches),1);};
  const stopped=async count=>{const result=await run();assert.equal(result.ok,false);assert.match(result.error,new RegExp('识别到 '+count+' 个'));assert.equal(await page.evaluate(()=>searches),0);};
  const tests=[];const test=(name,fn)=>tests.push([name,fn]);
  test('Chinese search text ignores the material magnifier ligature',async()=>{
    await page.evaluate(()=>document.querySelector('button').innerHTML='<mat-icon>search</mat-icon><span>检索</span>');await success();
  });
  test('visible search text is not overwritten by a verbose aria label',async()=>{
    await page.evaluate(()=>{const b=document.querySelector('button');b.setAttribute('aria-label','Submit document query');b.innerHTML='<span aria-hidden="true">search</span><span>搜索</span>';});await success();
  });
  test('uppercase English and hidden SVG titles do not corrupt the label',async()=>{
    await page.evaluate(()=>document.querySelector('button').innerHTML='<svg><title>Magnifier</title></svg><span>SEARCH</span><span style="display:none">Hidden tooltip</span>');await success();
  });
  test('search action can be a sibling outside the input form',async()=>{
    await page.evaluate(()=>{const form=document.createElement('form');const main=document.getElementById('main');main.prepend(form);form.append(document.querySelector('select'),document.querySelector('input'));});await success();
  });
  test('an explicitly form-associated action may be outside the main panel',async()=>{
    await page.evaluate(()=>{const main=document.getElementById('main'),form=document.createElement('form'),b=document.querySelector('button');form.id='query-form';main.prepend(form);form.append(document.querySelector('select'),document.querySelector('input'));document.body.append(b);b.setAttribute('form',form.id);b.type='button';});await success();
  });
  test('navigation Search is never confused with the query action',async()=>{
    await page.evaluate(()=>{const nav=document.createElement('nav');nav.innerHTML='<a href="#">Search</a><button>Search</button>';nav.onclick=()=>window.navClicks++;window.navClicks=0;document.body.prepend(nav);});await success();assert.equal(await page.evaluate(()=>navClicks),0);
  });
  test('nested role-button wrappers represent one action',async()=>{
    await page.evaluate(()=>{const b=document.querySelector('button'),wrap=document.createElement('div');wrap.setAttribute('role','button');b.before(wrap);wrap.append(b);});await success();
  });
  test('an icon-only button needs an exact accessible search name',async()=>{
    await page.evaluate(()=>{const b=document.querySelector('button');b.innerHTML='<mat-icon>search</mat-icon>';b.setAttribute('aria-label','Search documents');});await success();
  });
  test('input submit buttons and associated labels are supported',async()=>{
    await page.evaluate(()=>{const old=document.querySelector('button'),b=document.createElement('input');b.type='submit';b.value='检索';b.onclick=old.onclick;old.replaceWith(b);});await success();
  });
  test('aria-labelledby can name an icon-only search action',async()=>{
    await page.evaluate(()=>{const b=document.querySelector('button'),label=document.createElement('span');label.id='search-name';label.hidden=true;label.textContent='检索';document.body.append(label);b.innerHTML='<mat-icon>search</mat-icon>';b.removeAttribute('aria-label');b.setAttribute('aria-labelledby',label.id);});await success();
  });
  test('a conflicting aria-label never turns Clear into a search action',async()=>{
    await page.evaluate(()=>{const b=document.querySelector('button');b.textContent='Clear';b.setAttribute('aria-label','Search');});await stopped(0);
  });
  test('internal and external actions associated with one form remain ambiguous',async()=>{
    await page.evaluate(()=>{const input=document.querySelector('input'),form=input.form,b=document.querySelector('button');form.append(b);b.after(b.cloneNode(true));const external=form.querySelectorAll('button')[1];document.body.append(external);external.setAttribute('form',form.id);});await stopped(2);
  });
  test('two real query actions stop instead of picking the first',async()=>{
    await page.evaluate(()=>{const b=document.querySelector('button');b.after(b.cloneNode(true));});await stopped(2);
  });
  test('missing query action does not fall back to navigation',async()=>{
    await page.evaluate(()=>{document.querySelector('button').remove();const nav=document.createElement('nav');nav.innerHTML='<a href="#">Search</a><button>Search</button>';window.navClicks=0;nav.onclick=()=>navClicks++;document.body.prepend(nav);});await stopped(0);assert.equal(await page.evaluate(()=>navClicks),0);
  });
  test('similarly named actions such as Search history are rejected',async()=>{
    await page.evaluate(()=>document.querySelector('button').textContent='Search history');await stopped(0);
  });
  test('buttons owned by another form are not borrowed',async()=>{
    await page.evaluate(()=>{const main=document.getElementById('main'),query=document.createElement('form'),other=document.createElement('form'),b=document.querySelector('button');main.prepend(query);query.append(document.querySelector('select'),document.querySelector('input'));main.append(other);other.append(b);});await stopped(0);
  });
  test('hidden duplicate actions do not create ambiguity',async()=>{
    await page.evaluate(()=>{const b=document.querySelector('button'),hidden=b.cloneNode(true),wrapper=document.createElement('div');wrapper.style.display='none';wrapper.append(hidden);document.getElementById('main').append(wrapper);});await success();
  });
  test('aria-hidden duplicate actions are not treated as active controls',async()=>{
    await page.evaluate(()=>{const b=document.querySelector('button'),wrapper=document.createElement('div');wrapper.setAttribute('aria-hidden','true');wrapper.append(b.cloneNode(true));document.getElementById('main').append(wrapper);});await success();
  });
  test('disabled search button is not force-enabled or clicked',async()=>{
    await page.evaluate(()=>document.querySelector('button').disabled=true);const result=await run();assert.match(result.error,/控件尚不可用/);assert.equal(await page.evaluate(()=>searches),0);
  });
  test('button replacement during input is re-resolved before click',async()=>{
    await page.evaluate(()=>document.querySelector('input').addEventListener('input',()=>{const old=document.querySelector('button'),b=old.cloneNode(true);b.innerHTML='<mat-icon>search</mat-icon>检索';b.onclick=old.onclick;old.replaceWith(b);}));await success();
  });
  test('readonly button diagnostics redact arbitrary labels, values, URLs and input text',async()=>{
    await page.evaluate(()=>{document.querySelector('input').value='PRIVATE_QUERY';document.querySelector('button').innerHTML='<mat-icon>search</mat-icon>检索';const b=document.createElement('button');b.setAttribute('aria-label','Search PRIVATE_PERSON');b.textContent='PRIVATE_ACCOUNT';document.body.append(b);});
    const d=await page.evaluate(inspectWorkPage);assert.ok(d.buttons.some(b=>b.display==='检索'&&b.icon_count===1));
    assert.ok(!JSON.stringify(d).includes('PRIVATE_'));assert.equal(await page.evaluate(()=>searches),0);
  });
  try {
    for(const origin of ['https://webofscience.clarivate.cn','https://www.webofscience.com']) {
      for(const [name,fn] of tests){await page.goto(origin+'/wos/woscc/basic-search');await fn();console.log('PASS '+new URL(origin).hostname+' '+name);}
    }
    console.log(`WOS search controls: ${tests.length*2} offline cases passed.`);
  } finally {await context.close();await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
