// Heartbeats only. Pairing secrets stay in the extension's trusted session storage.
if (!globalThis.__saAssistantHeartbeat) {
  globalThis.__saAssistantHeartbeat = true;
  setInterval(() => { chrome.runtime.sendMessage({type: "tick"}).catch(() => {}); }, 1500);
  chrome.runtime.sendMessage({type: "tick"}).catch(() => {});
}
