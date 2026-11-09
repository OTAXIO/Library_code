"""Zero-match hand-off: mark 需导入 on the site and in the roster; never import.

Each row is read live first. Only a still-pending row with zero matches and no
library item is closed, through the page's own status dialog, with the fixed
note. The roster is completed only from the read-back processed row.
"""
from __future__ import annotations

from dataclasses import dataclass
import re

from batch_recovery import check_legacy_import_journals
from bridge import BrowserRejected
from core import SafetyStop, require_owner
from roster_write import mark_skipped_many, reconcile_processed
from wos_policy import match_count
from zero_match import WorkflowStore, verify_sa

NOTE = "需导入"
PENDING_NOTE = "需导入结案待核"
LEGACY_NOTE = "历史入库断点待核"


@dataclass
class NeedImportResult:
    roster: object
    completed_ids: tuple
    synced_ids: tuple
    skipped: dict
    checked: int
    cancelled: bool
    halted: bool
    reason: str = ""


class _Interrupted(SafetyStop):
    def __init__(self, message, cancelled=False):
        super().__init__(message)
        self.cancelled = cancelled


def changed_match_note(count):
    return "网页已有匹配，待认领" if count == 1 else "网页已有多条匹配"


def run_need_import_batch(roster, records, bridge, owner, cancel=lambda: False,
                          progress=lambda text: None, audit=lambda action, result, sa_id: None,
                          retry_skipped=False):
    owner = require_owner(owner)
    if type(retry_skipped) is not bool:
        raise SafetyStop("跳过任务处理模式无效。")
    records = list(records)
    if (not records or len({r.sa_id for r in records}) != len(records)
            or any(r not in roster.records or r.owner != owner or r.matches != 0 or r.done
                   or r.skipped != retry_skipped for r in records)):
        raise SafetyStop("范围包含重复、他人、非零匹配或状态不符的任务，未开始。")
    current = roster
    completed, synced, skipped = [], [], {}
    checked, halted, reason = 0, False, ""
    store = None
    intent = {"phase": "complete_intent", "note": NOTE}

    def note(action, result, sa_id):
        try:
            audit(action, result, sa_id)
        except Exception:
            progress("log.txt 未能保存需导入记录，请检查文件权限和格式。")

    def guard():
        if cancel():
            raise _Interrupted("已按请求暂停；当前记录未标记，已确认的操作不会重复提交。", cancelled=True)
        try:
            current.assert_unchanged()
        except (SafetyStop, OSError) as exc:
            raise _Interrupted("名单核验失败，未提交下一步：" + str(exc)) from exc

    def call(action, payload):
        guard()
        return bridge.call(action, payload)

    def finish(cancelled=False):
        nonlocal current, halted
        targets = [r for r in current.records if r.sa_id in skipped and not r.done]
        if targets:
            try:
                current = mark_skipped_many(current, targets,
                                            reasons={r.sa_id: skipped[r.sa_id] for r in targets}).roster
                for r in targets:
                    note("记录跳过：" + skipped[r.sa_id], "已跳过", r.sa_id)
            except Exception as exc:
                halted = True
                for r in targets:
                    skipped[r.sa_id] += "；Excel 数字 2 写入失败：" + str(exc)
                    note("写入跳过标记", "已暂停", r.sa_id)
        return NeedImportResult(current, tuple(completed), tuple(synced), skipped,
                                checked, cancelled, halted, reason)

    for target in records:
        record = next((r for r in current.records if r.sa_id == target.sa_id), None)
        if not record or record.done:
            continue
        checked += 1
        progress(f"标注需导入 {checked}/{len(records)}：{record.sa_id}")
        try:
            fresh = call("status", {"sa_id": record.sa_id})
        except _Interrupted as exc:
            halted, reason = not exc.cancelled, str(exc)
            return finish(cancelled=exc.cancelled)
        except Exception as exc:
            halted, reason = True, "读取网页失败，未标注：" + str(exc)
            note("读取零匹配记录", "已暂停", record.sa_id)
            break
        if not isinstance(fresh, dict) or fresh.get("need_import_protocol") != 1:
            halted, reason = True, "请重载扩展 0.4.57 并重新绑定 SA 页；旧扩展不支持需导入结案，未提交。"
            note("需导入协议检查", "已暂停", record.sa_id)
            break
        row = fresh.get("row")
        legacy_unknown = PENDING_NOTE in record.remark
        try:
            verify_sa(record, row)
            count = match_count(row)
        except SafetyStop:
            skipped[record.sa_id] = PENDING_NOTE if legacy_unknown else "名单与网页不一致"
            note("零匹配记录与网页不一致", "已跳过", record.sa_id)
            continue
        if row.get("markStatus") == "已处理":
            try:
                current = reconcile_processed(current, record, row, owner).roster
            except Exception as exc:
                halted, reason = True, "同步后台已处理状态失败：" + str(exc)
                note("同步已处理记录", "已暂停", record.sa_id)
                break
            synced.append(record.sa_id)
            note("后台原已处理，仅同步名单", "已执行", record.sa_id)
            continue
        if not legacy_unknown and (count or str(row.get("itemId") or "").strip(",")):
            # The roster's zero is stale. Leave a visible reason instead of an
            # empty red row; claiming waits for a roster that shows the match.
            skipped[record.sa_id] = changed_match_note(count)
            note("网页匹配数已非 0，未标注需导入", "已跳过", record.sa_id)
            continue
        if not legacy_unknown and row.get("remark") not in ("", None, NOTE):
            skipped[record.sa_id] = "待人工核验"
            note("网页已有其他备注，未覆盖", "已跳过", record.sa_id)
            continue
        if not legacy_unknown:
            try:
                check_legacy_import_journals(current.path.parent, record)
            except SafetyStop as exc:
                skipped[record.sa_id] = LEGACY_NOTE
                note(str(exc), "已跳过", record.sa_id)
                continue
        try:
            guard()
            if store is None:
                store = WorkflowStore(current.path.parent / "runtime" / "need-import")
            reserved = store.reserve(record, intent)
        except _Interrupted as exc:
            halted, reason = not exc.cancelled, str(exc)
            return finish(cancelled=exc.cancelled)
        except Exception as exc:
            halted, reason = True, "无法保存需导入结案断点，未提交：" + str(exc)
            break
        if legacy_unknown or not reserved:
            skipped[record.sa_id] = PENDING_NOTE
            note("已有结案断点，仅只读核验，未重发", "已跳过", record.sa_id)
            continue
        note("需导入结案意图", "已执行", record.sa_id)
        try:
            result = call("complete", {"sa_id": record.sa_id, "expected": row, "reviewed": True,
                                       "owner": owner, "note": NOTE})
        except _Interrupted as exc:
            store.release_reservation(record, intent)
            halted, reason = not exc.cancelled, str(exc)
            return finish(cancelled=exc.cancelled)
        except Exception as exc:
            prewrite = (isinstance(exc, BrowserRejected) and exc.action == "complete"
                        and re.match(r"^(?:\[扩展 [\d.]+\] )?【已暂停】", str(exc))
                        and "已发出写入" not in str(exc))
            if prewrite:
                store.release_reservation(record, intent)
                skipped[record.sa_id] = "需导入未提交"
                note("需导入提交前校验未通过：" + str(exc), "已跳过", record.sa_id)
                continue
            skipped[record.sa_id] = PENDING_NOTE
            halted, reason = True, "需导入结案结果未确认，已暂停；请在网页核验后用“重试跳过项”同步：" + str(exc)
            note("需导入结案", "已暂停", record.sa_id)
            break
        verified = result.get("row") if isinstance(result, dict) else None
        if (not isinstance(result, dict) or result.get("verified") is not True or not isinstance(verified, dict)
                or verified.get("saLzkId") != record.sa_id or verified.get("markStatus") != "已处理"
                or verified.get("remark") != NOTE):
            skipped[record.sa_id] = PENDING_NOTE
            halted, reason = True, "需导入结案回执未确认，已暂停；禁止重复提交，请先核验网页。"
            note("需导入结案回执", "已暂停", record.sa_id)
            break
        try:
            current = reconcile_processed(current, record, verified, owner).roster
        except Exception as exc:
            halted, reason = True, "网页已标注需导入，但名单同步失败；重读名单后用“重试跳过项”同步：" + str(exc)
            note("需导入名单同步", "已暂停", record.sa_id)
            break
        completed.append(record.sa_id)
        note("网页及名单已标注需导入", "已执行", record.sa_id)
    return finish()
