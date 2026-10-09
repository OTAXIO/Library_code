"""Bounded automatic claim queue for unambiguous, owned comparison rows."""
from __future__ import annotations

from dataclasses import dataclass

from approval import auto_complete_claim, claim_completion_note, complete_claim, verify_claim_result
from automation import classify
from claim import sa_claim_source
from core import SafetyStop
from remarks import CLAIMED, detail_value
from roster_write import mark_skipped_many, reconcile_processed


@dataclass
class ClaimBatchResult:
    roster: object
    completed_ids: tuple
    synced_ids: tuple
    skipped: dict
    checked: int
    cancelled: bool
    halted: bool


def automatic_claim_selection(record, before, comparison, result):
    """Accept only the extension's one exact person + one exact author match."""
    if not isinstance(result, dict) or result.get("row") != before or result.get("comparison") != comparison:
        raise SafetyStop("人员查找前后条目或比对详情发生变化。")
    source, staff_id = sa_claim_source(comparison)
    if not record.staff_id or staff_id != record.staff_id or before.get("gh") != staff_id:
        raise SafetyStop("名单、SA 和后台人员编号不一致。")
    prepared = result.get("prepared")
    if not isinstance(prepared, dict) or prepared.get("staff_id") != staff_id or prepared.get("sa_text") != source:
        raise SafetyStop("人员查找结果与 SA 提交信息不一致。")
    person = prepared.get("person")
    if (not isinstance(person, dict) or not all(isinstance(person.get(key), str) and person[key]
            for key in ("id", "wno", "name")) or person["wno"] != staff_id or
            not isinstance(person.get("names"), list) or
            any(not isinstance(name, str) or not name.strip() for name in person["names"])):
        raise SafetyStop("未查到唯一、完整的人员信息。")
    suggested = result.get("suggested_index")
    authors = prepared.get("authors")
    if isinstance(suggested, bool) or not isinstance(suggested, int) or not isinstance(authors, list):
        raise SafetyStop("人员姓名与论文署名不能唯一对应。")
    matches = [author for author in authors if isinstance(author, dict) and
               author.get("index") == suggested and author.get("eligible") is True and
               author.get("scholarId") == "" and isinstance(author.get("fullname"), str) and
               author["fullname"].strip() in person["names"] and
               not isinstance(author.get("order"), bool) and
               str(author.get("order", "")).isdigit() and int(author["order"]) > 0]
    if len(matches) != 1:
        raise SafetyStop("唯一建议署名不可认领或已存在认领关系。")
    return prepared, person, matches[0], source, staff_id


def run_claim_batch(roster, records, bridge, cancel=lambda: False, progress=lambda text: None,
                    audit=lambda action, result, sa_id: None, limit=100,
                    retry_skipped=False, skip_writer=None):
    """Process at most ``limit`` rows; never continue after an uncertain write."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise SafetyStop("单次自动认领数量必须为 1–100。")
    if type(retry_skipped) is not bool:
        raise SafetyStop("跳过任务处理模式无效。")
    ids = []
    for record in records:
        # Normal automation never re-enters a persistent red row. The dedicated
        # retry action does the inverse and accepts only numeric-2 rows.
        if not record.done and record.skipped == retry_skipped and record.sa_id not in ids:
            ids.append(record.sa_id)
    ids = ids[:limit]
    current_roster = roster
    completed, synced, skipped = [], [], {}
    checked = 0
    halted = False

    def note(action, result, sa_id):
        try:
            audit(action, result, sa_id)
        except Exception:
            progress("log.txt 未能保存自动认领记录，请检查文件权限和格式。")

    def finish(cancelled=False):
        """Persist newly skipped owned rows as numeric 2 in one atomic write."""
        nonlocal current_roster, halted
        targets = []
        for sa_id in skipped:
            item = next((candidate for candidate in current_roster.records
                         if candidate.sa_id == sa_id), None)
            # Never modify another owner's row. A retry row already contains 2.
            if item and item.owner == "谭勋策" and not item.done and (not item.skipped or getattr(current_roster,'status_separate',False)):
                targets.append(item)
        if targets:
            writer = skip_writer or mark_skipped_many
            try:
                if skip_writer:
                    update = writer(current_roster, targets)
                else:
                    update = writer(current_roster, targets,
                                    reasons={item.sa_id: skipped[item.sa_id][:2000] for item in targets})
                current_roster = update.roster
                for item in targets:
                    note("写入跳过标记", "已执行", item.sa_id)
            except Exception as exc:
                halted = True
                for item in targets:
                    skipped[item.sa_id] += "；Excel 数字 2 写入失败：" + str(exc)
                    note("写入跳过标记", "已暂停", item.sa_id)
        return ClaimBatchResult(current_roster, tuple(completed), tuple(synced), skipped,
                                checked, cancelled, halted)

    for sa_id in ids:
        if cancel():
            return finish(cancelled=True)
        record = next((item for item in current_roster.records if item.sa_id == sa_id), None)
        if not record or record.done or record.skipped != retry_skipped:
            continue
        checked += 1
        progress(f"自动认领 {checked}/{len(ids)}：{sa_id}")
        if record.owner != "谭勋策":
            skipped[sa_id] = "不是谭勋策负责的记录。"
            note("自动认领列表条目", "已跳过", sa_id)
            continue
        reason = str(record.reason or "")
        supported_reason = reason == "作者不一致" or ("DOI" in reason and "WOS" in reason)
        if record.matches != 1 or not supported_reason:
            skipped[sa_id] = "仅处理单匹配的作者认领或 DOI/WOSID 双缺失认领；本条保留人工核验。"
            note("自动认领列表条目", "已跳过", sa_id)
            continue
        try:
            current_roster.assert_unchanged()
            found = bridge.call("search", {"sa_id": sa_id})
        except Exception as exc:
            skipped[sa_id] = "读取网页失败，浏览器状态需人工核验：" + str(exc)
            note("自动认领列表条目", "已暂停", sa_id)
            halted = True
            break
        try:
            completion = reconcile_processed(current_roster, record, found.get("row"))
        except Exception as exc:
            skipped[sa_id] = "同步后台已处理状态失败：" + str(exc)
            note("自动认领列表条目", "已暂停", sa_id)
            halted = True
            break
        if completion:
            current_roster = completion.roster
            synced.append(sa_id)
            note("自动认领列表条目", "已执行", sa_id)
            continue
        try:
            before, comparison = found["row"], found.get("comparison")
            if before.get("reason") != record.reason or str(before.get("matchCount")) != str(record.matches):
                raise SafetyStop("网页待处理原因或匹配数与名单不一致，本条保留人工核验。")
            claim_state = detail_value(comparison, "认领状态", "library")
        except Exception as exc:
            skipped[sa_id] = str(exc)
            note("自动认领列表条目", "已跳过", sa_id)
            continue

        # A pending author-mismatch row can already have a valid claim. Do not
        # duplicate the claim; verify the fresh detail and close it as 已认领.
        if claim_state == CLAIMED:
            try:
                completion_note = claim_completion_note(record, comparison, claimed=True)
            except Exception as exc:
                skipped[sa_id] = str(exc)
                note("已认领记录核验", "已跳过", sa_id)
                continue
            note("已认领记录结案意图", "已执行", sa_id)
            try:
                closed = complete_claim(current_roster, record, bridge, before, comparison,
                                        reviewed=True, note=completion_note)
            except Exception as exc:
                skipped[sa_id] = "已认领，但批注或结案未全部核验：" + str(exc)
                note("已认领记录结案", "已暂停", sa_id)
                halted = True
                break
            current_roster = closed.completion.roster
            completed.append(sa_id)
            note("已认领记录结案", "已执行", sa_id)
            continue

        try:
            plan = classify(record, found)
            if plan.route != "claim":
                raise SafetyStop(plan.reason)
            completion_note = claim_completion_note(record, comparison, claimed=False)
            source, staff_id = sa_claim_source(comparison)
            payload = {"sa_id": sa_id, "expected": before, "sa_text": source,
                       "staff_id": staff_id, "roster_staff_id": record.staff_id}
        except Exception as exc:
            skipped[sa_id] = str(exc)
            note("自动认领列表条目", "已跳过", sa_id)
            continue
        try:
            prepared_result = bridge.call("prepare_claim", payload)
        except Exception as exc:
            if "作者认领页面结构已变化" in str(exc):
                skipped[sa_id] = "网页没有可用的作者认领窗口。"
                note("自动认领列表条目", "已跳过", sa_id)
                continue
            skipped[sa_id] = "人员或署名无法自动确认：" + str(exc)
            note("自动认领列表条目", "已跳过", sa_id)
            try:
                bridge.call("search", {"sa_id": sa_id})
            except Exception:
                halted = True
                note("恢复认领工作页", "已暂停", sa_id)
                break
            continue
        try:
            prepared, person, author, _, _ = automatic_claim_selection(
                record, before, comparison, prepared_result)
        except Exception as exc:
            skipped[sa_id] = str(exc)
            note("自动认领列表条目", "已跳过", sa_id)
            try:
                bridge.call("search", {"sa_id": sa_id})
            except Exception:
                halted = True
                note("恢复认领工作页", "已暂停", sa_id)
                break
            continue
        submit = {**payload, "prepared": prepared, "author_index": author["index"], "confirmed": True}
        note("自动认领提交意图", "已执行", sa_id)
        try:
            proof = bridge.call("submit_claim", submit)
            verify_claim_result(record, proof, person, author)
        except Exception as exc:
            skipped[sa_id] = "认领结果不确定，禁止继续或重试：" + str(exc)
            note("自动认领列表条目", "已暂停", sa_id)
            halted = True
            break
        note("作者认领已核验", "已执行", sa_id)
        try:
            closed = auto_complete_claim(current_roster, record, bridge, before, comparison,
                                         proof, person, author, confirmed=True, note=completion_note)
        except Exception as exc:
            skipped[sa_id] = "认领成功，批注或结案未全部核验：" + str(exc)
            note("自动批注结案", "已暂停", sa_id)
            halted = True
            break
        current_roster = closed.completion.roster
        completed.append(sa_id)
        note("自动认领列表条目", "已执行", sa_id)

    return finish()
