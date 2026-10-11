/* Material export controls, synthetic pages only. No live WOS/user profile. */
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const {chromium}=require('playwright');
const {runWOSCommand}=require('../extension/wos-adapter.js');
const {inspectWorkPage}=require('../extension/page-diagnostics.js');
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
            // WOS uses an Angular auxiliary route for this SAME record's
            // modal. The final Export must survive this observed route change.
            history.pushState({},'',location.pathname+'(overlay:export/ext)');
            const panel=document.createElement('mat-dialog-container');
            panel.setAttribute('role','dialog');
            panel.innerHTML='<h2>Export Records to Tab Delimited File</h2><mat-select role="combobox" aria-label="Record Content" tabindex="0">'+
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
              history.replaceState({},'',location.pathname.replace('(overlay:export/ext)',''));
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
      const expected=origin+recordPath;
      await reset(origin);
      assert.equal((await prepare()).ok,true);
      // Reproduce the user's shown state: the modal exists and Full Record is
      // already selected. The selector is a listbox button, not a combobox.
      await page.evaluate(()=>{
        const selector=document.querySelector('mat-select');
        selector.setAttribute('role','button');selector.setAttribute('aria-haspopup','listbox');
        const inner=document.createElement('span');inner.setAttribute('role','combobox');
        inner.tabIndex=0;while(selector.firstChild)inner.append(selector.firstChild);
        selector.append(inner);
        // A nested role=dialog around Record Content alone is not the complete panel.
        const small=document.createElement('div');small.setAttribute('role','dialog');
        selector.replaceWith(small);small.append(selector);
      });
      const resumed=await page.evaluate(runWOSCommand,{...command('wos_prepare_export'),record_url:expected});
      assert.equal(resumed.ok,true,JSON.stringify(resumed));
      assert.equal(resumed.data.record_url,expected,'the export overlay retains the canonical UT');
      const diagnostic=await page.evaluate(inspectWorkPage,{action:'wos_diagnose'});
      assert.equal(diagnostic.record_route,true);
      assert.equal(diagnostic.export_dialog,true,'the exact overlay permits safe unsubmitted preview recovery');
      assert.equal(await page.evaluate(()=>contentSelections),1,'an already selected Full Record is not reopened');
      assert.equal((await page.evaluate(runWOSCommand,{...command('wos_export_status'),record_url:expected})).data.state,'unsubmitted');
      const resumedDownload=page.waitForEvent('download');
      assert.equal((await page.evaluate(runWOSCommand,{...command('wos_download'),record_url:expected})).ok,true);
      await resumedDownload;
      assert.equal(await page.evaluate(()=>exportsMade),1);
      assert.equal((await page.evaluate(runWOSCommand,{...command('wos_export_status'),record_url:expected})).data.state,'submitted');
      const repeated=await page.evaluate(runWOSCommand,{...command('wos_prepare_export'),record_url:expected});
      assert.equal(repeated.ok,false);assert.equal(await page.evaluate(()=>exportsMade),1);
      console.log(`PASS ${new URL(origin).hostname} existing Tab Delimited / Full Record modal resumes exactly once`);checks++;

      await reset(origin);assert.equal((await prepare()).ok,true);
      await page.evaluate(()=>{
        const title=document.querySelector('mat-dialog-container h2');
        const replacement=document.createElement('div');replacement.textContent=title.textContent;title.replaceWith(replacement);
      });
      const divHeading=await page.evaluate(inspectWorkPage,{action:'wos_diagnose'});
      assert.equal(divHeading.export_dialog,true,'a known exact export heading is not limited to h2 tags');
      assert.equal((await page.evaluate(runWOSCommand,{...command('wos_prepare_export'),record_url:expected})).ok,true);
      console.log(`PASS ${new URL(origin).hostname} fixed export heading in a DIV preserves safe preview recovery`);checks++;

      await reset(origin);assert.equal((await prepare()).ok,true);
      await page.evaluate(()=>{
        window.blockedZeroTimers=0;const original=window.setTimeout;
        window.setTimeout=(fn,ms,...args)=>{
          if(ms===0){blockedZeroTimers++;return -1;}
          return original(fn,ms,...args);
        };
      });
      const immediateDownload=page.waitForEvent('download');
      const immediate=await page.evaluate(runWOSCommand,{...command('wos_download'),record_url:expected});
      assert.equal(immediate.ok,true);assert.equal(immediate.data.click_observed,true);
      assert.equal(immediate.data.export_submission_protocol,1);
      assert.equal(await page.evaluate(()=>blockedZeroTimers),0,'actual final Export must not be delegated to a late zero timer');
      await immediateDownload;assert.equal(await page.evaluate(()=>exportsMade),1);
      console.log(`PASS ${new URL(origin).hostname} successful reply proves an immediate final Export click`);checks++;

      await reset(origin);assert.equal((await prepare()).ok,true);
      await page.evaluate(()=>document.querySelector('mat-dialog-container > button').click=()=>{});
      const noClick=await page.evaluate(runWOSCommand,{...command('wos_download'),record_url:expected});
      assert.equal(noClick.ok,false);assert.match(noClick.error,/点击事件未确认/);
      assert.equal(await page.evaluate(()=>exportsMade),0);assert.equal(observedDownloads,0);
      const unknown=await page.evaluate(runWOSCommand,{...command('wos_export_status'),record_url:expected});
      assert.equal(unknown.data.state,'submitted');assert.equal(unknown.data.click_observed,false);
      assert.equal((await page.evaluate(runWOSCommand,{...command('wos_download'),record_url:expected})).ok,false);
      console.log(`PASS ${new URL(origin).hostname} missing click event cannot fabricate success or authorize retry`);checks++;

      await reset(origin);assert.equal((await prepare()).ok,true);
      await page.locator('mat-dialog-container h2').evaluate(el=>el.textContent='Export Records to Excel');
      const foreign=await page.evaluate(runWOSCommand,{...command('wos_prepare_export'),record_url:expected});
      assert.equal(foreign.ok,false);assert.equal(await page.evaluate(()=>exportsMade),0);
      console.log(`PASS ${new URL(origin).hostname} another export format is not adopted`);checks++;

      for(const suffix of ['(overlay:export/excel)','(overlay:export/ext//other:x)',
        '(overlay:export/ext)(other:x)','(overlay:export/ext)/extra','(overlay:export/ext)junk']){
        await reset(origin);assert.equal((await prepare()).ok,true);
        await page.evaluate(path=>history.replaceState({},'',path),recordPath+suffix);
        await refuseDownload();
        console.log(`PASS ${new URL(origin).hostname} unknown auxiliary route is refused`);checks++;
      }

      await reset(origin);assert.equal((await prepare()).ok,true);
      const wrong=await page.evaluate(runWOSCommand,{...command('wos_prepare_export'),record_url:expected.replace('000123456789012','000999999999999')});
      assert.equal(wrong.ok,false);assert.equal(await page.evaluate(()=>exportsMade),0);
      console.log(`PASS ${new URL(origin).hostname} a different searched UT cannot authorize the open modal`);checks++;

      await reset(origin);assert.equal((await prepare()).ok,true);
      const manualDownload=page.waitForEvent('download');
      await page.locator('mat-dialog-container > button').click();await manualDownload;
      const manualState=await page.evaluate(runWOSCommand,{...command('wos_export_status'),record_url:expected});
      assert.equal(manualState.data.state,'submitted');
      assert.equal((await page.evaluate(runWOSCommand,{...command('wos_prepare_export'),record_url:expected})).ok,false);
      assert.equal(await page.evaluate(()=>exportsMade),1);
      console.log(`PASS ${new URL(origin).hostname} a user's manual final Export also blocks duplicate resume`);checks++;

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
