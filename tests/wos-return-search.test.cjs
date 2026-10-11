/* Existing-page semantic navigation only. All traffic is a local fixture. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require('playwright');
const {runWOSCommand}=require('../extension/wos-adapter.js');
const edge='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const record='/wos/woscc/full-record/WOS:000123456789012';
const navigation='<nav><a href="/wos/woscc/advanced-search">Advanced Search</a></nav>';
const faults=[
  {name:'foreign dialog is preserved',html:navigation+'<section role="dialog">Export</section>',error:/有弹窗/},
  {name:'two navigation actions stay ambiguous',html:navigation+navigation,error:/未唯一/},
  {name:'paper content is not a navigation action',html:'<article><a href="/wos/woscc/basic-search">Advanced Search</a></article>',error:/未唯一/},
  {name:'hidden navigation is not used',html:'<div hidden>'+navigation+'</div>',error:/未唯一/},
  {name:'new-tab navigation is not followed',html:navigation.replace('<a ','<a target="_blank" '),error:/当前工作页/},
  {name:'foreign-origin link is not followed',html:navigation.replace('/wos/woscc/advanced-search','https://foreign.example/wos/woscc/basic-search'),error:/当前 WOS/},
  {name:'disabled navigation is not force enabled',html:'<nav><button disabled>Advanced Search</button></nav>',error:/控件尚不可用/},
  {name:'unrelated core route cannot return on assumed authority',route:'/wos/woscc/saved-searches',html:navigation,error:/当前不是/},
  {name:'author search is not an authorized document return',route:'/wos/author/author-search',html:navigation,error:/核心合集/},
];
(async()=>{
  const browser=await chromium.launch({headless:true,...(fs.existsSync(edge)?{executablePath:edge}:{})});
  let checks=0;
  try{
    const context=await browser.newContext();
    await context.route('**/*',r=>r.fulfill({status:200,contentType:'text/html',body:'<main></main>'}));
    const page=await context.newPage();
    for(const origin of ['https://www.webofscience.com','https://webofscience.clarivate.cn']){
      for(const route of [record,'/wos/woscc/summary/previous']){
        await page.goto(origin+route);
        await page.evaluate(html=>{
          document.body.innerHTML=html;window.navClicks=0;window.ownedState='preserved';
          document.querySelector('a').onclick=event=>{event.preventDefault();navClicks++;history.pushState({},'',event.currentTarget.href);};
        },navigation);
        const result=await page.evaluate(runWOSCommand,{action:'wos_return_search',title:'Synthetic paper',sa_id:'offline',expires:Date.now()+30000});
        assert.equal(result.ok,true,JSON.stringify(result));assert.equal(result.data.click_observed,true);
        assert.equal(result.data.search_navigation_protocol,1);assert.equal(await page.evaluate(()=>navClicks),1);
        assert.equal(await page.evaluate(()=>ownedState),'preserved');assert.equal(page.url(),origin+'/wos/woscc/advanced-search');
        checks++;
      }
      for(const item of faults){
        await page.goto(origin+(item.route||record));
        await page.evaluate(html=>{document.body.innerHTML=html;window.navClicks=0;
          for(const el of document.querySelectorAll('a,button'))el.onclick=event=>{event.preventDefault();navClicks++;};},item.html);
        const result=await page.evaluate(runWOSCommand,{action:'wos_return_search',title:'Synthetic paper',sa_id:'offline',expires:Date.now()+30000});
        assert.equal(result.ok,false,item.name);assert.match(result.error,item.error,item.name);
        assert.equal(await page.evaluate(()=>navClicks),0,item.name);assert.equal(page.url(),origin+(item.route||record));checks++;
      }
    }
    console.log(`WOS return navigation: ${checks} offline checks passed; no Search or export submitted.`);
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
