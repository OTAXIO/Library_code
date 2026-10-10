// Heartbeats only. Pairing secrets stay in the extension's trusted session storage.
if (!globalThis.__saAssistantHeartbeat) {
  globalThis.__saAssistantHeartbeat = true;
  const stop = () => {
    clearInterval(globalThis.__saAssistantHeartbeatTimer);
    globalThis.__saAssistantHeartbeat = false;
  };
  const tick = () => {
    // Reloading an unpacked extension invalidates old content-script contexts.
    // sendMessage can throw synchronously, before a Promise exists. Stop this
    // retired timer; a fresh context is installed when the work page is paired.
    try {
      if (!chrome.runtime?.id) { stop(); return; }
      chrome.runtime.sendMessage({type: "tick"}).catch(() => {
        if (!chrome.runtime?.id) stop();
      });
    } catch (_) { stop(); }
  };
  globalThis.__saAssistantHeartbeatTimer = setInterval(tick, 1500);
  tick();
}
