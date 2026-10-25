"""Bounded automatic claim queue for unambiguous, owned comparison rows."""
from __future__ import annotations

from dataclasses import dataclass

from approval import auto_complete_claim, claim_completion_note, complete_claim, verify_claim_result, verified_claim_rejection
from automation import classify
from claim import inspect_staff_claim, sa_claim_source
from core import SafetyStop
from bridge import BrowserRejected
import re
import unicodedata
import json
from pathlib import Path
from datetime import datetime
import uuid
from remarks import CLAIMED, detail_value
from roster_write import mark_skipped_many, reconcile_processed
from skip_notes import brief_skip_note


@dataclass
class ClaimBatchResult:
    roster: object
    completed_ids: tuple
    synced_ids: tuple
    skipped: dict
    checked: int
    cancelled: bool
    halted: bool
    reason: str = ""


class _ClaimInterrupted(SafetyStop):
    """Local pause/stale-roster guard, not a reason to mark a paper skipped."""
    def __init__(self, message, cancelled=False):
        super().__init__(message)
        self.cancelled = cancelled


def _name_forms(value):
    """Return conservative family/given-name forms for one registered alias."""
    value = unicodedata.normalize('NFKC', str(value or '')).casefold().strip()
    compact = ''.join(re.findall(r'[^\W_]+', value, flags=re.UNICODE))
    forms = []
    if ',' in value:
        family, given = value.split(',', 1)
        family = ''.join(re.findall(r'[^\W_]+', family, flags=re.UNICODE))
        given = ''.join(re.findall(r'[^\W_]+', given, flags=re.UNICODE))
        if family and given:
            forms.append((family, given))
    else:
        parts = re.findall(r'[^\W_]+', value, flags=re.UNICODE)
        if len(parts) >= 2:
            # Registered aliases occur in both family-given and given-family
            # order.  We do not choose between those orders here; uniqueness
            # across the actual eligible author rows decides the result.
            forms.extend(((parts[0], ''.join(parts[1:])),
                          (parts[-1], ''.join(parts[:-1]))))
    return compact, tuple(dict.fromkeys(forms))


def _one_edit_apart(left, right):
    if abs(len(left) - len(right)) > 1:
        return False
    if len(left) > len(right):
        left, right = right, left
    if len(left) == len(right):
        return sum(a != b for a, b in zip(left, right)) == 1
    offset = differences = 0
    for index, char in enumerate(left):
        if char != right[index + offset]:
            differences += 1
            offset += 1
            if differences > 1 or char != right[index + offset]:
                return False
    return True


def _alias_score(author_name, aliases):
    """Score one author by general name rules; never use display name as identity."""
    author_compact, author_forms = _name_forms(author_name)
    best = 0
    for alias in aliases:
        alias_compact, alias_forms = _name_forms(alias)
        if author_compact and author_compact == alias_compact:
            best = max(best, 100)
        for family, given in author_forms:
            for alias_family, alias_given in alias_forms:
                if family != alias_family or not given or not alias_given or given[0] != alias_given[0]:
                    continue
                if given == alias_given:
                    best = max(best, 100)
                elif min(len(given), len(alias_given)) >= 4 and _one_edit_apart(given, alias_given):
                    best = max(best, 85)
                elif min(len(given), len(alias_given)) == 1:
                    best = max(best, 75)
                elif (given.startswith(alias_given) or alias_given.startswith(given)) and min(len(given), len(alias_given)) <= 2:
                    best = max(best, 70)
    return best


def automatic_claim_selection(record, before, comparison, result, include_non_institution=False):
    """Use full staff identity, then one unique high-confidence author name."""
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
    if isinstance(suggested, bool) or suggested is not None and not isinstance(suggested, int) or not isinstance(authors, list):
        raise SafetyStop("人员姓名与论文署名不能唯一对应。")
    aliases = tuple(person['names'])
    candidates = []
    for author in authors:
        if (not isinstance(author, dict) or type(author.get('index')) is not int or
                (author.get('eligible') is not True and not (include_non_institution and
                    author.get('eligible') is False and author.get('non_institution') is True)) or author.get('scholarId') != '' or
                not isinstance(author.get('fullname'), str) or
                isinstance(author.get('order'), bool) or
                not str(author.get('order', '')).isdigit() or int(author['order']) <= 0):
            continue
        score = _alias_score(author['fullname'], aliases)
        if score >= 70:
            candidates.append((score, author))
    if not candidates:
        raise SafetyStop("唯一建议署名不可认领或已存在认领关系。")
    best = max(score for score, _ in candidates)
    matches = [author for score, author in candidates if score == best]
    if len(matches) != 1 or suggested is not None and matches[0]['index'] != suggested:
        raise SafetyStop("人员姓名与论文署名不能唯一对应。")
    return prepared, person, matches[0], source, staff_id


def run_claim_batch(roster, records, bridge, cancel=lambda: False, progress=lambda text: None,
                    audit=lambda action, result, sa_id: None, limit=100,
                    retry_skipped=False, skip_writer=None):
    """Retry the skipped snapshot once; ordinary batches keep their limit."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= (max(100,len(roster.records)) if retry_skipped is True else 100):
        raise SafetyStop("单次自动认领数量必须为 1–100。")
    if type(retry_skipped) is not bool:
        raise SafetyStop("跳过任务处理模式无效。")
    ids = []
    for record in records:
        # Normal automation never re-enters a persistent red row. The dedicated
        # retry action does the inverse and accepts only numeric-2 rows.
        if not record.done and record.skipped == retry_skipped and record.sa_id not in ids:
            ids.append(record.sa_id)
    ids = ids if retry_skipped else ids[:limit]
    current_roster = roster
    completed, synced, skipped = [], [], {}
    checked = 0
    halted = False
    halt_reason = ""

    def check_current():
        if cancel():
            raise _ClaimInterrupted("已按请求暂停；当前记录未标记跳过，已确认的操作不会重复提交。", cancelled=True)
        try:
            current_roster.assert_unchanged()
        except (SafetyStop, OSError) as exc:
            raise _ClaimInterrupted("名单核验失败，未提交下一步：" + str(exc)) from exc

    transport = bridge

    class CheckedBridge:
        # The closure helpers use the same guard; checking only the outer loop
        # allowed submit_claim/complete after a long lookup and a user pause.
        def call(self, *args, **kwargs):
            check_current()
            return transport.call(*args, **kwargs)

    bridge = CheckedBridge()

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
                                    reasons={item.sa_id: brief_skip_note(skipped[item.sa_id]) for item in targets})
                current_roster = update.roster
                for item in targets:
                    note("记录跳过：" + skipped[item.sa_id], "已跳过", item.sa_id)
            except Exception as exc:
                halted = True
                for item in targets:
                    skipped[item.sa_id] += "；Excel 数字 2 写入失败：" + str(exc)
                    note("写入跳过标记", "已暂停", item.sa_id)
        return ClaimBatchResult(current_roster, tuple(completed), tuple(synced), skipped,
                                checked, cancelled, halted, halt_reason)

    def interrupted(exc):
        nonlocal halted, halt_reason
        halted, halt_reason = not exc.cancelled, str(exc)
        progress(halt_reason)
        return finish(cancelled=exc.cancelled)

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
            found = bridge.call("search", {"sa_id": sa_id})
            check_current()
        except _ClaimInterrupted as exc:
            return interrupted(exc)
        except Exception as exc:
            # A completed read can reject ONE paper's malformed identity card.
            # It has not submitted a claim and leaves only its readonly detail;
            # the next exact-ID read can release that detail normally. Never
            # classify timeouts, login errors, editors or unknown writes here.
            if (retry_skipped and isinstance(exc, BrowserRejected) and exc.action == 'search'
                    and '【已暂停】' in str(exc) and '已发出写入' not in str(exc)
                    and re.search(r'认领卡片字段不完整|新版本库认领状态未唯一确认|新版认领卡片与 SA 人员信息不一致|作者角色标记未明确',str(exc))):
                try:
                    # A login expiry can surface first as an empty card. Prove
                    # the same task is still readable before treating this as
                    # a per-paper problem or rewriting its existing skip note.
                    current = bridge.call('status', {'sa_id': sa_id}).get('row', {})
                    if (current.get('saLzkId') != sa_id or current.get('gh') != record.staff_id
                            or current.get('markStatus') != '待处理'
                            or str(current.get('matchCount')) != str(record.matches)):
                        raise SafetyStop('卡片异常后的后台身份或处理状态未确认，保留原备注。')
                except _ClaimInterrupted as interrupted_error:
                    return interrupted(interrupted_error)
                except Exception as status_error:
                    halted=True; halt_reason='认领卡片读取失败且会话未确认，未改本条备注：'+str(status_error)
                    note('卡片异常后的会话核验', '已暂停', sa_id)
                    break
                skipped[sa_id] = '认领信息待核'
                note('认领卡片只读核验未通过，保留本条并继续', '已跳过', sa_id)
                continue
            skipped[sa_id] = "读取网页失败，浏览器状态需人工核验：" + str(exc)
            halt_reason = skipped[sa_id]
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

        if claim_state != CLAIMED and record.remark in {'认领回执待核','认领结案待核','认领接口400待核','非交大作者结案待核'}:
            skipped[sa_id]=record.remark
            note('旧认领写入结果未确认，仅回读，不重复提交','已跳过',sa_id)
            continue

        # A pending author-mismatch row can already have a valid claim. Do not
        # duplicate the claim; verify the fresh detail and close it as 已认领.
        if claim_state == CLAIMED:
            try:
                inspect_staff_claim(record, bridge, before, comparison)
                completion_note = claim_completion_note(record, comparison, claimed=True)
            except Exception as exc:
                skipped[sa_id] = str(exc)
                note("已认领记录核验", "已跳过", sa_id)
                continue
            note("已认领记录结案意图", "已执行", sa_id)
            try:
                check_current()
                closed = complete_claim(current_roster, record, bridge, before, comparison,
                                        reviewed=True, note=completion_note)
            except _ClaimInterrupted as exc:
                return interrupted(exc)
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
            check_current()
            if getattr(current_roster, 'path', None):
                try:
                    folder=Path(current_roster.path).resolve().parent/'runtime/claim-preflight'
                    folder.mkdir(parents=True,exist_ok=True)
                    filename=datetime.now().strftime('%Y%m%d-%H%M%S-')+uuid.uuid4().hex+'.json'
                    (folder/filename).write_text(json.dumps({'sa_id':sa_id,
                        'result':prepared_result},ensure_ascii=False,indent=2),encoding='utf-8')
                except (OSError, TypeError, ValueError):
                    progress('认领只读诊断未能保存；未据此重新查询或重提。')
        except _ClaimInterrupted as exc:
            return interrupted(exc)
        except Exception as exc:
            if "作者认领页面结构已变化" in str(exc):
                skipped[sa_id] = "网页没有可用的作者认领窗口。"
                note("自动认领列表条目", "已跳过", sa_id)
                continue
            skipped[sa_id] = "人员或署名无法自动确认：" + str(exc)
            note("自动认领列表条目", "已跳过", sa_id)
            try:
                bridge.call("search", {"sa_id": sa_id})
            except _ClaimInterrupted as exc:
                return interrupted(exc)
            except Exception:
                halted = True
                note("恢复认领工作页", "已暂停", sa_id)
                break
            continue
        try:
            prepared, person, author, _, _ = automatic_claim_selection(
                record, before, comparison, prepared_result, include_non_institution=True)
        except Exception as exc:
            skipped[sa_id] = str(exc)
            note("自动认领列表条目", "已跳过", sa_id)
            try:
                bridge.call("search", {"sa_id": sa_id})
            except _ClaimInterrupted as exc:
                return interrupted(exc)
            except Exception:
                halted = True
                note("恢复认领工作页", "已暂停", sa_id)
                break
            continue
        if author.get('non_institution') is True and author.get('eligible') is False:
            from approval import complete_non_institution_author
            try:
                check_current()
                closed=complete_non_institution_author(current_roster, record, bridge, before,
                    comparison, prepared, author)
            except _ClaimInterrupted as exc:
                return interrupted(exc)
            except Exception as exc:
                if isinstance(exc,BrowserRejected) and '【已暂停】' in str(exc) and '已发出写入' not in str(exc):
                    skipped[sa_id]='非交大作者资料待核'
                    note('非交大作者结案前只读校验：'+str(exc),'已跳过',sa_id)
                    continue
                halted=True; halt_reason='非交大作者结案结果待核：'+str(exc)
                skipped[sa_id]='非交大作者结案待核'
                note('非交大作者结案','已暂停',sa_id)
                break
            current_roster=closed.completion.roster
            completed.append(sa_id)
            note('非交大作者批注及结案','已执行',sa_id)
            continue
        submit = {**payload, "prepared": prepared, "author_index": author["index"], "confirmed": True}
        note("自动认领提交意图", "已执行", sa_id)
        try:
            proof = bridge.call("submit_claim", submit)
            rejected=verified_claim_rejection(proof,before,person,author)
            if rejected:
                message=rejected['message']
                if rejected['code'] in (401,403,600) or re.search(r'登录|验证码|无.*权限|权限不足',message):
                    halted=True;halt_reason='后台会话或认领权限未通过：'+message
                    note(halt_reason,'已暂停',sa_id);break
                skipped[sa_id]='后台拒绝认领'
                note('后台明确拒绝认领：'+message,'已跳过',sa_id)
                continue
            verify_claim_result(record, proof, person, author)
        except _ClaimInterrupted as exc:
            return interrupted(exc)
        except Exception as exc:
            if isinstance(exc,BrowserRejected) and '【已暂停】' in str(exc) and '已发出写入' not in str(exc):
                skipped[sa_id]=str(exc)
                note('认领提交前校验未通过，未发出写入','已跳过',sa_id)
                try:
                    bridge.call('search',{'sa_id':sa_id})
                except _ClaimInterrupted as interrupted_error:
                    return interrupted(interrupted_error)
                except Exception as recovery_error:
                    halted=True;halt_reason='恢复认领只读页失败：'+str(recovery_error)
                    break
                continue
            skipped[sa_id] = "认领结果不确定，禁止继续或重试：" + str(exc)
            note("自动认领列表条目", "已暂停", sa_id)
            halted = True
            break
        note("作者认领已核验", "已执行", sa_id)
        try:
            check_current()
            closed = auto_complete_claim(current_roster, record, bridge, before, comparison,
                                         proof, person, author, confirmed=True, note=completion_note)
        except _ClaimInterrupted as exc:
            return interrupted(exc)
        except Exception as exc:
            skipped[sa_id] = "认领成功，批注或结案未全部核验：" + str(exc)
            note("自动批注结案", "已暂停", sa_id)
            halted = True
            break
        current_roster = closed.completion.roster
        completed.append(sa_id)
        note("自动认领列表条目", "已执行", sa_id)

    return finish()
