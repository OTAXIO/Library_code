"""Genuine WOS Full Record TXT capture through the paired extension only."""
from __future__ import annotations

import re
import time
from pathlib import Path

from automation import ImportStore, doi, wos, norm
from bridge import BrowserRejected
from core import SafetyStop
from wos_files import archive_export, find_correlated_export, find_export, read_export, record_ut
from wos_policy import WOSPolicyStop


def rejected_before_export(message):
    # These fixed adapter failures are emitted by fingerprint(), before
    # wos_check_export/wos_download can submit. A post-click context failure,
    # submitted/invalid preview or any unrecognized error remains uncertain.
    return bool(re.fullmatch(
        r"(?:\[扩展 \d+\.\d+\.\d+\] )?\[WOS 已暂停\] "
        r"(?:未处于 WOS 核心合集单篇完整记录页|WOS 页面已不是本次检索确认的单篇记录，未导出)",
        str(message)))


def preflight(bridge, *, resume_export=False):
    """Read actual extension capabilities before Search or export."""
    # Read-only capability/access handshake, not search-form readiness. The
    # bound tab is activated and prepared by the extension before any Search.
    try:
        result = bridge.call("wos_diagnose", {}, timeout=25)
    except SafetyStop as exc:
        if "未知 WOS 调度命令" in str(exc):
            raise SafetyStop("当前插件仍是旧版本，请重载 0.4.11、刷新工作页并重新绑定；尚未提交检索。") from exc
        raise
    if (not isinstance(result, dict) or type(result.get("wos_download_protocol")) is not int
            or result.get("wos_download_protocol") != 1
            or type(result.get("search_prepare_protocol")) is not int
            or result.get("search_prepare_protocol") != 1
            or type(result.get("export_prepare_protocol")) is not int
            or result.get("export_prepare_protocol") != 1
            or result.get("result_reader") != "shared-diagnostic"
            or result.get("read_results_world") != "ISOLATED"
            or not re.fullmatch(r"\d+\.\d+\.\d+", str(result.get("extension_version", "")))
            or tuple(map(int, result["extension_version"].split("."))) < (0, 4, 9)):
        raise SafetyStop("插件下载接口不兼容，请重载 0.4.11、刷新 WOS 页并重新绑定；未提交检索或下载。")
    page = result.get("page")
    if (not isinstance(page, dict)
            or any(type(page.get(k)) is not bool for k in ("core_search_route", "wos_error", "site_timeout", "login_required", "busy"))
            or any(type(page.get(k)) is not int or not 0 <= page[k] <= 99999 for k in ("query_input_count", "dialog_count"))):
        raise SafetyStop("插件未返回完整的页面就绪检查，请重载后连接；未提交检索。")
    if page["wos_error"]:
        raise SafetyStop("WOS 网站显示 Oops, something went wrong!；请先恢复文献页。名单保持原样，这不是论文零结果。")
    if page.get("site_timeout"):
        raise SafetyStop("WOS 网站返回 5xx/连接超时页；名单保持原样，这不是论文零结果。")
    if page["login_required"]:
        raise SafetyStop("WOS 登录或验证码需要人工处理；本轮尚未开始，名单保持原样。")
    if page["dialog_count"] and not (resume_export and page.get("export_dialog") is True):
        raise SafetyStop("WOS 当前存在操作弹窗，请先人工处理；未提交检索。")
    return {"extension_version": result["extension_version"]}


class WOSDownload:
    """Download/archive only; the orchestrator validates affiliation and identity."""
    def __init__(self, bridge, store, unchanged, stop, audit, download_dir=None):
        self.bridge, self.store, self.unchanged, self.stop, self.audit = bridge, store, unchanged, stop, audit
        self.download_dir = Path(download_dir) if download_dir is not None else Path.home() / "Downloads"

    def _guard(self):
        self.unchanged()
        if self.stop is not None and self.stop.is_set():
            raise SafetyStop("已暂停下载。")

    def _save_file(self, record, path, raw, candidate, expected_url=""):
        self._guard()
        # A wrong download path/UT is a session failure, not a paper decision.
        if expected_url and candidate["wos"] != record_ut(expected_url):
            raise SafetyStop("下载 TXT 与本次 WOS 单篇页面入藏号不一致，未采纳。")
        conflict = ""
        for name, expected in (("doi", doi(record.doi)), ("wos", wos(record.wos))):
            if expected and candidate[name] != expected:
                conflict = "标识待核验"
        if not conflict and norm(record.title) != norm(candidate["title"]):
            conflict = "题名待核验"
        if conflict:
            if not expected_url:
                raise SafetyStop("下载 TXT 与名单不一致且缺少本次导出入藏号，未建立关联或上传。")
            # The searched UT proves a real download completed, but never
            # overrides conflicting SA facts. Keep the genuine bytes unlinked
            # under code, leave the backend untouched and skip only this task.
            saved_path = archive_export(self.store, path, raw, candidate)
            self.store.save(record, {"phase": "downloaded_unlinked", "candidate": candidate,
                "identity_confirmed": False, "linked_to_roster": False, "note": conflict,
                "saved_path": saved_path, "original_path": str(path), "record_url": expected_url})
            self.audit("保存未关联 WOS TXT：" + conflict, "已跳过", record.sa_id)
            raise WOSPolicyStop("本次 TXT 已下载并保存在 code，但与名单不一致；未建立关联或上传。", conflict)
        saved_path = archive_export(self.store, path, raw, candidate, record)
        state = {"phase": "downloaded", "candidate": candidate,
                 "identity_confirmed": bool(doi(record.doi) or wos(record.wos)),
                 "saved_path": saved_path, "original_path": str(path), "record_url": expected_url}
        self.store.save(record, state)
        self.audit("核验并存档 WOS TXT", "已执行", record.sa_id)
        return state

    def _recover_file(self, record, expected_url="", expected_sha="", *, correlated=False):
        self._guard()
        folders = (self.store.root / "未关联下载", self.download_dir)
        found = (find_correlated_export(folders, expected_url) if correlated and expected_url else
                 find_export(record, folders, expected_url, correlated=correlated))
        if found and expected_sha and found[2]["sha256"] != expected_sha:
            raise SafetyStop("下载目录 TXT 与原存档校验码不一致，不替换历史文件。")
        return self._save_file(record, *found, expected_url) if found else None

    def prepare(self, record, progress=lambda _: None):
        self._guard()
        if record.owner != "谭勋策" or record.done or record.matches != 0:
            raise SafetyStop("TXT 下载仅执行谭勋策的未完成零匹配任务。")
        cached = self.store.get(record)
        if cached is not None and not isinstance(cached, dict):
            raise SafetyStop("TXT 下载日志结构异常，未执行浏览器操作。")
        if cached and cached.get("phase") == "downloaded_unlinked":
            if (cached.get("note") not in ("题名待核验", "标识待核验")
                    or cached.get("identity_confirmed") is not False
                    or cached.get("linked_to_roster") is not False
                    or cached.get("candidate", {}).get("wos") != record_ut(cached.get("record_url", ""))):
                raise SafetyStop("未关联 TXT 断点不完整，未再次检索或导入。")
            self.store.bytes(cached)
            raise WOSPolicyStop("此前 TXT 已完成下载，但与名单不一致；未重新导出或上传。", cached["note"])
        if cached and cached.get("phase") == "downloaded":
            # A journal alone does not prove the file still exists. Restore only
            # identical original bytes, never replace a historical hash silently.
            try:
                self.store.bytes(cached)
            except OSError:
                recovered = self._recover_file(record, cached.get("record_url", ""), cached["candidate"]["sha256"],
                                               correlated=bool(cached.get("record_url")))
                if recovered:
                    return recovered
                raise SafetyStop("已存档 TXT 缺失，下载目录也未找到原文件；不重复导出，请检查备份。") from None
            progress("复用已保存的 TXT；不重复检索或下载")
            return cached
        progress("核查下载目录中已完成的 WOS TXT（按内容匹配，不按最新文件）")
        expected_url = cached.get("record_url", "") if cached else ""
        # The durable journal is keyed to these unchanged SA facts and its UT
        # came from this task's unique WOS search. It can recover the matching
        # download even when SA omitted both identifiers. This does NOT promote
        # title-only evidence into permission to import or close a task.
        correlated = bool(cached and expected_url and cached.get("phase") in ("export_preparing", "export_intent"))
        recovered = self._recover_file(record, expected_url, correlated=correlated)
        if recovered:
            progress("已接回下载目录中的同篇 TXT，原件保留；继续核验交大归属")
            return recovered
        query = {"sa_id": record.sa_id, "title": record.title, "doi": doi(record.doi), "wos": wos(record.wos)}
        if cached and cached.get("phase") not in ("export_preparing", "export_intent"):
            raise SafetyStop("TXT 下载断点结构未知，不重复检索或导出。")
        if not cached:
            progress("WOS 检索并核验唯一文献（最多 120 秒）")
            searched = self.bridge.call("wos_search", query, timeout=120)
            self.audit("wos_search", "已执行", record.sa_id)
            self._guard()
            if not isinstance(searched, dict):
                raise SafetyStop("WOS 检索没有返回唯一单篇记录。")
            expected_url = searched.get("record_url", "")
            record_ut(expected_url)
            cached = {"phase": "export_preparing", "record_url": expected_url}
            self.store.save(record, cached)
        query["record_url"] = expected_url
        if cached["phase"] == "export_intent":
            # A lost ACK must never trigger another Export. Only the same page's
            # unsubmitted owned preview can prove the old command never clicked.
            progress("续查上一轮导出：只读确认，不重复点击 Export")
            status = self.bridge.call("wos_export_status", query, timeout=25)
            if not isinstance(status, dict) or status.get("state") != "unsubmitted":
                raise SafetyStop("上一轮 Export 已提交或结果不明；尚未找到对应 TXT。请等待下载或人工导出后再继续，不重复导出。")
        else:
            self._guard()
            progress("准备 Tab delimited / Full Record；白屏时仅在导出前恢复一次")
            try:
                prepared = self.bridge.call("wos_export_prepare", query, timeout=75)
            except BrowserRejected as exc:
                if exc.action != "wos_export_prepare" or not rejected_before_export(exc):
                    raise
                # Preparation has never reached final Export. If a refresh or
                # page change lost the record, repeat only this completed search
                # once, require the SAME prior UT, then prepare a fresh preview.
                # export_intent / unknown ACKs never enter this branch.
                self._guard()
                progress("尚未提交最终 Export，重新检索找回同一入藏号；不会导出别篇论文")
                searched = self.bridge.call("wos_search", query, timeout=120)
                if (not isinstance(searched, dict)
                        or record_ut(searched.get("record_url", "")) != record_ut(expected_url)):
                    raise SafetyStop("找回的 WOS 入藏号与导出断点不一致，未导出。")
                expected_url = searched["record_url"]
                query["record_url"] = expected_url
                self.store.save(record, {"phase": "export_preparing", "record_url": expected_url})
                self._guard()
                prepared = self.bridge.call("wos_export_prepare", query, timeout=75)
            if (not isinstance(prepared, dict) or prepared.get("ready") is not True
                    or record_ut(prepared.get("record_url", "")) != record_ut(expected_url)):
                raise SafetyStop("未确认完整记录导出预览，未点击最终 Export。")
        self._guard()
        self.store.save(record, {"phase": "export_intent", "record_url": expected_url})
        progress("点击弹窗最终 Export 并等待 TXT；不会重复提交")
        try:
            result = self.bridge.call("wos_export", {**query, "prepared": True}, timeout=75)
        except SafetyStop as exc:
            if (isinstance(exc, BrowserRejected) and exc.action == "wos_export"
                    and rejected_before_export(exc)):
                # The authenticated reply proves the final click never began.
                # Retain that receipt; a refreshed modal may be prepared again.
                self.store.save(record, {"phase": "export_preparing", "record_url": expected_url,
                                         "rejected_before_click": str(exc)})
                raise
            # Some genuine HTTPS downloads have no full-record referrer. Keep
            # the browser boundary strict; adopt local bytes only by exact
            # title + supplied identifier + the searched UT, never by recency.
            progress("导出回执未完成，核查本地对应 TXT（不重新点击 Export）")
            for _ in range(6):
                recovered = self._recover_file(record, expected_url, correlated=True)
                if recovered:
                    return recovered
                if self.stop is not None:
                    if self.stop.wait(1):
                        raise SafetyStop("已暂停；导出断点保留，未重复提交。")
                else:
                    time.sleep(1)
            raise
        self.audit("wos_export", "已执行", record.sa_id)
        progress("核验下载文件的题名、DOI 和 WOS 号")
        if not isinstance(result, dict):
            raise SafetyStop("插件未返回完整的 TXT 下载结果。")
        path = Path(result.get("path", ""))
        if result.get("sa_id") != record.sa_id:
            raise SafetyStop("无法确定本次导出的 TXT 文件。")
        raw, candidate = read_export(path)
        return self._save_file(record, path, raw, candidate, expected_url)


def default_store(base=None):
    return ImportStore(Path(base or Path(__file__).resolve().parent) / "runtime" / "wos-downloads")
