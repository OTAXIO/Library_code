/* Only isolated local fixtures; never attaches to a user's browser profile. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('playwright');
const {runWOSCommand}=require('../extension/wos-adapter.js');
const fixture=fs.readFileSync(path.join(__dirname,'fixtures/wos.html'),'utf8');
const edge='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const record='/wos/woscc/full-record/WOS:000123456789012';
const other=record.replace('789012','789013');
const anchor=(attrs='',url=record)=>`<a ${attrs} href="${url}">Synthetic paper</a>`;
const faults=[
  ['multiple results',`<h1>2 Documents</h1>${anchor()}`],
  ['unknown total',`<h1>Search results</h1>${anchor()}`],
  ['two record targets',`<h1>1 Documents</h1>${anchor()}${anchor('',other)}`],
  ['changed unique record',`<h1>1 Documents</h1>${anchor('',other)}`],
  ['conflicting totals',`<h1>1 Documents</h1><h2>2 Documents</h2>${anchor()}`],
  ['loading',`<h1>1 Documents</h1><div role="progressbar">Loading</div>${anchor()}`],
  ['confirmed zero',`<p>Your search found no results</p>${anchor()}`],
  ['foreign dialog',`<h1>1 Documents</h1>${anchor()}<section role="dialog">Other action</section>`],
  ['new tab forbidden',`<h1>1 Documents</h1>${anchor('target="_blank"')}`],
  ['download forbidden',`<h1>1 Documents</h1>${anchor('download')}`],
  ['disabled',`<h1>1 Documents</h1>${anchor('aria-disabled="true"')}`],
  ['hidden',`<h1>1 Documents</h1>${anchor('hidden')}`],
  ['publisher link',`<h1>1 Documents</h1>${anchor('','https://publisher.example.invalid'+record)}`],
];
(async()=>{
  const browser=await chromium.launch({headless:true,...(fs.existsSync(edge)?{executablePath:edge}:{})});
  const context=await browser.newContext();
  await context.route('**/*',route=>route.fulfill({status:200,contentType:'text/html',body:fixture}));
  const page=await context.newPage();let checks=0;
  try{
    for(const origin of ['https://www.webofscience.com','https://webofscience.clarivate.cn']){
      const command={action:'wos_open_result',sa_id:'offline',title:'Synthetic paper',
        navigate_url:origin+record,expires:Date.now()+30000};
      await page.goto(origin+'/wos/woscc/summary/offline');
      await page.evaluate(html=>{
        document.getElementById('main').innerHTML=html;window.opensMade=0;window.fixtureNonce='kept';
        const original=Element.prototype.getAttribute;
        Element.prototype.getAttribute=function(name){return name==='href'?null:original.call(this,name);};
        document.querySelector('a').onclick=event=>{
          event.preventDefault();window.opensMade++;history.pushState({},'',event.currentTarget.href);
          document.getElementById('main').innerHTML='<h1>Synthetic paper</h1><button>Export</button>';
        };
      },`<h1>1 Documents</h1>${anchor()}`);
      const opened=await page.evaluate(runWOSCommand,{...command,expires:Date.now()+30000});
      assert.equal(opened.ok,true,JSON.stringify(opened));assert.equal(opened.data.submitted,true);
      await page.waitForURL(origin+record);
      assert.equal(await page.evaluate(()=>window.fixtureNonce),'kept','the site SPA must not be reloaded');
      assert.equal(await page.evaluate(()=>window.opensMade),1);
      assert.equal((await page.evaluate(runWOSCommand,{...command,expires:Date.now()+30000})).ok,false,
        'a second click on an already mounted record is forbidden');
      assert.equal(await page.evaluate(()=>window.opensMade),1);checks+=3;
      for(const [name,html] of faults){
        await page.goto(origin+'/wos/woscc/summary/offline?case='+checks);
        await page.evaluate(html=>{
          document.getElementById('main').innerHTML=html;window.opensMade=0;
          for(const a of document.querySelectorAll('a'))a.onclick=event=>{event.preventDefault();window.opensMade++;};
        },html);
        const before=page.url(),result=await page.evaluate(runWOSCommand,{...command,expires:Date.now()+30000});
        assert.equal(result.ok,false,name);assert.equal(await page.evaluate(()=>window.opensMade),0,name);
        assert.equal(page.url(),before,name);checks++;
      }
      for(const url of [origin+record+'?altered=1',origin+record+'#altered',
          'https://publisher.example.invalid'+record]){
        await page.goto(origin+'/wos/woscc/summary/offline?case='+checks);
        await page.evaluate(html=>document.getElementById('main').innerHTML=html,`<h1>1 Documents</h1>${anchor()}`);
        assert.equal((await page.evaluate(runWOSCommand,{...command,navigate_url:url,expires:Date.now()+30000})).ok,false);checks++;
      }
      console.log(`PASS ${origin}: own-link navigation and all fail-closed guards`);
    }
    console.log(`WOS own-link navigation: ${checks} isolated checks passed.`);
  }finally{await context.close();await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
