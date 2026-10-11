/* Startup-readiness probes only: all browser traffic is intercepted locally. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require('playwright');
const {inspectWorkPage}=require('../extension/page-diagnostics.js');
const edge='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const cases=[
  {name:'header-only page is unavailable, never a paper zero result',
    html:'<header>Web of Science</header><main></main>',inputs:0,login:false,dialogs:0,zero:false,error:false},
  {name:'rendered title input is ready without exposing the private query',
    html:'<main><select><option>Title</option></select><input type="text" value="PRIVATE_QUERY"></main>',
    inputs:1,login:false,dialogs:0,zero:false,error:false},
  {name:'navigation and hidden inputs do not make a search form ready',
    html:'<nav><input type="search" value="PRIVATE_QUERY"></nav><main><input hidden><input style="display:none"></main>',
    inputs:0,login:false,dialogs:0,zero:false,error:false},
  {name:'live disabled Search with decorative circle-notch is an in-flight query',
    html:'<main><input><button data-ta="run-search" disabled><mat-icon aria-hidden="true" class="svg-spinner" data-mat-icon-name="circle-notch">loading</mat-icon></button></main>',
    inputs:1,login:false,dialogs:0,zero:false,error:false,busy:true},
  {name:'disabled Search without the specific spinner is not a running query',
    html:'<main><input><button data-ta="run-search" disabled>Search</button></main>',
    inputs:1,login:false,dialogs:0,zero:false,error:false},
  {name:'hidden pending Search is not current page busy evidence',
    html:'<main><input></main><div hidden><button data-ta="run-search" disabled><mat-icon class="svg-spinner" svgicon="circle-notch"></mat-icon></button></div>',
    inputs:1,login:false,dialogs:0,zero:false,error:false},
  {name:'a stale zero banner during the exact pending Search is not a completed zero',
    html:'<main>Your search found no results<input><button data-ta="run-search" disabled><mat-icon aria-hidden="true" class="svg-spinner" svgicon="circle-notch">loading</mat-icon></button></main>',
    inputs:1,login:false,dialogs:0,zero:false,error:false,busy:true},
  {name:'visible login gate is distinct from initialization and does not expose a password',
    html:'<main><input type="password" value="PRIVATE_SECRET"></main>',inputs:0,login:true,dialogs:0,zero:false,error:false},
  {name:'passive captcha badge does not stop a healthy WOS page',
    html:'<main><input></main><div class="grecaptcha-badge"><iframe src="https://example.test/recaptcha/anchor" height="78"></iframe></div>',
    inputs:1,login:false,dialogs:0,zero:false,error:false},
  {name:'visible captcha challenge requires human verification',
    html:'<iframe src="https://example.test/recaptcha/bframe" height="200"></iframe>',
    inputs:0,login:true,dialogs:0,zero:false,error:false},
  {name:'collapsed background captcha frame is not a visible challenge',
    html:'<main><input></main><iframe src="https://example.test/captcha" height="0" style="height:0;border:0"></iframe>',
    inputs:1,login:false,dialogs:0,zero:false,error:false},
  {name:'site Oops error is distinct from paper zero results',
    html:'<main>Oops, something went wrong!</main>',inputs:0,login:false,dialogs:0,zero:false,error:true},
  {name:'only visible active dialogs require human handling',
    html:'<main><input><div role="dialog">PRIVATE_DIALOG</div><div role="dialog" aria-hidden="true">Hidden</div></main>',
    inputs:1,login:false,dialogs:1,zero:false,error:false},
  {name:'a previous Chinese zero-result banner does not hide a healthy input',
    html:'<main><p>您的检索未找到结果</p><input type="text" value="PRIVATE_QUERY"></main>',
    inputs:1,login:false,dialogs:0,zero:true,error:false},
  {name:'Cloudflare 524 page is a site timeout, never a paper zero result',
    html:'<h1>A timeout occurred</h1><h2>Error code 524</h2>',
    inputs:0,login:false,dialogs:0,zero:false,error:false,timeout:true},
  {name:'HTTP 503 page is a site timeout rather than a missing paper',
    html:'<h1>HTTP 503</h1>',inputs:0,login:false,dialogs:0,zero:false,error:false,timeout:true},
  {name:'a sentence in paper content is not a server error heading',
    html:'<main><input><p>A timeout occurred during the experiment. Connection timed out.</p></main>',
    inputs:1,login:false,dialogs:0,zero:false,error:false,timeout:false},
];
(async()=>{
  const browser=await chromium.launch({headless:true,...(fs.existsSync(edge)?{executablePath:edge}:{})});
  try{
    const context=await browser.newContext();
    await context.route('**/*',route=>route.fulfill({status:200,contentType:'text/html',body:'<body></body>'}));
    const page=await context.newPage();
    for(const origin of ['https://www.webofscience.com','https://webofscience.clarivate.cn']){
      for(const item of cases){
        await page.goto(origin+'/wos/woscc/basic-search');
        await page.evaluate(html=>document.body.innerHTML=html,item.html);
        const diagnostic=await page.evaluate(inspectWorkPage,{action:'wos_diagnose'});
        assert.equal(diagnostic.core_search_route,true,item.name);
        assert.equal(diagnostic.query_input_count,item.inputs,item.name);
        assert.equal(diagnostic.login_required,item.login,item.name);
        assert.equal(diagnostic.dialog_count,item.dialogs,item.name);
        assert.equal(diagnostic.zero_result,item.zero,item.name);
        assert.equal(diagnostic.busy,Boolean(item.busy),item.name);
        assert.equal(diagnostic.wos_error,item.error,item.name);
        assert.equal(diagnostic.site_timeout,Boolean(item.timeout),item.name);
        if(item.timeout){
          const probe=await page.evaluate(inspectWorkPage,{action:'wos_read_results',expires:Date.now()+30000});
          assert.equal(probe.ok,false,item.name);
          assert.match(probe.error,/不是文献零结果/,item.name);
        }
        assert.equal(JSON.stringify(diagnostic).includes('PRIVATE_'),false,item.name);
        assert.equal(page.url(),origin+'/wos/woscc/basic-search');
        console.log(`PASS ${new URL(origin).hostname} ${item.name}`);
      }
    }
    console.log(`WOS startup readiness: ${cases.length*2} offline cases passed.`);
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
