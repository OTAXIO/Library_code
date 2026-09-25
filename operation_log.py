"""Local, bounded human-readable record of desktop assistant operations."""
from __future__ import annotations

import json
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path


class OperationLog:
    LIMIT = 100

    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.RLock()

    def read(self):
        if not self.path.exists():
            return []
        content = self.path.read_text(encoding="utf-8")
        if not content.strip():
            return []
        data = json.loads(content)
        if (not isinstance(data, dict) or data.get("version") != 1 or
                not isinstance(data.get("operations"), list) or
                any(not isinstance(item, dict) for item in data["operations"])):
            raise ValueError("log.txt 格式不兼容，已保留原文件。")
        return data["operations"]

    def record(self, action, result, sa_id=""):
        if not isinstance(action, str) or not action.strip() or result not in {"已执行", "已暂停"}:
            raise ValueError("操作日志字段无效。")
        entry = {"time": datetime.now().astimezone().isoformat(timespec="seconds"),
                 "action": action.strip(), "sa_id": str(sa_id or ""), "result": result}
        with self.lock:
            entries = (self.read() + [entry])[-self.LIMIT:]
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = None
            try:
                with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                                 dir=self.path.parent, prefix=".log-", suffix=".tmp",
                                                 delete=False) as stream:
                    temporary = Path(stream.name)
                    json.dump({"version": 1, "operations": entries}, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
        return entry
