/* Semantic, fail-closed WOS UI adapter. No scraping API or cookie access.
 * Export uses WOS's own controls. Layout/language changes stop for the user. */
async function runWOSCommand(command) {
  const fail = text => {throw new Error(text);};
  const norm = s => String(s??"").normalize("NFKC").replace(/\s+/g," ").trim();
  const visible = el => el && el.getClientRects().length>0 && getComputedStyle(el).visibility!=="hidden";
  const all = (sel,root=document)=>[...root.querySelectorAll(sel)].filter(visible);
  const caption = el => norm(el.getAttribute("aria-label") || el.innerText || el.textContent);
  const selection = el => el.tagName==="SELECT" ? norm(el.selectedOptions[0]?.textContent) : norm(el.innerText || el.textContent || el.getAttribute("aria-label"));
  const fieldText = el => selection(el).replace(/(?:arrow_drop_down|expand_more|keyboard_arrow_down)/g, "").trim().replace(/\s*\((?:TS|TI|DO|UT|ALL)\)$/, "");
  const fieldNames = ["All Fields","所有字段","全部字段","所有欄位","Topic","主题","主題","Title","标题","题名","標題","題名",
    "DOI","Accession Number","入藏号","入藏號","Author","作者","Publication Titles","出版物名称","出版物名稱"];
  const fields = () => {
    const matches=all('[role="combobox"],select,[aria-haspopup="listbox"]').filter(el=>fieldNames.includes(fieldText(el)));
    // A nested wrapper and its combobox are one control, not two search rows.
    return matches.filter(el=>!matches.some(other=>other!==el && el.contains(other)));
  };
  const one = (items,label)=>{if(items.length!==1)fail(label+"未唯一识别，请人工调整网页后继续");return items[0];};
  const check=()=>{
    // This function is serialized into the page; keep its exact origins aligned
    // with workflow-background.js and manifest.json (covered by browser tests).
    if(!["https://www.webofscience.com","https://webofscience.clarivate.cn"].includes(location.origin) || new URL(location.href).username || new URL(location.href).password)
      fail("WOS 网址不受支持，请在 www.webofscience.com 或 webofscience.clarivate.cn 的 HTTPS 文献检索页操作");
    if(!Number.isFinite(command.expires) || Date.now()>=command.expires-2500)fail("WOS 操作超时，请人工查看网页");
    if(/Oops,?\s*something went wrong!?/i.test(document.body?.innerText||""))
      fail("WOS 网站自身报错：Oops, something went wrong! 这不是导入管理页的问题。请先点击 WOS 网页顶部 Search 或导航菜单重新进入检索；若仍报错，请人工检查登录、校园网/机构访问。网页恢复前不继续检索或导入");
    if(!location.pathname.startsWith("/wos/woscc/"))fail("请在 WOS 核心合集的文献检索页登录，不能使用作者检索");
    if(all('iframe[src*="captcha"],input[type="password"],#challenge-form').length)fail("登录或验证码需要人工处理");
  };
  const wait=async(fn,label,ms=30000)=>{
    const end=Math.min(Date.now()+ms,command.expires-3000);
    while(Date.now()<end){check();if(fn())return;await new Promise(r=>setTimeout(r,180));}
    fail(label+"超时，未自动重复操作");
  };
  const button=(labels,root=document)=>one(all('button,[role="button"],a',root).filter(el=>labels.includes(caption(el))),labels.join(" / "));
  const click=el=>{check();if(el.disabled||el.getAttribute("aria-disabled")==="true")fail("控件尚不可用");el.click();};
  const set=(el,value)=>{
    const proto=el.tagName==="TEXTAREA"?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto,"value").set.call(el,value);
    el.dispatchEvent(new Event("input",{bubbles:true}));el.dispatchEvent(new Event("change",{bubbles:true}));
  };
  const fullRecord = () => /^\/wos\/woscc\/full-record\/WOS:\d{15}\/?$/.test(decodeURI(location.pathname));
  const fingerprint = () => {
    if(!fullRecord())fail("未处于 WOS 核心合集单篇完整记录页");
    const ut=decodeURI(location.pathname).match(/WOS:\d{15}/)[0];
    if(command.wos && command.wos!==ut)fail("WOS 页面入藏号与名单不一致");
    return location.origin+location.pathname;
  };
  try {
    check();
    if(!["wos_search","wos_prepare_export","wos_download"].includes(command.action))fail("未知 WOS 命令");
    if(typeof command.title!=="string" || !command.title.trim() || command.title.length>1500)fail("题名缺失或过长");
    if(command.action==="wos_search") {
      if(!/\/(?:basic-search|advanced-search|fielded-search)\/?$/.test(location.pathname))fail("请先进入 WOS 核心合集的字段检索页");
      if(all('[role="dialog"],mat-dialog-container').length)fail("WOS 有弹窗，请人工处理");
      const choices=command.wos?["Accession Number","入藏号","入藏號"]:command.doi?["DOI"]:["Title","标题","题名","標題","題名"];
      const query=command.wos||command.doi||command.title;
      // New WOS defaults to Smart Search. Follow only visible, specifically
      // labelled links to Advanced -> Fielded Search; never invent route URLs
      // or change the account's Smart Search preference.
      const navigation=["Fielded Search","字段检索","字段搜索","欄位檢索","Advanced Search","高级检索","高级搜索","進階檢索"];
      for(let step=0;fields().length===0 && step<2;step++) {
        const links=all('a,button,[role="tab"]').filter(el=>navigation.includes(caption(el)) && el.getAttribute("aria-selected")!=="true");
        const fielded=links.filter(el=>["Fielded Search","字段检索","字段搜索","欄位檢索"].includes(caption(el)));
        const next=fielded.length?fielded:links;
        if(next.length!==1)break;
        const clicked=next[0];click(clicked);
        await wait(()=>fields().length>0 || (!clicked.isConnected && all('a,button,[role="tab"]').some(el=>navigation.includes(caption(el)))),"切换字段检索",15000);
      }
      const combos=fields();
      if(combos.length!==1)fail(`检索字段选择器未唯一识别（识别到 ${combos.length} 个）。请进入 Advanced Search / 高级检索 → Fielded Search / 字段检索，只保留一行条件。可点扩展“检查工作页”复制控件诊断`);
      let field=combos[0];
      if(field.tagName==="SELECT"){
        const option=one([...field.options].filter(o=>choices.includes(norm(o.textContent))),"检索字段");
        field.value=option.value;field.dispatchEvent(new Event("change",{bubbles:true}));
      } else {
        click(field);
        await wait(()=>all('[role="option"],mat-option').some(e=>choices.includes(caption(e))),"字段菜单",5000);
        click(one(all('[role="option"],mat-option').filter(e=>choices.includes(caption(e))),"检索字段"));
      }
      await wait(()=>fields().length===1 && choices.includes(fieldText(fields()[0])),"确认检索字段",5000);
      field=fields()[0];
      // Only an empty single-row basic-search form can be controlled.
      const scope=field.closest("form") || document;
      const inputs=all('input:not([type]),input[type="text"],input[type="search"],textarea',scope).filter(e=>!e.readOnly&&!e.disabled);
      const input=one(inputs,"单行文献检索输入框");
      set(input,query);
      const previous=location.href;
      click(button(["Search","检索","搜索"],scope));
      await wait(()=>location.href!==previous,"WOS 检索结果");
      await wait(()=>all('a[href*="/full-record/WOS:"]').length || /No results found|未找到结果|没有检索结果/.test(document.body.innerText),"加载结果");
      if(/No results found|未找到结果|没有检索结果/.test(document.body.innerText))fail("WOS 未找到记录；这不等于未发表，也不自动标记完成");
      const links=all('a[href*="/wos/woscc/full-record/WOS:"]');
      const urls=new Map();for(const a of links){const url=new URL(a.href);if(url.origin===location.origin)urls.set(url.origin+url.pathname,a);}
      // Full results total must be one, not simply one rendered/visible match.
      const text=norm(document.body.innerText);
      const single=/(?:^|\s)1 (?:result|results|document|documents)(?:\s|$)/i.test(text) || /(?:^|\s)1 条(?:结果|记录)(?:\s|$)/.test(text);
      if(!single || urls.size!==1)fail("WOS 结果不是可确认的唯一记录，请人工选择并核对后使用“导出当前 WOS 文献”");
      click([...urls.values()][0]);await wait(fullRecord,"打开单篇记录");
      return {ok:true,data:{record_url:fingerprint()}};
    }
    const recordURL=fingerprint();
    if(command.action==="wos_prepare_export") {
      if(all('[role="dialog"],mat-dialog-container').length)fail("已有 WOS 弹窗，请人工关闭后再导出");
      click(button(["Export","导出"]));
      await wait(()=>all('[role="menuitem"],button,a,mat-option').some(el=>["Tab delimited file","Tab-delimited file","Tab delimited","制表符分隔文件","制表符分隔","制表符"].includes(caption(el))),"导出格式",5000);
      click(one(all('[role="menuitem"],button,a,mat-option').filter(el=>["Tab delimited file","Tab-delimited file","Tab delimited","制表符分隔文件","制表符分隔","制表符"].includes(caption(el))),"Tab delimited"));
      await wait(()=>all('mat-dialog-container,[role="dialog"]').length,"导出设置",5000);
      const dialogs=all('mat-dialog-container,[role="dialog"]');
      const dialog=one(dialogs.filter(el=>!dialogs.some(other=>other!==el&&el.contains(other))),"导出设置窗口");
      const selectors=all('select,[role="combobox"]',dialog);
      const select=one(selectors,"记录内容选择器");
      const full=["Full Record","全记录","完整记录"];
      if(select.tagName==="SELECT"){
        select.value=one([...select.options].filter(o=>full.includes(norm(o.textContent))),"Full Record").value;
        select.dispatchEvent(new Event("change",{bubbles:true}));
      }else{
        click(select);await wait(()=>all('[role="option"],mat-option').some(o=>full.includes(caption(o))),"完整记录选项",5000);
        click(one(all('[role="option"],mat-option').filter(o=>full.includes(caption(o))),"Full Record"));
      }
      // Full-record page + exact one-record range only; never 'all marked'.
      const ranges=all('input[type="number"],input[type="text"]',dialog);
      if(ranges.length){
        if(ranges.length!==2)fail("导出记录范围未知");
        for(const el of ranges)set(el,"1");
      }
      const selected=selection(select);
      if(!full.includes(selected))fail("未确认 Full Record 选项");
      window.__saWOSExport={id:command.sa_id,url:recordURL,dialog,select,ranges,submitted:false};
      return {ok:true,data:{ready:true,record_url:recordURL}};
    }
    const state=window.__saWOSExport;
    if(!state || state.id!==command.sa_id || state.url!==recordURL || state.submitted || !visible(state.dialog))fail("导出预览失效或已提交");
    const selected=selection(state.select);
    if(!["Full Record","全记录","完整记录"].includes(selected) || state.ranges.some(el=>el.value!=="1"))fail("导出选项被修改");
    state.submitted=true;
    click(button(["Export","导出"],state.dialog));
    return {ok:true,data:{submitted:true,record_url:recordURL}};
  } catch(error){return {ok:false,error:"[WOS 已暂停] "+error.message};}
}
if(typeof module!=="undefined")module.exports={runWOSCommand};
