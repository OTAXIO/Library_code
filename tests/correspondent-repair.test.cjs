/* Original native UI save contract; all network requests use a synthetic page. */
const assert=require('node:assert/strict'),fs=require('node:fs'),path=require('node:path');
const {test,before,after}=require('node:test');
const {chromium}=require('playwright');
const {runSACommand}=require('../extension/adapter.js');
let browser,page;
before(async()=>{
  browser=await chromium.launch({headless:true,executablePath:'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe'});
  const context=await browser.newContext();
  const fixture=fs.readFileSync(path.join(__dirname,'fixtures/compare.html'),'utf8');
  await context.route('**/*',r=>r.fulfill({status:200,contentType:'text/html',body:fixture}));
  page=await context.newPage();
});
after(async()=>{await browser?.close();});
async function setup(mode='ok'){
  await page.goto('about:blank');await page.goto('http://admin.ir.lib.sjtu.edu.cn/#/dataCompare/list');
  await page.evaluate(mode=>{
    testConfig.modern=true;testConfig.claim='已认领';testConfig.libraryDoi='10.20517/demo.2026.1';
    let metadata={title:['Synthetic paper'],doi:[testConfig.libraryDoi],year:[2026],
      authorInstitution:[{address:'Shanghai Jiao Tong University',order:1,claimStatus:9,institutionId:'sjtu'}],
      author:[{fullname:'Other Author',order:1,scholarId:'other-scholar',institutionOrderNums:[1],correspondent:false},
        {fullname:'Demo',order:2,scholarId:'scholar-001',institutionOrderNums:[1],correspondent:false,
          ownFirst:null,commonFirst:null,ownCorrespondent:null,commonCorrespondent:null}]};
    window.roleWrites=0;
    if(mode==='conflicting-correspondence')metadata.correspondent=['Other Author'];
    const show=detail.show;
    detail.show=function(r){show.call(this,r);this.itemData={id:r.itemId.replace(/^,/,''),updateTime:'v1',metadata:copy(metadata)};};
    let ownPanel;
    const editor={drawer:false,$refs:{},handleCloses(){this.drawer=false;ownPanel?.remove();}};detail.$refs.itemEdit=editor;
    detail.refreshCompareData=function(){this.show(synthetic);};
    detail.editItem=function(id){
      const f={id,updateTime:'v1',metadata:copy(metadata)};
      f.metadata.author.forEach(a=>a.institutionOrderNums=a.institutionOrderNums.join(','));
      const panel=document.createElement('div');panel.className='el-drawer';document.body.append(panel);
      ownPanel=panel;
      editor.drawer=true;editor.$refs.itemModelEdit={ruleForm:f,loading:false,
        validInfo:{author:{customConfig:{correspondent:true}}},
        onCorrespondentChange(a){if(mode==='mutated-form')f.metadata.year=[2030];},
        onSubmit(){
          window.roleWrites++;metadata=copy(f.metadata);
          metadata.author.forEach(a=>a.institutionOrderNums=a.institutionOrderNums.split(',').map(Number));
          if(mode==='normalizes'){
            metadata.year=['2026'];metadata.authorInstitution[0].claimStatus=100;
            metadata.author.forEach(a=>{for(const k of ['ownFirst','commonFirst','ownCorrespondent','commonCorrespondent'])a[k]=Boolean(a[k]);});
            metadata.author[0].ownFirst=true;metadata.correspondent=['Demo'];
          }
          if(mode==='changed-readback')metadata.year=[2030];
          if(!['keeps-open','normalizes'].includes(mode)){editor.drawer=false;panel.remove();}
        }};
    };
    if(mode==='existing-editor')editor.drawer=true;
    if(mode==='other-claim')testConfig.libraryClaimStaff='00002';
  },mode);
  const c={action:'search',sa_id:'demo-001',expires:Date.now()+60000};
  const read=await page.evaluate(runSACommand,c);
  if(mode==='existing-editor'||mode==='other-claim')return {read};
  assert.equal(read.ok,true,JSON.stringify(read));
  return {read,payload:{action:'repair_correspondent',sa_id:'demo-001',expires:Date.now()+60000,
    expected:read.data.row,item_id:'1234567890123456789',staff_id:'00001',author:'Demo',order:2,
    person:{id:'scholar-001',wno:'00001'},candidate:{title:'Synthetic paper',doi:'10.20517/demo.2026.1',
      sha256:'c'.repeat(64),sjtu:true},evidence:{protocol:1,url:'https://www.oaepublish.com/articles/demo.2026.1',
      doi:'10.20517/demo.2026.1',title:'Synthetic paper',candidate_sha256:'c'.repeat(64),
      correspondent_names:['Demo'],source_sha256:'d'.repeat(64)}}};
}
test('one native save changes only the exact claimed author correspondent flag',async()=>{
  const {payload}=await setup();const r=await page.evaluate(runSACommand,payload);
  assert.equal(r.ok,true,JSON.stringify(r));assert.equal(r.data.verified,true);
  assert.equal(r.data.correspondent,true);assert.equal(await page.evaluate(()=>roleWrites),1);
  const metadata=await page.evaluate(()=>detail.itemData.metadata);
  assert.equal(metadata.author[0].correspondent,false);assert.equal(metadata.author[0].scholarId,'other-scholar');
  assert.equal(metadata.author[1].scholarId,'scholar-001');assert.equal(metadata.author[1].ownFirst,null);
  assert.deepEqual(metadata.year,[2026]);
});
test('native save may keep its editor open; a verified clean form closes without resubmitting',async()=>{
  const {payload}=await setup('keeps-open');const r=await page.evaluate(runSACommand,payload);
  assert.equal(r.ok,true,JSON.stringify(r));assert.equal(await page.evaluate(()=>roleWrites),1);
  assert.equal(await page.evaluate(()=>detail.$refs.itemEdit.drawer),false);
});
test('observed native normalization preserves year, units and all author identities',async()=>{
  const {payload}=await setup('normalizes');const r=await page.evaluate(runSACommand,payload);
  assert.equal(r.ok,true,JSON.stringify(r));assert.equal(await page.evaluate(()=>roleWrites),1);
  assert.equal(await page.evaluate(()=>detail.$refs.itemEdit.drawer),false);
});
test('wrong source, byline, staff or platform never submits a role write',async()=>{
  for(const edit of [p=>p.evidence.url='https://untrusted.example/',p=>p.author='Other Author',
                    p=>p.staff_id='1',p=>p.person.id='other-scholar',p=>p.item_id='other-item']){
    const {payload}=await setup();edit(payload);const r=await page.evaluate(runSACommand,payload);
    assert.equal(r.ok,false);assert.equal(await page.evaluate(()=>roleWrites),0);
  }
});
test('a mutation outside the one role field blocks the native save',async()=>{
  const {payload}=await setup('mutated-form');const r=await page.evaluate(runSACommand,payload);
  assert.equal(r.ok,false);assert.match(r.error,/表单或原文献/);assert.equal(await page.evaluate(()=>roleWrites),0);
});
test('changed post-save metadata remains uncertain instead of reporting success',async()=>{
  const {payload}=await setup('changed-readback');const r=await page.evaluate(runSACommand,payload);
  assert.equal(r.ok,false);assert.match(r.error,/只改一个通讯作者/);assert.match(r.error,/已发出写入，禁止自动重试/);
  assert.equal(await page.evaluate(()=>roleWrites),1);
});
test('an existing human editor or wrong claimed staff blocks preflight',async()=>{
  for(const mode of ['existing-editor','other-claim']){
    const {read}=await setup(mode);assert.equal(read.ok,false);assert.equal(await page.evaluate(()=>roleWrites),0);
  }
});
test('existing contradictory correspondent text cannot be erased by a role repair',async()=>{
  const {payload}=await setup('conflicting-correspondence');const r=await page.evaluate(runSACommand,payload);
  assert.equal(r.ok,false);assert.match(r.error,/文本与逐作者标记冲突/);
  assert.equal(await page.evaluate(()=>roleWrites),0);
});
