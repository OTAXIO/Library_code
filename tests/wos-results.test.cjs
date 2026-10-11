/* DOM compatibility regressions. All URLs are fulfilled locally; no user tab,
 * production WOS session, roster, or credentials are accessed. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('playwright');
const {runWOSCommand}=require('../extension/wos-adapter.js');
const {inspectWorkPage}=require('../extension/page-diagnostics.js');
const fixture=fs.readFileSync(path.join(__dirname,'fixtures/wos.html'),'utf8');
const edge='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const record='/wos/woscc/full-record/WOS:000123456789012';
const second='/wos/woscc/full-record/WOS:000123456789013';
const link=(attrs='',target=record)=>`<a href="${target}" ${attrs}><span>PRIVATE_TITLE</span></a>`;
const cases=[
  {name:'screenshot English found-no-results banner on basic search with a stale record link',
    route:'/wos/woscc/basic-search',html:`<section role="alert"><b>Your search found no results</b><p>Check the spelling and/or broaden your search parameters</p></section>${link()}`,
    state:'zero',total:null},
  {name:'English found-no-results banner split over elements',
    route:'/wos/woscc/basic-search',html:'<section role="alert"><span>Your </span><span>search </span><span>found </span><span>no </span><span>results</span></section>',state:'zero',total:null},
  {name:'inline count and unit without a text-space',html:`<div role="tab"><b>1</b><span>Documents</span></div>${link()}`,state:'single',total:1},
  {name:'English document tab',html:`<div role="tab">1 Documents</div>${link()}`,state:'single',total:1},
  {name:'Chinese count split over inline nodes',html:`<h2><span>1</span><span>篇</span><span>文献</span></h2>${link()}`,state:'single',total:1},
  {name:'count split over block nodes',html:`<div role="tab"><div>1</div><div>Documents</div></div>${link()}`,state:'single',total:1},
  {name:'a display contents document tab still provides total evidence',html:`<div role="tab" style="display:contents"><b>1</b><span>Documents</span></div>${link()}`,state:'single',total:1},
  {name:'plain result count container with adjacent spans',html:`<div><b>1</b><span>Documents</span></div>${link()}`,state:'single',total:1},
  {name:'count in a title is not the results total',html:`<article><h2>A study of 1 Documents</h2>${link()}</article>`,state:'loading',total:null},
  {name:'even an exact count-like paper title is not a total',html:`<article>${link().replace('PRIVATE_TITLE','1Documents')}</article>`,state:'loading',total:null},
  {name:'display contents title wrapper',html:`<h1>1 Documents</h1>${link('style="display:contents"')}`,state:'single',total:1,contents:1},
  {name:'encoded colon and trailing slash deduplicate one record',html:`<h1>1 Documents</h1>${link()}${link('',record.replace('WOS:','WOS%3A')+'/')}`,state:'single',total:1},
  {name:'explicit routerLink target without href',html:`<h1>1 Documents</h1><a routerLink="${record}"><span>PRIVATE_TITLE</span></a>`,state:'single',total:1,router:1},
  {name:'reflected router link with a rendered child',html:`<h1>1 Documents</h1><a ng-reflect-router-link="${record}" style="display:contents"><span>PRIVATE_TITLE</span></a>`,state:'single',total:1,contents:1,router:1},
  {name:'hidden recommendation does not add a second result',html:`<h1>1 Documents</h1>${link()}<section hidden>${link('',second)}</section>`,state:'single',total:1},
  {name:'aria-hidden recommendation is not an active result',html:`<h1>1 Documents</h1>${link()}<section aria-hidden="true">${link('',second)}</section>`,state:'single',total:1},
  {name:'CSS-hidden parent does not contribute a route',html:`<h1>1 Documents</h1>${link()}<section style="display:none">${link('style="display:contents"',second)}</section>`,state:'single',total:1},
  {name:'visible references are not a results total',html:`<div role="tab">1 Documents</div>${link()}<h3>65 References</h3><h3>5 Citations</h3>`,state:'single',total:1},
  {name:'multiple total is never narrowed to the rendered page',html:`<h1>2 Documents</h1>${link()}`,state:'multiple',total:2},
  {name:'thousands separator is a total, not one document',html:`<div role="tab">1,001 Documents</div>${link()}`,state:'multiple',total:1001},
  {name:'two distinct canonical routes remain ambiguous',html:`<h1>1 Documents</h1>${link()}${link('',second)}`,state:'multiple',total:1},
  {name:'conflicting result totals do not approve a result',html:`<h1>1 Documents</h1><div role="tab">2 Documents</div>${link()}`,state:'multiple',total:null},
  {name:'conflicting targets on one control do not pick href first',html:`<h1>1 Documents</h1>${link(`routerLink="${second}"`)}`,state:'multiple',total:1},
  {name:'unknown total stays pending with diagnostics',html:`<h1>Search results</h1>${link()}`,state:'loading',total:null},
  {name:'one total with no canonical target stays pending',html:'<h1>1 Documents</h1><a role="link">PRIVATE_TITLE</a>',state:'loading',total:1},
  {name:'publisher full text is not a WOS record',html:`<h1>1 Documents</h1>${link('','https://publisher.example.invalid'+record)}`,state:'loading',total:1},
  {name:'URL credentials are not accepted as a record target',html:`<h1>1 Documents</h1>${link('','https://user:PRIVATE_KEY@HOST'+record)}`,state:'loading',total:1},
  {name:'encoded route separators remain rejected',html:`<h1>1 Documents</h1>${link('',record.replaceAll('/','%2F'))}`,state:'loading',total:1},
  {name:'short accession numbers remain rejected',html:`<h1>1 Documents</h1>${link('',record.slice(0,-1))}`,state:'loading',total:1},
  {name:'a loading indicator delays navigation',html:`<h1>1 Documents</h1><span role="progressbar">Loading</span>${link()}`,state:'loading',total:1},
  {name:'live Search spinner prevents accepting a still-mounted result',
    html:`<h1>1 Documents</h1>${link()}<button data-ta="run-search" disabled><mat-icon aria-hidden="true" class="svg-spinner" data-mat-icon-name="circle-notch">loading</mat-icon></button>`,state:'loading',total:1},
  {name:'old zero banner while Search spins is pending, not a completed zero',route:'/wos/woscc/basic-search',
    html:'Your search found no results<button data-ta="run-search" disabled><mat-icon aria-hidden="true" class="svg-spinner" svgicon="circle-notch">loading</mat-icon></button>',state:'loading',total:null},
  {name:'zero document total on the summary is a completed empty result',html:'<div role="tab"><b>0</b><span>Documents</span></div>',state:'zero',total:0},
];
(async()=>{
  const browser=await chromium.launch({headless:true,...(fs.existsSync(edge)?{executablePath:edge}:{})});
  const context=await browser.newContext();
  await context.route('**/*',route=>route.fulfill({status:200,contentType:'text/html',body:fixture}));
  const page=await context.newPage();
  try{
    for(const origin of ['https://webofscience.clarivate.cn','https://www.webofscience.com']){
      for(const test of cases){
        await page.goto(origin+(test.route||'/wos/woscc/summary/offline'));
        await page.evaluate(html=>document.getElementById('main').innerHTML=html,test.html.replace('HOST',new URL(origin).hostname));
        const url=page.url();
        const result=await page.evaluate(runWOSCommand,{action:'wos_read_results',sa_id:'offline',title:'PRIVATE_QUERY',expires:Date.now()+30000});
        assert.equal(result.ok,true,`${test.name}: ${JSON.stringify(result)}`);
        assert.equal(result.data.state,test.state,`${test.name}: ${JSON.stringify(result)}`);
        assert.equal(result.data.diagnostic.result_total,test.total,test.name);
        if(test.state==='single')assert.equal(new URL(result.data.navigate_url).pathname.replace(/\/$/,''),record);
        assert.equal(page.url(),url,'the probe must never click or navigate');
        const diagnostic=await page.evaluate(inspectWorkPage);
        assert.equal(diagnostic.result_total,test.total,test.name);
        assert.equal(diagnostic.canonical_record_link_count,result.data.diagnostic.canonical_record_link_count,test.name);
        const shared=await page.evaluate(inspectWorkPage,{action:'wos_read_results',title:'PRIVATE_QUERY',expires:Date.now()+30000});
        assert.equal(shared.ok,true,test.name);
        assert.equal(shared.data.state,test.state,test.name);
        assert.equal(shared.data.diagnostic.result_total,diagnostic.result_total,test.name);
        assert.equal(shared.data.diagnostic.canonical_record_link_count,diagnostic.canonical_record_link_count,test.name);
        if(test.state==='single')assert.equal(new URL(shared.data.navigate_url).pathname,record);
        assert.ok(!JSON.stringify(shared.data.diagnostic).includes('PRIVATE_'));
        if(test.contents)assert.equal(diagnostic.contents_record_link_count,test.contents,test.name);
        if(test.router)assert.equal(diagnostic.router_record_link_count,test.router,test.name);
        assert.ok(!JSON.stringify(result.data.diagnostic).includes('PRIVATE_'));
        assert.ok(!JSON.stringify(diagnostic).includes('PRIVATE_'));
        console.log(`PASS ${new URL(origin).hostname} ${test.name}`);
      }
      await page.goto(origin+record);
      await page.evaluate(()=>document.getElementById('main').innerHTML='<p>Loading</p>');
      const cmd={action:'wos_read_results',sa_id:'offline',title:'Synthetic',expires:Date.now()+30000};
      assert.equal((await page.evaluate(runWOSCommand,cmd)).data.state,'loading','a record URL alone is not a mounted record');
      assert.equal((await page.evaluate(inspectWorkPage,cmd)).data.state,'loading');
      await page.evaluate(()=>document.getElementById('main').innerHTML='<h1>PRIVATE_TITLE</h1><button><mat-icon>download</mat-icon>Export<mat-icon>expand_more</mat-icon></button>');
      assert.equal((await page.evaluate(runWOSCommand,cmd)).data.state,'record','decorative export icons do not hide record readiness');
      assert.equal((await page.evaluate(inspectWorkPage,cmd)).data.state,'record');
      console.log(`PASS ${new URL(origin).hostname} record DOM readiness and decorated export`);
    }
    console.log(`WOS result DOM: ${cases.length*2+4} offline cases passed.`);
  }finally{await context.close();await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
