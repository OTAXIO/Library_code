"""Zero-match rows: link a paper the library already holds, else mark 需导入.

Each row is read live first, then searched read-only on the library frontend
(front_lookup). If the library holds exactly one agreeing item, its platform ID
is written through the row's own 编辑 dialog, both sides are re-verified, and
the existing staff-ID claim/closure flow takes over. Only a positive frontend
absence closes the row with the fixed 需导入 note. Nothing is ever imported.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from bridge import BrowserRejected
from claim_batch import run_claim_batch
from core import SafetyStop, require_owner
from front_lookup import lookup
from roster_write import mark_skipped_many, reconcile_processed
from wos_policy import match_count
from zero_match import verify_paper_comparison, verify_sa

NOTE = "需导入"
PENDING_NOTE = "需导入结案待核"
# The link dialog stores only the platform ID; this is the reviewed reason.
LINK_NOTE = "前台检索核验本库同一文献，补充平台关联"
LINK_PENDING_NOTE = "前台已有，关联待核"


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
    claimed_ids: tuple = ()   # held by the library: linked, then claimed and closed


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
    completed, synced, claimed, skipped = [], [], [], {}
    # The claim batch writes its own red rows; keep them only for the report.
    claim_skipped = {}
    checked, halted, reason = 0, False, ""

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
        return NeedImportResult(current, tuple(completed), tuple(synced), {**claim_skipped, **skipped},
                                checked, cancelled, halted, reason, tuple(claimed))

    def prewrite(exc):
        return isinstance(exc, BrowserRejected) and "【已暂停】" in str(exc) and "已发出写入" not in str(exc)

    for target in records:
        record = next((r for r in current.records if r.sa_id == target.sa_id), None)
        if not record or record.done:
            continue
        checked += 1
        progress(f"零匹配前台查重 {checked}/{len(records)}：{record.sa_id}")
        try:
            fresh = call("status", {"sa_id": record.sa_id})
        except _Interrupted as exc:
            halted, reason = not exc.cancelled, str(exc)
            return finish(cancelled=exc.cancelled)
        except Exception as exc:
            halted, reason = True, "读取网页失败，未标注：" + str(exc)
            note("读取零匹配记录", "已暂停", record.sa_id)
            break
        if not isinstance(fresh, dict) or fresh.get("need_import_protocol") != 2:
            halted, reason = True, "请重载扩展 0.4.58 并重新绑定 SA 页和前台页；旧扩展不支持前台查重，未提交。"
            note("需导入协议检查", "已暂停", record.sa_id)
            break
        row = fresh.get("row")
        try:
            verify_sa(record, row)
            count = match_count(row)
        except SafetyStop:
            skipped[record.sa_id] = "名单与网页不一致"
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
        item_id = str(row.get("itemId") or "").strip(",")
        # A single match on a roster zero is either our own unconfirmed link or
        # someone else's; it may be claimed only if the frontend agrees on it.
        linked = count == 1 and bool(re.fullmatch(r"\d{1,40}", item_id))
        if (count or item_id) and not linked:
            # The roster's zero is stale. Leave a visible reason instead of an
            # empty red row; claiming waits for a roster that shows the match.
            skipped[record.sa_id] = changed_match_note(count)
            note("网页匹配数已非 0，未标注需导入", "已跳过", record.sa_id)
            continue
        if row.get("remark") not in ("", None) and (linked or row.get("remark") != NOTE):
            skipped[record.sa_id] = "待人工核验"
            note("网页已有其他备注，未覆盖", "已跳过", record.sa_id)
            continue
        try:
            found = lookup(call, record)
        except _Interrupted as exc:
            halted, reason = not exc.cancelled, str(exc)
            return finish(cancelled=exc.cancelled)
        except Exception as exc:
            # Login expiry or an unbound frontend affects every row: stop
            # before writing anything rather than marking papers 需导入.
            halted, reason = True, "前台检索未完成，未标注需导入：" + str(exc)
            note("前台检索本库", "已暂停", record.sa_id)
            break
        if found.review:
            skipped[record.sa_id] = found.review
            note("前台检索：" + found.review, "已跳过", record.sa_id)
            continue
        if linked and (not found.item or found.item["id"] != item_id):
            skipped[record.sa_id] = changed_match_note(count)
            note("网页已有匹配但与前台检索不一致", "已跳过", record.sa_id)
            continue
        if found.item:
            item = found.item
            if row.get("remark"):
                # Claim closure writes its own note; never stack it on 需导入.
                skipped[record.sa_id] = "待人工核验"
                note("前台已有但网页已有备注，未关联", "已跳过", record.sa_id)
                continue
            if not linked:
                note("前台本库已有，关联平台唯一号意图：" + item["id"], "已执行", record.sa_id)
                try:
                    proof = call("link", {"sa_id": record.sa_id, "expected": row, "reviewed": True,
                                          "note": LINK_NOTE, "item_id": item["id"]})
                except _Interrupted as exc:
                    halted, reason = not exc.cancelled, str(exc)
                    return finish(cancelled=exc.cancelled)
                except Exception as exc:
                    if prewrite(exc):
                        skipped[record.sa_id] = "前台已有，关联未提交"
                        note("关联提交前校验未通过：" + str(exc), "已跳过", record.sa_id)
                        continue
                    skipped[record.sa_id] = LINK_PENDING_NOTE
                    halted, reason = True, "平台号关联结果未确认，已暂停；请核验网页后用“重试跳过项”续验：" + str(exc)
                    note("关联平台唯一号", "已暂停", record.sa_id)
                    break
                after = proof.get("row") if isinstance(proof, dict) else None
                if (not isinstance(proof, dict) or proof.get("verified") is not True or not isinstance(after, dict)
                        or after.get("saLzkId") != record.sa_id
                        or str(after.get("itemId") or "").strip(",") != item["id"]):
                    skipped[record.sa_id] = LINK_PENDING_NOTE
                    halted, reason = True, "平台号关联回执未确认，已暂停；禁止重复提交，请先核验网页。"
                    note("关联平台唯一号回执", "已暂停", record.sa_id)
                    break
                note("平台唯一号已关联并回读", "已执行", record.sa_id)
            try:
                detail = call("search", {"sa_id": record.sa_id})
                after = detail.get("row") if isinstance(detail, dict) else None
                verify_sa(record, after)
                if (str(after.get("matchCount")) != "1" or str(after.get("itemId") or "").strip(",") != item["id"]
                        or after.get("markStatus") != "待处理"):
                    raise SafetyStop("关联后未回读为唯一待处理匹配。")
                # Both sides: library fields equal the frontend item, SA fields the roster.
                verify_paper_comparison(detail.get("comparison"), item, record)
            except _Interrupted as exc:
                halted, reason = not exc.cancelled, str(exc)
                return finish(cancelled=exc.cancelled)
            except SafetyStop as exc:
                skipped[record.sa_id] = "已关联，两边信息待核"
                note("关联后比对核验：" + str(exc), "已跳过", record.sa_id)
                continue
            except Exception as exc:
                halted, reason = True, "关联后读取比对详情失败，已暂停：" + str(exc)
                note("关联后比对核验", "已暂停", record.sa_id)
                break
            try:
                guard()
                outcome = run_claim_batch(current, [record], bridge, cancel=cancel, progress=progress, audit=audit,
                                          retry_skipped=retry_skipped, owner=owner, current_match=True)
            except _Interrupted as exc:
                halted, reason = not exc.cancelled, str(exc)
                return finish(cancelled=exc.cancelled)
            except Exception as exc:
                halted, reason = True, "关联后认领未开始：" + str(exc)
                note("关联后认领", "已暂停", record.sa_id)
                break
            current = outcome.roster
            claim_skipped.update(outcome.skipped)
            if outcome.completed_ids or outcome.synced_ids:
                claimed.append(record.sa_id)
            if outcome.cancelled or outcome.halted:
                halted, reason = outcome.halted, outcome.reason or reason
                return finish(cancelled=outcome.cancelled)
            continue
        note("需导入结案意图", "已执行", record.sa_id)
        try:
            result = call("complete", {"sa_id": record.sa_id, "expected": row, "reviewed": True,
                                       "owner": owner, "note": NOTE})
        except _Interrupted as exc:
            halted, reason = not exc.cancelled, str(exc)
            return finish(cancelled=exc.cancelled)
        except Exception as exc:
            if prewrite(exc):
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
