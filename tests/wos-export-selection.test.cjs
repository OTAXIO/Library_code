/* Material export controls, synthetic pages only. No live WOS/user profile. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('playwright');
const {runWOSCommand}=require('../extension/wos-adapter.js');
const fixture=fs.readFileSync(path.join(__dirname,'fixtures/wos-navigation.html'),'utf8');
const edge='C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe';
const origins=['https://www.webofscience.com','https://webofscience.clarivate.cn'];
const recordPath='/wos/woscc/full-record/WOS:000123456789012';
const command=action=>({action,sa_id:'offline-material-export',title:'Synthetic paper',
  wos:'WOS:000123456789012',expires:Date.now()+30000});

(async()=>{
  const browser=await chromium.launch({headless:true,...(fs.existsSync(edge)?{executablePath:edge}:{})});
  let checks=0;
  try{
    const context=await browser.newContext({acceptDownloads:true});
    // Even the realistic origin strings never reach an external network.
    await context.route('**/*',route=>route.fulfill({status:200,contentType:'text/html',body:fixture}));
    const page=await context.newPage();
    let observedDownloads=0;
    page.on('download',()=>observedDownloads++);
    const reset=async(origin,options={})=>{
      await page.goto(origin+recordPath);
      observedDownloads=0;
      await page.evaluate(({full='Full Record',icon='arrow_drop_down',dishonest=false})=>{
        const main=document.getElementById('main');
        window.exportsMade=0;
        window.contentSelections=0;
        document.getElementById('export').onclick=()=>{
          const format=document.createElement('button');format.setAttribute('role','menuitem');
          format.innerHTML='<mat-icon>description</mat-icon><span>Tab delimited file</span>';
          main.append(format);
          format.onclick=()=>{
            format.remove();
            const panel=document.createElement('mat-dialog-container');
            panel.setAttribute('role','dialog');
            panel.innerHTML='<mat-select role="combobox" aria-label="Record Content" tabindex="0">'+
              '<span class="selected-content">Author, Title, Source</span><mat-icon>'+icon+'</mat-icon></mat-select>'+
              '<input type="number" value="4"><input type="number" value="7">'+
              '<button type="button"><mat-icon>download</mat-icon><span>Export</span></button>';
            main.append(panel);
            const selector=panel.querySelector('[role="combobox"]');
            selector.onclick=()=>{
              // Angular Material attaches its option list to a sibling overlay,
              // not inside the mat-select or the export dialog.
              const overlay=document.createElement('div');overlay.className='cdk-overlay-container';
              const option=document.createElement('mat-option');option.setAttribute('role','option');
              option.innerHTML='<mat-icon>description</mat-icon><span>'+full+'</span>';
              overlay.append(option);document.body.append(overlay);
              option.onclick=()=>{
                window.contentSelections++;
                selector.querySelector('.selected-content').textContent=dishonest?'Author, Title, Source':full;
                overlay.remove();
              };
            };
            panel.querySelector('button').onclick=()=>{
              // A fixture itself never generates a file for partial content or
              // the wrong range; the adapter must also refuse before this click.
              if(!['Full Record','完整记录','全记录'].includes(selector.querySelector('.selected-content').textContent)||
                  [...panel.querySelectorAll('input')].some(input=>input.value!=='1'))return;
              window.exportsMade++;
              const text='TI\tAU\tAF\tSO\tPY\tC1\tUT\tDI\r\nSynthetic paper\tTest, A\tAlice Test\tSynthetic Journal\t2026\tShanghai Jiao Tong Univ\tWOS:000123456789012\t10.1234/test\r\n';
              const file=document.createElement('a');file.href=URL.createObjectURL(new Blob([text],{type:'text/plain'}));
              file.download='material-synthetic.txt';file.click();panel.remove();
            };
          };
        };
      },options);
    };
    const prepare=()=>page.evaluate(runWOSCommand,command('wos_prepare_export'));
    const refuseDownload=async()=>{
      const check=await page.evaluate(runWOSCommand,command('wos_check_export'));
      const submit=await page.evaluate(runWOSCommand,command('wos_download'));
      assert.equal(check.ok,false,JSON.stringify(check));assert.equal(submit.ok,false,JSON.stringify(submit));
      assert.equal(await page.evaluate(()=>exportsMade),0,'unsafe content must never invoke the export handler');
      assert.equal(observedDownloads,0,'unsafe content must never generate any download');
    };
    for(const origin of origins){
      for(const [full,icon] of [['Full Record','arrow_drop_down'],['完整记录','expand_more'],['全记录','arrow_drop_down']]){
        await reset(origin,{full,icon});
        const ready=await prepare();assert.equal(ready.ok,true,JSON.stringify(ready));
        assert.equal(await page.evaluate(()=>contentSelections),1);
        assert.deepEqual(await page.locator('mat-dialog-container input').evaluateAll(inputs=>inputs.map(input=>input.value)),['1','1']);
        const checked=await page.evaluate(runWOSCommand,command('wos_check_export'));
        assert.equal(checked.ok,true,JSON.stringify(checked));assert.equal(observedDownloads,0);
        const downloaded=page.waitForEvent('download');
        const submitted=await page.evaluate(runWOSCommand,command('wos_download'));
        assert.equal(submitted.ok,true,JSON.stringify(submitted));
        assert.equal((await downloaded).suggestedFilename(),'material-synthetic.txt');
        assert.equal(await page.evaluate(()=>exportsMade),1);assert.equal(observedDownloads,1);
        const duplicate=await page.evaluate(runWOSCommand,command('wos_download'));
        assert.equal(duplicate.ok,false);assert.equal(await page.evaluate(()=>exportsMade),1);
        console.log(`PASS ${new URL(origin).hostname} ${full} ignores ${icon}; one download only`);checks++;
      }
      await reset(origin);
      assert.equal((await prepare()).ok,true);
      await page.evaluate(()=>{
        const selector=document.querySelector('mat-select');
        selector.querySelector('.selected-content').textContent='';
        selector.setAttribute('aria-label','Full Record');
      });
      const accessible=await page.evaluate(runWOSCommand,command('wos_check_export'));
      assert.equal(accessible.ok,true,JSON.stringify(accessible));
      assert.equal(await page.evaluate(()=>exportsMade),0,'checking an exact accessible selected label never submits');
      console.log(`PASS ${new URL(origin).hostname} icon-only selection preserves the exact Full Record accessible label`);checks++;

      await reset(origin);
      assert.equal((await prepare()).ok,true);
      await page.evaluate(()=>{
        const selector=document.querySelector('mat-select');
        selector.setAttribute('aria-label','Full Record');
        selector.querySelector('.selected-content').textContent='Author, Title, Source';
      });
      await refuseDownload();
      console.log(`PASS ${new URL(origin).hostname} visible partial content is not overridden by a Full Record aria label`);checks++;

      await reset(origin);
      assert.equal((await prepare()).ok,true);
      await page.evaluate(()=>{
        const selector=document.querySelector('mat-select');
        selector.querySelector('.selected-content').textContent='Cited References';
        const fake=document.createElement('span');fake.hidden=true;fake.textContent='Full Record';selector.append(fake);
      });
      await refuseDownload();
      console.log(`PASS ${new URL(origin).hostname} hidden Full Record text cannot authorize partial content`);checks++;

      await reset(origin,{dishonest:true});
      const rejected=await prepare();assert.equal(rejected.ok,false,JSON.stringify(rejected));
      assert.equal(await page.evaluate(()=>contentSelections),1);
      const submitted=await page.evaluate(runWOSCommand,command('wos_download'));
      assert.equal(submitted.ok,false);assert.equal(await page.evaluate(()=>exportsMade),0);assert.equal(observedDownloads,0);
      console.log(`PASS ${new URL(origin).hostname} clicking a labelled option is insufficient if Full Record is not selected`);checks++;
    }
    console.log(`WOS Material export selection: ${checks} offline checks passed across both origins.`);
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
