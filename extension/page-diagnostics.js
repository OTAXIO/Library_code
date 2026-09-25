/* User-clicked read-only diagnosis. Never reads input values, cookies, storage,
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
  return {site:u.hostname,path:u.pathname,route:u.hash.split("?")[0],
    wos_error:/Oops,?\s*something went wrong!?/i.test(text),
    smart_search:/Smart Search|智能检索|智能搜索/.test(text),
    fielded_search:/Fielded Search|字段检索|字段搜索/.test(text),
    wos_import_button:/WOS\s*数据导入\s*[（(]\s*Txt\s*[）)]/i.test(text),
    controls};
}
if(typeof module!=="undefined")module.exports={inspectWorkPage};
