"""Single orchestrator for zero-match supplementation.

UI never drives individual writes. WOSDownload owns genuine TXT capture;
WOSFlow owns upload/import/push; this module owns library resolution, SA closure
and Excel reconciliation. Each write has a durable intent and read-back guard.
CAPTCHA/session failures pause the queue without turning its tail into failures.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from automation import WOSFlow, classify, doi, identity, norm, parse_wos, wos
from claim import sa_claim_source
from claim_batch import automatic_claim_selection
from approval import verify_claim_result
from core import SafetyStop
from roster_write import mark_skipped_many, reconcile_processed, record_data_sources
from wos_batch import WOSDownload, preflight
from wos_policy import WOSPolicyStop, require_no_author_review, zero_result_note
from skip_notes import brief_skip_note

OWNER = "谭勋策"
COMPLETION_NOTE = "已入库"
EXISTING_NOTE = "已核验本库同一文献；补充平台关联"


class PaperSkip(SafetyStop):
    def __init__(self, message, note):
        super().__init__(message)
        self.note = note


class WorkflowStore:
    """Separate journal: download/import journals retain their existing contracts."""
    def __init__(self, root):
        self.path = Path(root) / "zero-match.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS jobs (sa_id TEXT PRIMARY KEY, record_key TEXT NOT NULL, data TEXT NOT NULL)")

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
            row = db.execute("SELECT record_key,data FROM jobs WHERE sa_id=?", (record.sa_id,)).fetchone()
        if not row:
            return {}
        if row[0] != record.key:
            raise SafetyStop("历史补录任务的名单内容已改变，不能覆盖或重复提交。")
        state = json.loads(row[1])
        if not isinstance(state, dict):
            raise SafetyStop("补录进度日志结构异常。")
        return state

    def save(self, record, state):
        self.get(record)
        with self.connect() as db:
            db.execute("INSERT INTO jobs VALUES(?,?,?) ON CONFLICT(sa_id) DO UPDATE SET data=excluded.data",
                       (record.sa_id, record.key, json.dumps(state, ensure_ascii=False)))


class SerialWOS:
    """Bounded serial pacing, not an anti-bot bypass. Never retry a Search."""
    def __init__(self, transport, store, stop, progress, interval=15):
        self.transport, self.stop, self.progress = transport, stop, progress
        self.store = store
        self.interval, self.last_search = max(10, interval), 0.0
        self.ready = False

    def call(self, action, payload, timeout=120):
        if not self.ready:
            preflight(self.transport, resume_export=action in ("wos_export_prepare", "wos_export_status", "wos_export"))
            self.ready = True
        if action == "wos_search":
            while time.monotonic() - self.last_search < self.interval:
                self.progress("等待下一次 WOS 检索（串行节流，不重复提交）…")
                if self.stop.wait(min(1, self.interval - (time.monotonic() - self.last_search))):
                    raise SafetyStop("已请求暂停。")
            self.last_search = time.monotonic()
        return self.transport.call(action, payload, timeout=timeout)


@dataclass
class BatchResult:
    roster: object
    outcomes: list = field(default_factory=list)
    remaining: int = 0
    halted: bool = False
    reason: str = ""


def select_records(roster, owner, limit, scope="pending"):
    if owner != OWNER:
        raise SafetyStop("本次补录试验仅允许谭勋策的任务。")
    if type(limit) is not int or not 1 <= limit <= 100 or scope not in ("pending", "skipped"):
        raise SafetyStop("本轮范围必须为待补/跳过项目，条数为 1–100。")
    roster.assert_unchanged()
    return [r for r in roster.records if r.owner == owner and not r.done and r.matches == 0
            and r.skipped == (scope == "skipped")][:limit]


def verify_sa(record, row, facts=True):
    if not isinstance(row, dict) or row.get("saLzkId") != record.sa_id:
        raise SafetyStop("后台 SA 编号不一致。")
    if row.get("markStatus") not in ("待处理", "已处理"):
        raise SafetyStop("后台处理状态未知。")
    if facts and (norm(row.get("titleValue")) != norm(record.title) or
            "doiValue" not in row or doi(row["doiValue"]) != doi(record.doi) or
            "wosValue" not in row or wos(row["wosValue"]) != wos(record.wos) or
            record.staff_id and row.get("gh") != record.staff_id):
        raise SafetyStop("后台 SA 题名、标识或人员编号与名单不一致。")


def resolved_item(answer, candidate):
    if not isinstance(answer, dict) or answer.get("verified") is not True or not isinstance(answer.get("items"), list):
        raise SafetyStop("未取得完整的本库只读检索证据。")
    items = answer["items"]
    if len(items) > 1:
        raise SafetyStop("本库同一文献存在多个平台条目，先人工合并；不自动选择。")
    if not items:
        return None
    item = items[0]
    if (not isinstance(item, dict) or not isinstance(item.get("id"), str) or
            not re.fullmatch(r"\d{1,40}", item["id"]) or norm(item.get("title")) != norm(candidate["title"]) or
            wos(item.get("wos")) != candidate["wos"] or doi(item.get("doi")) != candidate["doi"]):
        raise SafetyStop("本库记录与 TXT 的题名、DOI、WOS号不一致。")
    return item


def verify_paper_comparison(comparison, candidate, record):
    if not isinstance(comparison, list):
        raise SafetyStop("缺少关联后的比对详情。")
    for label, expected, source, normalizer in (("题名", candidate["title"], record.title, norm),
                                                ("DOI", candidate["doi"], record.doi, doi),
                                                ("WOS记录号", candidate["wos"], record.wos, wos)):
        fields = [f for f in comparison if isinstance(f, dict) and f.get("label") == label]
        if len(fields) != 1 or not isinstance(fields[0].get("library"), str) or normalizer(fields[0]["library"]) != normalizer(expected):
            raise SafetyStop("关联后的本库详情未确认：" + label)
        if not isinstance(fields[0].get("sa"), str) or normalizer(fields[0]["sa"]) != normalizer(source):
            raise SafetyStop("关联后的 SA 详情与名单不一致：" + label)


def verify_comparison(comparison, candidate, record, allow_unclaimed=False):
    verify_paper_comparison(comparison, candidate, record)
    # These labels/role flags are produced by the site's public comparison
    # formatter. Agreement is read-only evidence; we never infer or change a role.
    def field(label):
        matches = [f for f in comparison if isinstance(f, dict) and f.get("label") == label]
        if len(matches) != 1 or any(not isinstance(matches[0].get(side), str) for side in ("sa", "library")):
            raise PaperSkip("缺少完整作者/单位详情，暂不结案。", "已补录/关联，作者或单位详情待核验")
        return matches[0]
    claimed = field("认领状态")
    claim_state = claimed["library"].strip()
    if claim_state != "已认领" and not (allow_unclaimed and claim_state in ("未认领", "无人认领", "未被认领")):
        raise PaperSkip("已关联文献，但未回读到本条已认领。", "已补录/关联，尚未认领，暂不结案")
    try:
        _, staff = sa_claim_source(comparison)
    except SafetyStop as exc:
        raise PaperSkip(str(exc), "已补录/关联，作者编号待核验") from exc
    if not record.staff_id or staff != record.staff_id:
        raise PaperSkip("作者编号与名单未完整对应。", "已补录/关联，作者编号待核验")
    author = field("作者信息")
    for side in ("sa", "library"):
        numbers = re.findall(r"工号\s*[：:]\s*([0-9]+)(?=\s|$)", author[side])
        if numbers != [staff]:
            raise PaperSkip("作者详情人员编号不唯一或不一致。", "已补录/关联，作者身份待核验")
    for label in ("是否第一作者", "是否通讯作者"):
        flags = [re.findall(re.escape(label) + r"\s*[：:]\s*([是否])(?=\s|$)", author[side]) for side in ("sa", "library")]
        if len(flags[0]) != 1 or flags[0] != flags[1]:
            raise PaperSkip("作者角色不一致或不完整，不自动判断。", "已补录/关联，涉及作者角色判断，暂不处理")
    unit = field("交大是否第一单位")
    if unit["sa"].strip() not in ("是", "否") or unit["sa"].strip() != unit["library"].strip():
        raise PaperSkip("第一单位不一致或未知，不自动修改。", "已补录/关联，第一单位待核验，暂不处理")
    return claim_state == "已认领"


def run_batch(roster, records, sa, download, imports, workflow, stop,
              progress=lambda _: None, audit=lambda *args: None, *, retry_skipped=False):
    """Explicitly authorized one-click workflow; a write failure halts, not retries."""
    records = list(records)
    if (not 1 <= len(records) <= 100 or len({r.sa_id for r in records}) != len(records) or
            type(retry_skipped) is not bool or
            any(r not in roster.records or r.owner != OWNER or r.matches != 0 for r in records)):
        raise SafetyStop("补录范围包含重复、他人或非零匹配任务，未开始。")
    result = BatchResult(roster, remaining=len(records))
    def record_operation(action, outcome, sa_id):
        try:
            audit(action, outcome, sa_id)
        except Exception:
            progress("log.txt 保存失败；独立断点日志仍保留，请检查日志目录。")
    downloader = WOSDownload(download, download.store, lambda: result.roster.assert_unchanged(), stop, record_operation)
    flow = WOSFlow(sa, imports, lambda: result.roster.assert_unchanged(), progress, record_operation)
    backend_ready = False

    def call(action, payload):
        result.roster.assert_unchanged()
        if stop.is_set():
            raise SafetyStop("已请求暂停。")
        progress(f"零匹配补录 {position}/{len(records)} · {stage} · {payload.get('sa_id', '')}")
        try:
            value = sa.call(action, payload, timeout=75)
        except Exception:
            record_operation(action, "已暂停", payload.get("sa_id", ""))
            raise
        record_operation(action, "已执行", payload.get("sa_id", ""))
        return value

    def close_non_sjtu(record, candidate, raw, state):
        """Close a proven negative, not an import. Unknown writes never retry."""
        fresh = call("status", {"sa_id": record.sa_id})
        row = fresh.get("row")
        verify_sa(record, row)
        require_no_author_review(record.reason, row.get("reason"))
        if (fresh.get("non_sjtu_completion_protocol") != 1 or
                type(fresh.get("non_sjtu_completion_protocol")) is not int):
            raise SafetyStop("请重载扩展 0.4.5 并刷新绑定 SA 页；尚未提交非交大结案。")
        if (row.get("markStatus") != "待处理" or str(row.get("matchCount")) != "0"
                or str(row.get("itemId", "")).lstrip(",") or row.get("remark", "") not in ("", "非交大")):
            raise SafetyStop("非交大结案前后台状态、匹配或备注已改变，未覆盖。")
        if state and state.get("phase") != "downloaded":
            raise SafetyStop("此条已有写入断点，不能改作非交大结案或重复提交。")
        if parse_wos(raw) != candidate:
            raise SafetyStop("非交大 TXT 与核验记录不一致，未结案。")
        # Recheck title, every supplied identifier, and the complete author map.
        try:
            identity(record, candidate)
        except WOSPolicyStop as exc:
            if exc.note != "非交大":
                raise
        else:
            raise SafetyStop("TXT 不具备完整非交大证据，未结案。")
        archive = download.store.archive(raw)
        evidence = {"candidate": candidate, "sha256": candidate["sha256"],
                    "path": str((download.store.root / (archive + ".txt")).resolve())}
        # Provenance is already verified, independently of the website write.
        # Persist it first so a lost completion ACK cannot drop the WOS source;
        # the completion flag and remark still wait for verified read-back.
        updated = record_data_sources(result.roster, [record], "WOS")
        result.roster = updated.roster
        record = next(r for r in result.roster.records if r.sa_id == record.sa_id)
        intent = {**state, **evidence, "phase": "non_sjtu_complete_intent", "note": "非交大"}
        workflow.save(record, intent)
        proof = call("complete", {"sa_id": record.sa_id, "expected": row,
                    "reviewed": True, "note": "非交大", "owner": OWNER,
                    "non_sjtu_evidence": candidate})
        after = proof.get("row")
        verify_sa(record, after)
        if (proof.get("verified") is not True or after.get("markStatus") != "已处理"
                or after.get("remark") != "非交大" or str(after.get("matchCount")) != "0"
                or str(after.get("itemId", "")).lstrip(",")):
            raise SafetyStop("非交大备注及已处理状态未完整回读，名单未标完成；不重复提交。")
        synced = reconcile_processed(result.roster, record, after)
        if not synced:
            raise SafetyStop("非交大网页结案未确认，名单未标完成。")
        result.roster = synced.roster
        workflow.save(record, {**intent, "phase": "non_sjtu_done"})
        record_operation("非交大网页结案及名单回写", "已执行", record.sa_id)
        return evidence

    def close_imported(record, candidate, item, state, fresh):
        """Confirmed intake closes as 已入库, independently of author recognition.

        A local journal or upload toast is insufficient. Re-read the actual
        single-record pushed batch and linked paper, then submit closure once.
        Unknown old claim/closure intents must not be reset by a new note.
        """
        if state["phase"] in ("claim_intent", "complete_intent"):
            raise SafetyStop("上次认领或结案结果未确认；只读续验，不更改备注或重复提交。")
        if type(fresh.get("import_completion_protocol")) is not int or fresh["import_completion_protocol"] != 1:
            raise SafetyStop("请重载扩展 0.4.11 并重新绑定 SA 页；尚未提交“已入库”结案。")
        verify_paper_comparison(fresh.get("comparison"), candidate, record)
        imported = imports.get(record)
        if (not imported or imported.get("phase") != "pushed" or imported.get("candidate") != candidate
                or parse_wos(imports.bytes(imported)) != candidate):
            raise SafetyStop("没有本条完整 TXT 和已确认推送的入库断点，不能备注已入库。")
        previous = imported.get("batch") or {}
        if not isinstance(previous.get("id"), str) or not previous["id"]:
            raise SafetyStop("已入库批次编号不完整，未结案。")
        checked = call("import_check", {"sa_id": record.sa_id, "candidate": candidate,
            "instructions": "SA补充-" + record.sa_id, "batch_id": previous["id"], "expect_pushed": True})
        batch = checked.get("batch") or {}
        if (checked.get("verified") is not True or batch.get("id") != previous["id"]
                or str(batch.get("status")) != "2" or batch.get("source") != "WOS"
                or batch.get("instructions") != "SA补充-" + record.sa_id
                or any(type(batch.get(key)) not in (int, str) or str(batch[key]) != value
                       for key, value in (("total", "1"), ("actual", "1"), ("fail", "0")))):
            raise SafetyStop("未回读到本条成功 1、失败 0 的已推送入库批次；未结案。")
        fresh = call("search", {"sa_id": record.sa_id})
        row = fresh.get("row")
        verify_sa(record, row)
        verify_paper_comparison(fresh.get("comparison"), candidate, record)
        if (row.get("markStatus") != "待处理" or str(row.get("matchCount")) != "1"
                or str(row.get("itemId", "")).lstrip(",") != item["id"]
                or row.get("remark", "") not in ("", COMPLETION_NOTE)):
            raise SafetyStop("入库结案前关联、状态或备注改变；未覆盖。")
        state = {**state, "phase": "complete_intent", "note": COMPLETION_NOTE}
        workflow.save(record, state)
        proof = call("complete", {"sa_id": record.sa_id, "expected": row,
            "expected_comparison": fresh["comparison"], "reviewed": True, "owner": OWNER,
            "note": COMPLETION_NOTE,
            "imported_evidence": {"candidate": candidate, "batch": batch, "item_id": item["id"]}})
        after = proof.get("row")
        verify_sa(record, after)
        if (proof.get("verified") is not True or after.get("markStatus") != "已处理"
                or after.get("remark") != COMPLETION_NOTE
                or str(after.get("itemId", "")).lstrip(",") != item["id"]):
            raise SafetyStop("“已入库”备注或已处理状态未完整回读；名单未标完成，不重复提交。")
        synced = reconcile_processed(result.roster, record, after)
        if not synced:
            raise SafetyStop("入库结案未确认，名单未标完成。")
        result.roster = synced.roster
        workflow.save(record, {**state, "phase": "done"})
        record_operation("已入库网页结案及名单回写", "已执行", record.sa_id)

    # One genuine export can serve identical owned rows; each SA closure is
    # still separately verified. Never reuse across contradictory identifiers.
    files = {}
    for position, original in enumerate(records, 1):
        evidence = {}
        record = next(r for r in result.roster.records if r.sa_id == original.sa_id)
        # Enforce the scope at the executor too, not only in the UI's selector.
        # Never open/search/download a skipped task from a stale ordinary queue.
        if record.done or record.skipped and not retry_skipped:
            result.outcomes.append({"sa_id": record.sa_id, "title": record.title,
                "status": "already_done" if record.done else "already_skipped",
                "message": "名单已完成，未重复执行" if record.done else "名单已跳过，未重复执行：" + record.remark})
            result.remaining -= 1
            continue
        if stop.is_set():
            result.halted, result.reason = True, "已暂停；未执行项目保持原样。"
            break
        stage = "核验后台状态"
        progress(f"零匹配补录 {position}/{len(records)} · {stage} · {record.sa_id}")
        try:
            fresh = call("status", {"sa_id": record.sa_id})
            row = fresh.get("row")
            # Already-processed rows are mirrored by their exact SA ID, even
            # if a colleague corrected their metadata after this list export.
            verify_sa(record, row, facts=False)
            synced = reconcile_processed(result.roster, record, row)
            if synced:
                result.roster = synced.roster
                record_operation("同步后台原已处理状态及备注", "已执行", record.sa_id)
                result.outcomes.append({"sa_id": record.sa_id, "title": record.title, "status": "synced", "message": "后台原已处理，已同步名单；没有重复导入。"})
                result.remaining -= 1
                continue
            verify_sa(record, row)
            state = workflow.get(record)
            if state.get("phase") in ("non_sjtu_complete_intent", "non_sjtu_done"):
                raise SafetyStop("上次非交大结案尚未回读为已处理；只读续验，不重复提交或导入。")
            import_state = imports.get(record)
            if not import_state or import_state.get("phase") != "pushed":
                require_no_author_review(record.reason, row.get("reason"))
            if not state and classify(record, fresh).route != "wos":
                raise PaperSkip("后台不再是未处理零匹配任务，未重复导入。", "后台匹配状态已变化，待核验")
            if not backend_ready:
                capability = call("import_capabilities", {"sa_id": record.sa_id})
                if (capability.get("zero_match_protocol") != 1 or capability.get("library_resolution") is not True):
                    raise SafetyStop("请重载扩展 0.4.2 并重新绑定工作页；尚未下载或导入。")
                backend_ready = True
            stage = "下载并核验 TXT"
            key = (record.title, record.doi, record.wos)
            if import_state:
                raw = imports.bytes(import_state)
            elif key in files:
                raw = files[key]
            else:
                saved = downloader.prepare(record, progress)
                raw = download.store.bytes(saved)
            candidate = parse_wos(raw)
            try:
                strong = identity(record, candidate)
            except WOSPolicyStop as exc:
                if exc.note != "非交大":
                    raise
                if import_state:
                    raise SafetyStop("此条已有上传/导入断点，不能改作非交大结案；请核验既有结果。")
                stage = "非交大网页批注及结案"
                evidence = close_non_sjtu(record, candidate, raw, state)
                files[key] = raw
                result.outcomes.append({"sa_id": record.sa_id, "title": record.title,
                                        "status": "done", "message": "非交大", **evidence})
                result.remaining -= 1
                continue
            except SafetyStop as exc:
                raise PaperSkip(str(exc), "WOS 文献标识或归属证据待核验：" + str(exc)) from exc
            if not strong:
                raise PaperSkip("只有题名匹配，文献身份仍需人工核验；未上传。", "WOS 文献身份待核验")
            files[key] = raw
            path = imports.root / (imports.archive(raw) + ".txt")
            evidence = {"candidate": candidate, "path": str(path.resolve()), "sha256": candidate["sha256"]}
            state = {**state, "candidate": candidate}
            # Never overwrite a previous write intent with an earlier stage.
            state.setdefault("phase", "downloaded")
            workflow.save(record, state)
            update = record_data_sources(result.roster, [record], "WOS")
            result.roster = update.roster
            record_operation("记录已核验 WOS 数据来源", "已执行", record.sa_id)
            record = next(r for r in result.roster.records if r.sa_id == record.sa_id)
            stage = "核查本库是否已存在"
            item = resolved_item(call("import_resolve", {"sa_id": record.sa_id, "candidate": candidate}), candidate)
            imported = bool(import_state and import_state.get("phase") == "pushed")
            if not item:
                if type(fresh.get("import_completion_protocol")) is not int or fresh["import_completion_protocol"] != 1:
                    raise SafetyStop("请重载扩展 0.4.11 并重新绑定 SA 页；尚未上传或导入。")
                # A linked/completion-intent record must never re-import when
                # the retrieval index is temporarily missing its target.
                if state["phase"] in ("link_intent", "linked", "claim_intent", "claimed", "complete_intent", "done"):
                    raise SafetyStop("曾关联的本库文献暂未查到，未重新导入或结案。")
                stage = "上传、导入及推送"
                flow.prepare_file(record, path, candidate["sha256"])
                imported_state = flow.proceed(record)
                if imported_state.get("phase") != "pushed":
                    raise SafetyStop("未回读到推送完成。")
                imported = True
                item = resolved_item(call("import_resolve", {"sa_id": record.sa_id, "candidate": candidate}), candidate)
                if not item:
                    raise SafetyStop("推送已确认，本库检索尚未出现该文献；下次只续验，不重复上传。")
            stage = "关联平台唯一号"
            fresh = call("search", {"sa_id": record.sa_id})
            row = fresh.get("row")
            verify_sa(record, row)
            if row.get("markStatus") != "待处理":
                raise SafetyStop("关联前处理状态已改变，请只读重查。")
            count = str(row.get("matchCount"))
            existing = str(row.get("itemId", "")).lstrip(",")
            if count == "0" and not existing:
                if state["phase"] in ("link_intent", "linked", "claim_intent", "claimed", "complete_intent", "done"):
                    raise SafetyStop("平台关联曾提交但尚未回读成功，不再次提交。")
                workflow.save(record, {**state, "phase": "link_intent", "item_id": item["id"]})
                proof = call("link", {"sa_id": record.sa_id, "expected": row, "reviewed": True,
                                      "note": "已核验WOS完整记录与本库同一文献", "item_id": item["id"]})
                if proof.get("verified") is not True:
                    raise SafetyStop("平台号关联结果未核验，不结案。")
            elif count != "1" or existing != item["id"]:
                raise SafetyStop("SA 已关联其他/多个平台条目，停止避免覆盖。")
            state = {**state, "phase": state["phase"] if state["phase"] in ("claim_intent", "complete_intent") else "linked", "item_id": item["id"],
                     "note": state.get("note") or (COMPLETION_NOTE if imported else EXISTING_NOTE)}
            workflow.save(record, state)
            stage = "网页批注及结案"
            fresh = call("search", {"sa_id": record.sa_id})
            row = fresh.get("row")
            verify_sa(record, row)
            if str(row.get("matchCount")) != "1" or str(row.get("itemId", "")).lstrip(",") != item["id"]:
                raise SafetyStop("平台关联未回读为唯一匹配。")
            if imported:
                close_imported(record, candidate, item, state, fresh)
                result.outcomes.append({"sa_id": record.sa_id, "title": record.title,
                                        "status": "done", "message": COMPLETION_NOTE, **evidence})
                result.remaining -= 1
                continue
            require_no_author_review(row.get("reason"))
            claimed = verify_comparison(fresh.get("comparison"), candidate, record, allow_unclaimed=True)
            if row.get("markStatus") == "待处理" and not claimed:
                if state["phase"] in ("claim_intent", "complete_intent"):
                    raise SafetyStop("上次认领/结案效果未确认，不重复认领或提交。")
                stage = "按精确编号认领"
                source, staff = sa_claim_source(fresh["comparison"])
                payload = {"sa_id": record.sa_id, "expected": row, "sa_text": source,
                           "staff_id": staff, "roster_staff_id": record.staff_id}
                prepared_result = call("prepare_claim", payload)
                try:
                    prepared, person, author, _, _ = automatic_claim_selection(record, row, fresh["comparison"], prepared_result)
                except SafetyStop as exc:
                    # Recover only our own read-only/claim preview through the
                    # existing adapter. Unknown windows still stop that adapter.
                    call("search", {"sa_id": record.sa_id})
                    raise PaperSkip(str(exc), "已补录/关联，人员或署名不能唯一认领") from exc
                workflow.save(record, {**state, "phase": "claim_intent"})
                proof = call("submit_claim", {**payload, "prepared": prepared, "author_index": author["index"], "confirmed": True})
                verify_claim_result(record, proof, person, author)
                state = {**state, "phase": "claimed"}
                workflow.save(record, state)
                fresh = call("search", {"sa_id": record.sa_id})
                row = fresh.get("row")
                verify_sa(record, row)
                if str(row.get("matchCount")) != "1" or str(row.get("itemId", "")).lstrip(",") != item["id"]:
                    raise SafetyStop("认领后平台关联已改变，停止结案。")
                require_no_author_review(row.get("reason"))
                verify_comparison(fresh.get("comparison"), candidate, record)
            stage = "网页批注及结案"
            from remarks import CLAIMED, SA_MISSING_IDS, detail_value
            if state["phase"] != "complete_intent":
                missing = not detail_value(fresh["comparison"], "DOI", "sa") and not detail_value(fresh["comparison"], "WOS记录号", "sa")
                reason = str(row.get("reason", ""))
                rules = []
                if missing and "DOI" in reason and "WOS" in reason:
                    rules.append(SA_MISSING_IDS)
                if "作者不一致" in reason or rules:
                    rules.append(CLAIMED)
                base_note = COMPLETION_NOTE if imported else EXISTING_NOTE
                state["note"] = "；".join([*rules, base_note])
                workflow.save(record, state)
            if row.get("markStatus") == "已处理":
                synced = reconcile_processed(result.roster, record, row)
            else:
                if row.get("remark", "") not in ("", state["note"]):
                    raise SafetyStop("后台已有其他备注，未覆盖。")
                if workflow.get(record).get("phase") == "complete_intent":
                    raise SafetyStop("上次结案效果未确认，不重复提交。")
                workflow.save(record, {**state, "phase": "complete_intent"})
                proof = call("complete", {"sa_id": record.sa_id, "expected": row,
                    "expected_comparison": fresh["comparison"], "reviewed": True, "note": state["note"]})
                after = proof.get("row")
                verify_sa(record, after)
                if (proof.get("verified") is not True or after.get("markStatus") != "已处理" or
                        after.get("remark") != state["note"] or str(after.get("itemId", "")).lstrip(",") != item["id"]):
                    raise SafetyStop("网页结案未完整回读，Excel 未标记完成。")
                synced = reconcile_processed(result.roster, record, after)
            if not synced:
                raise SafetyStop("网页完成状态未确认。")
            result.roster = synced.roster
            record_operation("回写完成批注及状态", "已执行", record.sa_id)
            workflow.save(record, {**state, "phase": "done"})
            result.outcomes.append({"sa_id": record.sa_id, "title": record.title, "status": "done", "message": state["note"], **evidence})
            result.remaining -= 1
        except SafetyStop as exc:
            note = exc.note if isinstance(exc, (WOSPolicyStop, PaperSkip)) else zero_result_note(str(exc))
            if note and (isinstance(exc, (PaperSkip, WOSPolicyStop)) or stage == "下载并核验 TXT"):
                note = brief_skip_note(note)
                try:
                    saved = mark_skipped_many(result.roster, [record], reasons={record.sa_id: note})
                except Exception as write_error:
                    result.halted, result.reason = True, f"跳过说明未能保存（{type(write_error).__name__}）；先重读名单核验已保存内容。"
                    result.outcomes.append({"sa_id": record.sa_id, "title": record.title, "status": "halted", "message": result.reason})
                    break
                result.roster = saved.roster
                record_operation("记录跳过：" + str(exc), "已跳过", record.sa_id)
                result.outcomes.append({"sa_id": record.sa_id, "title": record.title,
                    "status": "skip", "message": note, "detail": str(exc), **evidence})
                result.remaining -= 1
                continue
            result.halted, result.reason = True, f"{stage}：{exc}"
            result.outcomes.append({"sa_id": record.sa_id, "title": record.title, "status": "halted", "message": result.reason, **evidence})
            break
        except Exception as exc:
            # Preserve all durable write intents and completed Excel transactions.
            # Never expose an arbitrary exception containing account/session data.
            result.halted, result.reason = True, f"{stage}异常（{type(exc).__name__}）；先核验既有结果，不重复提交。"
            result.outcomes.append({"sa_id": record.sa_id, "title": record.title, "status": "halted", "message": result.reason, **evidence})
            break
    return result
