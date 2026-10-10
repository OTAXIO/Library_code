/* Shared read-only diagnosis/result probe. Never reads input values, cookies or
 * storage. Manual diagnosis exposes counts only. The authenticated dispatcher
 * may receive one strictly validated DOM record URL for navigation, never HTML. */
function inspectWorkPage(command) {
  const probing=command?.action==='wos_read_results';
  const fail=message=>probing?{ok:false,error:'[WOS 已暂停] '+message}:{error:message};
  const u=new URL(location.href);
  const wos=["https://www.webofscience.com","https://webofscience.clarivate.cn"].includes(u.origin) && u.pathname.startsWith("/wos/");
  const admin=["http:","https:"].includes(u.protocol) && u.hostname==="admin.ir.lib.sjtu.edu.cn";
  if((!wos && !admin) || u.username || u.password)return fail('非工作网站');
  if(probing&&(!wos||!u.pathname.startsWith('/wos/woscc/')))return fail('请在 WOS 核心合集的文献页面操作');
  if(probing&&(!Number.isFinite(command.expires)||Date.now()>=command.expires-12000))return fail('WOS 只读检查已超时，未重复检索');
  const visible=el=>el.getClientRects().length>0&&getComputedStyle(el).visibility!=="hidden";
  const text=String(document.body?.innerText||"");
  const normal=s=>String(s||"").replace(/\s+/g," ").trim().slice(0,120);
  const controls=[...document.querySelectorAll('[role="combobox"],select,[aria-haspopup="listbox"]')].filter(visible).slice(0,15).map(el=>({
    tag:el.tagName,role:el.getAttribute("role")||"",label:normal(el.getAttribute("aria-label")),
    // Input text can contain a user's query. Never include it in diagnostics.
    display:["INPUT","TEXTAREA"].includes(el.tagName)?"[输入内容省略]":normal(el.tagName==="SELECT"?el.selectedOptions[0]?.textContent:el.innerText)}));
  const icons='mat-icon,.mat-icon,svg,.material-icons,.material-icons-outlined,.material-symbols-outlined,.material-symbols-rounded,.material-symbols-sharp';
  const cleanText=el=>{
    if(el.matches('input[type="submit"],input[type="button"]'))return normal(el.value);
    const parts=[];
    const visit=node=>{
      if(node.nodeType===Node.TEXT_NODE){parts.push(node.nodeValue);return;}
      if(node.nodeType!==Node.ELEMENT_NODE||node.matches(icons+',script,style,[hidden],[aria-hidden="true"]'))return;
      const style=getComputedStyle(node);
      if(style.display==='none'||style.visibility==='hidden'||style.visibility==='collapse')return;
      for(const child of node.childNodes)visit(child);
    };
    visit(el);return normal(parts.join(''));
  };
  // Button text can also contain an account name or query. Return only fixed
  // UI labels; arbitrary text and aria-label values are deliberately redacted.
  const fixed=['search','检索','搜索','檢索','搜尋','search documents','search publications','检索文献','搜索文献','文献检索','檢索文獻','clear','清除'];
  const safeLabel=value=>{const label=normal(value);return fixed.includes(label.toLowerCase())?label:label?'[非检索文案省略]':'';};
  const buttonElements=[...document.querySelectorAll('button,[role="button"],input[type="submit"],input[type="button"]')].filter(visible);
  const buttons=buttonElements.slice(0,40).map(el=>({tag:el.tagName,
    display:safeLabel(cleanText(el)),aria_label:safeLabel(el.getAttribute('aria-label')),
    has_labelledby:el.hasAttribute('aria-labelledby'),icon_count:el.querySelectorAll(icons).length,
    in_navigation:!!el.closest('nav,header,footer,aside,[role="navigation"],[role="banner"]'),
    in_form:!!el.closest('form'),form_associated:!!el.form,
    disabled:!!el.disabled||el.getAttribute('aria-disabled')==='true'}));
  // Production navigation and user diagnosis now use this exact same reader,
  // not two copies that can disagree after page or extension changes.
  const renderedLink=el=>{
    if(el.closest('[hidden],[inert],[aria-hidden="true"]'))return false;
    const style=getComputedStyle(el);
    if(style.display==='none'||style.visibility==='hidden'||style.visibility==='collapse')return false;
    if(el.getClientRects().length)return true;
    return style.display==='contents'&&[...el.querySelectorAll('*')].some(child=>
      !child.closest('[hidden],[inert],[aria-hidden="true"]')&&visible(child));
  };
  const recordLinks=new Set(),encodedLinks=new Set(),contentsLinks=new Set(),routerLinks=new Set();
  const linkControls=document.querySelectorAll('a[href],a[routerlink],a[ng-reflect-router-link],[role="link"][routerlink],[role="link"][ng-reflect-router-link]');
  for(const anchor of linkControls){
    if(!renderedLink(anchor))continue;
    for(const attr of ['href','routerlink','ng-reflect-router-link']){
      const value=anchor.getAttribute(attr);
      if(!value)continue;
      try{
        const link=new URL(value,location.href);
        if(link.origin!==u.origin||link.username||link.password||/%(?:2f|5c)/i.test(link.pathname))continue;
        const path=decodeURIComponent(link.pathname);
        if(!/^\/wos\/woscc\/full-record\/WOS:\d{15}\/?$/.test(path))continue;
        const key=path.replace(/\/$/,'');
        recordLinks.add(key);
        if(/%3a/i.test(link.pathname))encodedLinks.add(key);
        if(!anchor.getClientRects().length)contentsLinks.add(key);
        if(attr!=='href')routerLinks.add(key);
      }catch{}
    }
  }
  const summary=/^\/wos\/woscc\/summary\//.test(u.pathname),totals=new Set();
  if(wos&&summary){
    const unit='(?:results?|records?|documents?|(?:条|個|个|篇)?\\s*(?:结果|結果|记录|紀錄|文献|文獻))';
    const number='(?:\\d{1,3}(?:[,.]\\d{3})+|\\d{1,6})';
    const exact=new RegExp('^('+number+')\\s*'+unit+'$','i');
    const add=match=>{if(match)totals.add(Number(match[1].replace(/[,.]/g,'')));};
    for(const el of [...document.querySelectorAll('h1,h2,h3,[role="heading"],[role="tab"]')].filter(renderedLink)){
      if(!el.closest('article,form,a[href*="full-record"],[hidden],[inert],[aria-hidden="true"]'))add(normal(String(el.innerText||'').normalize('NFKC')).match(exact));
    }
    if(!totals.size&&document.body){
      const unitOnly=new RegExp('^'+unit+'$','i');
      const walker=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);
      for(let node=walker.nextNode();node;node=walker.nextNode()){
        const label=normal(String(node.nodeValue||'').normalize('NFKC'));
        if(!exact.test(label)&&!unitOnly.test(label))continue;
        let el=node.parentElement;
        for(let depth=0;el&&depth<3;depth++,el=el.parentElement){
          if(el.closest('article,form,a,[role="link"],nav,aside,[hidden],[inert],[aria-hidden="true"]'))break;
          if(renderedLink(el))add(normal(String(el.innerText||'').normalize('NFKC')).match(exact));
        }
      }
    }
  }
  const noResult=/(?:no\s+(?:results?|records?|documents?)\s+(?:were\s+)?found|your\s+search\s+(?:did\s+not\s+(?:return|find)\s+any|(?:returned|found)\s+no)\s+results?|您的?\s*(?:检索|搜索|檢索|搜尋)\s*(?:未找到|没有找到|沒有找到|未檢索到)\s*(?:任何)?\s*(?:结果|結果)|未找到\s*(?:任何)?\s*(?:结果|結果)|没有\s*(?:检索|搜索)\s*结果|沒有\s*(?:檢索|搜尋)\s*結果)/i.test(text);
  const busy=[...document.querySelectorAll('[aria-busy="true"],[role="progressbar"],mat-spinner,mat-progress-bar,.mat-mdc-progress-spinner')]
    .some(el=>visible(el)&&!el.closest('[hidden],[inert],[aria-hidden="true"]'));
  // Readiness is only a coarse, read-only startup check. Never read a query,
  // account name or input value, and leave ambiguous controls to the adapter.
  const queryInputs=[...document.querySelectorAll('input:not([type]),input[type="text"],input[type="search"],textarea')]
    .filter(el=>visible(el)&&!el.closest('nav,header,footer,aside,[role="navigation"],[role="banner"],[hidden],[inert],[aria-hidden="true"]'));
  // A reCAPTCHA badge/hidden anchor is not an active verification challenge.
  // Do not read account fields; report only the kind of visible access gate.
  const challenge=[...document.querySelectorAll('#challenge-form,#cf-challenge-running,iframe[src*="captcha"],iframe[src*="challenge"]')]
    .some(el=>visible(el)&&!el.closest('.grecaptcha-badge,[hidden],[inert],[aria-hidden="true"]')&&
      (el.tagName!=="IFRAME"||el.getBoundingClientRect().height>=70));
  const login=[...document.querySelectorAll('input[type="password"]')].some(el=>visible(el)&&
    !el.closest('[hidden],[inert],[aria-hidden="true"]'));
  const gateKind=challenge?'verification':login?'login':'';
  const loginRequired=Boolean(gateKind);
  const dialogCount=[...document.querySelectorAll('[role="dialog"],mat-dialog-container')]
    .filter(el=>visible(el)&&!el.closest('[hidden],[inert],[aria-hidden="true"]')).length;
  let recordRoute=false;
  try{recordRoute=!/%(?:2f|5c)/i.test(u.pathname)&&/^\/wos\/woscc\/full-record\/WOS:\d{15}\/?$/.test(decodeURIComponent(u.pathname));}catch{}
  const dialogs=[...document.querySelectorAll('[role="dialog"],mat-dialog-container')].filter(visible);
  const exportHeadings=['Export Records to Tab Delimited File','将记录导出到制表符分隔文件','导出记录至制表符分隔文件',
    '导出记录到制表符分隔文件','导出记录到制表符分隔的文件'];
  const exportPanels=dialogs.filter(panel=>{
    const selectors=[...panel.querySelectorAll('select,[role="combobox"],[aria-haspopup="listbox"]')].filter(visible);
    const unique=selectors.filter(el=>!selectors.some(other=>other!==el&&el.contains(other)));
    const actions=[...panel.querySelectorAll('button,[role="button"],a')].filter(el=>visible(el)&&
      ['Export','导出'].includes(cleanText(el)||normal(el.getAttribute('aria-label'))));
    return unique.length===1&&actions.length===1&&[...panel.querySelectorAll('h1,h2,h3,[role="heading"],.mat-dialog-title,.mat-mdc-dialog-title')]
      .some(el=>visible(el)&&exportHeadings.includes(cleanText(el)));
  }).filter(panel=>!dialogs.some(other=>other!==panel&&panel.contains(other)&&
      [...other.querySelectorAll('h1,h2,h3,[role="heading"]')].some(el=>exportHeadings.includes(cleanText(el)))));
  const exportDialog=recordRoute&&exportPanels.length===1&&dialogs.every(el=>el.contains(exportPanels[0])||exportPanels[0].contains(el));
  const errorHeadings=[document.title,...[...document.querySelectorAll('h1,h2,[role="alert"]')]
    .filter(visible).map(el=>el.innerText)].map(normal);
  const siteTimeout=errorHeadings.some(label=>/\b(?:Error\s+(?:code\s*)?|HTTP\s+)(?:500|502|503|504|520|521|522|523|524)\b|\b524\s*:\s*A timeout occurred\b/i.test(label)
    || /^(?:A timeout occurred|Connection timed out|Bad gateway|Web server is down)$/i.test(label));
  const data={site:u.hostname,path:u.pathname,route:u.hash.split("?")[0],
    wos_error:/Oops,?\s*something went wrong!?/i.test(text),
    site_timeout:siteTimeout,
    core_search_route:/^\/wos\/woscc\/(?:basic-search|advanced-search|fielded-search)\/?$/.test(u.pathname),
    query_input_count:queryInputs.length,login_required:loginRequired,access_gate:gateKind,dialog_count:dialogCount,export_dialog:exportDialog,
    summary_route:summary,record_route:recordRoute,
    zero_result:noResult||(summary&&totals.size===1&&totals.has(0)&&!recordLinks.size&&!busy),busy,
    result_total:totals.size===1?[...totals][0]:null,result_total_conflict:totals.size>1,
    canonical_record_link_count:recordLinks.size,encoded_record_link_count:encodedLinks.size,
    contents_record_link_count:contentsLinks.size,router_record_link_count:routerLinks.size,
    smart_search:/Smart Search|智能检索|智能搜索/i.test(text),
    fielded_search:/Fielded Search|字段检索|字段搜索/i.test(text),
    wos_import_button:/WOS\s*数据导入\s*[（(]\s*Txt\s*[）)]/i.test(text),
    controls,buttons,visible_button_count:buttonElements.length};
  if(!probing)return data;
  if(data.site_timeout)return fail('WOS 网站返回 5xx/连接超时页；不是文献零结果，请恢复网页后再继续');
  if(data.wos_error)return fail('WOS 网站报错：Oops, something went wrong! 请先恢复机构访问或检索页面');
  if(loginRequired)
    return fail(gateKind==='verification'?'WOS 显示人工验证，请完成验证后继续；未查询当前论文':'WOS 显示登录表单，请完成登录后继续；未查询当前论文');
  if(dialogCount)
    return fail('WOS 有弹窗，请人工处理');
  const diagnostic={summary_route:summary,record_route:recordRoute,busy,
    blank_record:recordRoute&&!busy&&!text.trim(),
    result_total:data.result_total,result_total_conflict:data.result_total_conflict,
    canonical_record_link_count:recordLinks.size};
  const result=(state,more={})=>({ok:true,data:{state,diagnostic,...more}});
  if(recordRoute){
    const ut=decodeURIComponent(u.pathname).match(/WOS:\d{15}/)[0];
    if(command.wos&&command.wos!==ut)return fail('WOS 页面入藏号与名单不一致');
    const exports=[...document.querySelectorAll('button,[role="button"],a')].filter(el=>
      visible(el)&&['Export','导出'].includes(cleanText(el)||normal(el.getAttribute('aria-label'))));
    diagnostic.export_action_count=exports.length;
    return busy||exports.length!==1?result('loading'):result('record',{record_url:u.origin+u.pathname});
  }
  if(noResult)return result('zero');
  if(!summary||busy)return result('loading');
  if([...totals].some(count=>count>1)||recordLinks.size>1)return result('multiple');
  if(data.zero_result)return result('zero');
  if(!data.result_total_conflict&&data.result_total===1&&recordLinks.size===1)
    return result('single',{navigate_url:u.origin+[...recordLinks][0]});
  return result('loading');
}
if(typeof module!=="undefined")module.exports={inspectWorkPage};
