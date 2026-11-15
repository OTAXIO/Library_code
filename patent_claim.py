"""Conservative multi-item patent claims using the shared native claim protocol."""
import re

from approval import verify_claim_result
from bridge import BrowserRejected
from claim_batch import ClaimBatchResult, automatic_claim_selection
from core import SafetyStop, require_owner
from need_import import run_need_import_batch
from roster_write import mark_skipped_many, reconcile_processed
from zero_match import WorkflowStore


def item_ids(value):
    return tuple(x.strip() for x in str(value or '').split(',') if x.strip())


def verify_patent_row(record, row):
    ids = item_ids(row.get('itemId')) if isinstance(row, dict) else ()
    if (not isinstance(row, dict) or row.get('saPatentId') != record.sa_id
            or not isinstance(row.get('gh'), str) or row['gh'].strip() != record.staff_id
            or str(row.get('matchCount')) != str(record.matches)
            or any(not re.fullmatch(r'[0-9]{1,40}', item) for item in ids)
            or len(ids) != len(set(ids)) or set(ids) != set(item_ids(record.item_ids))
            or row.get('markStatus') not in ('待处理', '已处理')):
        raise SafetyStop('名单与网页不一致')


class _Paused(SafetyStop):
    pass


def run_patent_batch(roster, records, bridge, owner, cancel=lambda: False,
                     progress=lambda text: None, audit=lambda action, result, sa_id: None,
                     retry_skipped=False):
    owner = require_owner(owner)
    records = list(records)
    if (roster.kind != 'patent' or type(retry_skipped) is not bool or not records
            or len({r.sa_id for r in records}) != len(records)
            or any(r not in roster.records or r.owner != owner or r.done or r.skipped != retry_skipped for r in records)):
        raise SafetyStop('专利运行队列与负责人、名单或范围不一致。')
    current = roster
    completed, synced, skipped = [], [], {}
    checked, halted, reason = 0, False, ''

    def guard():
        if cancel():
            raise _Paused('已按请求暂停，保留已确认条目和写入断点。')
        current.assert_unchanged()

    def call(action, payload):
        guard()
        return bridge.call(action, payload)

    def event(action, outcome, sa_id):
        try:
            audit(action, outcome, sa_id)
        except Exception:
            progress('log.txt 未能保存专利记录。')

    class CheckedBridge:
        def call(self, action, payload):
            return call(action, payload)

    def read(record):
        result = call('search', {'sa_id': record.sa_id, 'close_detail': True})
        verify_patent_row(record, result.get('row'))
        left, cards = result.get('left'), result.get('cards')
        if (not isinstance(left, dict) or left.get('id') != record.sa_id
                or left.get('gh') != record.staff_id or not isinstance(left.get('xm'), str)
                or not left['xm'].strip() or not isinstance(cards, list)
                or len(cards) != record.matches
                or {c.get('itemId') for c in cards} != set(item_ids(record.item_ids))):
            raise SafetyStop('专利详情与名单不一致')
        return result

    for original in records:
        record = next(r for r in current.records if r.sa_id == original.sa_id)
        if cancel():
            break
        checked += 1
        progress(f'专利认领 {checked}/{len(records)}：{record.sa_id}')
        # Three or more matches are a local-only handoff; never query the site.
        if record.matches >= 3:
            skipped[record.sa_id] = f'匹配到 {record.matches} 条，未结项'
            continue
        try:
            status = call('status', {'sa_id': record.sa_id})
            if status.get('page_kind') != 'patent':
                raise _Paused('绑定页类型不符：请切换到 SA 专利比对页并重新配对。')
            try:
                verify_patent_row(record, status.get('row'))
            except SafetyStop:
                skipped[record.sa_id] = '名单与网页不一致'
                continue
            if status['row']['markStatus'] == '已处理':
                current = reconcile_processed(current, record, status['row'], owner).roster
                synced.append(record.sa_id)
                continue
            if record.matches == 0:
                result = run_need_import_batch(current, [record], CheckedBridge(), owner, cancel,
                                               progress, audit, retry_skipped)
                current = result.roster
                completed.extend(result.completed_ids); synced.extend(result.synced_ids)
                skipped.update(result.skipped)
                if result.halted or result.cancelled:
                    halted, reason = result.halted, result.reason
                    break
                continue
            failed = False
            for index, item_id in enumerate(item_ids(record.item_ids), 1):
                try:
                    found = read(record)
                    card = next(c for c in found['cards'] if c['itemId'] == item_id)
                    if card.get('claimed') is True and card.get('staff_id') == record.staff_id:
                        continue
                    store = WorkflowStore(current.path.parent / 'runtime/patent-claims' / item_id)
                    if store.get(record):
                        raise _Paused(f'第 {index} 个条目：原认领写入结果未确认，仅回读，禁止重复提交。')
                    source = f"{found['left']['xm'].strip()}({record.staff_id})"
                    payload = {'sa_id': record.sa_id, 'item_id': item_id, 'expected': found['row'],
                               'sa_text': source, 'staff_id': record.staff_id, 'roster_staff_id': record.staff_id}
                    inspected = call('inspect_claim', payload)
                    if inspected.get('verified') is not True or inspected.get('row') != found['row'] or inspected.get('staff_id') != record.staff_id:
                        raise SafetyStop('人员认领关系未确认')
                    if inspected.get('claimed') is True:
                        # Native scholar binding is also checked, but closure still needs a fresh card.
                        if not inspected.get('scholar_id') or not inspected.get('order'):
                            raise SafetyStop('认领学者或发明人行未确认')
                        continue
                    prepared = call('prepare_claim', payload)
                    comparison = [{'label': '认领状态', 'sa': source, 'library': '未认领'}]
                    normalized = {**found['row'], 'gh': record.staff_id}
                    try:
                        if prepared.get('row') != found['row']:
                            raise SafetyStop('人员查找前后条目发生变化。')
                        plan, person, author, _, _ = automatic_claim_selection(
                            record, normalized, comparison, {**prepared, 'row': normalized})
                    except SafetyStop as exc:
                        raise SafetyStop('人员姓名与发明人不能唯一对应：' + str(exc)) from exc
                    if plan.get('item_id') != item_id:
                        raise SafetyStop('认领计划平台号不一致')
                    guard()
                    intent = {'phase': 'claim_intent', 'item_id': item_id, 'prepared': plan, 'author': author}
                    if not store.reserve(record, intent):
                        raise _Paused('已有认领意图，禁止重复提交。')
                    event(f'专利第 {index} 个条目认领意图', '已执行', record.sa_id)
                    try:
                        proof = call('submit_claim', {**payload, 'prepared': plan, 'author_index': author['index'], 'confirmed': True})
                        if proof.get('row') != found['row']:
                            raise SafetyStop('认领回读目标行不一致。')
                        verify_claim_result(record, proof, person, author, id_field='saPatentId')
                    except BrowserRejected as exc:
                        if '【已暂停】' in str(exc) and '已发出写入' not in str(exc):
                            store.release_reservation(record, intent)
                            raise SafetyStop(str(exc)) from exc
                        raise _Paused('专利认领回执未确认，禁止重复提交：' + str(exc)) from exc
                    except Exception as exc:
                        raise _Paused('专利认领回执未确认，禁止重复提交：' + str(exc)) from exc
                    event(f'专利第 {index} 个条目已核验认领', '已执行', record.sa_id)
                except _Paused:
                    raise
                except SafetyStop as exc:
                    skipped[record.sa_id] = f'第 {index} 个条目：{exc}'
                    failed = True
                    # Restore only clean, invocation-owned claim windows.
                    call('search', {'sa_id': record.sa_id, 'close_detail': True})
                    break
            if failed:
                continue
            found = read(record)
            if not all(c.get('claimed') is True and c.get('staff_id') == record.staff_id for c in found['cards']):
                skipped[record.sa_id] = '全部条目认领回读尚未确认，未结项'
                continue
            store = WorkflowStore(current.path.parent / 'runtime/patent-claims/complete')
            guard()
            if not store.reserve(record, {'phase': 'complete_intent', 'note': '已认领'}):
                raise _Paused('原专利结案回执待核，禁止重复提交。')
            event('专利结案意图', '已执行', record.sa_id)
            result = call('complete', {'sa_id': record.sa_id, 'expected': found['row'],
                                      'reviewed': True, 'owner': owner, 'note': '已认领'})
            verify_patent_row(record, result.get('row'))
            if result.get('verified') is not True or result['row'].get('markStatus') != '已处理' or result['row'].get('remark') != '已认领':
                raise _Paused('专利结案回读未确认，禁止重复提交。')
            current = reconcile_processed(current, record, result['row'], owner).roster
            completed.append(record.sa_id)
            event('专利网页及名单已认领', '已执行', record.sa_id)
        except Exception as exc:
            halted, reason = not cancel(), str(exc)
            event('专利暂停：' + reason, '已暂停', record.sa_id)
            break
    targets = [r for r in current.records if r.sa_id in skipped and not r.done]
    if targets:
        current = mark_skipped_many(current, targets, reasons={r.sa_id: skipped[r.sa_id] for r in targets}).roster
        for record in targets:
            event('专利跳过：' + skipped[record.sa_id], '已跳过', record.sa_id)
    return ClaimBatchResult(current, tuple(completed), tuple(synced), skipped, checked, cancel(), halted, reason)
