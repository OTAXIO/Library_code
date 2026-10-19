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
from claim import sa_claim_source
from claim_batch import automatic_claim_selection
from core import SafetyStop, digest, file_hash
from metadata_excel import build_excel
from metadata_sources import KIND, SOURCE, MetadataClient, MetadataSkip, doi, norm
from roster_write import mark_skipped_many, mark_skipped_without_note, reconcile_processed, record_data_sources
from wos_policy import NonZeroMatchSkip, require_no_author_review
from zero_match import OWNER, BatchResult, WorkflowStore, verify_import_match, verify_paper_comparison, verify_sa


def claim_state(comparison, record):
    """Read explicit site role flags; never assign/infer an author or unit role."""
    def field(label, side):
        rows = [row for row in comparison if row.get("label") == label]
        if len(rows) != 1 or not isinstance(rows[0].get(side), str):
            raise SafetyStop("已入库，作者详情待核验：" + label)
        return rows[0][side].strip()
    _, staff = sa_claim_source(comparison)
    if not record.staff_id or staff != record.staff_id:
        raise SafetyStop("已入库，作者编号待核验。")
    for label in ("是否第一作者", "是否通讯作者"):
        flags = [re.findall(re.escape(label) + r"\s*[：:]\s*([是否])(?=\s|$)", field("作者信息", side))
                 for side in ("sa", "library")]
        if len(flags[0]) != 1 or flags[0] != flags[1]:
            raise SafetyStop("已入库，作者角色待核验；未自动更改。")
    unit = [field("交大是否第一单位", side) for side in ("sa", "library")]
    if unit[0] not in ("是", "否") or unit[0] != unit[1]:
        raise SafetyStop("已入库，第一单位待核验；未自动更改。")
    state = field("认领状态", "library")
    if state not in ("已认领", "未认领", "无人认领", "未被认领"):
        raise SafetyStop("已入库，认领状态待核验。")
    return state == "已认领"


def validate_batch(batch, record, previous=None, pushed=False):
    if (not isinstance(batch, dict) or not isinstance(batch.get("id"), str) or not batch["id"]
            or not isinstance(batch.get("modelId"), str) or not batch["modelId"]
            or batch.get("source") != "人工提交" or batch.get("instructions") != "SA补充-" + record.sa_id
            or previous and batch["id"] != previous):
        raise SafetyStop("Excel 入库批次与本条任务不一致。")
    for k in ("total", "actual", "fail", "status"):
        if type(batch.get(k)) not in (str, int) or not str(batch[k]).isdigit():
            raise SafetyStop("Excel 批次计数未确认。")
    if (int(batch["total"]) != 1 or int(batch["fail"]) != 0 or int(batch["status"]) not in (1, 2)
            or int(batch["actual"]) not in (0, 1)
            or (pushed or int(batch["status"]) == 2) and (int(batch["status"]) != 2 or int(batch["actual"]) != 1)):
        raise SafetyStop("尚未确认单篇入库成功、失败零及推送完成。")
    if int(batch["status"]) == 2 and any(type(batch.get(k)) not in (str, int) or str(batch[k]) != expected
            for k, expected in (("increase", "1"), ("duplicateSkip", "0"),
                                ("duplicateIncreaseUpdate", "0"), ("duplicateOverallCoverage", "0"))):
        raise SafetyStop("批次未确认为新增一篇、重复覆盖零；未冒充新增入库结案。")
    return batch


def resolved(answer, candidate):
    if not isinstance(answer, dict) or answer.get("verified") is not True or not isinstance(answer.get("items"), list):
        raise SafetyStop("本库只读检索未确认成功。")
    items = answer["items"]
    if len(items) > 1:
        raise MetadataSkip("本库多条待核验")
    if not items:
        return None
    item = items[0]
    if (not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"].isdigit()
            or len(item["id"]) > 40 or norm(item.get("title")) != norm(candidate["title"])
            or doi(item.get("doi")) != candidate["doi"]):
        raise SafetyStop("本库 DOI/题名/平台号与元数据不一致。")
    return item


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
    if (not 1 <= len(records) <= 100 or len({r.sa_id for r in records}) != len(records)
            or any(r not in roster.records or r.owner != OWNER or r.matches != 0 for r in records)
            or type(retry_skipped) is not bool):
        raise SafetyStop("Excel 自动导入仅允许谭勋策名单中的零匹配任务。")
    root = Path(base).resolve() / "runtime/metadata-imports"
    store = WorkflowStore(root)
    client = client or MetadataClient(root / "papers", stop.is_set)
    result = BatchResult(roster, remaining=len(records))
    def checkpoint():
        result.roster.assert_unchanged()
        if stop.is_set():
            raise SafetyStop("已请求暂停；未执行项目保持原样。")
    def call(action, payload):
        checkpoint()
        progress(f"Crossref＋OpenAlex · {position}/{len(records)} · {action} · {record.sa_id}")
        value = bridge.call(action, {"sa_id": record.sa_id, **payload}, timeout=90)
        try:
            audit(action, "已执行", record.sa_id)
        except Exception:
            progress("log.txt 保存失败；独立入库断点仍保留。")
        return value
    for position, original in enumerate(records, 1):
        record = next(r for r in result.roster.records if r.sa_id == original.sa_id)
        state = store.get(record)
        try:
            checkpoint()
            if record.done or record.skipped and not retry_skipped:
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
                    validate_batch(checked.get("batch"),record,state["batch"]["id"],pushed=True)
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
            synced = reconcile_processed(result.roster, record, row)
            if synced:
                result.roster = synced.roster
                result.outcomes.append({"sa_id": record.sa_id, "status": "synced", "message": "后台已处理，仅同步名单"})
                result.remaining -= 1
                continue
            verify_sa(record, row)
            verify_import_match(row, state)
            if fresh.get("metadata_completion_protocol") != 2:
                raise SafetyStop("请重载 0.4.28 扩展并重新绑定 SA 和导入页；旧协议未执行上传或重提。")
            if not state:
                try:
                    require_no_author_review(record.reason, row.get("reason"))
                except SafetyStop:
                    raise MetadataSkip("作者角色待核验") from None
                progress(f"核验 Crossref 与 OpenAlex · {record.title}")
                candidate, folder = client.prepare(record)
                checkpoint()
                path, checksum = excel_builder(folder, candidate)
                state = {"phase": "prepared", "candidate": candidate, "path": str(path),
                    "xlsx_sha256": checksum, "instructions": "SA补充-" + record.sa_id}
                store.save(record, state)
            candidate = state["candidate"]
            path = Path(state["path"])
            if (candidate.get("kind") != KIND or candidate.get("source") != SOURCE or not candidate.get("sjtu")
                    or digest({k: v for k, v in candidate.items() if k != "sha256"}) != candidate.get("sha256")
                    or path.is_symlink() or not path.resolve().is_relative_to(root) or not path.is_file()
                    or file_hash(path) != state["xlsx_sha256"]):
                raise SafetyStop("归档 Excel 或元数据哈希校验失败，未上传。")
            # Rebuild the evidence from both archived raw responses on every resume.
            cache = json.loads((path.parent / "metadata.json").read_text(encoding="utf-8"))
            from metadata_sources import merge_metadata
            if cache.get("record_key") != record.key or merge_metadata(record, cache["crossref"], cache["openalex"]) != candidate:
                raise SafetyStop("归档来源与本条名单不一致。")
            payload = {"candidate": candidate, "instructions": state["instructions"]}
            result.roster = record_data_sources(result.roster, [record], SOURCE).roster
            record = next(r for r in result.roster.records if r.sa_id == record.sa_id)
            if state["phase"] == "prepared":
                item = resolved(call("import_excel_resolve", payload), candidate)
                if item:
                    # This mode never claims an already-existing article or
                    # asserts that it imported one. Leave author cases for claim mode.
                    raise MetadataSkip("本库已有文献")
                probe = call("import_excel_probe", {})
                if probe.get("metadata_excel_protocol") != 1 or probe.get("source") != "人工提交" or not probe.get("model_id"):
                    raise SafetyStop("通用 Excel 导入协议未确认。")
                state["model_id"] = probe["model_id"]
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
                checked = call("import_excel_check", {**payload, "batch_id": state.get("batch", {}).get("id", ""),
                    "expect_pushed": state["phase"] in ("push_intent", "pushed", "link_intent", "linked", "claim_intent", "claimed", "complete_intent", "done")})
                if checked.get("verified") is not True:
                    raise SafetyStop("未回读到实际 Excel 入库批次。")
                batch = validate_batch(checked.get("batch"), record, state.get("batch", {}).get("id"))
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
                    batch = validate_batch(checked.get("batch"), record, batch["id"], pushed=True)
                else:
                    validate_batch(batch, record, pushed=True)
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
                    "note": "已核验Crossref及OpenAlex与本库同一文献", "item_id": item["id"]})
                if proof.get("verified") is not True:
                    raise SafetyStop("平台关联未确认。")
                state["phase"] = "linked"; store.save(record, state)
            fresh = call("search", {})
            row = fresh.get("row"); verify_sa(record, row)
            if str(row.get("matchCount")) != "1" or str(row.get("itemId", "")).lstrip(',') != item["id"]:
                raise SafetyStop("关联未回读为本条唯一平台号。")
            verify_paper_comparison(fresh.get("comparison"), candidate, record)
            # Linking a newly imported paper changes a zero-match task into an
            # author-mismatch task. Import success alone must not close it.
            require_no_author_review(row.get("reason"))
            claimed = claim_state(fresh["comparison"], record)
            if state["phase"] == "claim_intent":
                raise SafetyStop("上次作者认领回执未确认；保留入库断点，不重复认领或直接结案。")
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
                verify_paper_comparison(fresh.get("comparison"), candidate, record)
                if str(row.get("matchCount")) != "1" or str(row.get("itemId", "")).lstrip(',') != item["id"]:
                    raise SafetyStop("认领后平台关联改变，未结案。")
                require_no_author_review(row.get("reason"))
                if not claim_state(fresh["comparison"], record):
                    raise SafetyStop("作者认领未回读成功，未结案。")
            if state["phase"] in ("complete_intent", "done"):
                raise SafetyStop("上次结案结果待核验，不重复提交。")
            if row.get("markStatus") != "待处理" or row.get("remark", "") not in ("", "已入库"):
                raise SafetyStop("结案前状态或人工备注改变，未覆盖。")
            state["phase"] = "complete_intent"; store.save(record, state)
            proof = call("complete", {"expected": row, "expected_comparison": fresh["comparison"],
                "reviewed": True, "owner": OWNER, "note": "已入库", "metadata_imported_evidence": {
                    "candidate": candidate, "batch": state["batch"], "item_id": item["id"], "xlsx_sha256": state["xlsx_sha256"]}})
            after = proof.get("row"); verify_sa(record, after)
            if (proof.get("verified") is not True or after.get("remark") != "已入库" or after.get("markStatus") != "已处理"
                    or str(after.get("itemId", "")).lstrip(',') != item["id"]):
                raise SafetyStop("网页批注或结案未确认；list 未标完成。")
            result.roster = reconcile_processed(result.roster, record, after).roster
            state["phase"] = "done"; store.save(record, state)
            result.outcomes.append({"sa_id": record.sa_id, "status": "done", "message": "已入库", "batch_id": state["batch"]["id"], "item_id": item["id"]})
            result.remaining -= 1
        except (MetadataSkip, NonZeroMatchSkip) as exc:
            if state.get("phase") not in (None, "prepared"):
                result.halted, result.reason = True, str(exc)
                break
            checkpoint()
            saved = (mark_skipped_without_note(result.roster, [record]) if isinstance(exc, NonZeroMatchSkip)
                     else mark_skipped_many(result.roster, [record], reasons={record.sa_id: exc.note}))
            result.roster = saved.roster
            result.outcomes.append({"sa_id": record.sa_id, "status": "skip", "message": str(exc)})
            result.remaining -= 1
        except SafetyStop as exc:
            result.halted, result.reason = True, str(exc)
            break
    return result
