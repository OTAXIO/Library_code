"""WOS-only Browser Skill transport; no credentials or production writes.

Every run owns a new Agent Window in an explicitly configured, connected profile.
It never borrows a user tab, restarts the daemon, or changes browser settings.
Search is submitted once; TXT is captured from the observed WOS export button.
The existing WOSDownload parser and intake policy still validate the actual bytes.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path

from core import SafetyStop

ORIGINS = ("https://webofscience.clarivate.cn", "https://www.webofscience.com")
DEFAULTS = {"transport": "extension", "instance_id": "", "origin": ORIGINS[0]}

# Never echo arbitrary CLI messages: an error may contain a page, URL or secret.
_CLI_CODES = {"timeout", "session_busy", "not_found", "invalid_params",
              "browser_disconnected", "extension_disconnected", "protocol_mismatch",
              "permission_denied", "unsupported", "cancelled", "tab_not_owned",
              "borrow_outcome_unknown"}
_OPERATIONS = {
    ("status",): "检查后台服务", ("browsers",): "检查浏览器连接",
    ("session", "list"): "检查专用会话", ("session", "start"): "创建下载窗口",
    ("session", "stop"): "关闭下载窗口", ("tab", "list"): "确认下载标签页",
    ("debug", "start"): "开启下载诊断", ("debug", "stop"): "结束下载诊断",
    ("debug", "export"): "保存下载诊断", ("debug", "activity"): "检查操作状态",
    ("navigate",): "打开 WOS 页面", ("reload",): "刷新 WOS 页面",
    ("observe",): "读取网页", ("evaluate",): "检查 WOS 页面",
    ("click",): "点击已核验控件", ("download",): "保存 TXT",
    ("request-help",): "等待人工处理",
}


class WOSBrowserStop(SafetyStop):
    """The shared browser page/session is blocked; do not dispatch another paper."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


def read_settings(root):
    path = Path(root) / "wos_browser.json"
    if not path.exists():
        return dict(DEFAULTS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return validate_settings(data)
    except (OSError, ValueError, TypeError):
        raise SafetyStop("WOS 下载连接配置无效，请在设置页重新保存；不会改用其他浏览器。") from None


def validate_settings(data):
    if (not isinstance(data, dict) or set(data) != set(DEFAULTS)
            or data["transport"] not in {"extension", "browser-skill"}
            or data["origin"] not in ORIGINS
            or not isinstance(data["instance_id"], str)
            or (data["instance_id"] and not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", data["instance_id"]))
            or (data["transport"] == "browser-skill" and not data["instance_id"])):
        raise ValueError("请选择下载方式；浏览器技能需填写已授权的浏览器编号。")
    return dict(data)


def select_transport(extension_bridge, base, stop=None, progress=lambda text: None):
    base = Path(base)
    settings = read_settings(base / "runtime")
    if settings["transport"] == "browser-skill":
        return BrowserSkillWOS(base, settings["instance_id"], settings["origin"], stop, progress)
    if extension_bridge is None or not extension_bridge.online:
        raise SafetyStop("原插件尚未连接。请配对 WOS 页，或在设置中选择已授权的浏览器技能下载。")
    return extension_bridge


class BrowserSkillWOS:
    transport = "browser-skill"

    def __init__(self, base, instance_id, origin, stop=None, progress=lambda text: None, cli=None):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", instance_id) or origin not in ORIGINS:
            raise SafetyStop("需要明确的 Browser Skill 浏览器编号和受支持的 WOS 网址。")
        self.base = Path(base).resolve()
        self.instance_id, self.origin = instance_id, origin
        self.stop, self.progress = stop, progress
        self.cli = cli or shutil.which("bsk") or shutil.which("bsk.exe")
        if not self.cli:
            installed = Path.home() / ".local" / "bin" / "bsk.exe"
            if installed.is_file():
                self.cli = str(installed)
        if not self.cli:
            raise SafetyStop("未安装 bsk CLI；请使用原插件下载或安装浏览器技能的 CLI。")
        self.session = None
        self.tab_id = None
        self.query = None
        self.record_url = None
        self.export_attempted = False
        self.blocked = False
        self.capturing = False
        self.interaction = {}
        self.version = "未记录"
        self.deadline = 0.0
        self.search_pending = False
        self.reload_used = False

    def _run(self, args, timeout=20, cleanup=False):
        if not cleanup:
            if self.stop is not None and self.stop.is_set():
                raise WOSBrowserStop("已暂停下载；不再提交浏览器操作。")
            if self.deadline:
                timeout = min(timeout, self.deadline - time.monotonic())
            if timeout < 1:
                raise WOSBrowserStop("浏览器技能操作超时；结果不明，未自动重复检索或导出。")
        env = dict(os.environ, BSK_AUTO_START="0")
        try:
            completed = subprocess.run([self.cli, *map(str, args), "--json"],
                                       cwd=self.base, env=env, capture_output=True,
                                       encoding="utf-8", errors="replace", timeout=timeout,
                                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WOSBrowserStop("浏览器技能通信失败或操作超时；先检查网页，未自动重复操作。") from exc
        try:
            data = json.loads(completed.stdout)
        except ValueError:
            raise WOSBrowserStop("浏览器技能没有返回有效 JSON；未继续下一篇。") from None
        reported_exit = data.get("exit_code") if isinstance(data, dict) else None
        if (completed.returncode or (isinstance(data, dict) and data.get("code"))
                or (type(reported_exit) is int and reported_exit != 0)):
            raise self._cli_failure(args, data, completed.returncode)
        expects_list = args[0] == "browsers" or list(args[:2]) == ["session", "list"]
        if not isinstance(data, list if expects_list else dict):
            raise WOSBrowserStop("浏览器技能返回结构不兼容；未继续下一篇。")
        return data

    def _cli_failure(self, args, data, returncode):
        """Explain local connection failures without leaking raw CLI output.

        bsk's local startup/IPC errors have code=null and exit_code=2. They
        must not become 'None', be treated as success, or as missing literature.
        """
        data = data if isinstance(data, dict) else {}
        raw_code = data.get("code")
        exit_code = returncode or data.get("exit_code")
        if type(exit_code) is not int:
            exit_code = 0
        code = raw_code if isinstance(raw_code, str) and raw_code in _CLI_CODES else (
            f"cli_exit_{exit_code}" if exit_code else "cli_error")
        message = data.get("message")
        message = message.lower() if isinstance(message, str) else ""
        # Only recognize fixed local CLI prefixes, never arbitrary WOS text.
        if message.startswith("ensure daemon is running:"):
            if "automatic daemon startup is disabled" in message:
                code = "daemon_unavailable"
            elif "connect ipc named pipe" in message and any(
                    part in message for part in ("拒绝访问", "permission denied", "access is denied", "access denied")):
                code = "ipc_permission_denied"
        operation = _OPERATIONS.get(tuple(args[:2]), _OPERATIONS.get(tuple(args[:1]), "浏览器操作"))
        if code == "daemon_unavailable":
            reason = ("浏览器技能后台服务未运行（daemon_unavailable）。\n"
                      "请先启动服务，再到“设置 → 检查下载连接”验证；仅打开网页还不够。")
        elif code == "ipc_permission_denied":
            reason = ("浏览器技能的本地通信通道被拒绝访问（ipc_permission_denied）。\n"
                      "请确认助手和服务在同一 Windows 账号的普通终端环境运行；不要重启共享服务或反复提交。")
        else:
            reason = f"浏览器技能在“{operation}”时失败（{code}）。"
        if self.session is None and self.query is None:
            reason += "\n尚未开始 WOS 检索或下载，本次不修改名单。"
        else:
            reason += "\n当前操作未确认，已暂停整批；先检查网页，不要重复提交。"
        diagnostic = {"time": int(time.time()), "operation": operation, "code": code,
                      "exit_code": exit_code, "session_created": self.session is not None,
                      "search_pending": self.search_pending, "export_attempted": self.export_attempted,
                      "message": reason}
        try:
            from paper_classify import atomic_json
            path = self.base / "runtime" / "wos-browser" / "last-connection-error.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(path, diagnostic)
        except (OSError, ValueError):
            pass  # A diagnostic write failure must never hide the original error.
        return WOSBrowserStop(reason, code=code)

    def check_connection(self):
        """Read-only service/profile check; no session, navigation or download."""
        browsers = self._run(["browsers"])
        selected = [b for b in browsers if isinstance(b, dict)
                    and b.get("instance_id") == self.instance_id and not b.get("unresponsive")]
        if len(selected) != 1:
            raise WOSBrowserStop("所配置的 Browser Skill 浏览器未连接；未切换到其他浏览器。")
        version = selected[0].get("extension_version")
        self.version = version if isinstance(version, str) and re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?", version) else "未记录"
        return {"transport": self.transport, "browser_skill_version": self.version}

    def _args(self, command):
        return [command, "--session", self.session, "--tab-id", self.tab_id]

    def _observe(self):
        return self._run(self._args("observe") + ["--max-tokens", 4500])

    def _evaluate(self, filename, name, command):
        source = (self.base / "extension" / filename).read_text(encoding="utf-8")
        command = {**command, "expires": int((time.time() + max(14, self.deadline - time.monotonic())) * 1000)}
        expression = "(async()=>{" + source + ";return await " + name + "(" + json.dumps(command, ensure_ascii=False) + ");})()"
        response = self._run(self._args("evaluate") + ["--timeout", "20s", expression], timeout=22)
        if not isinstance(response, dict) or response.get("ok") is not True or "value" not in response:
            raise WOSBrowserStop("WOS 页面检查没有返回结果；未重复操作。")
        value = response["value"]
        if not isinstance(value, dict):
            raise WOSBrowserStop("WOS 页面返回结构不兼容；未重复操作。")
        return value

    def _probe(self, action="wos_diagnose"):
        return self._evaluate("page-diagnostics.js", "inspectWorkPage", {"action": action})

    def _page_action(self, action, query):
        # Read the real page before each input/menu operation. The same audited
        # semantic adapter performs only fixed-label WOS interactions.
        self._observe()
        result = self._evaluate("wos-adapter.js", "runWOSCommand", {**query, "action": action})
        if result.get("ok") is not True:
            error = str(result.get("error", "WOS 页面操作未完成"))
            if "未找到记录" in error or "不是可确认的唯一记录" in error:
                raise SafetyStop(error)
            raise WOSBrowserStop(error)
        return result.get("data", {})

    def _human_gate(self, page):
        if not page.get("login_required"):
            return
        self.progress("WOS 登录/验证码需要你处理，请在专用浏览器窗口完成。")
        if self.interaction.get("request_help") == "enabled":
            # One request only. Disabled/cancelled/timed-out help is not consent.
            result = self._run(["request-help", "--session", self.session,
                                "--prompt", "请在 WOS 专用测试页完成登录或验证码；无需提供账号密码。",
                                "--timeout", "45s"], timeout=48)
            if result.get("status") in {"completed", "continued"} or result.get("outcome") in {"completed", "continued"}:
                self._observe()
                if not self._probe().get("login_required"):
                    return
        raise WOSBrowserStop("WOS 登录或验证码需要人工处理；未继续下一篇。")

    def _navigate(self, url):
        # WOS may leave analytics/personalization requests pending even when
        # the search form works. Wait for the navigation commit, then check
        # semantic readiness rather than waiting for every resource to load.
        try:
            self._run(self._args("navigate") + [url, "--wait-until", "commit", "--timeout", "15s"], timeout=20)
        except WOSBrowserStop as exc:
            if exc.code != "timeout":
                raise
            activity = self._run(["debug", "activity", "--session", self.session])
            tabs = self._run(["tab", "list", "--scope", "agent", "--session", self.session]).get("tabs", [])
            current = [tab for tab in tabs if tab.get("tab_id") == self.tab_id]
            if activity.get("activity", {}).get("state") != "idle" or len(current) != 1 or current[0].get("url") != url:
                raise WOSBrowserStop("WOS 导航尚未确认；未再次打开或刷新，已暂停整批。") from exc
            # The intended URL committed and the command settled. Inspect it
            # once; never resubmit the timed-out navigation blindly.
        self._observe()

    def _reload_blank(self):
        if self.reload_used or self.search_pending or self.export_attempted or self.record_url:
            raise WOSBrowserStop("已提交的检索/导出或已刷新过的页面不可自动刷新；请核对当前网页。")
        self.reload_used = True
        self.progress("WOS 检索页为空白，执行一次安全刷新（尚未提交检索或导出）…")
        self._run(self._args("reload") + ["--wait-until", "commit", "--timeout", "15s"], timeout=20)
        self._observe()

    def _click_navigation(self, labels):
        observed=self._observe()
        pattern=r'^\s*(@e\d+) (?:link|button|tab) "(?:'+ '|'.join(re.escape(label) for label in labels)+r')"(?:\s|$)'
        refs=re.findall(pattern,observed.get('text',''),flags=re.M)
        if len(refs)!=1:
            raise WOSBrowserStop('未唯一识别 WOS 页面内导航；未刷新登录环境或继续下一篇。')
        self._run(self._args('click')+[refs[0]])
        self._observe()

    def _search_page(self):
        page=self._probe()
        self._human_gate(page)
        if page.get('site_timeout'):
            raise WOSBrowserStop('WOS 网站返回 5xx/连接超时页；这不是文献零结果，已暂停整批。')
        if page.get('wos_error') or page.get('dialog_count') or page.get('error'):
            raise WOSBrowserStop('WOS 网站报错或存在操作窗口；请先处理网页。')
        if page.get('core_search_route') and page.get('query_input_count')==1 and not page.get('zero_result'):
            # Initial preflight has already loaded this form. Re-navigating to
            # the same URL can blank WOS while startup preferences are pending.
            self._ready_search()
            return
        if page.get('zero_result'):
            # Reset the old zero-result panel through visible SPA navigation,
            # not a fresh document / login bootstrap or a repeated Search.
            self._click_navigation(['Smart Search','智能检索','智能搜索'])
        self._click_navigation(['Advanced Search','高级检索','高级搜索','進階檢索'])
        self._ready_search()

    def _open_record(self, url):
        self._validate_record(url)
        self._observe()
        # Canonical link was read from this results DOM. Follow its native
        # Angular link, preserving the initialized WOS app and login state.
        path=url[len(self.origin):]
        raw=path.replace('%3A',':').replace('%3a',':').rstrip('/')
        encoded=raw.replace('WOS:','WOS%3A')
        lower_encoded=encoded.replace('%3A','%3a')
        values=dict.fromkeys(part+suffix for part in (raw,encoded,lower_encoded,
                                                     self.origin+raw,self.origin+encoded,self.origin+lower_encoded)
                             for suffix in ('','/'))
        # The shared reader also accepts explicit routerLink and trailing-slash
        # forms. Query/hash suffixes never change the canonical accession path.
        selector=','.join(tag+'['+attr+operator+'"'+value+suffix+'"]'
                          for tag in ('a','[role="link"]')
                          for attr in ('href','routerlink','ng-reflect-router-link')
                          for value in values
                          for operator,suffix in (('=',''),('^=','?'),('^=','#')))
        self._run(self._args('click')+[selector])
        self._observe()

    def _start(self):
        self.check_connection()
        # A second task on the same site shares login state. Don't disturb it.
        for session in self._run(["session", "list"]):
            if session.get("browser_instance_id") != self.instance_id:
                continue
            tabs = self._run(["tab", "list", "--scope", "agent", "--session", session["session_id"]])
            if any(str(t.get("url", "")).startswith(ORIGINS) for t in tabs.get("tabs", [])):
                raise WOSBrowserStop("另一个浏览器技能任务正在操作 WOS；请完成该任务后再下载，未抢占标签页。")
        started = self._run(["session", "start", "--browser", self.instance_id])
        self.session = started.get("session_id")
        self.interaction = started.get("interaction", {})
        if not self.session:
            raise WOSBrowserStop("浏览器技能未创建专用会话。")
        tabs = self._run(["tab", "list", "--scope", "agent", "--session", self.session]).get("tabs", [])
        if len(tabs) != 1 or type(tabs[0].get("tab_id")) is not int:
            raise WOSBrowserStop("无法确认专用会话的标签页；未操作其他标签页。")
        self.tab_id = tabs[0]["tab_id"]
        self._run(["debug", "start", "--session", self.session, "--tab-id", self.tab_id,
                   "--name", "WOS-TXT-download"])
        self.capturing = True
        self._navigate(self.origin + "/wos/woscc/basic-search")

    def _ready_search(self):
        waiting_since = time.monotonic()
        while time.monotonic() < self.deadline - 14:
            page = self._probe()
            if page.get("error"):
                raise WOSBrowserStop(str(page["error"]))
            self._human_gate(page)
            if page.get("site_timeout"):
                raise WOSBrowserStop("WOS 网站返回 5xx/连接超时页；未检索论文，未记作未查询到。")
            if page.get("wos_error") or page.get("dialog_count"):
                raise WOSBrowserStop("WOS 网站报错或存在操作窗口；先处理网页，未把它当成未查询到。")
            if page.get("core_search_route") and page.get("query_input_count", 0) == 1 and not page.get("busy"):
                return page
            blank = (page.get("core_search_route") and page.get("query_input_count", 0) == 0
                     and not any(page.get(key) for key in ("busy", "smart_search", "fielded_search",
                                                          "zero_result", "canonical_record_link_count")))
            if blank and time.monotonic() - waiting_since >= 10 and not self.reload_used:
                self._reload_blank()
            self.progress("等待 WOS 字段检索页显示（尚未点击检索）…")
            time.sleep(1)
        raise WOSBrowserStop("WOS 字段检索页未就绪；未点击检索，也未继续下一篇。")

    def call(self, action, query, timeout=120):
        if action not in {"wos_diagnose", "wos_search", "wos_export"}:
            raise SafetyStop("浏览器技能下载通道不支持后台修改、上传或导入。")
        if self.blocked:
            raise WOSBrowserStop("上一条浏览器操作未确认；已暂停，禁止继续自动提交。")
        self.deadline = time.monotonic() + timeout
        try:
            if not self.session:
                self._start()
            if action == "wos_diagnose":
                page = self._ready_search()
                return {"transport": self.transport, "browser_skill_version": self.version,
                        "wos_download_protocol": 2, "page": page}
            if action == "wos_search":
                if not isinstance(query.get("sa_id"), str) or not query["sa_id"]:
                    raise SafetyStop("下载缺少唯一名单 ID。")
                self.query, self.record_url, self.export_attempted = dict(query), None, False
                self._search_page()
                self.search_pending = True
                self._page_action("wos_start_search", query)
                navigated = None
                while time.monotonic() < self.deadline - 14:
                    result = self._probe("wos_read_results")
                    if result.get("ok") is not True:
                        raise WOSBrowserStop(str(result.get("error", "WOS 只读检查失败")))
                    page = result.get("data", {})
                    state = page.get("state")
                    if state == "zero":
                        self.search_pending = False
                        raise SafetyStop("WOS 未找到记录；这不等于未发表，也不自动标记完成")
                    if state == "multiple":
                        self.search_pending = False
                        raise SafetyStop("WOS 结果不是可确认的唯一记录；请人工核验，未下载任意一篇。")
                    if state == "single" and not navigated:
                        self.search_pending = False
                        url = page.get("navigate_url", "")
                        self._validate_record(url)
                        navigated = url
                        self._open_record(url)
                    elif state == "record":
                        self.search_pending = False
                        url = page.get("record_url", "")
                        self._validate_record(url)
                        if navigated is None or url.rstrip("/") == navigated.rstrip("/"):
                            self.record_url = url
                            return {"record_url": url}
                    self.progress("WOS 已提交一次检索，正在等待结果；不会重复点击…")
                    time.sleep(1)
                raise WOSBrowserStop("WOS 检索结果超时；已暂停，未把页面问题当作未查询到。")
            if self.query != query or not self.record_url or self.export_attempted:
                raise WOSBrowserStop("单篇导出上下文失效或已提交；未重复下载。")
            self._page_action("wos_prepare_export", query)
            observed = self._observe()
            text = str(observed.get("text", ""))
            refs = re.findall(r'^\s*(@e\d+) button "(?:Export|导出)"(?:\s|$)', text, flags=re.M)
            if len(refs) != 1 or not re.search(r"L1 modal", text):
                raise WOSBrowserStop("未唯一识别导出弹窗中的最终导出按钮；未点击下载。")
            self._page_action("wos_check_export", query)
            # _page_action refreshed the ref map; read once more before input.
            observed = self._observe()
            refs = re.findall(r'^\s*(@e\d+) button "(?:Export|导出)"(?:\s|$)', observed.get("text", ""), flags=re.M)
            if len(refs) != 1 or not re.search(r"L1 modal", observed.get("text", "")):
                raise WOSBrowserStop("最终导出按钮发生变化；未点击下载。")
            directory = self.base / "runtime" / "wos-browser" / "downloads"
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / (uuid.uuid4().hex + ".txt")
            self.export_attempted = True
            self.progress("已核验 Full Record，正在捕获本次 TXT 下载…")
            downloaded = self._run(self._args("download") + [refs[0], "--out", path, "--timeout", "45s"], timeout=48)
            if (Path(downloaded.get("path", "")).resolve() != path.resolve()
                    or type(downloaded.get("byte_size")) is not int
                    or not 1 <= downloaded["byte_size"] <= 524288 or not path.is_file()
                    or path.is_symlink() or path.stat().st_size != downloaded["byte_size"]):
                raise WOSBrowserStop("捕获的 TXT 路径或大小无法核验；未采纳，不会自动重试。")
            # The capture returned verified bytes on disk. An extra DOM read
            # after the export dialog closes can fail during browser/CDP
            # cleanup and incorrectly report an already completed download.
            return {"path": str(path), "sa_id": query["sa_id"], "record_url": self.record_url}
        except WOSBrowserStop:
            self.blocked = True
            raise

    def _validate_record(self, url):
        if not isinstance(url, str) or not re.fullmatch(re.escape(self.origin) + r"/wos/woscc/full-record/WOS(?::|%3[Aa])\d{15}/?", url):
            raise WOSBrowserStop("WOS 记录链接未通过同源和单篇校验；未打开该链接。")
        supplied = str((self.query or {}).get("wos", "")).replace("WOS:", "")
        if supplied and not url.rstrip("/").endswith(supplied):
            raise SafetyStop("WOS 唯一记录与名单冲突；未导出。")

    def close(self):
        if not self.session:
            return
        try:
            if self.capturing:
                try:
                    self._run(["debug", "stop", "--session", self.session], cleanup=True)
                except SafetyStop:
                    # Download capture can already have detached the debugger.
                    # Still try to export the retained evidence before ending
                    # our session; never restart the shared daemon to recover.
                    self.progress("调试记录可能已自动停止，尝试保存已保留的浏览器证据…")
                try:
                    directory = self.base / "runtime" / "wos-browser" / "evidence"
                    directory.mkdir(parents=True, exist_ok=True)
                    self._run(["debug", "export", "--session", self.session, "--output",
                               directory / (uuid.uuid4().hex + ".json")], timeout=25, cleanup=True)
                except SafetyStop:
                    self.progress("浏览器证据未能导出；TXT 和逐篇下载报告仍保留。")
        finally:
            try:
                self._run(["session", "stop", self.session], cleanup=True)
            except SafetyStop:
                self.progress("专用浏览器会话已失效或未能关闭，请检查 Browser Skill 窗口。")
            self.session = None
