document.getElementById("pair").addEventListener("click", async () => {
  const status = document.getElementById("status");
  try {
    const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
    const url = new URL(tab?.url || "about:blank");
    if (!["http:", "https:"].includes(url.protocol) || url.hostname !== "admin.ir.lib.sjtu.edu.cn" ||
        url.hash.split("?")[0] !== "#/dataCompare/list")
      throw new Error("请先切换到 SA 数据比对 → 比对结果页，再点“连接 SA 比对页”。WOS 和导入页使用下方绑定按钮。");
    const result = await chrome.runtime.sendMessage({type: "pair", tabId: tab?.id,
      token: document.getElementById("token").value.trim()});
    status.textContent = result.ok ? "已连接。自动认领可直接回桌面开始；自动导入请再绑定 WOS 页和后台导入页。" : result.error;
    if (result.ok) document.getElementById("token").value = "";
  } catch (error) { status.textContent = error.message; }
});
document.getElementById("disconnect").addEventListener("click", async () => {
  const result = await chrome.runtime.sendMessage({type: "disconnect"});
  document.getElementById("status").textContent = result.ok ? "已断开。已发出的请求不能撤回，请核验页面结果。" : result.error;
});
for (const [id, role] of [["wos", "wosTabId"], ["import", "importTabId"]]) {
  document.getElementById(id).addEventListener("click", async () => {
    const status = document.getElementById("status");
    try {
      const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
      const result = await chrome.runtime.sendMessage({type: "bind_workflow", role, tabId: tab?.id});
      status.textContent = result.ok ? (role === "wosTabId"
        ? "WOS 页已绑定。再绑定后台导入页，然后在桌面开始自动导入。"
        : "导入页已绑定。三个网页保持打开，在桌面开始自动导入。") : result.error;
    } catch (error) { status.textContent = error.message; }
  });
}
for(const [id,type] of [["open-import","open_import"],["inspect","inspect_workflow"]]) {
  document.getElementById(id).addEventListener("click",async()=>{
    const status=document.getElementById("status");
    try{
      const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
      const result=await chrome.runtime.sendMessage({type,tabId:tab?.id});
      if(!result.ok){status.textContent=result.error;return;}
      if(result.data){
        const box=document.getElementById("diagnostics");box.hidden=false;box.value=JSON.stringify(result.data,null,2);
        box.style.width="100%";
        status.textContent=result.data.wos_error?"WOS 自身错误页：请先点网页顶部 Search 恢复，再继续。下方诊断可复制反馈。":"只读检查完成。下方内容可选中复制；不含检索框输入、账号或 Cookie。";
      }else status.textContent=result.message;
    }catch(error){status.textContent=error.message;}
  });
}
