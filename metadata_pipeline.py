"""Optional zero-match Excel pipeline, isolated from the WOS import journal.

Every irreversible native action has a durable intent. Unknown upload, import,
push, link and closure outcomes are reconciled by reads; never replayed blindly.
"""
from __future__ import annotations

import base64
import json
import re
from pathlib import Path

from approval import verify_claim_result
from automation import wos
from claim import sa_claim_source
from claim_batch import automatic_claim_selection
from core import SafetyStop, digest, file_hash
from metadata_excel import build_excel
from metadata_identity import title_key
from metadata_sources import KIND, SOURCE, MetadataClient, MetadataSkip, doi
from primary_metadata import KIND as PRIMARY_KIND, trusted_page
from roster_write import mark_skipped_many, mark_skipped_without_note, reconcile_processed, record_data_sources
from roster_annotations import highlight_new_admission
from batch_recovery import RecordReadback,UnconfirmedBatch,park_record
from wos_policy import NonZeroMatchSkip, require_no_author_review
from zero_match import OWNER, BatchResult, WorkflowStore, library_items, verify_import_match, verify_sa


class MetadataReview(MetadataSkip):
    """A known per-paper role disagreement, not an uncertain write or transport failure."""


class MetadataDuplicateReview(MetadataSkip):
    """An exact, fully counted pushed duplicate; never a new admission or closure."""


def claim_state(comparison, record, reason=None):
    """Read explicit site role flags; never assign/infer an author or unit role."""
    if not isinstance(comparison, list) or any(not isinstance(row, dict) for row in comparison):
        raise SafetyStop("已入库，作者详情结构待核验；未自动更改。")
    if reason is not None and not isinstance(reason, str):
        raise SafetyStop("已入库，作者待处理原因结构待核验；未自动更改。")
    def field(label, side):
        rows = [row for row in comparison if row.get("label") == label]
        if len(rows) != 1 or not isinstance(rows[0].get(side), str):
            raise SafetyStop("已入库，作者详情待核验：" + label)
        return rows[0][side].strip()
    _, staff = sa_claim_source(comparison)
    if not record.staff_id or staff != record.staff_id:
        raise SafetyStop("已入库，作者编号待核验。")
    # Legacy author details expose a staff number; do not ignore a conflicting
    # number just because role flags agree. The modern role card intentionally
    # has no staff field, so its absence is not invented into a disagreement.
    authors = [field("作者信息", side) for side in ("sa", "library")]
    state = field("认领状态", "library")
    unbound_card = authors[1] == '' and state in ("未认领", "无人认领", "未被认领")
    for author in authors:
        labels = re.findall(r"工号\s*[：:]", author)
        if labels and (len(labels) != 1 or
                re.findall(r"工号\s*[：:]\s*([0-9]+)(?=\s|$)", author) != [staff]):
            raise SafetyStop("已入库，作者详情人员编号不唯一或不一致；未自动更改。")
    for label in ("是否第一作者", "是否通讯作者"):
        flag_label = re.escape(label) + r"\s*[：:]"
        flags = [re.findall(flag_label + r"\s*([是否])(?=\s|$)", author) for author in authors]
        if unbound_card and len(flags[0]) == 1 and len(re.findall(flag_label, authors[0])) == 1:
            continue
        if (any(len(value) != 1 for value in flags) or
                any(len(re.findall(flag_label, author)) != 1 for author in authors)):
            raise SafetyStop("已入库，作者角色结构待核验；未自动更改。")
        if flags[0] != flags[1]:
            raise MetadataReview(label.removeprefix("是否") + "待核验")
    unit = [field("交大是否第一单位", side) for side in ("sa", "library")]
    if any(value not in ("是", "否") for value in unit):
        raise SafetyStop("已入库，第一单位结构待核验；未自动更改。")
    if unit[0] != unit[1]:
        raise MetadataReview("第一单位待核验")
    if state not in ("已认领", "未认领", "无人认领", "未被认领"):
        raise SafetyStop("已入库，认领状态待核验。")
    # A cached reason can outlive linking/claiming. Only these exact two-role
    # reasons are resolved by the explicit, person-bound comparison above.
    # Co-first authorship, order and institutional issues remain review-only.
    resolved_reasons = {"第一作者不一致", "是否第一作者不一致", "通讯作者不一致",
                        "是否通讯作者不一致", "通信作者不一致"}
    remaining = ";".join(part.strip() for part in re.split(r"[;；]", reason or "")
                         if part.strip() not in resolved_reasons)
    try:
        require_no_author_review(remaining)
    except SafetyStop:
        raise MetadataReview("作者角色待核验") from None
    return state == "已认领"


def _validated_batch_base(batch, record, previous=None, pushed=False, *, model_id=None):
    if (not isinstance(batch, dict) or not isinstance(batch.get("id"), str) or not batch["id"]
            or not isinstance(batch.get("modelId"), str) or not batch["modelId"]
            or batch.get("source") != "人工提交" or batch.get("instructions") != "SA补充-" + record.sa_id
            or previous and batch["id"] != previous):
        raise SafetyStop("Excel 入库批次与本条任务不一致。")
    if model_id is not None and (not isinstance(model_id, str) or not model_id or batch["modelId"] != model_id):
        raise SafetyStop("Excel 批次模型与本次已确认模型不一致，未推送或结案。")
    for k in ("total", "actual", "fail", "status"):
        if type(batch.get(k)) not in (str, int) or not str(batch[k]).isdigit():
            raise SafetyStop("Excel 批次计数未确认。")
    if (int(batch["total"]) != 1 or int(batch["fail"]) != 0 or int(batch["status"]) not in (1, 2)
            or int(batch["actual"]) not in (0, 1)
            or (pushed or int(batch["status"]) == 2) and (int(batch["status"]) != 2 or int(batch["actual"]) != 1)):
        raise SafetyStop("尚未确认单篇入库成功、失败零及推送完成。")
    return batch


def validate_batch(batch, record, previous=None, pushed=False, *, model_id=None):
    batch = _validated_batch_base(batch, record, previous, pushed, model_id=model_id)
    if int(batch["status"]) == 2 and any(type(batch.get(k)) not in (str, int) or str(batch[k]) != expected
            for k, expected in (("increase", "1"), ("duplicateSkip", "0"),
                                ("duplicateIncreaseUpdate", "0"), ("duplicateOverallCoverage", "0"))):
        raise SafetyStop("批次未确认为新增一篇、重复覆盖零；未冒充新增入库结案。")
    return batch


def verified_pushed_duplicate(batch, record, previous, *, model_id):
    """Recognize only a complete non-new result; never infer it from a zero/missing counter."""
    if not isinstance(model_id, str) or not model_id:
        return False
    try:
        _validated_batch_base(batch, record, previous, pushed=True, model_id=model_id)
    except SafetyStop:
        return False
    keys = ("increase", "duplicateSkip", "duplicateIncreaseUpdate", "duplicateOverallCoverage")
    if any(type(batch.get(key)) not in (str, int) or str(batch[key]) not in ("0", "1") for key in keys):
        return False
    return str(batch["increase"]) == "0" and sum(int(batch[key]) for key in keys[1:]) == 1


def _verified_title_key(value):
    """Malformed native/archive evidence is a pause, not a per-paper skip."""
    try:
        return title_key(value)
    except MetadataSkip:
        raise SafetyStop("题名结构待核验；未修改完成或跳过状态。") from None


def resolved(answer, candidate):
    items = library_items(answer)
    if len(items) > 1:
        raise MetadataSkip("本库多条待核验")
    if not items:
        return None
    item = items[0]
    if (not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].isdigit()
            or len(item["id"]) > 40 or _verified_title_key(item.get("title")) != _verified_title_key(candidate["title"])
            or doi(item.get("doi")) != candidate["doi"]):
        raise SafetyStop("本库 DOI/题名/平台号与元数据不一致。")
    if candidate.get('kind') == PRIMARY_KIND:
        venue_label='发表期刊' if candidate.get('document_type')=='journal-article' else '会议名称'
        expected = {'url': candidate['primary_url'], 'year': candidate['fields']['年份'],
                    'venue': candidate['fields'][venue_label], 'authors': candidate['author_names']}
        if item.get('primary_identity') != expected:
            raise SafetyStop('本库官方网址、作者顺序、会议或年份不一致；未关联或结案。')
    return item


def verify_metadata_comparison(comparison, candidate, record):
    """Use the same presentation-only title identity as metadata discovery.

    WOS keeps its independent comparison rules. The original comparison is
    never rewritten, and every identifier and title word must still agree.
    """
    if not isinstance(comparison, list):
        raise SafetyStop("缺少关联后的比对详情。")
    for label, expected, source, normalizer in (
            ("题名", candidate["title"], record.title, _verified_title_key),
            ("DOI", candidate["doi"], record.doi, doi),
            ("WOS记录号", candidate["wos"], record.wos, wos)):
        fields = [field for field in comparison if isinstance(field, dict) and field.get("label") == label]
        if (len(fields) != 1 or not isinstance(fields[0].get("library"), str)
                or normalizer(fields[0]["library"]) != normalizer(expected)):
            raise SafetyStop("关联后的本库详情未确认：" + label)
        if (not isinstance(fields[0].get("sa"), str)
                or normalizer(fields[0]["sa"]) != normalizer(source)):
            raise SafetyStop("关联后的 SA 详情与名单不一致：" + label)


def check_cross_workflow(base, record):
    """Never start Excel while a WOS or another-source write is unresolved."""
    import sqlite3
    root = Path(base) / "runtime/wos-imports"
    for name, table, safe in (("imports.sqlite3", "imports", "exported"),
                              ("zero-match.sqlite3", "jobs", "downloaded")):
        path = root / name
        if not path.exists():
            continue
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        try:
            saved = db.execute(f"SELECT record_key,data FROM {table} WHERE sa_id=?", (record.sa_id,)).fetchone()
        finally:
            db.close()
        if saved and (saved[0] != record.key or json.loads(saved[1]).get("phase") != safe):
            raise SafetyStop("此条存在 WOS 写入断点，未切换来源或重复导入。")


def run_metadata_batch(base, roster, records, bridge, stop, progress=lambda _: None,
                       audit=lambda *args: None, *, retry_skipped=False, client=None, excel_builder=build_excel):
    records = list(records)
    if (not 1 <= len(records) <= (len(roster.records) if retry_skipped else 100) or len({r.sa_id for r in records}) != len(records)
            or any(r not in roster.records or r.owner != OWNER or r.matches != 0 for r in records)
            or type(retry_skipped) is not bool):
        raise SafetyStop("Excel 自动导入仅允许谭勋策名单中的零匹配任务。")
    root = Path(base).resolve() / "runtime/metadata-imports"
    store = WorkflowStore(root)
    client = client or MetadataClient(root / "papers", stop.is_set, progress)
    result = BatchResult(roster, remaining=len(records))
    def checkpoint():
        result.roster.assert_unchanged()
        if stop.is_set():
            raise SafetyStop("已请求暂停；未执行项目保持原样。")
    def call(action, payload):
        checkpoint()
        progress(f"元数据入库 · {position}/{len(records)} · {action} · {record.sa_id}")
        if action.startswith('import_excel_') and state.get('model_id'):
            payload = {**payload, 'model_id': state['model_id']}
        if action.startswith('import_excel_') and state.get('primary_fields'):
            payload = {**payload, 'primary_fields': state['primary_fields']}
        if action == "search":
            payload = {**payload, "close_detail": True}
        value = readback.call(bridge,action,{"sa_id": record.sa_id, **payload},timeout=90)
        if action == "search" and value.get("detail_released") is not True:
            raise SafetyStop("本条只读详情尚未确认收起，未继续下一篇或提交认领结案。")
        try:
            audit(action, "已执行", record.sa_id)
        except Exception:
            progress("log.txt 保存失败；独立入库断点仍保留。")
        return value
    def accept_batch(batch, previous=None, pushed=False):
        try:
            return validate_batch(batch, record, previous, pushed, model_id=state.get("model_id"))
        except SafetyStop:
            # The transport has already returned verified=True and the exact
            # native batch has a complete single-record pushed tally. Preserve
            # that real result; do not pretend this was a new paper, replay a
            # push, link a conflicting identity, or convert unknown ACKs to skips.
            if (state.get("phase") not in ("import_intent", "imported", "push_intent")
                    or not verified_pushed_duplicate(batch, record, previous, model_id=state.get("model_id"))):
                raise
            state.update(review_origin_phase=state["phase"], phase="duplicate_review", batch=batch,
                         deferred_reason="本库重复待核验")
            store.save(record, state)
            raise MetadataDuplicateReview("本库重复待核验") from None
    for position, original in enumerate(records, 1):
        readback=RecordReadback()
        record = next(r for r in result.roster.records if r.sa_id == original.sa_id)
        state = store.get(record)
        resumed_import=state.get('phase')=='import_intent' and not state.get('batch')
        try:
            checkpoint()
            if record.done or record.skipped and not retry_skipped:
                if record.done:
                    result.roster=highlight_new_admission(result.roster,record,state).roster
                # Explicit acceptance/resume can contain a previous completed
                # row followed by pending ones. Read its actual pushed batch
                # once so the extension can safely retire a retained native
                # wizard. Never upload, claim, close or rewrite this done row.
                pending = [r for r in records[position:] if not r.done and not r.skipped]
                if record.done and state.get("phase") == "done" and pending and all(
                        store.get(r).get("phase") in (None, "prepared") for r in pending):
                    checked=call("import_excel_check", {"candidate":state["candidate"],
                        "instructions":state["instructions"],"batch_id":state["batch"]["id"],"expect_pushed":True})
                    if checked.get("verified") is not True:
                        raise SafetyStop("上一篇已完成批次未确认，未重置旧向导。")
                    validate_batch(checked.get("batch"),record,state["batch"]["id"],pushed=True,
                                   model_id=state.get("model_id"))
                elif (record.skipped and state.get("phase") in ("linked", "claimed")
                      and state.get("deferred_reason") and pending):
                    # An explicit resume can begin with a previously shelved
                    # import whose old extension retained its read-only drawer.
                    # Retire ONLY that exact record, without uploading, claiming,
                    # closing the SA task or rewriting its skip/note.
                    fresh = call("status", {})
                    if type(fresh.get("metadata_detail_protocol")) is not int or fresh["metadata_detail_protocol"] != 1:
                        raise SafetyStop("请重载 0.4.31 扩展后续验；旧版只读窗口未自动丢弃。")
                    verify_sa(record, fresh.get("row"))
                    verify_import_match(fresh["row"], state)
                    if str(fresh["row"].get("matchCount")) != "1":
                        raise SafetyStop("跳过项原关联尚未确认，未清理窗口或重导。")
                    released = call("search", {})
                    verify_sa(record, released.get("row"))
                    verify_import_match(released["row"], state)
                    if str(released["row"].get("matchCount")) != "1":
                        raise SafetyStop("收起只读详情时原关联改变，未继续后续任务。")
                    verify_metadata_comparison(released.get("comparison"), state["candidate"], record)
                result.outcomes.append({"sa_id": record.sa_id, "status": "already_skipped", "message": "已跳过或完成，未重复执行"})
                result.remaining -= 1
                continue
            check_cross_workflow(base, record)
            fresh = call("status", {})
            row = fresh.get("row")
            verify_sa(record, row, facts=False)
            # An unknown previous closure is successful only with the exact
            # expected platform ID/note, not some unrelated colleague's update.
            if state.get("phase") in ("complete_intent", "done") and row.get("markStatus") == "已处理":
                if row.get("remark") != "已入库" or str(row.get("itemId", "")).lstrip(',') != state.get("item_id"):
                    raise SafetyStop("原结案断点与后台状态不一致，未同步。")
            # Linking (or native claiming) can itself mark a correct SA as
            # processed. An owned import still needs the truthful intake note;
            # do not mistake that for an unrelated, already-completed task.
            owned_processed = (row.get("markStatus") == "已处理"
                and state.get("phase") in ("link_intent", "linked", "claim_intent", "claimed", "complete_intent", "done"))
            if owned_processed and (not isinstance(state.get("item_id"), str) or not state["item_id"]
                    or str(row.get("itemId", "")).lstrip(',') != state["item_id"]
                    or str(row.get("matchCount")) != "1"):
                raise SafetyStop("已处理记录与原入库关联断点不一致，未同步名单或覆盖。")
            if row.get("markStatus") == "已处理" and state.get("phase") not in (None, "prepared") and not owned_processed:
                raise SafetyStop("已有入库写入断点待核验，不能仅凭后台已处理同步完成；未重复导入。")
            synced = None if owned_processed else reconcile_processed(result.roster, record, row)
            if synced:
                result.roster = synced.roster
                result.outcomes.append({"sa_id": record.sa_id, "status": "synced", "message": "后台已处理，仅同步名单"})
                result.remaining -= 1
                continue
            verify_sa(record, row)
            verify_import_match(row, state)
            if (type(fresh.get("metadata_completion_protocol")) is not int or fresh["metadata_completion_protocol"] != 4
                    or type(fresh.get("metadata_detail_protocol")) is not int or fresh["metadata_detail_protocol"] != 1):
                raise SafetyStop("请重载 0.4.31 扩展并重新绑定 SA 和导入页；旧协议未执行上传或重提。")
            # A prepared archive is not a permission to ignore changes in the
            # live task. Recheck role issues before the first external write
            # on every resume; already-written intents use their own readback.
            if state.get("phase") in (None, "prepared"):
                if any(value is not None and not isinstance(value, str)
                       for value in (record.reason, row.get("reason"))):
                    raise SafetyStop("后台待处理原因结构待核验，未上传或修改跳过状态。")
                try:
                    require_no_author_review(record.reason, row.get("reason"))
                except SafetyStop:
                    raise MetadataSkip("作者角色待核验") from None
            if not state:
                progress(f"核验原始元数据与来源 · {record.title}")
                candidate, folder = client.prepare(record)
                checkpoint()
                path, checksum = excel_builder(folder, candidate)
                state = {"phase": "prepared", "candidate": candidate, "path": str(path),
                    "xlsx_sha256": checksum, "instructions": "SA补充-" + record.sa_id}
                store.save(record, state)
            candidate = state["candidate"]
            path = Path(state["path"])
            primary = candidate.get('kind') == PRIMARY_KIND
            if primary and (type(fresh.get('publisher_metadata_protocol')) is not int
                            or fresh['publisher_metadata_protocol'] != 1):
                raise SafetyStop('请加载支持官方论文集的扩展 0.4.37；未上传或重复导入。')
            if primary and candidate.get('document_type')=='journal-article' and (
                    type(fresh.get('publisher_journal_protocol')) is not int or fresh['publisher_journal_protocol']!=2):
                raise SafetyStop('请加载支持原生期刊字段的扩展 0.4.40；未上传或重复导入。')
            source_ok = (candidate.get('source') == trusted_page(candidate.get('primary_url')) if primary
                         else candidate.get('kind') == KIND and candidate.get('source') == SOURCE)
            if (not source_ok or not candidate.get("sjtu")
                    or digest({k: v for k, v in candidate.items() if k != "sha256"}) != candidate.get("sha256")
                    or path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file()
                    or file_hash(path) != state["xlsx_sha256"]):
                raise SafetyStop("归档 Excel 或元数据哈希校验失败，未上传。")
            # The same production cache reader reparses either the two API
            # responses or the official HTML + original PDF, including hashes.
            verified_candidate, _ = MetadataClient(root / 'papers', stop.is_set).read_cache(path.parent, record)
            if verified_candidate != candidate:
                raise SafetyStop("归档来源与本条名单不一致。")
            payload = {"candidate": candidate, "instructions": state["instructions"]}
            result.roster = record_data_sources(result.roster, [record], candidate['source']).roster
            record = next(r for r in result.roster.records if r.sa_id == record.sa_id)
            if state["phase"] == "duplicate_review":
                # Explicit retry is read-only. The original batch and all its
                # counts must still agree; no upload/import/push is repeated.
                checked = call("import_excel_check", {**payload, "batch_id": state["batch"]["id"], "expect_pushed": True})
                if (checked.get("verified") is not True or not verified_pushed_duplicate(checked.get("batch"),
                        record, state["batch"]["id"], model_id=state.get("model_id"))):
                    raise SafetyStop("原重复批次结果改变，保留断点待核验，未重传或结案。")
                state["batch"] = checked["batch"]; store.save(record, state)
                raise MetadataDuplicateReview("本库重复待核验")
            if state["phase"] == "prepared":
                item = None if primary else resolved(call("import_excel_resolve", payload), candidate)
                if item:
                    # This mode never claims an already-existing article or
                    # asserts that it imported one. Leave author cases for claim mode.
                    raise MetadataSkip("本库已有文献")
                model_name = candidate.get("model_name", "期刊论文")
                if model_name not in ("期刊论文", "会议论文"):
                    raise SafetyStop("元数据模型未知，未上传。")
                probe = call("import_excel_probe", {"model_name": model_name})
                if (type(probe.get("metadata_excel_protocol")) is not int or probe["metadata_excel_protocol"] != 1
                        or probe.get("source") != "人工提交" or not isinstance(probe.get("model_id"), str)
                        or not probe["model_id"].strip()):
                    raise SafetyStop("通用 Excel 导入协议未确认。")
                state["model_id"] = probe["model_id"]
                if probe.get("model_name", model_name) != model_name:
                    raise SafetyStop("原生模型与文献类型不一致，未上传。")
                fields = probe.get("fields")
                if not isinstance(fields, list) or not fields or any(not isinstance(field, dict) for field in fields):
                    raise SafetyStop("原生模型字段未确认，未上传。")
                for label in candidate["fields"]:
                    if sum(field.get("label") == label for field in fields) != 1:
                        raise MetadataSkip("导入字段待核验")
                if any(str(field.get("required")).lower() in ("1", "true") and not candidate["fields"].get(field.get("label"))
                       for field in fields):
                    raise MetadataSkip("导入必填字段缺失")
                if primary:
                    mapping = {}
                    identity_labels=(('URL','年份','发表期刊') if candidate.get('document_type')=='journal-article'
                                     else ('相关网址','年份','会议名称'))
                    for label in identity_labels:
                        native = [f.get('field') for f in fields if f.get('label') == label]
                        if len(native) != 1 or not isinstance(native[0], str) or not native[0]:
                            raise SafetyStop('官方来源原生身份字段未确认，未上传。')
                        mapping[label] = native[0]
                    if len(set(mapping.values())) != 3:
                        raise SafetyStop('官方来源原生身份字段重叠，未上传。')
                    state['primary_fields'] = mapping
                    if resolved(call('import_excel_resolve', payload), candidate):
                        raise MetadataSkip('本库已有文献')
                batches = call("import_excel_scan", payload).get("batches")
                if batches != []:
                    raise SafetyStop("已有同说明批次，未重复上传；请核验历史批次。")
                # Revalidate the zero match immediately before the first write.
                current = call("status", {}).get("row")
                verify_sa(record, current)
                verify_import_match(current, state)
                if current.get("markStatus") != "待处理":
                    raise SafetyStop("导入前后台状态改变。")
                state["phase"] = "upload_intent"; store.save(record, state)
                upload = call("import_excel_upload", {**payload, "model_id": state["model_id"],
                    "xlsx_sha256": state["xlsx_sha256"], "content": base64.b64encode(path.read_bytes()).decode()})
                if upload.get("uploaded") is not True or upload.get("validated") is not True or upload.get("sha256") != state["xlsx_sha256"]:
                    raise SafetyStop("Excel 上传或格式校验未确认，未提交导入。")
                state.update(phase="uploaded", upload=upload); store.save(record, state)
            if state["phase"] == "upload_intent":
                # Recover only an owned, exact existing file/form. This never
                # selects another local file or dispatches a file-input change.
                upload = call("import_excel_upload_status", {**payload, "model_id": state["model_id"],
                    "xlsx_sha256": state["xlsx_sha256"]})
                if upload.get("uploaded") is not True or upload.get("validated") is not True or upload.get("sha256") != state["xlsx_sha256"]:
                    raise SafetyStop("上次 Excel 上传结果仍未确认，未自动重复上传。")
                state.update(phase="uploaded", upload=upload); store.save(record, state)
            if state["phase"] == "uploaded":
                if call("import_excel_scan", payload).get("batches") != []:
                    raise SafetyStop("提交前发现同说明批次，未重复导入。")
                state["phase"] = "import_intent"; store.save(record, state)
                answer = call("import_excel_submit", {**payload, "model_id": state["model_id"],
                    "xlsx_sha256": state["xlsx_sha256"], "upload": state["upload"]})
                if answer.get("submitted") is not True:
                    raise SafetyStop("Excel 导入提交未确认。")
            if state["phase"] in ("import_intent", "imported", "push_intent", "pushed", "link_intent", "linked", "claim_intent", "claimed", "complete_intent", "done"):
                if resumed_import:
                    scan=call('import_excel_scan',payload)
                    total=scan.get('scanned_count')
                    if scan.get('batches')==[] and type(total) is int and 0<=total<=5000:
                        raise UnconfirmedBatch('完整扫描 '+str(total)+' 个批次仍无对应原提交；保留断点，未重传。')
                checked = call("import_excel_check", {**payload, "batch_id": state.get("batch", {}).get("id", ""),
                    "expect_pushed": state["phase"] in ("push_intent", "pushed", "link_intent", "linked", "claim_intent", "claimed", "complete_intent", "done")})
                if checked.get("verified") is not True:
                    raise SafetyStop("未回读到实际 Excel 入库批次。")
                batch = accept_batch(checked.get("batch"), state.get("batch", {}).get("id"))
                if state["phase"] == "push_intent" and str(batch["status"]) != "2":
                    raise SafetyStop("上次推送结果待核验；未重复推送。")
                if str(batch["status"]) == "1":
                    if state["phase"] not in ("import_intent", "imported"):
                        raise SafetyStop("已推送断点与后台矛盾，未重新推送。")
                    state.update(phase="push_intent", batch=batch); store.save(record, state)
                    answer = call("import_excel_push", {**payload, "batch": batch})
                    if answer.get("submitted") is not True:
                        raise SafetyStop("推送回执未确认。")
                    checked = call("import_excel_check", {**payload, "batch_id": batch["id"], "expect_pushed": True})
                    if checked.get("verified") is not True:
                        raise SafetyStop("推送尚未确认。")
                    batch = accept_batch(checked.get("batch"), batch["id"], pushed=True)
                else:
                    validate_batch(batch, record, pushed=True, model_id=state.get("model_id"))
                state.update(batch=batch)
                if state["phase"] in ("import_intent", "imported", "push_intent"):
                    state["phase"] = "pushed"
                store.save(record, state)
            else:
                raise SafetyStop("Excel 入库断点阶段未知。")
            # Batch verification closes only our read-only view, but Element UI
            # leaves its drawer in the DOM during the close animation. Allow a
            # short cancellable grace period; the resolver STILL refuses any
            # visible dialog/drawer, so no human window is ignored or dismissed.
            if stop.wait(0.8):
                raise SafetyStop("已请求暂停；保留已推送批次，下次只续验。")
            item = resolved(call("import_excel_resolve", payload), candidate)
            if not item:
                raise SafetyStop("批次已推送，本库索引未查到；下次只续验，不重新导入。")
            # A genuine zero-match SA has no right-hand comparison yet; asking
            # search to open it can legitimately return an empty detail. Before
            # linking, verify only the fresh exact list row. Read the full
            # comparison after the exact platform link has been confirmed.
            fresh = call("status", {})
            row = fresh.get("row"); verify_sa(record, row)
            verify_import_match(row, state)
            if str(row.get("matchCount")) == "0":
                if state["phase"] in ("link_intent", "linked", "claim_intent", "claimed", "complete_intent", "done"):
                    raise SafetyStop("上次关联未回读成功，不重复提交。")
                state.update(phase="link_intent", item_id=item["id"]); store.save(record, state)
                proof = call("link", {"expected": row, "reviewed": True,
                    "note": "已核验" + candidate['source'] + "与本库同一文献", "item_id": item["id"]})
                if proof.get("verified") is not True:
                    raise SafetyStop("平台关联未确认。")
                state["phase"] = "linked"; store.save(record, state)
            fresh = call("search", {})
            row = fresh.get("row"); verify_sa(record, row)
            if str(row.get("matchCount")) != "1" or str(row.get("itemId", "")).lstrip(',') != item["id"]:
                raise SafetyStop("关联未回读为本条唯一平台号。")
            verify_metadata_comparison(fresh.get("comparison"), candidate, record)
            # Linking a newly imported paper changes a zero-match task into an
            # author-mismatch task. Import success alone must not close it.
            if state["phase"] == "claim_intent":
                raise SafetyStop("上次作者认领回执未确认；保留入库断点，不重复认领或直接结案。")
            try:
                claimed = claim_state(fresh["comparison"], record, row.get("reason"))
            except MetadataReview as review:
                if str(review) != '通讯作者待核验':
                    raise
                from publisher_roles import repair_correspondence
                if not repair_correspondence(record, fresh['comparison'], row, state, store, call):
                    raise review
                fresh = call('search', {})
                row = fresh['row']; verify_sa(record, row); verify_import_match(row, state)
                verify_metadata_comparison(fresh['comparison'], candidate, record)
                claimed = claim_state(fresh['comparison'], record, row.get('reason'))
            if not claimed:
                if state["phase"] in ("claimed", "complete_intent", "done"):
                    raise SafetyStop("已认领断点与网页不一致，未重复认领。")
                source, staff = sa_claim_source(fresh["comparison"])
                params = {"expected": row, "sa_text": source, "staff_id": staff, "roster_staff_id": record.staff_id}
                found = call("prepare_claim", params)
                prepared, person, author, _, _ = automatic_claim_selection(record, row, fresh["comparison"], found)
                state.update(phase="claim_intent", claim={"person": person, "author": author}); store.save(record, state)
                proof = call("submit_claim", {**params, "prepared": prepared, "author_index": author["index"], "confirmed": True})
                verify_claim_result(record, proof, person, author)
                state["phase"] = "claimed"; store.save(record, state)
                fresh = call("search", {})
                row = fresh.get("row"); verify_sa(record, row)
                verify_import_match(row, state)
                verify_metadata_comparison(fresh.get("comparison"), candidate, record)
                if str(row.get("matchCount")) != "1" or str(row.get("itemId", "")).lstrip(',') != item["id"]:
                    raise SafetyStop("认领后平台关联改变，未结案。")
                if not claim_state(fresh["comparison"], record, row.get("reason")):
                    raise SafetyStop("作者认领未回读成功，未结案。")
            if row.get("markStatus") not in ("待处理", "已处理") or row.get("remark", "") not in ("", "已入库"):
                raise SafetyStop("结案前状态或人工备注改变，未覆盖。")
            if state["phase"] in ("complete_intent", "done"):
                if row.get("markStatus") != "已处理" or row.get("remark") != "已入库":
                    raise SafetyStop("上次结案结果待核验，不重复提交。")
                # A lost ACK can be reconciled only after the exact batch,
                # item, claim, roles and website note above were read again.
                proof = {"verified": True, "row": row}
            else:
                state["phase"] = "complete_intent"; store.save(record, state)
                proof = call("complete", {"expected": row, "expected_comparison": fresh["comparison"],
                    "reviewed": True, "owner": OWNER, "note": "已入库", "metadata_imported_evidence": {
                        "candidate": candidate, "batch": state["batch"], "item_id": item["id"], "xlsx_sha256": state["xlsx_sha256"]}})
            after = proof.get("row"); verify_sa(record, after)
            if (proof.get("verified") is not True or after.get("remark") != "已入库" or after.get("markStatus") != "已处理"
                    or str(after.get("matchCount")) != "1"
                    or str(after.get("itemId", "")).lstrip(',') != item["id"]):
                raise SafetyStop("网页批注或结案未确认；list 未标完成。")
            result.roster = reconcile_processed(result.roster, record, after).roster
            state.pop("deferred_reason", None)
            if state.get('correspondence_repair', {}).get('phase') == 'intent':
                state['correspondence_repair'].update(phase='verified_readback',
                    comparison_sha256=digest(fresh['comparison']))
            state["phase"] = "done"; store.save(record, state)
            result.roster=highlight_new_admission(result.roster,record,state).roster
            result.outcomes.append({"sa_id": record.sa_id, "status": "done", "message": "已入库", "batch_id": state["batch"]["id"], "item_id": item["id"]})
            result.remaining -= 1
        except MetadataDuplicateReview as exc:
            if state.get("phase") != "duplicate_review":
                result.halted, result.reason = True, str(exc)
                break
            checkpoint()
            result.roster = mark_skipped_many(result.roster, [record], reasons={record.sa_id: exc.note}).roster
            result.outcomes.append({"sa_id": record.sa_id, "status": "skip", "message": exc.note + "（未新增，未结案）"})
            result.remaining -= 1
        except MetadataReview as exc:
            # Known completed writes stay in the journal. A disagreement is
            # shelved locally, never declared a completed SA or re-imported.
            # Unknown claim/closure intents cannot be hidden as a skip.
            if state.get("phase") not in ("linked", "claimed"):
                result.halted, result.reason = True, str(exc)
                break
            checkpoint()
            state["deferred_reason"] = exc.note; store.save(record, state)
            result.roster = mark_skipped_many(result.roster, [record], reasons={record.sa_id: exc.note}).roster
            result.outcomes.append({"sa_id": record.sa_id, "status": "skip",
                "message": exc.note + "（已入库，未结案）"})
            result.remaining -= 1
        except (MetadataSkip, NonZeroMatchSkip) as exc:
            if state.get("phase") not in (None, "prepared"):
                if retry_skipped and isinstance(exc,MetadataSkip):
                    try:result.roster=park_record(result.roster,record,exc.note,bridge,stop)
                    except SafetyStop as blocked:
                        result.halted,result.reason=True,str(blocked)
                        break
                    result.outcomes.append({'sa_id':record.sa_id,'status':'deferred','message':exc.note+'；保留原断点，已继续后续条目。'})
                    result.remaining-=1
                    continue
                result.halted, result.reason = True, str(exc)
                break
            checkpoint()
            saved = (mark_skipped_without_note(result.roster, [record]) if isinstance(exc, NonZeroMatchSkip)
                     else mark_skipped_many(result.roster, [record], reasons={record.sa_id: exc.note}))
            result.roster = saved.roster
            result.outcomes.append({"sa_id": record.sa_id, "status": "skip", "message": str(exc)})
            result.remaining -= 1
        except SafetyStop as exc:
            note=readback.isolated_note(exc,retry_skipped)
            if note:
                try:result.roster=park_record(result.roster,record,note,bridge,stop)
                except SafetyStop as blocked:
                    result.halted,result.reason=True,str(blocked)
                    break
                result.outcomes.append({'sa_id':record.sa_id,'status':'deferred','message':note+'；保留原断点，已继续后续条目。','detail':str(exc)})
                result.remaining-=1
                progress('本条'+note+'，已保留断点；继续下一条。')
                continue
            result.halted, result.reason = True, str(exc)
            break
    return result
