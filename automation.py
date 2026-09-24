"""Evidence-first routing and a durable, single-record WOS import workflow.

No model output is an executable command. No completion flag is written here.
Network/UI mutations are restricted to the paired extension's named operations.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from claim import sa_claim_source
from core import SafetyStop

MAX_TXT = 512 * 1024


def norm(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def doi(value):
    value = norm(value)
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        value = value.removeprefix(prefix).strip()
    if value in ("", "—", "-", "无", "null"):
        return ""
    if not re.fullmatch(r"10\.\d{4,9}/\S+", value):
        raise SafetyStop("DOI 格式不明确，请人工核验。")
    return value


def wos(value):
    value = str(value or "").strip().upper()
    if value in ("", "—", "-", "无", "NULL"):
        return ""
    value = value.removeprefix("WOS:")
    if not re.fullmatch(r"\d{15}", value):
        raise SafetyStop("WOS 入藏号格式不明确，请人工核验。")
    return "WOS:" + value


@dataclass(frozen=True)
class Plan:
    route: str
    reason: str
    issues: tuple = ()


def classify(record, result):
    row = result.get("row", {})
    if row.get("saLzkId") != record.sa_id:
        raise SafetyStop("自动判断的网页 ID 与名单不一致。")
    if record.done or row.get("markStatus") != "待处理":
        return Plan("manual", "已完成或网页非待处理状态，不重复自动操作。")
    count = row.get("matchCount")
    if isinstance(count, bool) or not re.fullmatch(r"\d+", str(count)):
        raise SafetyStop("匹配数量未知。")
    count = int(count)
    ids = [x for x in str(row.get("itemId", "")).strip(",").split(",") if x]
    if count != len(ids):
        raise SafetyStop("匹配数量与平台号不一致。")
    if count == 0:
        # The list may be older than the current SA submission. Never import an
        # old title/identifier just because the SA row ID is still the same.
        if not isinstance(row.get("titleValue"), str) or norm(row["titleValue"]) != norm(record.title):
            raise SafetyStop("网页 SA 题名与名单已不一致/缺失，请先核验并更新名单。")
        if "doiValue" not in row or "wosValue" not in row or doi(row["doiValue"]) != doi(record.doi) or wos(row["wosValue"]) != wos(record.wos):
            raise SafetyStop("网页 SA 的 DOI/WOS 与名单不一致/缺失，停止导入旧资料。")
        return Plan("wos", "本库无匹配：检索 WOS，核验单篇文献与交大归属后导入。")
    if count > 1:
        return Plan("manual", "存在多个平台条目，需人工查重/合并，禁止自动挑选。")
    fields = result.get("comparison")
    if not isinstance(fields, list) or not fields:
        return Plan("manual", "缺少比对字段。")
    if any(not isinstance(f, dict) or not all(isinstance(f.get(k), str) for k in ("label", "sa", "library")) for f in fields):
        raise SafetyStop("比对字段格式未知。")
    issues = tuple(f["label"] for f in fields if norm(f["sa"]) != norm(f["library"]))
    if len({f["label"] for f in fields}) != len(fields):
        return Plan("manual", "字段标签重复，不能自动判断。", issues)
    # Multiple issues are not silently collapsed to an 'all done' route.
    if any("通讯" in label for label in issues):
        return Plan("metadata", "通讯作者差异：打开编辑页，须按原文人工核验。", issues)
    claims = [f for f in fields if f["label"] == "认领状态"]
    if claims and norm(claims[0]["library"]) in ("未认领", "无人认领", "未被认领"):
        try:
            _, staff = sa_claim_source(fields)
            if record.staff_id and staff != record.staff_id:
                raise SafetyStop("编号冲突")
        except SafetyStop:
            return Plan("manual", "认领编号缺失或冲突，请人工核对。", issues)
        return Plan("claim", "未认领：按 SA 括号编号查找人员，唯一署名可预选，提交仍需确认。", issues)
    if issues:
        return Plan("metadata", "存在字段差异，打开编辑页；不凭差异文字覆盖元数据。", issues)
    return Plan("manual", "未发现可安全自动修改的差异，请人工复核后批准。")


def parse_wos(raw):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_TXT:
        raise SafetyStop("WOS TXT 为空或超过单篇安全上限 512 KB。")
    try:
        encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        text = raw.decode(encoding, errors="strict")
        rows = list(csv.reader(io.StringIO(text), delimiter="\t", strict=True))
    except (UnicodeError, csv.Error) as exc:
        raise SafetyStop("不是有效的 UTF-8/UTF-16 WOS 制表符 TXT。") from exc
    rows = [r for r in rows if any(c.strip() for c in r)]
    if len(rows) != 2:
        raise SafetyStop("必须是表头加一篇完整记录，不能导入多篇或纯文本标签格式。")
    headers = [x.strip() for x in rows[0]]
    if len(headers) != len(set(headers)) or len(rows[1]) != len(headers):
        raise SafetyStop("WOS 表头重复或列数不一致。")
    data = dict(zip(headers, rows[1]))
    if not {"TI", "AU", "AF", "SO", "PY", "C1", "UT", "DI"}.issubset(data):
        raise SafetyStop("缺少完整记录字段，请选择 Tab delimited + Full Record 重新导出。")
    if not all(data[k].strip() for k in ("TI", "AU", "SO", "PY", "UT")):
        raise SafetyStop("题名/作者/期刊/年份/WOS 入藏号不完整。")
    if not re.fullmatch(r"(?:19|20)\d{2}", data["PY"].strip()):
        raise SafetyStop("发表年份格式未知。")
    affiliation = data["C1"]
    sjtu = bool(re.search(r"\bShanghai\s+(?:Jiao\s*Tong|Jiaotong)\s+(?:Univ(?:ersity)?)\b|上海交通大学", affiliation, re.I))
    return {"title": data["TI"].strip(), "doi": doi(data["DI"]), "wos": wos(data["UT"]),
            "authors": data["AF"].strip() or data["AU"].strip(), "year": data["PY"].strip(),
            "journal": data["SO"].strip(), "affiliation": affiliation.strip(), "sjtu": sjtu,
            "sha256": hashlib.sha256(raw).hexdigest()}


def identity(record, candidate):
    # Any supplied identifier contradiction wins over matching title/model advice.
    expected_doi, expected_wos = doi(record.doi), wos(record.wos)
    if expected_doi and expected_doi != candidate["doi"]:
        raise SafetyStop("导出文献 DOI 与名单冲突/缺失，禁止导入。")
    if expected_wos and expected_wos != candidate["wos"]:
        raise SafetyStop("导出 WOS 入藏号与名单冲突，禁止导入。")
    if not candidate["sjtu"]:
        raise SafetyStop("WOS 完整记录没有明确上海交通大学署名，请人工核验归属，不能自动设为本校成果。")
    return bool((expected_doi or expected_wos) and norm(record.title) == norm(candidate["title"]))


class ImportStore:
    """Write-ahead journal. A crash/timeout leaves an intent that cannot be retried."""
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "imports.sqlite3"
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS imports (sa_id TEXT PRIMARY KEY, record_key TEXT NOT NULL, data TEXT NOT NULL)")

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def get(self, record):
        with self.connect() as db:
            row = db.execute("SELECT record_key,data FROM imports WHERE sa_id=?", (record.sa_id,)).fetchone()
        if not row:
            return None
        if row[0] != record.key:
            raise SafetyStop("名单内容变更，但存在历史导入记录。请人工核对批次，禁止重建任务重复导入。")
        return json.loads(row[1])

    def save(self, record, state):
        # Re-read protects against accidentally overwriting another revision.
        self.get(record)
        with self.connect() as db:
            db.execute("INSERT INTO imports VALUES(?,?,?) ON CONFLICT(sa_id) DO UPDATE SET data=excluded.data",
                       (record.sa_id, record.key, json.dumps(state, ensure_ascii=False)))

    def archive(self, raw):
        sha = hashlib.sha256(raw).hexdigest()
        path = self.root / (sha + ".txt")
        if path.exists():
            if path.read_bytes() != raw:
                raise SafetyStop("已存档导出文件被修改。")
        else:
            with path.open("xb") as stream:
                stream.write(raw)
                stream.flush()
                import os
                os.fsync(stream.fileno())
        return sha

    def bytes(self, state):
        sha = state["candidate"]["sha256"]
        if not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise SafetyStop("文件校验码无效。")
        raw = (self.root / (sha + ".txt")).read_bytes()
        if hashlib.sha256(raw).hexdigest() != sha:
            raise SafetyStop("原始 WOS 导出文件发生变化。")
        parse_wos(raw)
        return raw


class WOSFlow:
    def __init__(self, bridge, store, unchanged=lambda: None, progress=lambda text: None):
        self.bridge, self.store = bridge, store
        self.unchanged, self.progress = unchanged, progress

    def call(self, action, payload):
        self.unchanged()
        self.progress({"wos_search": "WOS 检索中…", "wos_export": "导出单篇完整记录…",
                       "import_scan": "核对目标批次…", "import_upload": "上传 WOS TXT…",
                       "import_submit": "提交导入一次…", "import_check": "回读批次和文献…",
                       "import_push": "按 PPT 设置推送一次…"}.get(action, "核验网页…"))
        return self.bridge.call(action, payload, timeout=75)

    def prepare(self, record, current_wos=False):
        existing = self.store.get(record)
        if existing:
            return existing
        query = {"sa_id": record.sa_id, "title": record.title, "doi": doi(record.doi), "wos": wos(record.wos)}
        if not current_wos:
            self.call("wos_search", query)
        result = self.call("wos_export", query)
        # This is the exact download reported for this export, not the latest file
        # in Downloads. Only TXT bytes (never arbitrary secrets) may be forwarded.
        path = Path(result.get("path", ""))
        if result.get("sa_id") != record.sa_id or not path.is_absolute() or path.suffix.lower() != ".txt" or path.is_symlink():
            raise SafetyStop("无法确定本次导出的 TXT 文件；请人工核验下载。")
        if not path.is_file() or not 1 <= path.stat().st_size <= MAX_TXT:
            raise SafetyStop("本次下载未完成或文件大小异常。")
        raw = path.read_bytes()
        candidate = parse_wos(raw)
        strong = identity(record, candidate)
        self.store.archive(raw)
        state = {"phase": "exported", "candidate": candidate, "identity_confirmed": strong,
                 "instructions": "SA补充-" + record.sa_id, "batch": None}
        self.store.save(record, state)
        return state

    def proceed(self, record, confirm_identity=False):
        state = self.store.get(record)
        if not state:
            raise SafetyStop("请先自动判断并检索 WOS。")
        candidate = state["candidate"]
        identity(record, candidate)
        if confirm_identity:
            state["identity_confirmed"] = True
            self.store.save(record, state)
        if not state["identity_confirmed"]:
            return state
        if state["phase"] == "pushed":
            return state
        raw = self.store.bytes(state)
        common = {"sa_id": record.sa_id, "instructions": state["instructions"], "candidate": candidate}
        # Recheck current SA status immediately before beginning a new upload.
        if state["phase"] == "exported":
            result = self.call("search", {"sa_id": record.sa_id})
            if classify(record, result).route != "wos":
                raise SafetyStop("比对状态已变化，不再符合缺失条目的导入条件。")
            scan = self.call("import_scan", common)
            if scan.get("batches") != []:
                raise SafetyStop("已有同说明批次。请人工检查，禁止重复上传。")
            state["phase"] = "upload_intent"
            self.store.save(record, state)
            uploaded = self.call("import_upload", {**common, "content": base64.b64encode(raw).decode()})
            if uploaded.get("uploaded") is not True or uploaded.get("sha256") != candidate["sha256"]:
                raise SafetyStop("上传回读不匹配。请核验网页，禁止重试。")
            state["upload"] = uploaded
            state["phase"] = "import_intent"
            self.store.save(record, state)
            self.call("import_submit", {**common, "upload": uploaded})
        if state["phase"] == "upload_intent":
            raise SafetyStop("上次上传结果不明。请人工核对上传窗口；不会自动再次上传。")
        if state["phase"] in ("import_intent", "imported", "push_intent"):
            checked = self.call("import_check", {**common, "batch_id": (state.get("batch") or {}).get("id", ""),
                                                  "expect_pushed": state["phase"] == "push_intent"})
            if checked.get("verified") is not True:
                raise SafetyStop("导入/推送尚未核验成功。等待后台完成后可点“继续 / 核验”，仅只读检查。")
            batch = checked.get("batch", {})
            if not isinstance(batch.get("id"), str) or not batch["id"]:
                raise SafetyStop("批次 ID 无效。")
            if state.get("batch") and state["batch"]["id"] != batch["id"]:
                raise SafetyStop("回读到了不同批次，停止。")
            state["batch"] = batch
            if str(batch.get("status")) == "2":
                state["phase"] = "pushed"
                self.store.save(record, state)
                return state
            if state["phase"] == "push_intent":
                raise SafetyStop("推送已提交但尚未核验完成。不会再次推送，请等待后只读核验。")
            state["phase"] = "imported"
            self.store.save(record, state)
            state["phase"] = "push_intent"
            self.store.save(record, state)
            pushed = self.call("import_push", {**common, "batch": batch})
            if pushed.get("submitted") is not True:
                raise SafetyStop("推送结果不明，请人工核对。")
            # Recursion only enters the read-only push_intent branch, never retry.
            return self.proceed(record)
        return state
