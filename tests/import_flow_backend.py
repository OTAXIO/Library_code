"""Real WOSFlow + Bridge for the disposable import extension integration test."""
import json
import sys
from pathlib import Path

from automation import ImportStore, WOSFlow
from bridge import Bridge
from core import Record, SafetyStop

# JSON sent by Node is UTF-8, independent of the Windows console codepage.
sys.stdin.reconfigure(encoding="utf-8", errors="strict")
sys.stdout.reconfigure(encoding="utf-8", errors="strict")


root = Path(sys.argv[1]).resolve()
bridge = Bridge(0)
record = Record(2, "测试负责人", "demo-001", "Synthetic paper", "10.1234/test", "", "00001", 0, "", "待处理", "", "1")
audit = []
lose_import_ack = False
lose_push_ack = False
store = None


class LossyBridge:
    def call(self, action, payload, timeout=75):
        global lose_import_ack, lose_push_ack
        audit.append(action)
        value = bridge.call(action, payload, timeout=timeout)
        if action == "import_submit" and lose_import_ack:
            lose_import_ack = False
            raise SafetyStop("Synthetic lost import acknowledgement after the server accepted it")
        if action == "import_push" and lose_push_ack:
            lose_push_ack = False
            raise SafetyStop("Synthetic lost push acknowledgement after the server accepted it")
        return value


try:
    print(json.dumps({"token": bridge.token, "port": bridge.port}), flush=True)
    for line in sys.stdin:
        task = json.loads(line)
        if task.get("exit"):
            break
        try:
            action, payload = task["action"], task.get("payload", {})
            if action == "reset_flow":
                name = payload["scenario"]
                if name not in ("normal", "lost-ack", "lost-push-ack", "wrong-identity"):
                    raise ValueError("Unknown isolated test scenario")
                store = ImportStore(root / name)
                audit.clear()
                lose_import_ack = payload.get("lose_import_ack", False)
                lose_push_ack = payload.get("lose_push_ack", False)
                value = {"ready": True}
            elif action == "prepare_file":
                file = Path(payload["path"]).resolve()
                if not file.is_relative_to(root / "downloads"):
                    raise ValueError("Test can only read its disposable synthetic download")
                value = WOSFlow(LossyBridge(), store).prepare_file(record, file)
            elif action == "proceed":
                # Rebuild both the flow and store every call: resume uses durable
                # state, not an in-memory phase retained by a prior invocation.
                store = ImportStore(store.root)
                value = WOSFlow(LossyBridge(), store).proceed(record)
            elif action == "state":
                value = {"state": ImportStore(store.root).get(record), "actions": audit[:]}
            else:
                value = bridge.call(action, payload, timeout=40)
            print(json.dumps({"ok": True, "data": value}), flush=True)
        except Exception as error:
            print(json.dumps({"ok": False, "error": str(error)}), flush=True)
finally:
    bridge.close()
