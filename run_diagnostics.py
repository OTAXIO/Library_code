"""Private, best-effort run evidence. Never records transport payloads or credentials.

This observes the existing extension Bridge; it cannot replay a command, change
an import journal, or turn a lost acknowledgement into a completed operation.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
from datetime import datetime
import traceback
from urllib.parse import urlsplit
from uuid import uuid4


def safe_text(value, secrets=()):
    value = str(value)
    for secret in secrets:
        if isinstance(secret, str) and secret:
            value = value.replace(secret, "[已隐藏]")
    value = re.sub(r"(?i)\bBearer\s+[^\s\"',;<>]+", "Bearer [已隐藏]", value)
    value = re.sub(r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|authorization|"
                   r"password|passwd|secret|token|cookie|配对码)[\"']?\s*[:=]\s*)"
                   r"(?:\"[^\"]*\"|'[^']*'|[^\s&,;}]+)", r"\1[已隐藏]", value)
    # URLs can carry tokens or bibliographic queries. The service origin suffices.
    def origin(match):
        try:
            parsed = urlsplit(match[0])
            return parsed.scheme + "://" + (parsed.hostname or "[URL]") + "/[地址省略]"
        except ValueError:
            return "[URL省略]"
    value = re.sub(r"https?://[^\s\"'<>]+", origin, value)
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", value)[:2000]


class RunDiagnostics:
    def __init__(self, base, mode, scope, records, operation_log, progress, bridge=None):
        self.base = Path(base).resolve()
        self.log, self.progress_callback = operation_log, progress
        token = getattr(bridge, "token", "")
        self.secrets = (token,) if isinstance(token, str) and token else ()
        self.run_id = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid4().hex[:10]
        self.path = self.base / "runtime/run-reports" / (self.run_id + ".json")
        self.stage, self.sa_id, self.command = "开始前校验", "", None
        self.saved, self.warning_sent = False, False
        self.data = {"version": 1, "run_id": self.run_id, "mode": mode, "scope": scope,
                     "started_at": self.now(), "status": "running", "stage": self.stage,
                     "records": [{"sa_id": r.sa_id, "row": r.row, "title": r.title}
                                 for r in records if r.owner == "谭勋策"], "commands": []}
        self.save()

    @staticmethod
    def now():
        return datetime.now().astimezone().isoformat(timespec="milliseconds")

    def clean(self, value):
        if isinstance(value, str):
            return safe_text(value, self.secrets)
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        if isinstance(value, dict):
            return {k: self.clean(v) for k, v in value.items()}
        return value

    def warn(self):
        if not self.warning_sent:
            self.warning_sent = True
            try:
                self.progress_callback("诊断文件未能保存；原入库断点不受影响，请检查 code/runtime/run-reports。")
            except Exception:
                pass

    def save(self):
        temporary = None
        try:
            # Do not follow a replaced runtime/report directory outside this code.
            if not self.path.parent.resolve().is_relative_to(self.base):
                raise OSError("diagnostic directory outside code")
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".run-", suffix=".tmp", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(self.clean(self.data), stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            self.saved = True
        except Exception:
            self.warn()
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def progress(self, text):
        self.stage = safe_text(text, self.secrets)
        self.data["stage"] = self.stage
        self.save()
        self.progress_callback(text)

    def started(self, action, payload):
        self.sa_id = str(payload.get("sa_id", ""))
        self.stage = str(action)
        self.command = {"action": str(action), "sa_id": self.sa_id,
                        "started_at": self.now(), "status": "pending"}
        self.data["pending_command"] = self.command
        self.data["stage"] = self.stage
        self.save()  # Persist before dispatch, not only after a successful ACK.

    @staticmethod
    def evidence(answer):
        if not isinstance(answer, dict):
            return {"response_type": type(answer).__name__}
        keys = ("verified", "submitted", "uploaded", "validated", "detail_released",
                "metadata_completion_protocol", "metadata_detail_protocol", "metadata_excel_protocol")
        proof = {k: v for k in keys if type(v := answer.get(k)) in (bool, int, str)}
        for name, fields in (("row", ("saLzkId", "markStatus", "matchCount", "itemId")),
                             ("batch", ("id", "modelId", "status", "total", "actual", "fail", "increase",
                                        "duplicateSkip", "duplicateIncreaseUpdate", "duplicateOverallCoverage"))):
            value = answer.get(name)
            if isinstance(value, dict):
                proof[name] = {k: v for k in fields if type(v := value.get(k)) in (bool, int, str)}
        return proof

    def returned(self, answer=None, error=None):
        command = dict(self.command)
        command.update(finished_at=self.now(), status="error" if error else "returned")
        command["error" if error else "evidence"] = self.error(error) if error else self.evidence(answer)
        self.data["commands"].append(command)
        self.data.pop("pending_command", None)
        self.command = None
        self.save()

    def error(self, exc):
        return {"type": type(exc).__name__, "message": safe_text(exc, self.secrets),
                "stack": [{"file": Path(f.filename).name, "line": f.lineno, "function": f.name}
                          for f in traceback.extract_tb(exc.__traceback__)[-12:]]}

    def record(self, action, outcome, detail, sa_id=None):
        try:
            self.log.record(safe_text(action, self.secrets), outcome, self.sa_id if sa_id is None else sa_id,
                            detail=safe_text(detail, self.secrets), run_id=self.run_id)
        except Exception:
            self.warn()

    def finish(self, result):
        self.data.update(status="paused" if result.halted or result.cancelled else "finished", finished_at=self.now(),
                         summary=result.summary, reason=result.reason, outcomes=result.messages)
        if result.halted or result.cancelled:
            self.record(self.stage, "已暂停", result.reason or result.summary)
        for sid, message in result.messages.items():
            record = next((r for r in result.roster.records if r.sa_id == sid), None)
            if record is not None and record.skipped and not record.done:
                self.record("条目搁置", "已跳过", message, sid)
        self.save()

    def failed(self, exc):
        self.data.update(status="error", finished_at=self.now(), error=self.error(exc))
        self.record(self.stage, "已暂停", str(exc))
        self.save()


class TracedBridge:
    """A pass-through observer. Exactly one call to the original transport."""
    def __init__(self, bridge, diagnostics):
        self.transport, self.diagnostics = bridge, diagnostics

    def __getattr__(self, name):
        return getattr(self.transport, name)

    def call(self, action, payload, timeout=90):
        self.diagnostics.started(action, payload)
        try:
            answer = self.transport.call(action, payload, timeout=timeout)
        except Exception as exc:
            self.diagnostics.returned(error=exc)
            raise
        self.diagnostics.returned(answer=answer)
        return answer
