"""Pipe-controlled loopback bridge for isolated extension integration tests."""
import json
import sys
from bridge import Bridge

bridge = Bridge(0)  # Do not compete with the user's running assistant.
try:
    # Read by the test subprocess only; never log this ephemeral pairing secret.
    print(json.dumps({"token": bridge.token, "port": bridge.port}), flush=True)
    for line in sys.stdin:
        task = json.loads(line)
        if task.get("exit"):
            break
        try:
            result = bridge.call(task["action"], task.get("payload", {}), timeout=20)
            print(json.dumps({"ok": True, "data": result}), flush=True)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}), flush=True)
finally:
    bridge.close()
