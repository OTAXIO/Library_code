/* User-clicked read-only diagnosis. Never reads query input values, cookies, storage,
 * full HTML, or arbitrary URLs, and never transmits diagnostics to a server. */
function inspectWorkPage() {
  const u=new URL(location.href);
  const wos=["https://www.webofscience.com","https://webofscience.clarivate.cn"].includes(u.origin) && u.pathname.startsWith("/wos/");
  const admin=["http:","https:"].includes(u.protocol) && u.hostname==="admin.ir.lib.sjtu.edu.cn";
  if((!wos && !admin) || u.username || u.password)return {error:"非工作网站"};
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
  return {site:u.hostname,path:u.pathname,route:u.hash.split("?")[0],
    wos_error:/Oops,?\s*something went wrong!?/i.test(text),
    smart_search:/Smart Search|智能检索|智能搜索/i.test(text),
    fielded_search:/Fielded Search|字段检索|字段搜索/i.test(text),
    wos_import_button:/WOS\s*数据导入\s*[（(]\s*Txt\s*[）)]/i.test(text),
    controls,buttons,visible_button_count:buttonElements.length};
}
if(typeof module!=="undefined")module.exports={inspectWorkPage};
