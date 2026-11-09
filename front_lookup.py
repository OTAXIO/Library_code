"""Decide from the library frontend whether a zero-match paper is already held.

Read-only: searches www.ir.lib.sjtu.edu.cn by the SA row's DOI, WOS number and
exact title (quoted phrase, which the frontend treats as a whole-title match),
all restricted to this library. Only a single item agreeing on the title and
on DOI or WOS, with no conflicting identifier, may be linked. Absence must be
positive: every search answered, none truncated. Anything else is for review.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from automation import doi, norm, wos
from core import SafetyStop

SEARCH_TIMEOUT = 60


@dataclass(frozen=True)
class FrontLookup:
    item: dict | None = None   # the unique library item to link
    review: str = ""           # short reason when this cannot be decided automatically

    @property
    def absent(self):
        return self.item is None and not self.review


def title_key(value):
    """Case, width, spacing and punctuation-insensitive title comparison."""
    text = unicodedata.normalize("NFKC", norm(value))
    return re.sub(r"[\W_]+", "", text)


def _identifier(normalize, value):
    try:
        return normalize(value)
    except SafetyStop:
        return None


def searches(record):
    queries = []
    sa_doi, sa_wos = _identifier(doi, record.doi), _identifier(wos, record.wos)
    if sa_doi is None or sa_wos is None:
        raise SafetyStop("SA 的 DOI 或 WOS 号格式不明确")
    if sa_doi:
        queries.append(("doi", sa_doi))
    if sa_wos:
        queries.append(("wosId", sa_wos))
    title = " ".join(str(record.title or "").replace('"', " ").split())
    if title_key(title):
        queries.append(("title", f'"{title}"'))
    if not queries:
        raise SafetyStop("SA 没有可检索的题名、DOI 或 WOS 号")
    return queries, sa_doi, sa_wos


def _answer(result, field, text):
    if (not isinstance(result, dict) or result.get("front_search_protocol") != 1 or result.get("verified") is not True
            or result.get("field") != field or result.get("text") != text or type(result.get("total")) is not int
            or result["total"] < 0 or not isinstance(result.get("items"), list) or type(result.get("complete")) is not bool):
        raise SafetyStop("前台检索回执不完整，请重载扩展 0.4.58 并绑定已登录的前台页。")
    items = result["items"]
    if len(items) > result["total"] or result["complete"] != (len(items) >= result["total"]):
        raise SafetyStop("前台检索结果数量不一致。")
    for item in items:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str) or not re.fullmatch(r"\d{1,40}", item["id"])
                or any(not isinstance(item.get(key), str) for key in ("title", "doi", "wos"))
                or type(item.get("deleted")) is not bool):
            raise SafetyStop("前台成果条目格式异常。")
    return items, result["complete"]


def lookup(call, record, collect=lambda items: None):
    """``call(action, payload)`` sends one read-only frontend command."""
    try:
        queries, sa_doi, sa_wos = searches(record)
    except SafetyStop:
        return FrontLookup(review="SA 编号或题名待核")
    found = {}
    for field, text in queries:
        items, complete = _answer(call("front_search", {"field": field, "text": text}), field, text)
        collect(items)
        if not complete:
            return FrontLookup(review="前台检索结果过多，待人工核验")
        for item in items:
            evidence = (norm(item["title"]), _identifier(doi, item["doi"]),
                        _identifier(wos, item["wos"]), item["deleted"], item.get("model"))
            previous = found.get(item["id"])
            if previous is not None and previous[1] != evidence:
                return FrontLookup(review="前台同一平台号信息冲突，待人工核验")
            found.setdefault(item["id"], (item, evidence))
    sa_title = title_key(record.title)
    relevant, linkable = [], []
    for item, (item_title, item_doi, item_wos, deleted, _) in found.values():
        same_title = bool(sa_title) and title_key(item_title) == sa_title
        same_id = bool(sa_doi and item_doi == sa_doi or sa_wos and item_wos == sa_wos)
        if not (same_title or same_id):
            continue
        relevant.append(item)
        conflict = (item_doi is None or item_wos is None or sa_doi and item_doi and item_doi != sa_doi
                    or sa_wos and item_wos and item_wos != sa_wos)
        if same_title and same_id and not conflict and not deleted:
            linkable.append({**item, "doi": item_doi, "wos": item_wos})
    if not relevant:
        return FrontLookup(review="前台检索有结果，文献身份待核" if found else "")
    if len(relevant) > 1:
        return FrontLookup(review="前台有多条同一文献，待人工核验")
    if not linkable:
        return FrontLookup(review="前台疑似已有，信息不一致待核")
    return FrontLookup(item=linkable[0])
