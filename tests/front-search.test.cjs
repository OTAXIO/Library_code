/* Frontend lookup on a synthetic simpleSearch component: read-only, event-proven. */
const assert=require('node:assert/strict');
const {test,before,after}=require('node:test');const {chromium}=require('playwright');
const {runFrontCommand}=require('../extension/front-adapter.js');let browser,page;
// Mirrors the mounted parts used: .article-box list vm, its simpleSearch parent
// (getItemData/form/page/loading), the shared $EventBus and $store.state.
const fixture=`<!doctype html><div class="article-box"></div><script>
const listeners={};
const bus={$on:(n,f)=>(listeners[n]=listeners[n]||[]).push(f),
  $off:(n,f)=>listeners[n]=(listeners[n]||[]).filter(x=>x!==f),
  $emit:(n,...a)=>(listeners[n]||[]).slice().forEach(f=>f(...a))};
window.library={};window.failSearch=false;window.searches=[];window.writes=0;
const parent={loading:false,form:{field:'TKA',text:''},page:{current:1,size:10},getItemData(){},
  $options:{name:'simpleSearch'}};
const store={state:{token:'synthetic',mainId:'1244586319225556993'}};
bus.$on('handleSearch',t=>{
  parent.form={...parent.form,field:t.field,text:t.text};parent.loading=true;searches.push({...t,size:parent.page.size});
  setTimeout(()=>{
    parent.loading=false;if(failSearch)return;
    const records=(library[t.field+'|'+t.text]||[]);
    bus.$emit('searchItemList',{page:{records:records.slice(0,parent.page.size),total:records.length}},{},
      {field:t.field,text:t.text,institutionId:store.state.mainId,query:{...parent.page}});
  },30);
});
document.querySelector('.article-box').__vue__={$parent:parent,$EventBus:bus,$store:store,itemList:[]};
</script>`;
before(async()=>{browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
 const context=await browser.newContext();
 await context.route('**/*',r=>r.fulfill({status:200,contentType:'text/html',body:fixture}));page=await context.newPage();});
after(async()=>{await browser?.close();});
const record=(id,meta)=>({id,modelName:'期刊论文',isDeleted:0,metadata:meta});
async function search(command,setup=()=>{},url='http://www.ir.lib.sjtu.edu.cn/simpleSearch'){
  await page.goto(url);
  if(typeof setup==='function')await page.evaluate(setup);
  else await page.evaluate(data=>Object.assign(library,data),setup);
  return page.evaluate(runFrontCommand,{action:'front_search',expires:Date.now()+15000,...command});
}
test('a hit returns plain title/DOI/WOS and the platform ID from the response event',async()=>{
  const r=await search({field:'doi',text:'10.1109/x.1'},{'doi|10.1109/x.1':[record('2112369564765831170',{
    title:["Multimodal <span class='highlight'>Palpation</span> &amp; Size"],
    doi:["<span class='highlight'>10.1109</span>/<span class='highlight'>x.1</span>"],wosId:['WOS:000338338500004']})]});
  assert.equal(r.ok,true,JSON.stringify(r));
  assert.deepEqual(r.data.items,[{id:'2112369564765831170',title:'Multimodal Palpation & Size',doi:'10.1109/x.1',
    wos:'WOS:000338338500004',model:'期刊论文',deleted:false}]);
  assert.equal(r.data.total,1);assert.equal(r.data.complete,true);assert.equal(r.data.front_search_protocol,1);
  assert.equal((await page.evaluate(()=>searches))[0].size,50);
  assert.equal(await page.evaluate(()=>writes),0);
});
test('zero results are reported only from a completed search event',async()=>{
  const r=await search({field:'title',text:'"No such paper"'});
  assert.equal(r.ok,true);assert.equal(r.data.total,0);assert.deepEqual(r.data.items,[]);
});
test('a failed request (no event) times out instead of looking like zero results',async()=>{
  await page.goto('http://www.ir.lib.sjtu.edu.cn/simpleSearch');await page.evaluate(()=>{failSearch=true;});
  const r=await page.evaluate(runFrontCommand,{action:'front_search',field:'doi',text:'10.1/x',expires:Date.now()+6500});
  assert.equal(r.ok,false);assert.match(r.error,/^【已暂停】.*超时/);
});
test('truncation, anonymous sessions, other pages and odd inputs are refused',async()=>{
  const many=await search({field:'title',text:'"Common"'},{'title|"Common"':
    Array.from({length:60},(_,i)=>record(String(1000+i),{title:['Common']}))});
  assert.equal(many.ok,true);assert.equal(many.data.complete,false);assert.equal(many.data.items.length,50);
  const anonymous=await search({field:'doi',text:'10.1/x'},()=>{document.querySelector('.article-box').__vue__.$store.state.token='';});
  assert.match(anonymous.error,/未登录/);
  assert.match((await search({field:'doi',text:'10.1/x'},()=>{},'http://www.ir.lib.sjtu.edu.cn/itemDetail?id=1')).error,/检索结果页/);
  assert.match((await search({field:'doi',text:'10.1/x'},()=>{},'http://admin.ir.lib.sjtu.edu.cn/simpleSearch')).error,/前台/);
  for(const command of [{field:'ALL',text:'x'},{field:'doi',text:''},{field:'doi',text:'a\nb'}])
    assert.equal((await search(command)).ok,false);
  const twoTitles=await search({field:'doi',text:'10.1/y'},{'doi|10.1/y':[record('1',{title:['A','B'],doi:['10.1/y']})]});
  assert.match(twoTitles.error,/多个题名/);
});
