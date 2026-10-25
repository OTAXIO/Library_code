// Heartbeats only. Pairing secrets stay in the extension's trusted session storage.
{
  // A fresh pairing may run before a retired extension context notices reload.
  // Replace its interval immediately instead of trusting a stale boolean.
  if (globalThis.__saAssistantHeartbeatTimer != null)
    clearInterval(globalThis.__saAssistantHeartbeatTimer);
  globalThis.__saAssistantHeartbeat = true;
  let timer;
  const stop = () => {
    clearInterval(timer);
    if (globalThis.__saAssistantHeartbeatTimer === timer)
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
  timer = setInterval(tick, 1500);
  globalThis.__saAssistantHeartbeatTimer = timer;
  tick();
}
