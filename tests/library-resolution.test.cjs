/* Disposable browser, every HTTP request intercepted; no production traffic. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const {chromium}=require('playwright');
const {resolveLibraryRecord}=require('../extension/library-adapter.js');
const edge='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const origin='http://admin.ir.lib.sjtu.edu.cn';
const candidate={title:'Synthetic paper',doi:'10.1234/test',wos:'WOS:000123456789012',sjtu:true,sha256:'a'.repeat(64)};
const metadataCandidate={...candidate,kind:'crossref-openalex-v1',source:'Crossref+OpenAlex',wos:''};
const primaryUrl='https://proceedings.mlr.press/v306/test26a.html';
const primaryCandidate={...metadataCandidate,kind:'publisher-verified-v1',source:'PMLR官方论文集',doi:'',
  primary_url:primaryUrl,primary_evidence_sha256:'b'.repeat(64),author_names:['Alice Test'],
  fields:{'相关网址':primaryUrl,'年份':'2026','会议名称':'International Conference on Machine Learning'}};
const primaryFields={'相关网址':'url','年份':'year','会议名称':'conferenceName'};
const primaryItem={id:'1234567890123456789',metadata:{title:[candidate.title],doi:[],wosId:[],
  url:[primaryUrl],year:['2026'],conferenceName:[primaryCandidate.fields['会议名称']],author:[{fullname:'Alice Test'}]}};
const journalFields={'URL':'url','年份':'year','发表期刊':'source'};
const journalCandidate={...primaryCandidate,source:'Nature出版社',document_type:'journal-article',model_name:'期刊论文',
 doi:'10.1038/s99999-025-00001-1',primary_url:'https://www.nature.com/articles/s99999-025-00001-1',
 fields:{'URL':'https://www.nature.com/articles/s99999-025-00001-1','年份':'2025','发表期刊':'Synthetic Journal'}};
const journalItem={...primaryItem,metadata:{...primaryItem.metadata,doi:[journalCandidate.doi],url:[journalCandidate.primary_url],
 year:[2025],source:[{name:'Synthetic Journal',issn:null,standardName:null}]}};
const item={id:'1234567890123456789',metadata:{title:[candidate.title],doi:[candidate.doi],wosId:[candidate.wos]}};
const fixture=`<div id="app">Disposable library fixture</div><script>
window.config={rows:[],failure:false,incomplete:false};window.reads=[];window.routes=[];
const root=document.getElementById('app');
async function getData(page,query){
  reads.push({...query});this.loading=true;this.tableData=[];
  // A delayed native initialization query writes to the visible component,
  // not necessarily to the invocation whose result the adapter is awaiting.
  if(config.visibleRace)setTimeout(()=>{
    library.tableData=[];library.total=0;library.loading=false;
  },10);
  if(config.neverFinishes)await new Promise(()=>{});
  await new Promise(resolve=>setTimeout(resolve,config.visibleRace?350:10));
  if(config.reject)throw Error('Synthetic native query rejection');
  if(!config.failure&&!(config.titleFailure&&query.title)){
    this.tableData=config.badTable?{}:config.rows.map(x=>({...x}));
    this.total='total' in config?config.total:this.tableData.length+(config.incomplete?1:0);
  }
  this.loading=Boolean(config.stillLoading);
}
const library={$el:root,$options:{name:'EntryManage',components:{ItemEdit:{}},methods:{getData}},$children:[],
  $refs:{itemDetail:{}},page:{current:1,size:10},queryForm:{},tableData:[],total:0,loading:false,
  getData(...args){return getData.apply(this,args);}};
const batch={$el:root,$options:{name:'BatchManage'},$children:[],
  $refs:{wosDataText:{},batchPushModal:{}},$router:{async push(path){routes.push(path);location.hash='#'+path;root.__vue__=path==='/item/entryManage'?library:batch;}}};
root.__vue__=batch;
</script>`;
(async()=>{
  const browser=await chromium.launch({headless:true,...(fs.existsSync(edge)?{executablePath:edge}:{})});
  try{
    const context=await browser.newContext();
    await context.route('**/*',route=>route.fulfill({status:200,contentType:'text/html',body:fixture}));
    const page=await context.newPage();
    const cases=[
      {name:'empty verified DOI and title queries prove absence',rows:[],ok:true,count:0},
      {name:'exact existing paper returns all 19 ID digits',rows:[item],ok:true,count:1},
      {name:'numeric ID is never rounded into a platform match',rows:[{...item,id:1234567890123456789}],ok:false,error:/精确数字文本/},
      {name:'same DOI with different WOS ID blocks duplicate import',rows:[{...item,metadata:{...item.metadata,wosId:['WOS:999999999999999']}}],ok:false,error:/冲突/},
      {name:'same title with missing identifiers needs dedupe',rows:[{...item,metadata:{title:[candidate.title],doi:[],wosId:[]}}],ok:false,error:/人工查重/},
      {name:'HTTP failure is not a zero result',rows:[],failure:true,ok:false,error:/未确认成功/},
      {name:'truncated results do not prove absence',rows:[item],incomplete:true,ok:false,error:/不完整/},
      {name:'duplicate papers remain separate, never choose one',rows:[item,{...item,id:'9876543210987654321'}],ok:true,count:2},
      {name:'initialization cannot replace a pending exact query with a false zero',rows:[item],visibleRace:true,ok:true,count:1},
      {name:'failed query cannot borrow an unrelated successful empty count',rows:[],failure:true,visibleRace:true,ok:false,error:/未确认成功/},
      {name:'successful DOI query does not excuse a failed title query',rows:[],titleFailure:true,ok:false,error:/题名.*未确认成功/},
      {name:'native rejected Promise pauses rather than proving absence',rows:[],reject:true,ok:false,error:/查询失败/},
      {name:'boolean count is not proof of zero',rows:[],total:false,ok:false,error:/总数/},
      {name:'array count is not proof of zero',rows:[],total:[0],ok:false,error:/总数/},
      {name:'numeric text count is supported',rows:[],total:'0',ok:true,count:0},
      {name:'negative count is not proof of zero',rows:[],total:-1,ok:false,error:/总数/},
      {name:'fractional count is not proof of zero',rows:[],total:0.5,ok:false,error:/总数/},
      {name:'unsafe integer count is not evidence',rows:[],total:9007199254740992,ok:false,error:/总数/},
      {name:'large result needs dedupe rather than truncated absence',rows:[item],total:101,ok:false,error:/过多/},
      {name:'fulfilled native Promise must also have finished loading',rows:[],stillLoading:true,ok:false,error:/总数/},
      {name:'changed table structure is not proof of absence',rows:[],badTable:true,total:0,ok:false,error:/不完整/},
      {name:'changed metadata schema is not silently skipped',rows:[{...item,metadata:{title:candidate.title}}],ok:false,error:/结构不兼容/},
      {name:'missing metadata is not silently skipped',rows:[{id:item.id}],ok:false,error:/结构不兼容/},
      {name:'blank title is not evidence of an unrelated record',rows:[{...item,metadata:{title:['   ']}}],ok:false,error:/结构不兼容/},
      {name:'multiple identifiers require manual interpretation',rows:[{...item,metadata:{...item.metadata,doi:[candidate.doi,'10.1234/other']}}],ok:false,error:/标识结构不兼容/},
      {name:'native method removed is incompatible',rows:[],missingMethod:true,ok:false,error:/结构不兼容/,expectedReads:0},
      {name:'native synchronous response cannot prove completion',rows:[],noPromise:true,ok:false,error:/完成确认/,expectedReads:1},
      {name:'native method with changed this dependencies stops safely',rows:[],syncThrow:true,ok:false,error:/查询失败/,expectedReads:0},
      {name:'bound method writing only to visible component cannot prove absence',rows:[],boundMethod:true,ok:false,error:/总数/},
      {name:'metadata exact lookup supports missing WOS identifier',candidate:metadataCandidate,action:'import_excel_resolve',rows:[{...item,metadata:{...item.metadata,wosId:[]}}],ok:true,count:1},
      {name:'metadata title permits presentation-only dash quote HTML and trailing-period differences',candidate:{...metadataCandidate,title:'Synthetic "paper"-I & evidence'},action:'import_excel_resolve',rows:[{...item,metadata:{...item.metadata,title:['<i>Synthetic “paper” – I &amp; evidence.</i>']}}],ok:true,count:1},
      {name:'metadata title never removes changed words',candidate:metadataCandidate,action:'import_excel_resolve',rows:[{...item,metadata:{...item.metadata,title:['Synthetic different paper']}}],ok:false,error:/冲突/},
      {name:'metadata title never removes changed digits',candidate:{...metadataCandidate,title:'Synthetic paper 1'},action:'import_excel_resolve',rows:[{...item,metadata:{...item.metadata,title:['Synthetic paper 2']}}],ok:false,error:/冲突/},
      {name:'WOS title policy remains strict despite metadata presentation support',candidate:{...candidate,title:'Synthetic paper-I'},rows:[{...item,metadata:{...item.metadata,title:['Synthetic paper – I.']}}],ok:false,error:/冲突/},
      {name:'metadata title rejects hidden control characters before searching',candidate:{...metadataCandidate,title:'Synthetic\u0001 paper'},action:'import_excel_resolve',rows:[],ok:false,error:/无效字符/,expectedReads:0},
      {name:'metadata lookup cannot borrow WOS evidence with wrong kind',candidate:{...metadataCandidate,kind:'other'},action:'import_excel_resolve',rows:[],ok:false,error:/文献证据/,expectedReads:0},
      {name:'metadata lookup requires declared source',candidate:{...metadataCandidate,source:'Other'},action:'import_excel_resolve',rows:[],ok:false,error:/文献证据/,expectedReads:0},
      {name:'metadata lookup cannot contain a fabricated WOS identifier',candidate:{...metadataCandidate,wos:candidate.wos},action:'import_excel_resolve',rows:[],ok:false,error:/文献证据/,expectedReads:0},
      {name:'whitespace candidate title is not usable evidence',candidate:{...candidate,title:'  '},rows:[],ok:false,error:/文献证据/,expectedReads:0},
      {name:'unresolved native query expires without querying the title or uploading',rows:[],neverFinishes:true,shortDeadline:true,ok:false,error:/未完成/},
      {name:'official no-DOI lookup verifies native URL year venue and full author order',candidate:primaryCandidate,
        action:'import_excel_resolve',primary_fields:primaryFields,rows:[primaryItem],ok:true,count:1},
      {name:'official no-DOI empty full-title lookup proves absence',candidate:primaryCandidate,
        action:'import_excel_resolve',primary_fields:primaryFields,rows:[],ok:true,count:0},
      {name:'native index numeric year agrees with original textual batch preview',candidate:primaryCandidate,
        action:'import_excel_resolve',primary_fields:primaryFields,rows:[{...primaryItem,metadata:{...primaryItem.metadata,year:[2026]}}],ok:true,count:1},
      ...[true,null,2026.5,[2026],{value:2026},' 2026','2026.0'].map(value=>({
        name:'invalid native year remains review-only '+JSON.stringify(value),candidate:primaryCandidate,
        action:'import_excel_resolve',primary_fields:primaryFields,
        rows:[{...primaryItem,metadata:{...primaryItem.metadata,year:[value]}}],ok:false,error:/人工查重/})),
      {name:'official same-title URL mismatch never permits duplicate upload',candidate:primaryCandidate,
        action:'import_excel_resolve',primary_fields:primaryFields,rows:[{...primaryItem,metadata:{...primaryItem.metadata,url:['https://other.example/paper']}}],ok:false,error:/人工查重/},
      {name:'official same-title author order mismatch needs review',candidate:primaryCandidate,
        action:'import_excel_resolve',primary_fields:primaryFields,rows:[{...primaryItem,metadata:{...primaryItem.metadata,author:[{fullname:'Bob Other'}]}}],ok:false,error:/人工查重/},
      {name:'official source rejects unverified model field codes',candidate:primaryCandidate,
        action:'import_excel_resolve',rows:[],ok:false,error:/身份字段/,expectedReads:0},
      {name:'official source rejects website masquerading as publisher',candidate:{...primaryCandidate,primary_url:primaryUrl+'.evil'},
        action:'import_excel_resolve',primary_fields:primaryFields,rows:[],ok:false,error:/文献证据/,expectedReads:0},
      {name:'publisher journal uses actual URL year journal and full ordered authors plus DOI',candidate:journalCandidate,
        action:'import_excel_resolve',primary_fields:journalFields,rows:[journalItem],ok:true,count:1},
      {name:'publisher journal empty DOI and title queries prove absence',candidate:journalCandidate,
        action:'import_excel_resolve',primary_fields:journalFields,rows:[],ok:true,count:0},
      ...[[{name:'Different Journal'}],[{name:['Synthetic Journal']}],[{}],[['Synthetic Journal']],
          [{name:'Synthetic Journal'},{name:'Other Journal'}]].map(source=>({
        name:'ambiguous or changed native journal remains review-only '+JSON.stringify(source),candidate:journalCandidate,
        action:'import_excel_resolve',primary_fields:journalFields,
        rows:[{...journalItem,metadata:{...journalItem.metadata,source}}],ok:false,error:/人工查重/})),
      {name:'publisher journal cannot silently lose its DOI',candidate:{...journalCandidate,doi:''},
        action:'import_excel_resolve',primary_fields:journalFields,rows:[],ok:false,error:/文献证据/,expectedReads:0},
      {name:'publisher journal cannot borrow conference schema',candidate:journalCandidate,
        action:'import_excel_resolve',primary_fields:primaryFields,rows:[],ok:false,error:/身份字段/,expectedReads:0},
      {name:'publisher DOI URL disagreement blocks before any native query',candidate:{...journalCandidate,doi:'10.1038/other'},
        action:'import_excel_resolve',primary_fields:journalFields,rows:[],ok:false,error:/文献证据/,expectedReads:0},
    ];
    for(const c of cases){
      await page.goto(origin+'/#/collectItem/batchManage');
      await page.reload();
      await page.evaluate(config=>{
        window.config={failure:false,incomplete:false,...config};
        if(config.missingMethod)delete library.$options.methods.getData;
        if(config.noPromise)library.$options.methods.getData=function(page,query){reads.push({...query});this.total=0;this.tableData=[];this.loading=false;};
        if(config.syncThrow)library.$options.methods.getData=function(){return this.changedDependency.call();};
        if(config.boundMethod)library.$options.methods.getData=getData.bind(library);
      },c);
      const queryCandidate=c.candidate||candidate;
      const result=await page.evaluate(resolveLibraryRecord,{action:c.action||'import_resolve',sa_id:'demo-001',candidate:queryCandidate,primary_fields:c.primary_fields,expires:Date.now()+(c.shortDeadline?6800:30000)});
      assert.equal(result.ok,c.ok,JSON.stringify(result)+' '+c.name);
      if(c.ok){assert.equal(result.data.library_resolution_protocol,2);assert.equal(result.data.items.length,c.count);if(c.count)assert.equal(result.data.items[0].id,item.id);}
      else assert.match(result.error,c.error);
      assert.equal(new URL(page.url()).hash,'#/collectItem/batchManage',c.name);
      const reads=await page.evaluate(()=>window.reads);
      if(c.expectedReads!==undefined)assert.equal(reads.length,c.expectedReads,c.name);
      if(reads.length&&queryCandidate.doi)assert.equal(reads[0].doi,queryCandidate.doi,c.name);
      if(c.ok)assert.equal(reads[queryCandidate.doi?1:0].title,queryCandidate.title,c.name);
      console.log('PASS '+c.name);
    }
    await page.goto(origin+'/#/collectItem/batchManage');
    await page.reload();
    await page.evaluate(()=>document.body.insertAdjacentHTML('beforeend','<div class="el-message-box">Human operation</div>'));
    const result=await page.evaluate(resolveLibraryRecord,{action:'import_resolve',candidate,expires:Date.now()+30000});
    assert.equal(result.ok,false);assert.match(result.error,/人工操作/);
    assert.deepEqual(await page.evaluate(()=>window.reads),[]);
    console.log('PASS human operation window never closed or queried');
    console.log('Library resolution: '+(cases.length+1)+' offline cases passed.');
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
