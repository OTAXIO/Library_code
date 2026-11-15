document.getElementById("pair").addEventListener("click", async () => {
  const status = document.getElementById("status");
  try {
    const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
    const url = new URL(tab?.url || "about:blank");
    if (!["http:", "https:"].includes(url.protocol) || url.hostname !== "admin.ir.lib.sjtu.edu.cn" ||
        !["#/dataCompare/list", "#/saPatentCompare/list"].includes(url.hash.split("?")[0]))
      throw new Error("请先切换到 SA 比对结果页或专利比对页，再点“连接 SA 比对页”。");
    const result = await chrome.runtime.sendMessage({type: "pair", tabId: tab?.id,
      token: document.getElementById("token").value.trim()});
    status.textContent = result.ok ? "已连接。请回桌面开始认领。" : result.error;
    if (result.ok) document.getElementById("token").value = "";
  } catch (error) { status.textContent = error.message; }
});
document.getElementById("disconnect").addEventListener("click", async () => {
  const result = await chrome.runtime.sendMessage({type: "disconnect"});
  document.getElementById("status").textContent = result.ok ? "已断开。已发出的请求不能撤回，请核验页面结果。" : result.error;
});
document.getElementById("inspect").addEventListener("click",async()=>{
    const status=document.getElementById("status");
    try{
      const [tab]=await chrome.tabs.query({active:true,currentWindow:true});
      const result=await chrome.runtime.sendMessage({type:"inspect_workflow",tabId:tab?.id});
      if(!result.ok){status.textContent=result.error;return;}
      if(result.data){
        const box=document.getElementById("diagnostics");box.hidden=false;box.value=JSON.stringify(result.data,null,2);
        box.style.width="100%";
        status.textContent="只读检查完成。下方内容可选中复制；不含检索框输入、账号或 Cookie。";
      }else status.textContent=result.message;
    }catch(error){status.textContent=error.message;}
  });
