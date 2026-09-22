"""Authenticated loopback command bridge. No website credentials enter this process."""
from __future__ import annotations

import json
import queue
import re
import secrets
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from core import SafetyStop


class Bridge:
    def __init__(self, port=8765):
        self.token = secrets.token_urlsafe(32)
        self.lock = threading.Lock()
        self.pending = None
        self.last_seen = 0.0
        self.connected = ""
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass  # Do not log request headers, tokens, or personnel records.

            def reply(self, status, value):
                body = json.dumps(value, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                expected_host = f"127.0.0.1:{bridge.port}"
                origin = self.headers.get("Origin", "")
                if self.headers.get("Host") != expected_host or (origin and not re.fullmatch(r"chrome-extension://[a-p]{32}", origin)):
                    return self.reply(403, {"error": "origin denied"})
                supplied = self.headers.get("Authorization", "")
                if not secrets.compare_digest(supplied, "Bearer " + bridge.token):
                    return self.reply(401, {"error": "配对码无效，请重新配对"})
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 1_000_000:
                        raise ValueError("invalid size")
                    payload = json.loads(self.rfile.read(size))
                    if not isinstance(payload, dict):
                        raise ValueError("object required")
                except (ValueError, json.JSONDecodeError):
                    return self.reply(400, {"error": "invalid request"})
                if self.path == "/poll":
                    client = str(payload.get("client", ""))
                    if not re.fullmatch(r"\d+", client):
                        return self.reply(400, {"error": "invalid tab"})
                    with bridge.lock:
                        if bridge.connected and bridge.connected != client:
                            return self.reply(409, {"error": "另一个标签页已连接；请在桌面工具重新配对"})
                        bridge.connected = client
                        bridge.last_seen = time.monotonic()
                        pending = bridge.pending
                        command = None
                        if pending and not payload.get("claimOnly") and not pending["delivered"] and time.monotonic() < pending["deadline"]:
                            pending["delivered"] = True
                            command = pending["command"]
                    return self.reply(200, {"command": command})
                if self.path == "/result":
                    with bridge.lock:
                        pending = bridge.pending
                        if pending and payload.get("id") == pending["command"]["id"] and str(payload.get("client", "")) == bridge.connected:
                            try:
                                pending["result"].put_nowait(payload.get("result", {}))
                            except queue.Full:
                                pass
                    return self.reply(200, {"accepted": True})
                return self.reply(404, {"error": "not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_port
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def online(self):
        return bool(self.connected) and time.monotonic() - self.last_seen < 8

    def re_pair(self):
        with self.lock:
            if self.pending:
                raise SafetyStop("当前命令尚未结束，不能更换标签页。")
            self.token = secrets.token_urlsafe(32)
            self.connected = ""
            self.last_seen = 0

    def call(self, action, payload, timeout=90):
        with self.lock:
            if self.pending:
                raise SafetyStop("已有命令正在执行。")
            if not self.online:
                raise SafetyStop("浏览器未连接。请在已登录的比对页打开扩展并配对。")
            result = queue.Queue(maxsize=1)
            command = {"id": str(uuid.uuid4()), "action": action, **payload,
                       "expires": int((time.time() + timeout) * 1000)}
            self.pending = {"command": command, "result": result, "delivered": False,
                            "deadline": time.monotonic() + timeout}
        try:
            response = result.get(timeout=timeout)
            if not isinstance(response, dict) or not response.get("ok"):
                raise SafetyStop(str(response.get("error", "浏览器返回未知结果")) if isinstance(response, dict) else "浏览器结果格式异常")
            return response.get("data", {})
        except queue.Empty as exc:
            raise SafetyStop("命令超时，结果不明。不要重复提交；先检查网页，等待请求结束，再只读重查。") from exc
        finally:
            with self.lock:
                self.pending = None

    def close(self):
        self.server.shutdown()
        self.server.server_close()
