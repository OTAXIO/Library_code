"""Public metadata only: Crossref identity + OpenAlex printed affiliations.

No WOS identifier, coverage, author role, or institutional attribution is invented.
All successful responses are archived before an import can be prepared.
"""
from __future__ import annotations

import html
import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from core import SafetyStop, digest

SOURCE = "Crossref+OpenAlex"
KIND = "crossref-openalex-v1"
SJTU = re.compile(r"\bShanghai\s+(?:Jiao\s*Tong|Jiaotong)\s+Univ(?:ersity)?\b|上海交通大学", re.I)


class MetadataSkip(SafetyStop):
    def __init__(self, note):
        super().__init__(note)
        self.note = note


def norm(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def doi(value):
    value = re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", norm(value))
    if value and not re.fullmatch(r"10\.\d{4,9}/[^\s?#]+", value):
        raise MetadataSkip("DOI待核验")
    return value


def text(value):
    if not isinstance(value, str) or len(value) > 16000:
        raise MetadataSkip("元数据格式待核验")
    value = html.unescape(re.sub(r"<[^>]+>", "", value)).strip()
    if re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", value):
        raise MetadataSkip("元数据格式待核验")
    return value


def name_tokens(value):
    return sorted(re.findall(r"[^\W_]+", norm(value), re.UNICODE))


def merge_metadata(record, crossref, openalex):
    """A strict, deterministic join; normalized OpenAlex institutions alone fail."""
    if not isinstance(crossref, dict) or not isinstance(openalex, dict):
        raise SafetyStop("元数据接口返回结构异常，未判为未收录。")
    cd, od = doi(crossref.get("DOI")), doi(openalex.get("doi"))
    titles = crossref.get("title")
    if not isinstance(titles, list) or len(titles) != 1 or not cd or cd != od:
        raise MetadataSkip("文献标识不一致")
    title = text(titles[0])
    if norm(title) != norm(record.title) or norm(openalex.get("title")) != norm(title):
        raise MetadataSkip("题名待核验")
    if record.doi and doi(record.doi) != cd:
        raise MetadataSkip("DOI不一致")
    # Other identifiers are not verifiable through these two public APIs.
    if record.wos:
        raise MetadataSkip("WOS号待核验")
    conference = crossref.get("type") == "proceedings-article" and openalex.get("type") == "conference-paper"
    journal_article = crossref.get("type") == "journal-article" and openalex.get("type") in ("article", "review")
    if not journal_article and not conference:
        raise MetadataSkip("文献类型待核验")
    authors, byline = crossref.get("author"), openalex.get("authorships")
    if (not isinstance(authors, list) or not isinstance(byline, list) or not 1 <= len(authors) <= 100
            or len(authors) != len(byline)
            or type(openalex.get("authors_count")) is int and openalex["authors_count"] != len(byline)):
        raise MetadataSkip("作者信息不完整")
    names, addresses, mappings = [], [], []
    for author, joined in zip(authors, byline):
        if not isinstance(author, dict) or not isinstance(joined, dict):
            raise MetadataSkip("作者信息不完整")
        given, family = text(author.get("given", "")), text(author.get("family", ""))
        printed = text(joined.get("raw_author_name", ""))
        full = (given + " " + family).strip()
        if (not given or not family or name_tokens(full) != name_tokens(printed)
                or re.search(r"[;()\r\n]", full) or len(full) > 200):
            raise MetadataSkip("作者对应待核验")
        raw = joined.get("raw_affiliation_strings")
        if not isinstance(raw, list) or not raw:
            raise MetadataSkip("署名单位不完整")
        indices = []
        for address in raw:
            address = text(address)
            # The platform's actual template has no escaping for semicolons.
            if (not address or re.search(r"[;\r\n]", address) or "…" in address
                    or "..." in address or re.search(r"\b(?:unknown|unavailable)\b", address, re.I)):
                raise MetadataSkip("署名单位待核验")
            if address not in addresses:
                addresses.append(address)
            indices.append(addresses.index(address) + 1)
        names.append(full)
        mappings.append(sorted(set(indices)))
    if len({norm(name) for name in names}) != len(names):
        raise MetadataSkip("同名作者待核验")
    if not any(SJTU.search(address) for address in addresses):
        # A negative API attribution is not sufficient to close the SA as non-SJTU.
        raise MetadataSkip("未确认交大署名")
    journal = crossref.get("container-title")
    if (not isinstance(journal, list) or not 1 <= len(journal) <= (5 if conference else 1)
            or any(not text(value) for value in journal)):
        raise MetadataSkip("期刊信息待核验")
    date = (crossref.get("published-print") or crossref.get("published") or {}).get("date-parts")
    if (not isinstance(date, list) or len(date) != 1 or not isinstance(date[0], list)
            or not 1 <= len(date[0]) <= 3 or any(type(p) is not int for p in date[0])):
        raise MetadataSkip("发表日期待核验")
    parts = date[0]
    try:
        datetime(*(parts + [1] * (3 - len(parts))))
    except (ValueError, TypeError):
        raise MetadataSkip("发表日期待核验") from None
    if parts[0] > datetime.now(timezone.utc).year:
        raise MetadataSkip("发表日期待核验")
    issued = "-".join([str(parts[0]), *[f"{p:02d}" for p in parts[1:]]])
    oa_id = openalex.get("id", "")
    if not re.fullmatch(r"https://openalex.org/W\d+", oa_id):
        raise MetadataSkip("OpenAlex标识待核验")
    fields = {"题名": title, "作者": ";".join(f"{n}({','.join(map(str, ids))})" for n, ids in zip(names, mappings)),
        "作者单位": ";".join(f"({i}){a}" for i, a in enumerate(addresses, 1)),
        "发表日期": issued, "年份": str(parts[0]), "发表期刊": text(journal[0]), "DOI": cd,
        "URL": "https://doi.org/" + cd, "出版者": text(crossref.get("publisher", "")),
        "页码": text(crossref.get("page", "")), "期号": text(crossref.get("issue", "")),
        "卷号": text(crossref.get("volume", "")), "第三方ID": oa_id}
    issns = crossref.get("ISSN", [])
    if isinstance(issns, list) and all(isinstance(v, str) and re.fullmatch(r"\d{4}-\d{3}[\dX]", v) for v in issns):
        fields["ISSN"] = ";".join(issns)
    if conference:
        event = crossref.get("event")
        if not isinstance(event, dict) or not text(event.get("name", "")):
            raise MetadataSkip("会议信息不完整")
        # Use only labels in the repository's actual conference template.
        # Retain every supplied container title; do not guess which is a series.
        for label in ("发表期刊", "URL", "出版者", "期号", "卷号", "ISSN"):
            fields.pop(label, None)
        fields.update({"出处": " / ".join(text(value) for value in journal),
            "会议名称": text(event["name"]), "相关网址": "https://doi.org/" + cd})
        if len(journal) == 1:
            fields["会议录名称"] = text(journal[0])
        if event.get("location"):
            fields["会议地点"] = text(event["location"])
    candidate = {"kind": KIND, "title": title, "doi": cd, "wos": "", "sjtu": True,
        "source": SOURCE, "authors": ";".join(names), "author_names": names, "addresses": addresses,
        "affiliation": ";".join(addresses), "author_mapping": mappings, "fields": fields,
        "crossref_url": "https://api.crossref.org/works/" + quote(cd, safe=""), "openalex_url": oa_id}
    if conference:
        candidate.update(document_type="conference-paper", model_name="会议论文")
    candidate["sha256"] = digest(candidate)
    return candidate


class MetadataClient:
    def __init__(self, root, cancelled=lambda: False):
        self.root, self.cancelled = Path(root), cancelled
        self.root.mkdir(parents=True, exist_ok=True)
        self.last_request = 0.0

    def get(self, url):
        # A public GET is safe, but never retry 429/5xx aggressively or call it zero.
        while time.monotonic() - self.last_request < 1.1:
            if self.cancelled():
                raise SafetyStop("已请求暂停。")
            time.sleep(0.1)
        self.last_request = time.monotonic()
        if self.cancelled():
            raise SafetyStop("已请求暂停。")
        try:
            request = Request(url, headers={"User-Agent": "SA-Library-Assistant/0.4.30 (metadata verification)",
                                           "Accept": "application/json"})
            with urlopen(request, timeout=25) as response:
                if response.url.split('/')[2] not in ("api.crossref.org", "api.openalex.org"):
                    raise SafetyStop("元数据接口重定向异常。")
                raw = response.read(4_000_001)
            if len(raw) > 4_000_000:
                raise SafetyStop("元数据接口响应过大。")
            data = json.loads(raw)
            if isinstance(data, str):  # Observed OpenAlex JSON-string envelope.
                data = json.loads(data)
            if not isinstance(data, dict):
                raise ValueError("object required")
            return data
        except HTTPError as exc:
            if exc.code == 404:
                return None
            raise SafetyStop(f"元数据接口 HTTP {exc.code}；整批暂停，未标记后续任务。") from None
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            raise SafetyStop(f"元数据接口未确认成功（{type(exc).__name__}），未判为未收录。") from None

    def prepare(self, record):
        key = digest({"record": record.key, "protocol": KIND})
        folder = self.root / key
        if (folder / "metadata.json").exists():
            cached = json.loads((folder / "metadata.json").read_text(encoding="utf-8"))
            candidate = merge_metadata(record, cached["crossref"], cached["openalex"])
            if candidate != cached["candidate"]:
                raise SafetyStop("元数据缓存校验失败，未自动替换。")
            return candidate, folder
        identifier = doi(record.doi)
        if identifier:
            response = self.get("https://api.crossref.org/works/" + quote(identifier, safe=""))
            if response is None:
                raise MetadataSkip("Crossref未查询到")
            cr = response.get("message")
        else:
            response = self.get("https://api.crossref.org/works?" + urlencode({"query.title": record.title, "rows": 10}))
            rows = response.get("message", {}).get("items") if response else None
            if not isinstance(rows, list):
                raise SafetyStop("Crossref 检索响应未确认，未判为零结果。")
            exact = [v for v in rows if isinstance(v, dict) and isinstance(v.get("title"), list)
                     and len(v["title"]) == 1 and norm(text(v["title"][0])) == norm(record.title)]
            if len(exact) != 1:
                raise MetadataSkip("题名未唯一匹配" if exact else "Crossref未查询到")
            cr = exact[0]
        if not isinstance(cr, dict) or not doi(cr.get("DOI")):
            raise SafetyStop("Crossref DOI 响应结构异常。")
        oa = self.get("https://api.openalex.org/works/https://doi.org/" + quote(doi(cr["DOI"]), safe="/"))
        if oa is None:
            raise MetadataSkip("OpenAlex未查询到")
        candidate = merge_metadata(record, cr, oa)
        folder.mkdir(exist_ok=False)
        payload = {"record_key": record.key, "fetched_utc": datetime.now(timezone.utc).isoformat(),
                   "crossref": cr, "openalex": oa, "candidate": candidate}
        # Exclusive private archive. Credentials and cookies are never requested.
        with (folder / "metadata.json").open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
        return candidate, folder
