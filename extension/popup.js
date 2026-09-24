document.getElementById("pair").addEventListener("click", async () => {
  const status = document.getElementById("status");
  try {
    const [tab] = await chrome.tabs.query({active: true, currentWindow: true});
    const result = await chrome.runtime.sendMessage({type: "pair", tabId: tab?.id,
      token: document.getElementById("token").value.trim()});
    status.textContent = result.ok ? "已连接。回到桌面弹窗选择负责人并开始。" : result.error;
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
      status.textContent = result.ok ? "工作页已绑定。请保持标签页打开，回桌面操作。" : result.error;
    } catch (error) { status.textContent = error.message; }
  });
}
