/* Semantic, fail-closed WOS UI adapter. No scraping API or cookie access.
 * Export uses WOS's own controls. Layout/language changes stop for the user. */
async function runWOSCommand(command) {
  const fail = text => {throw new Error(text);};
  // background.js may need up to eight seconds to POST the result back to the
  // desktop bridge. Finish every page-side WOS step with a separate margin so a
  // completed action is not reported as an unknown desktop timeout.
  const resultMargin=12000;
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
    if(!Number.isFinite(command.expires) || Date.now()>=command.expires-resultMargin)fail("WOS 操作超时，请人工查看网页");
    if(/Oops,?\s*something went wrong!?/i.test(document.body?.innerText||""))
      fail("WOS 网站自身报错：Oops, something went wrong! 这不是导入管理页的问题。请先点击 WOS 网页顶部 Search 或导航菜单重新进入检索；若仍报错，请人工检查登录、校园网/机构访问。网页恢复前不继续检索或导入");
    if(!location.pathname.startsWith("/wos/woscc/"))fail("请在 WOS 核心合集的文献检索页登录，不能使用作者检索");
    if(all('#challenge-form,#cf-challenge-running,iframe[src*="captcha"],iframe[src*="challenge"]').some(el=>
      !el.closest('.grecaptcha-badge,[hidden],[inert],[aria-hidden="true"]')&&
      (el.tagName!=="IFRAME"||el.getBoundingClientRect().height>=70)))
      fail("WOS 显示人工验证，请完成验证后继续；不自动重试检索");
    if(all('input[type="password"]').some(el=>!el.closest('[hidden],[inert],[aria-hidden="true"]')))
      fail("WOS 显示登录表单，请完成登录后继续；不自动重试检索");
  };
  const wait=async(fn,label,ms=30000)=>{
    const end=Math.min(Date.now()+ms,command.expires-resultMargin);
    while(Date.now()<end){check();if(fn())return;await new Promise(r=>setTimeout(r,180));}
    fail(label+"超时，未自动重复操作");
  };
  const button=(labels,root=document)=>one(all('button,[role="button"],a',root).filter(el=>labels.includes(actionLabel(el))),labels.join(" / "));
  // Read the action label, not decorative Material/SVG ligature text. Every
  // action still requires its own exact fixed label; export content/range and
  // all write-side checks retain their existing guards.
  const searchIcons='mat-icon,.mat-icon,svg,.material-icons,.material-icons-outlined,.material-symbols-outlined,.material-symbols-rounded,.material-symbols-sharp';
  const searchText=el=>{
    if(el.matches('input[type="submit"],input[type="button"]'))return norm(el.value);
    const parts=[];
    const visit=node=>{
      if(node.nodeType===Node.TEXT_NODE){parts.push(node.nodeValue);return;}
      if(node.nodeType!==Node.ELEMENT_NODE)return;
      if(node.matches(searchIcons+',script,style,[hidden],[aria-hidden="true"]'))return;
      const style=getComputedStyle(node);
      if(style.display==='none'||style.visibility==='hidden'||style.visibility==='collapse')return;
      for(const child of node.childNodes)visit(child);
    };
    visit(el);return norm(parts.join(''));
  };
  const actionLabel=el=>searchText(el)||norm(el.getAttribute('aria-label'));
  const searchButton=(field,input)=>{
    if(!field.isConnected||!input.isConnected)fail("文献检索区域已变化，请重新核验页面");
    const short=['search','检索','搜索','檢索','搜尋'];
    const accessible=[...short,'search documents','search publications','检索文献','搜索文献','文献检索','檢索文獻'];
    const isAction=el=>{
      if(el.closest('nav,header,footer,aside,[role="navigation"],[role="banner"],[hidden],[inert],[aria-hidden="true"]'))return false;
      const label=searchText(el).toLowerCase();
      // Never let an aria label turn a visible Delete/Clear/history action into Search.
      if(label)return short.includes(label);
      const aria=norm(el.getAttribute('aria-label')).toLowerCase();
      if(aria)return accessible.includes(aria);
      const ids=norm(el.getAttribute('aria-labelledby')).split(' ').filter(Boolean);
      return accessible.includes(norm(ids.map(id=>document.getElementById(id)?.textContent||'').join(' ')).toLowerCase());
    };
    const selector='button,[role="button"],input[type="submit"],input[type="button"]';
    const form=input.form||field.closest('form');
    let root=form&&form.contains(field)&&form.contains(input)?form:field.parentElement;
    while(root&&!root.contains(input))root=root.parentElement;
    if(!root)fail("无法确认文献检索区域，请检查工作页");
    const boundary=root.closest('[role="tabpanel"],main,[role="main"]')||document.body;
    // A button may be a sibling of the condition form, or use HTML's explicit
    // form= association. Other forms and navigation links are never borrowed.
    const associated=form?all(selector).filter(el=>el.form===form):[];
    while(root){
      const matches=[...new Set([...all(selector,root),...associated])].filter(el=>{
        const owner=el.form||el.closest('form');
        return (!owner||owner===form)&&isAction(el);
      });
      const actions=matches.filter(el=>!matches.some(other=>other!==el&&el.contains(other)));
      if(actions.length){
        if(actions.length!==1)fail(`文献检索按钮未唯一识别（当前检索区域识别到 ${actions.length} 个）。未点击检索按钮；请点扩展“检查工作页”复制按钮诊断`);
        return actions[0];
      }
      if(root===boundary)break;
      root=root.parentElement;
    }
    fail("文献检索按钮未唯一识别（当前检索区域识别到 0 个）。未点击检索按钮；请点扩展“检查工作页”复制按钮诊断");
  };
  const click=el=>{check();if(el.disabled||el.getAttribute("aria-disabled")==="true")fail("控件尚不可用");el.click();};
  const set=(el,value)=>{
    const proto=el.tagName==="TEXTAREA"?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto,"value").set.call(el,value);
    el.dispatchEvent(new Event("input",{bubbles:true}));el.dispatchEvent(new Event("change",{bubbles:true}));
  };
  const decodedPath = value => {try{return /%(?:2f|5c)/i.test(value)?"":decodeURIComponent(value);}catch{return "";}};
  const fullRecordPath = value => /^\/wos\/woscc\/full-record\/WOS:\d{15}\/?$/.test(decodedPath(value));
  const fullRecord = () => fullRecordPath(location.pathname);
  // Some WOS title wrappers use display:contents: the anchor has no own box,
  // while its child text is rendered and clickable. Do not widen visibility for
  // search/export controls; only this read-only record-link reader needs it.
  const renderedLink=el=>{
    if(!el || el.closest('[hidden],[inert],[aria-hidden="true"]'))return false;
    const style=getComputedStyle(el);
    if(style.display==='none'||style.visibility==='hidden'||style.visibility==='collapse')return false;
    if(el.getClientRects().length)return true;
    return style.display==='contents'&&[...el.querySelectorAll('*')].some(child=>
      !child.closest('[hidden],[inert],[aria-hidden="true"]')&&visible(child));
  };
  const recordLinks=()=>{
    const links=new Map();
    const controls=document.querySelectorAll('a[href],a[routerlink],a[ng-reflect-router-link],[role="link"][routerlink],[role="link"][ng-reflect-router-link]');
    for(const anchor of controls){
      if(!renderedLink(anchor))continue;
      // Use only explicit route evidence already present in the DOM. Never
      // construct a record URL from title text, IDs in arbitrary attributes,
      // onclick code, or the first result. Different targets remain ambiguous.
      for(const attr of ['href','routerlink','ng-reflect-router-link']){
        const value=anchor.getAttribute(attr);
        if(!value)continue;
        try{
          const url=new URL(value,location.href);
          if(url.origin!==location.origin||url.username||url.password||!fullRecordPath(url.pathname))continue;
          const path=decodedPath(url.pathname).replace(/\/$/,'');
          const target=url.origin+path;
          if(!links.has(target))links.set(target,{element:anchor,url:target});
        }catch{}
      }
    }
    return links;
  };
  const resultTotal=()=>{
    // Result headers/tabs can render <b>1</b><span>Documents</span> with a CSS
    // gap, but innerText contains "1Documents". Parse the exact count+unit
    // label, not a whitespace requirement or the number of rendered cards.
    const unit='(?:results?|records?|documents?|(?:条|個|个|篇)?\\s*(?:结果|結果|记录|紀錄|文献|文獻))';
    const number='(?:\\d{1,3}(?:[,.]\\d{3})+|\\d{1,6})';
    const exact=new RegExp('^('+number+')\\s*'+unit+'$','i');
    const totals=new Set();
    const add=match=>{if(match)totals.add(Number(match[1].replace(/[,.]/g,'')));};
    for(const el of [...document.querySelectorAll('h1,h2,h3,[role="heading"],[role="tab"]')].filter(renderedLink)){
      if(!el.closest('article,form,a[href*="full-record"],[hidden],[inert],[aria-hidden="true"]'))add(norm(el.innerText).match(exact));
    }
    if(!totals.size&&document.body){
      // Plain containers are also used for counts. Inspect only short, exact
      // labels and their two parents; a phrase inside a paper title, search
      // history, or help paragraph must not become a result total.
      const unitOnly=new RegExp('^'+unit+'$','i');
      const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
      for(let node=walker.nextNode();node;node=walker.nextNode()){
        const text=norm(node.nodeValue);
        if(!exact.test(text)&&!unitOnly.test(text))continue;
        let el=node.parentElement;
        for(let depth=0;el&&depth<3;depth++,el=el.parentElement){
          if(el.closest('article,form,a,[role="link"],nav,aside,[hidden],[inert],[aria-hidden="true"]'))break;
          if(renderedLink(el))add(norm(el.innerText).match(exact));
        }
      }
    }
    return {value:totals.size===1?[...totals][0]:null,conflict:totals.size>1,values:[...totals]};
  };
  // A zero-result search stays on basic-search in the current WOS SPA. It is a
  // completed per-paper outcome, not a navigation/connection timeout. Keep the
  // phrases narrow so help text and search-history labels cannot become results.
  // WOS renders parts of the Chinese message in separate elements. `norm()`
  // therefore leaves spaces between words that look contiguous on screen.
  // Allow only whitespace between the known phrase fragments; do not use a
  // broad "0" match that could mistake search history or help text for a result.
  const noResultPattern=/(?:no\s+(?:results?|records?|documents?)\s+(?:were\s+)?found|your\s+search\s+(?:did\s+not\s+(?:return|find)\s+any|returned\s+no)\s+results?|您的?\s*(?:检索|搜索|檢索|搜尋)\s*(?:未找到|没有找到|沒有找到|未檢索到)\s*(?:任何)?\s*(?:结果|結果)|未找到\s*(?:任何)?\s*(?:结果|結果)|没有\s*(?:检索|搜索)\s*结果|沒有\s*(?:檢索|搜尋)\s*結果)/i;
  const noResults=()=>noResultPattern.test(norm(document.body?.innerText||""));
  const noResultTextNodes=()=>{
    const found=[];
    if(!document.body)return found;
    const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
    for(let node=walker.nextNode();node;node=walker.nextNode())
      if(visible(node.parentElement)&&noResultPattern.test(norm(node.nodeValue)))found.push(node);
    return found;
  };
  const fingerprint = () => {
    if(!fullRecord())fail("未处于 WOS 核心合集单篇完整记录页");
    const ut=decodedPath(location.pathname).match(/WOS:\d{15}/)[0];
    if(command.wos && command.wos!==ut)fail("WOS 页面入藏号与名单不一致");
    return location.origin+location.pathname;
  };
  const resultState = () => {
    const urls=recordLinks();
    const summary=/^\/wos\/woscc\/summary\//.test(location.pathname);
    const total=summary?resultTotal():{value:null,conflict:false,values:[]};
    const busy=all('[aria-busy="true"],[role="progressbar"],mat-spinner,mat-progress-bar,.mat-mdc-progress-spinner')
      .some(el=>!el.closest('[hidden],[inert],[aria-hidden="true"]'));
    const diagnostic={summary_route:summary,record_route:fullRecord(),busy,result_total:total.value,result_total_conflict:total.conflict,
      canonical_record_link_count:urls.size};
    if(fullRecord()){
      // A new URL alone does not prove the record's DOM has mounted. Avoid
      // reporting success while the old results/search panel is still rendered.
      const exports=all('button,[role="button"],a').filter(el=>["Export","导出"].includes(actionLabel(el)));
      diagnostic.export_action_count=exports.length;
      if(busy||exports.length!==1)return {state:"loading",diagnostic};
      return {state:"record",record_url:fingerprint(),diagnostic};
    }
    if(noResults())return {state:"zero",diagnostic};
    if(!summary||busy)return {state:"loading",diagnostic};
    if(total.values.some(count=>count>1)||urls.size>1)return {state:"multiple",diagnostic};
    if(!total.conflict&&total.value===0&&urls.size===0)return {state:"zero",diagnostic};
    if(!total.conflict&&total.value===1&&urls.size===1){
      return {state:"single",navigate_url:[...urls.values()][0].url,diagnostic};
    }
    return {state:"loading",diagnostic};
  };
  try {
    check();
    if(!["wos_search","wos_start_search","wos_read_results","wos_verify_record","wos_prepare_export","wos_check_export","wos_download"].includes(command.action))fail("未知 WOS 命令");
    if(typeof command.title!=="string" || !command.title.trim() || command.title.length>1500)fail("题名缺失或过长");
    if(command.action==="wos_read_results") {
      if(all('[role="dialog"],mat-dialog-container').length)fail("WOS 有弹窗，请人工处理");
      return {ok:true,data:resultState()};
    }
    if(command.action==="wos_verify_record")return {ok:true,data:{state:"record",record_url:fingerprint()}};
    if(command.action==="wos_search" || command.action==="wos_start_search") {
      if(!/\/(?:basic-search|advanced-search|fielded-search)\/?$/.test(location.pathname))fail("请先进入 WOS 核心合集的字段检索页");
      if(all('[role="dialog"],mat-dialog-container').length)fail("WOS 有弹窗，请人工处理");
      // WOS can keep the exact same no-result node for the next query. The
      // dispatcher treats this pre-click marker as a request to reload a clean
      // search page, then invokes this command again. No Search click has occurred,
      // so the real query is still submitted at most once.
      if(noResults())fail("WOS 检索页保留上一条零结果，需刷新检索页");
      const choices=command.wos?["Accession Number","入藏号","入藏號"]:command.doi?["DOI"]:["Title","标题","题名","標題","題名"];
      const query=command.wos||command.doi||command.title;
      // New WOS defaults to Smart Search. Follow only visible, specifically
      // labelled links to Advanced -> Fielded Search; never invent route URLs
      // or change the account's Smart Search preference.
      const fieldedLabels=["Fielded Search","字段检索","字段搜索","欄位檢索"];
      const advancedLabels=["Advanced Search","高级检索","高级搜索","進階檢索"];
      const navigation=[...fieldedLabels,...advancedLabels];
      const navigationControls=()=>all('a,button,[role="tab"]').filter(el=>navigation.includes(caption(el)));
      // Chrome may report the tab as complete before the WOS SPA mounts its search
      // controls. Wait for semantic controls instead of treating an empty first
      // render as a permanent selector mismatch.
      await wait(()=>fields().length>0 || navigationControls().length>0,"加载字段检索页面",15000);
      for(let step=0;fields().length===0 && step<3;step++) {
        const controls=navigationControls();
        const selectedFielded=controls.filter(el=>fieldedLabels.includes(caption(el)) && el.getAttribute("aria-selected")==="true");
        if(selectedFielded.length===1) {
          await wait(()=>fields().length>0,"加载字段检索条件",15000);
          break;
        }
        const fielded=controls.filter(el=>fieldedLabels.includes(caption(el)) && el.getAttribute("aria-selected")!=="true");
        const advanced=controls.filter(el=>advancedLabels.includes(caption(el)) && el.getAttribute("aria-selected")!=="true");
        const next=fielded.length?fielded:advanced;
        if(next.length!==1)break;
        const clicked=next[0];click(clicked);
        await wait(()=>fields().length>0 || !clicked.isConnected || clicked.getAttribute("aria-selected")==="true",
                   "切换字段检索",15000);
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
      // Only the uniquely identified single-row document query is controlled.
      const scope=field.closest("form") || document;
      const inputs=all('input:not([type]),input[type="text"],input[type="search"],textarea',scope).filter(e=>!e.readOnly&&!e.disabled);
      const input=one(inputs,"单行文献检索输入框");
      searchButton(field,input); // Ambiguity stops before replacing the query.
      set(input,query);
      const action=searchButton(field,input); // Input events may replace the button.
      if(action.disabled||action.getAttribute("aria-disabled")==="true")fail("控件尚不可用");
      if(command.action==="wos_start_search") {
        // Return before the click can navigate. A real WOS navigation may destroy
        // an injected execution context; keeping that navigation inside this long
        // command was the source of desktop timeouts and a permanently busy worker.
        // The extension background performs the subsequent read-only polling.
        setTimeout(()=>action.click(),0);
        return {ok:true,data:{submitted:true}};
      }
      const previous=location.href;
      // A previous zero-result banner may still be mounted when the next query is
      // entered. Only accept a banner that is new or changed after this click.
      const zeroBefore=noResults(), oldZeroNodes=new Set(noResultTextNodes());
      let zeroChanged=false;
      const observer=zeroBefore?new MutationObserver(records=>{
        for(const change of records){
          if(change.type==="characterData"&&oldZeroNodes.has(change.target)){zeroChanged=true;break;}
          if(change.type==="attributes"&&[...oldZeroNodes].some(node=>node.parentElement===change.target)){zeroChanged=true;break;}
          const changed=[...(change.addedNodes||[]),...(change.removedNodes||[])];
          if(changed.some(node=>noResultPattern.test(norm(node.textContent||node.nodeValue||"")) ||
              [...oldZeroNodes].some(old=>node===old||(node.nodeType===Node.ELEMENT_NODE&&node.contains(old))))) {
            zeroChanged=true;break;
          }
        }
      }):null;
      if(observer)observer.observe(document.body,{subtree:true,childList:true,characterData:true,attributes:true,attributeFilter:["hidden","aria-hidden","class","style"]});
      const freshZero=()=>noResults()&&(!zeroBefore||zeroChanged);
      try {
        click(action);
        await wait(()=>location.href!==previous||fullRecord()||recordLinks().size||freshZero(),"WOS 检索结果");
        await wait(()=>fullRecord()||recordLinks().size||freshZero(),"加载结果",60000);
      } finally {if(observer)observer.disconnect();}
      if(freshZero())fail("WOS 未找到记录；这不等于未发表，也不自动标记完成");
      if(fullRecord())return {ok:true,data:{record_url:fingerprint()}};
      // Legacy direct-adapter callers share the production result evidence.
      // Full total must be one, not simply one rendered/visible match.
      if(resultState().state!=="single")fail("WOS 结果不是可确认的唯一记录，请人工选择并核对后使用“只下载 WOS 当前打开的论文”");
      click([...recordLinks().values()][0].element);await wait(fullRecord,"打开单篇记录");
      return {ok:true,data:{record_url:fingerprint()}};
    }
    const recordURL=fingerprint();
    if(command.action==="wos_prepare_export") {
      if(all('[role="dialog"],mat-dialog-container').length)fail("已有 WOS 弹窗，请人工关闭后再导出");
      click(button(["Export","导出"]));
      await wait(()=>all('[role="menuitem"],button,a,mat-option').some(el=>["Tab delimited file","Tab-delimited file","Tab delimited","制表符分隔文件","制表符分隔","制表符"].includes(actionLabel(el))),"导出格式",5000);
      click(one(all('[role="menuitem"],button,a,mat-option').filter(el=>["Tab delimited file","Tab-delimited file","Tab delimited","制表符分隔文件","制表符分隔","制表符"].includes(actionLabel(el))),"Tab delimited"));
      await wait(()=>all('mat-dialog-container,[role="dialog"]').length,"导出设置",5000);
      const dialogs=all('mat-dialog-container,[role="dialog"]');
      // WOS now uses a nested role=dialog around Record Content alone. Choose
      // the smallest complete export panel, not the smallest arbitrary dialog.
      const panels=dialogs.filter(el=>
        all('select,[role="combobox"]',el).length===1 &&
        all('button,[role="button"],a',el).filter(control=>["Export","导出"].includes(actionLabel(control))).length===1);
      const dialog=one(panels.filter(el=>!panels.some(other=>other!==el&&el.contains(other))),"导出设置窗口");
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
    // Browser Skill captures the one download from its freshly observed final
    // button. This guard checks the same preview without submitting it again.
    if(command.action==="wos_check_export")return {ok:true,data:{ready:true,record_url:recordURL}};
    state.submitted=true;
    click(button(["Export","导出"],state.dialog));
    return {ok:true,data:{submitted:true,record_url:recordURL}};
  } catch(error){return {ok:false,error:"[WOS 已暂停] "+error.message};}
}
if(typeof module!=="undefined")module.exports={runWOSCommand};
