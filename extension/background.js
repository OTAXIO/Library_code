importScripts("adapter.js", "import-adapter.js", "wos-adapter.js", "workflow-background.js");
let polling = false;
let busy = false;
const trustedPopup = sender => sender.id === chrome.runtime.id && sender.url === chrome.runtime.getURL("popup.html");
const validPage = url => {
  try { const u = new URL(url); return ["http:", "https:"].includes(u.protocol) &&
    u.hostname === "admin.ir.lib.sjtu.edu.cn" && u.hash.split("?")[0] === "#/dataCompare/list"; }
  catch { return false; }
};
async function request(route, body, token) {
  const response = await fetch("http://127.0.0.1:8765" + route, {
    method: "POST", headers: {"Content-Type": "application/json", "Authorization": "Bearer " + token},
    body: JSON.stringify(body), signal: AbortSignal.timeout(8000), cache: "no-store"
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "桌面连接失败");
  return data;
}
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  (async () => {
    if (message.type === "pair") {
      if (!trustedPopup(sender)) throw new Error("只能由扩展弹窗配对");
      if (busy) throw new Error("正在执行命令，请完成后再配对");
      if (!/^[A-Za-z0-9_-]{43}$/.test(message.token || "")) throw new Error("配对码格式不正确");
      const tab = await chrome.tabs.get(message.tabId);
      if (!validPage(tab.url)) throw new Error("请先切换到 SA数据比对 → 比对结果 页面");
      await request("/poll", {client: String(tab.id), claimOnly: true}, message.token);
      await chrome.storage.session.clear();
      await chrome.storage.session.set({token: message.token, tabId: tab.id});
      await chrome.scripting.executeScript({target: {tabId: tab.id}, files: ["content.js"]});
      return {ok: true};
    }
    if (message.type === "bind_workflow") {
      if (!trustedPopup(sender)) throw new Error("只能由扩展弹窗绑定工作页");
      if (busy) throw new Error("正在执行命令，不能更换工作页");
      const pair = await chrome.storage.session.get(["token", "tabId"]);
      if (!pair.token) throw new Error("请先连接 SA 比对页");
      if (!["wosTabId", "importTabId"].includes(message.role)) throw new Error("未知工作页类型");
      const tab = await chrome.tabs.get(message.tabId);
      if (tab.id === pair.tabId || !validRolePage(tab.url, message.role))
        throw new Error("请在独立标签页打开对应工作页面，再点击绑定");
      await chrome.storage.session.set({[message.role]: tab.id});
      return {ok: true};
    }
    if (message.type === "disconnect") {
      if (!trustedPopup(sender)) throw new Error("只能由扩展弹窗断开");
      if (busy) throw new Error("命令执行中，请先在桌面等待结果；不能撤回已发出的请求");
      await chrome.storage.session.clear();
      return {ok: true};
    }
    if (message.type !== "tick" || polling) return {ok: true};
    const pair = await chrome.storage.session.get(["token", "tabId", "wosTabId", "importTabId"]);
    if (!pair.token || sender.tab?.id !== pair.tabId || !validPage(sender.tab.url)) return {ok: true};
    polling = true;
    let data;
    try { data = await request("/poll", {client: String(pair.tabId)}, pair.token); }
    finally { polling = false; }
    if (!data.command) return {ok: true};
    const command = data.command;
    let result;
    if (busy) result = {ok: false, error: "上一条浏览器命令仍在执行，暂停等待人工核验"};
    else {
      busy = true;
      try {
        const tab = await chrome.tabs.get(pair.tabId);
        if (!validPage(tab.url)) throw new Error("页面已切换，停止执行");
        if (workflowRole(command.action)) result = await dispatchWorkflow(command, pair);
        else {
          const outcomes = await chrome.scripting.executeScript({target: {tabId: pair.tabId},
            world: "MAIN", func: runSACommand, args: [command]});
          result = outcomes[0]?.result || {ok: false, error: "页面没有返回执行结果"};
        }
      } catch (error) { result = {ok: false, error: error.message}; }
      finally { busy = false; }
    }
    // Results can be resent safely, commands cannot. Failure here causes a desktop timeout.
    await request("/result", {client: String(pair.tabId), id: command.id, result}, pair.token);
    return {ok: true};
  })().then(sendResponse, error => sendResponse({ok: false, error: error.message}));
  return true;
});
