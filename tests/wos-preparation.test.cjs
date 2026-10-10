/* Semantic preparation is navigation-only. No real site/account/roster. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require('playwright');
const {runWOSCommand}=require('../extension/wos-adapter.js');
const edge='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const form='<form><select><option>Topic</option><option>Title</option></select><input value="UNCHANGED_QUERY"><button type="button">Search</button></form>';
const cases=[
  {name:'header without mounted controls waits rather than reporting zero',html:'<header>Web of Science</header>',state:'loading'},
  {name:'Smart query is not mistaken for a ready fielded search',html:'<main><input value="UNCHANGED_QUERY"><button>Search</button></main>',state:'loading'},
  {name:'selected Fielded tab waits for hydration without reclicking',html:'<main><button role="tab" aria-selected="true">Fielded Search</button></main>',state:'loading'},
  {name:'busy form waits and never fills or submits',html:'<main aria-busy="true">'+form+'</main>',state:'loading'},
  {name:'unique form is ready but Search is not clicked',html:'<main>'+form+'</main>',state:'ready'},
  {name:'accessible icon-only Title selector retains its exact label',html:'<main><form><button role="combobox" aria-label="Title" type="button"><mat-icon>expand_more</mat-icon></button><input value="UNCHANGED_QUERY"><button type="button">Search</button></form></main>',state:'ready'},
  {name:'navigation search input is not borrowed by the form',html:'<nav><input value="UNCHANGED_QUERY"><button>Search</button></nav><main>'+form+'</main>',state:'ready'},
  {name:'disabled blank-query Search is normal before filling',html:'<main>'+form.replace('<button type="button">','<button type="button" disabled>')+'</main>',state:'ready'},
  {name:'two rows are rejected before filling or Search',html:'<main>'+form+form+'</main>',error:/字段选择器未唯一/},
  {name:'two Search actions in one form are rejected',html:'<main>'+form.replace('</form>','<button type="button">Search</button></form>')+'</main>',error:/检索按钮未唯一/},
  {name:'two query inputs are rejected',html:'<main>'+form.replace('</form>','<input value="UNCHANGED_QUERY"></form>')+'</main>',error:/输入框未唯一/},
  {name:'login gate cannot become a paper zero',html:'<main><input type="password"></main>',error:/登录表单/},
  {name:'challenge cannot become a paper zero',html:'<main id="challenge-form">Human verification</main>',error:/人工验证/},
  {name:'server 524 cannot become a paper zero',html:'<h1>A timeout occurred</h1><h2>Error code 524</h2>',error:/5xx\/连接超时/},
  {name:'server 503 cannot become a missing-input error',html:'<h1>HTTP 503</h1>',error:/5xx\/连接超时/},
  {name:'old zero is a reset request, not this papers outcome',html:'<main>Your search found no results'+form+'</main>',state:'stale_zero'},
];
(async()=>{
  const browser=await chromium.launch({headless:true,...(fs.existsSync(edge)?{executablePath:edge}:{})});
  let checks=0;
  try{
    const context=await browser.newContext();
    await context.route('**/*',route=>route.fulfill({status:200,contentType:'text/html',body:'<body></body>'}));
    const page=await context.newPage();
    for(const origin of ['https://www.webofscience.com','https://webofscience.clarivate.cn']){
      for(const item of cases){
        await page.goto(origin+'/wos/woscc/basic-search');
        await page.evaluate(html=>{
          document.body.innerHTML=html;window.clicks=0;
          for(const button of document.querySelectorAll('button'))button.onclick=()=>window.clicks++;
        },item.html);
        const result=await page.evaluate(runWOSCommand,{action:'wos_prepare_search',title:'Synthetic paper',
          sa_id:'offline-preparation',expires:Date.now()+30000});
        if(item.error){assert.equal(result.ok,false,item.name);assert.match(result.error,item.error,item.name);}
        else {assert.equal(result.ok,true,JSON.stringify(result));assert.equal(result.data.state,item.state,item.name);}
        assert.equal(await page.evaluate(()=>clicks),0,item.name);
        assert.ok((await page.locator('input:not([type=password])').evaluateAll(inputs=>inputs.map(el=>el.value)))
          .every(value=>value==='UNCHANGED_QUERY'),item.name);
        checks++;
      }
      await page.evaluate(()=>{
        document.body.innerHTML='<main><button>Advanced Search<mat-icon>expand_more</mat-icon></button></main>';
        window.clicks=0;document.querySelector('button').onclick=()=>clicks++;
      });
      const command={action:'wos_prepare_search',title:'Synthetic paper',sa_id:'offline-preparation',expires:Date.now()+30000};
      const navigated=await page.evaluate(runWOSCommand,command);
      assert.equal(navigated.data.state,'navigating');
      await page.waitForFunction(()=>clicks===1);
      const again=await page.evaluate(runWOSCommand,{...command,attempted_navigation:[navigated.data.navigation_key]});
      assert.equal(again.data.state,'loading');assert.equal(await page.evaluate(()=>clicks),1);
      checks++;
    }
    console.log(`WOS navigation-only preparation: ${checks} offline checks passed; no query replaced or Search submitted.`);
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
