"""Bounded public-source discovery, separate from import/identity decisions.

Search broadens recall, not acceptance: every selected work must still pass
the unchanged two-source identity, author and printed-affiliation verification.
Only anonymous public GETs are used; no key registration or paid account setup.
"""
from collections import OrderedDict
from datetime import datetime, timezone
import json
import re
import time
from urllib.parse import quote, urlencode

from core import SafetyStop
from metadata_identity import MetadataSkip, doi, text, title_key


class MetadataDiscovery:
    def __init__(self, fetch, cancelled=lambda: False, progress=lambda _: None):
        self.fetch, self.cancelled, self.progress = fetch, cancelled, progress
        self.memo = OrderedDict()
        self.steps = []

    def discard_current(self):
        # A structurally invalid or conflicting response is not reusable proof.
        # This only forgets public GETs, never import intents or disk archives.
        for step in self.steps:
            self.memo.pop(step['url'], None)

    def get(self, url, label):
        if self.cancelled():
            raise SafetyStop("已请求暂停。")
        now = time.monotonic()
        entry = self.memo.get(url)
        cached = entry is not None and now - entry[0] < 300
        step = {"step": label, "url": url, "reused": cached}
        self.steps.append(step)
        self.progress(label + ("（复用本轮公开查询，仍逐条核验）" if cached else ""))
        if cached:
            self.memo.move_to_end(url)
            answer = json.loads(entry[1])
            fetched = entry[2]
        else:
            # Exceptions (including permissions, 429 and network errors) never
            # become cached negative results and never trigger another request.
            answer = self.fetch(url)
            fetched = datetime.now(timezone.utc).isoformat()
            encoded = json.dumps(answer, ensure_ascii=False)
            if len(encoded.encode('utf-8')) <= 1_000_000:
                self.memo[url] = (time.monotonic(), encoded, fetched)
                self.memo.move_to_end(url)
                while len(self.memo) > 32:
                    self.memo.popitem(last=False)
        step["returned"] = True
        step["fetched_utc"] = fetched
        return answer

    def crossref_work(self, identifier):
        response = self.get("https://api.crossref.org/works/" + quote(identifier, safe=""), "Crossref DOI 核验")
        if response is None:
            raise MetadataSkip("Crossref未查询到")
        work = response.get("message") if isinstance(response, dict) else None
        if not isinstance(work, dict) or doi(work.get("DOI")) != identifier:
            raise SafetyStop("Crossref DOI 响应未对应请求标识，未选择其他论文。")
        return work

    def exact(self, response, record, source):
        if source == "Crossref":
            rows = response.get("message", {}).get("items") if isinstance(response, dict) and isinstance(response.get("message"), dict) else None
        else:
            rows = response.get("results") if isinstance(response, dict) else None
        if not isinstance(rows, list) or len(rows) > (50 if source == "Crossref" else 25):
            raise SafetyStop(source + " 检索响应未确认，未判为未查询到。")
        target, matched, preview = title_key(record.title), [], []
        for row in rows:
            if not isinstance(row, dict):
                raise SafetyStop(source + " 检索候选结构异常。")
            titles = row.get("title")
            value = titles[0] if source == "Crossref" and isinstance(titles, list) and len(titles) == 1 else titles if source == "OpenAlex" else ""
            if not isinstance(value, str):
                raise SafetyStop(source + " 检索题名结构异常。")
            preview.append({"title": text(value), "doi": row.get("DOI" if source == "Crossref" else "doi")})
            if title_key(value) == target:
                matched.append(row)
        self.steps[-1].update(candidate_count=len(rows), exact_count=len(matched), candidates=preview)
        if len(matched) > 1:
            raise MetadataSkip("题名未唯一匹配")
        return matched[0] if matched else None

    def discover(self, record):
        self.steps = []
        if not title_key(record.title):
            raise MetadataSkip("题名待核验")
        identifier, expected_openalex = doi(record.doi), None
        if identifier:
            # A supplied DOI is binding: 404 is not permission to substitute a
            # similar title's DOI from either source.
            cr = self.crossref_work(identifier)
        else:
            response = self.get("https://api.crossref.org/works?" + urlencode({"query.title": record.title, "rows": 10}), "Crossref 题名检索（10 条）")
            cr = self.exact(response, record, "Crossref")
            if cr is None:
                response = self.get("https://api.crossref.org/works?" + urlencode({"query.bibliographic": record.title, "rows": 50, "select": "DOI,title"}), "Crossref 扩展检索（50 条）")
                found = self.exact(response, record, "Crossref")
                if found is not None:
                    identifier = doi(found.get("DOI"))
                    if not identifier:
                        raise MetadataSkip("Crossref DOI待核验")
                    cr = self.crossref_work(identifier)
            if cr is None:
                response = self.get("https://api.openalex.org/works?" + urlencode({"search": record.title, "per_page": 25, "select": "id,doi,title"}), "OpenAlex 题名检索（25 条）")
                found = self.exact(response, record, "OpenAlex")
                if found is None:
                    raise MetadataSkip("两源未精确匹配")
                expected_openalex = found.get("id")
                identifier = doi(found.get("doi"))
                if not identifier or not isinstance(expected_openalex, str) or not re.fullmatch(r"https://openalex.org/W\d+", expected_openalex):
                    raise MetadataSkip("OpenAlex标识待核验")
                cr = self.crossref_work(identifier)
        if not isinstance(cr, dict) or not doi(cr.get("DOI")):
            raise SafetyStop("Crossref DOI 响应结构异常。")
        identifier = doi(cr["DOI"])
        oa = self.get("https://api.openalex.org/works/https://doi.org/" + quote(identifier, safe="/"), "OpenAlex DOI 与原始署名核验")
        if oa is None:
            raise MetadataSkip("OpenAlex未查询到")
        if not isinstance(oa, dict) or expected_openalex is not None and oa.get("id") != expected_openalex:
            raise SafetyStop("OpenAlex 题名检索与 DOI 回读标识不一致，未选其他论文。")
        return cr, oa
