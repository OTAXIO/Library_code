"""Genuine WOS Full Record TXT capture through the paired extension only."""
from __future__ import annotations

import re
from pathlib import Path

from automation import ImportStore, MAX_TXT, doi, wos, norm, parse_wos
from core import SafetyStop


def preflight(bridge):
    """Read actual extension capabilities before Search or export."""
    # Read-only capability/access handshake, not search-form readiness. The
    # bound tab is activated and prepared by the extension before any Search.
    try:
        result = bridge.call("wos_diagnose", {}, timeout=25)
    except SafetyStop as exc:
        if "未知 WOS 调度命令" in str(exc):
            raise SafetyStop("当前插件仍是旧版本，请重载 0.4.3、刷新工作页并重新绑定；尚未提交检索。") from exc
        raise
    if (not isinstance(result, dict) or type(result.get("wos_download_protocol")) is not int
            or result.get("wos_download_protocol") != 1
            or type(result.get("search_prepare_protocol")) is not int
            or result.get("search_prepare_protocol") != 1
            or result.get("result_reader") != "shared-diagnostic"
            or result.get("read_results_world") != "ISOLATED"
            or not re.fullmatch(r"\d+\.\d+\.\d+", str(result.get("extension_version", "")))
            or tuple(map(int, result["extension_version"].split("."))) < (0, 4, 3)):
        raise SafetyStop("插件下载接口不兼容，请重载 0.4.3、刷新 WOS 页并重新绑定；未提交检索或下载。")
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
    if page["dialog_count"]:
        raise SafetyStop("WOS 当前存在操作弹窗，请先人工处理；未提交检索。")
    return {"extension_version": result["extension_version"]}


class WOSDownload:
    """Download/archive only; the orchestrator validates affiliation and identity."""
    def __init__(self, bridge, store, unchanged, stop, audit):
        self.bridge, self.store, self.unchanged, self.stop, self.audit = bridge, store, unchanged, stop, audit

    def prepare(self, record, progress=lambda _: None):
        self.unchanged()
        cached = self.store.get(record)
        if cached:
            progress("复用已保存的 TXT；不重复检索或下载")
            return cached
        query = {"sa_id": record.sa_id, "title": record.title, "doi": doi(record.doi), "wos": wos(record.wos)}
        for action in ("wos_search", "wos_export"):
            if self.stop is not None and self.stop.is_set():
                raise SafetyStop("已暂停下载。")
            self.unchanged()
            timeout = 120 if action == "wos_search" else 75
            progress("WOS 检索并核验唯一文献（最多 120 秒）" if action == "wos_search"
                     else "导出完整记录并等待 TXT（最多 75 秒）")
            result = self.bridge.call(action, query, timeout=timeout)
            self.audit(action, "已执行", record.sa_id)
        progress("核验下载文件的题名、DOI 和 WOS 号")
        if not isinstance(result, dict):
            raise SafetyStop("插件未返回完整的 TXT 下载结果。")
        path = Path(result.get("path", ""))
        if result.get("sa_id") != record.sa_id or not path.is_absolute() or path.suffix.lower() != ".txt" or path.is_symlink():
            raise SafetyStop("无法确定本次导出的 TXT 文件。")
        if not path.is_file() or not 1 <= path.stat().st_size <= MAX_TXT:
            raise SafetyStop("下载未完成或文件大小异常。")
        raw = path.read_bytes()
        candidate = parse_wos(raw)
        for name in ("doi", "wos"):
            if query[name] and query[name] != candidate[name]:
                raise SafetyStop(f"下载记录的 {name.upper()} 与名单冲突，未采纳。")
        confirmed = bool((query["doi"] or query["wos"]) and norm(record.title) == norm(candidate["title"]))
        self.store.archive(raw)
        state = {"phase": "downloaded", "candidate": candidate, "identity_confirmed": confirmed}
        self.store.save(record, state)
        return state


def default_store(base=None):
    return ImportStore(Path(base or Path(__file__).resolve().parent) / "runtime" / "wos-downloads")
