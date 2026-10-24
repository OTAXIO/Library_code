"""A narrowly authorized staff-number reassignment, separate from new imports.

The caller supplies the human-confirmed old/new wno and author order. This is
never inferred from a name or automatically applied to other conflicts. Each
native write has a durable intent; an uncertain result is read back, not replayed.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from claim import inspect_staff_claim, sa_claim_source
from bridge import BrowserRejected
from core import SafetyStop
from roster_write import reconcile_processed, write_lock
from zero_match import verify_sa


def validate_authorization(record, authorization):
    if (record.owner != '谭勋策' or not isinstance(authorization, dict)
            or set(authorization) != {'version', 'approved', 'sa_id', 'item_id',
                                     'staff_id', 'previous_staff_id', 'author_order'}
            or type(authorization.get('version')) is not int or authorization['version'] != 1 or authorization.get('approved') is not True
            or authorization.get('sa_id') != record.sa_id
            or authorization.get('staff_id') != record.staff_id
            or any(not isinstance(authorization.get(k), str)
                   or not re.fullmatch(r'[0-9]{1,40}', authorization[k])
                   for k in ('staff_id', 'previous_staff_id', 'item_id'))
            or authorization['previous_staff_id'] == authorization['staff_id']
            or type(authorization.get('author_order')) is not int
            or authorization['author_order'] < 1):
        raise SafetyStop('认领纠正必须明确这条 SA、平台号、作者序号和两个不同的完整工号。')


def run_reassignment(base, roster, record, bridge, authorization, audit=lambda *args: None,
                     cancel=lambda: False):
    """Return refreshed roster only after the site is processed with 已认领.

    Original list match count/item ID stay intact. Claim correction is not a new
    admission and deliberately never applies a yellow admission highlight.
    """
    validate_authorization(record, authorization)
    if record not in roster.records:
        raise SafetyStop('认领纠正目标不属于当前名单。')
    roster.assert_unchanged()
    if record.done:
        return roster
    folder = Path(base).resolve() / 'runtime' / 'claim-reassignments'
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (record.key + '.json')
    if path.is_symlink():
        raise SafetyStop('认领纠正断点不能是链接。')

    def save(value):
        temporary = path.with_suffix('.tmp')
        # No process may replace an active reassignment's intent or authorization.
        with temporary.open('x', encoding='utf-8') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def verify(row):
        verify_sa(record, row)
        if (row.get('gh') != record.staff_id or str(row.get('matchCount')) != '1'
                or str(row.get('itemId', '')).lstrip(',') != authorization['item_id']):
            raise SafetyStop('后台的唯一匹配平台号或完整工号已变化，未纠正。')

    def call(action, payload):
        roster.assert_unchanged()
        if cancel():
            raise SafetyStop('已请求暂停；保留认领断点，未提交下一步。')
        return bridge.call(action, payload)

    def submit_once(action, payload, unsubmitted_phase):
        try:
            return call(action, payload)
        except BrowserRejected as exc:
            # Only the adapter's explicit pre-write rejection is known NOT to
            # have submitted. Timeouts/lost ACKs/已发出写入 keep their intent.
            if exc.action == action and '【已暂停】' in str(exc) and '已发出写入' not in str(exc):
                state['phase'] = unsubmitted_phase
                state['last_preflight_rejection'] = str(exc)
                save(state)
            raise

    def read():
        roster.assert_unchanged()
        result = call('search', {'sa_id': record.sa_id, 'authorization': authorization})
        verify(result.get('row'))
        if type(result.get('staff_claim_protocol')) is not int or result['staff_claim_protocol'] != 1:
            raise SafetyStop('请加载支持完整工号认领核验的新版扩展；未修改认领。')
        source, staff = sa_claim_source(result.get('comparison'))
        if staff != record.staff_id:
            raise SafetyStop('SA 认领卡片工号与名单不一致。')
        return result, {'sa_id': record.sa_id, 'expected': result['row'], 'sa_text': source,
            'staff_id': staff, 'roster_staff_id': record.staff_id, 'authorization': authorization}

    with write_lock(path):
        state = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        if state and (state.get('record_key') != record.key or state.get('authorization') != authorization
                      or state.get('phase') not in {'prepared', 'cancel_intent', 'cancelled',
                          'claim_intent', 'claimed', 'complete_intent', 'done'}):
            raise SafetyStop('原认领纠正断点或授权不同，未覆盖。')
        result, payload = read()
        # A person may complete this exact task while a script is paused. A
        # verified existing claim is a read-only reconciliation, never a reason
        # to cancel/reclaim it again. Its full wno, scholar and order still bind.
        if (state.get('phase') in (None, 'prepared') and result['row'].get('markStatus') == '已处理'
                and result['row'].get('remark') == '已认领'):
            proof = inspect_staff_claim(record, bridge, result['row'], result['comparison'])
            if (proof.get('order') != authorization['author_order'] or state and
                    proof.get('scholar_id') != state['prepared']['person']['id']):
                raise SafetyStop('已处理认领的工号学者或作者序号与授权不一致，未同步名单。')
            if state:
                old = state['prepared']['authors']
                current = proof.get('prepared', {}).get('authors')
                if not isinstance(current, list) or len(old) != len(current):
                    raise SafetyStop('其他作者认领回读不完整，未同步名单。')
                for previous, now in zip(old, current):
                    if previous['order'] != authorization['author_order'] and previous != now:
                        raise SafetyStop('其他作者认领已改变，未同步名单。')
            latest, _ = read()
            if latest['row'] != result['row'] or latest['comparison'] != result['comparison']:
                raise SafetyStop('认领回读后网页状态改变，未同步名单。')
            saved = reconcile_processed(roster, record, latest['row'])
            state.update(record_key=record.key, authorization=authorization, phase='done',
                         completion_origin='verified_existing_claim', staff_proof=proof)
            save(state)
            audit('按完整工号核验已有认领并同步已处理名单', '已执行', record.sa_id)
            return saved.roster
        if not state:
            if result['row'].get('markStatus') != '待处理':
                raise SafetyStop('记录已被处理，未新建纠正任务或覆盖备注。')
            prepared = call('prepare_reassignment', payload)
            if (prepared.get('verified') is not True or prepared.get('stage') != 'original'
                    or prepared.get('row') != result['row'] or prepared.get('comparison') != result['comparison']):
                raise SafetyStop('原认领工号准备结果不一致，未修改。')
            evidence = prepared.get('prepared', {})
            if (evidence.get('authorization') != authorization or evidence.get('staff_id') != record.staff_id
                    or evidence.get('person', {}).get('wno') != record.staff_id
                    or evidence.get('previous_person', {}).get('wno') != authorization['previous_staff_id']
                    or evidence.get('person', {}).get('id') == evidence.get('previous_person', {}).get('id')):
                raise SafetyStop('新旧工号未解析为不同的唯一学者。')
            state = {'record_key': record.key, 'authorization': authorization,
                     'phase': 'prepared', 'prepared': evidence}
            save(state)
        payload['prepared'] = state['prepared']

        def inspect():
            fresh, data = read()
            response = call('inspect_reassignment', {**data, 'prepared': state['prepared']})
            if (response.get('verified') is not True or response.get('row') != fresh['row']
                    or response.get('staff_id') != record.staff_id
                    or response.get('scholar_id') != state['prepared']['person']['id']
                    or response.get('order') != authorization['author_order']):
                raise SafetyStop('认领纠正回读未确认，未重复操作。')
            return response

        if state['phase'] in ('cancel_intent', 'claim_intent', 'claimed', 'complete_intent', 'done'):
            proof = inspect()
            stage = proof.get('stage')
            if state['phase'] == 'cancel_intent' and stage == 'cancelled':
                state['phase'] = 'cancelled'; save(state)
            elif stage == 'claimed' and state['phase'] != 'cancel_intent':
                # Keep complete_intent: an unknown closure must not be resent.
                if state['phase'] == 'claim_intent':
                    state['phase'] = 'claimed'; save(state)
            else:
                raise SafetyStop('原认领操作结果尚未确认，保留断点；禁止自动重复提交。')
        if state['phase'] == 'prepared':
            audit('保存按完整工号撤销误认领的提交意图', '已执行', record.sa_id)
            state['phase'] = 'cancel_intent'; save(state)
            roster.assert_unchanged()
            cancelled = submit_once('cancel_reassignment', {**payload, 'confirmed': True}, 'prepared')
            if (cancelled.get('verified') is not True or cancelled.get('stage') != 'cancelled'
                    or cancelled.get('row') != result['row'] or cancelled.get('staff_id') != record.staff_id
                    or cancelled.get('scholar_id') != state['prepared']['person']['id']
                    or cancelled.get('order') != authorization['author_order']):
                raise SafetyStop('撤销认领回读未确认，保留断点，禁止重复提交。')
            state['phase'] = 'cancelled'; save(state)
            audit('撤销误认领后回读原工号已解除', '已执行', record.sa_id)
        if state['phase'] == 'cancelled':
            fresh, payload = read()
            audit('保存按 SA 完整工号重新认领的提交意图', '已执行', record.sa_id)
            state['phase'] = 'claim_intent'; save(state)
            proof = submit_once('submit_reassignment', {**payload, 'prepared': state['prepared'], 'confirmed': True}, 'cancelled')
            if (proof.get('verified') is not True or proof.get('claimed') is not True
                    or proof.get('staff_id') != record.staff_id
                    or proof.get('scholar_id') != state['prepared']['person']['id']
                    or proof.get('order') != authorization['author_order']):
                raise SafetyStop('新认领的工号、学者或作者行未确认，禁止重复提交。')
            state['phase'] = 'claimed'; save(state)
            audit('新工号和作者行认领回读一致', '已执行', record.sa_id)
        proof = inspect()
        if proof.get('stage') != 'claimed':
            raise SafetyStop('没有按 SA 工号确认新的作者认领，未结案。')
        latest, _ = read()
        row = latest['row']
        if row.get('remark', '') not in ('', '已认领'):
            raise SafetyStop('后台已有其他备注，未覆盖。')
        if row.get('markStatus') != '已处理' or row.get('remark') != '已认领':
            if state['phase'] in ('complete_intent', 'done'):
                raise SafetyStop('原结案结果尚未确认，保留断点；未重复结案。')
            state['phase'] = 'complete_intent'; save(state)
            roster.assert_unchanged()
            call('complete', {'sa_id': record.sa_id, 'expected': row,
                'expected_comparison': latest['comparison'], 'reviewed': True, 'owner': record.owner,
                'note': '已认领', 'staff_claim_evidence': proof})
            latest, _ = read()
            row = latest['row']
        if row.get('markStatus') != '已处理' or row.get('remark') != '已认领':
            raise SafetyStop('网页没有同时确认已认领批注和已处理，名单未标完成。')
        # Native metadata + two wno lookups were independently checked above.
        saved = reconcile_processed(roster, record, row)
        state['phase'] = 'done'; save(state)
        audit('纠正认领后网页和名单批注结案', '已执行', record.sa_id)
        return saved.roster
