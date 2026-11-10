"""Correct rows already closed as 已认领 whose author was claimed by a homonym.

The normal claim batch only handles open rows and never overrides an existing
claim, so such rows need this explicit two-step correction through the
production extension on the existing SA comparison page:

  --plan OUT.json     read-only. For each row: read the site, and when the
                      library claim belongs to a different full staff number,
                      find the author row it holds; the adapter's
                      prepare_reassignment must verify old/new wno, scholar
                      and author row. Nothing is cancelled or claimed.
  --apply PLAN.json   only after the operator approved PLAN: cancel the wrong
                      claim, claim by the SA staff number, keep the site's
                      processed state and note (run_reassignment rework mode).

Rows that are not such a conflict are reported for manual handling only.
"""
import argparse
import json
import re
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from bridge import Bridge, BrowserRejected
from claim import inspect_staff_claim, sa_claim_source
from claim_reassignment import claimed_note, run_reassignment
from core import SafetyStop, read_roster
from operation_log import OperationLog
from pairing_ui import copy_pairing_code
from zero_match import verify_sa

OWNER = '谭勋策'
WNO = re.compile(r'[0-9]{1,40}')


def name_key(value):
    """Order-insensitive name tokens: 'Wang, Wei' == 'Wei Wang'. Only a hint."""
    return tuple(sorted(p for p in re.split(r'[^0-9a-z一-鿿]+', str(value).lower()) if p))


def unique_record(roster, sa_id):
    found = [r for r in roster.records if r.sa_id == sa_id]
    if len(found) != 1 or found[0].owner != OWNER:
        raise SafetyStop(f'{sa_id} 在名单中不唯一或不属于{OWNER}。')
    return found[0]


def released(call, sa_id):
    # A same-ID read closes this record's claim drawer and detail, so the
    # next record never starts behind a stale drawer.
    call('search', {'sa_id': sa_id, 'close_detail': True})


def plan_one(bridge, record):
    call = bridge.call
    found = call('search', {'sa_id': record.sa_id, 'close_detail': True})
    row = found.get('row') or {}
    verify_sa(record, row)
    identity = found.get('claim_identity') or {}
    item_id = str(row.get('itemId', '')).lstrip(',')
    entry = {'sa_id': record.sa_id, 'list_row': record.row, 'staff_id': record.staff_id, 'item_id': item_id,
             'mark_status': row.get('markStatus'), 'remark': row.get('remark'), 'list_remark': record.remark,
             'claim_state': identity.get('state'), 'library_staff_id': identity.get('library_staff_id')}

    def manual(reason, **extra):
        return {**entry, **extra, 'action': 'manual', 'reason': reason}

    if found.get('closed_rework_protocol') != 1:
        return manual('扩展未更新到 0.4.61，未核对作者行')
    if not (record.done and not record.skipped and claimed_note(record.remark)):
        return manual('名单不是按“已认领”结案')
    if (row.get('markStatus') != '已处理' or row.get('updateUsername') != OWNER
            or not claimed_note(row.get('remark'))):
        return manual('网页不是本人按“已认领”结案')
    if str(row.get('matchCount')) != '1' or not WNO.fullmatch(item_id):
        return manual('网页不是唯一匹配')
    source, staff = sa_claim_source(found.get('comparison'))
    if staff != record.staff_id:
        return manual('SA 工号与名单不一致')
    base = {'sa_id': record.sa_id, 'expected': row, 'sa_text': source,
            'staff_id': staff, 'roster_staff_id': record.staff_id}
    if identity.get('state') == '已认领':
        proof = inspect_staff_claim(record, bridge, row, found['comparison'])
        return {**entry, 'action': 'already_correct', 'author_order': proof['order'], 'author': proof.get('author')}
    inspected = call('inspect_claim', base)
    prepared = inspected.get('prepared') or {}
    authors, person = prepared.get('authors') or [], prepared.get('person') or {}
    if person.get('wno') != staff or not person.get('id'):
        return manual('SA 工号未解析为唯一学者')
    if identity.get('state') != '认领不一致':
        names = {name_key(n) for n in person.get('names', [])}
        return manual('本库作者未认领，不属于撤销重认领', authors=[
            {k: a.get(k) for k in ('order', 'fullname', 'eligible', 'non_institution', 'scholarId')}
            for a in authors if name_key(a.get('fullname')) in names])
    previous = identity.get('library_staff_id')
    if not isinstance(previous, str) or not WNO.fullmatch(previous) or previous == staff:
        return manual('本库认领工号不明确')
    names = {name_key(n) for n in person.get('names', [])}
    candidates = [a for a in authors if a.get('eligible') and a.get('scholarId') and a['scholarId'] != person['id']]
    candidates.sort(key=lambda a: name_key(a.get('fullname')) not in names)
    rejected = []
    for author in candidates:
        authorization = {'version': 1, 'approved': True, 'sa_id': record.sa_id, 'item_id': item_id,
                         'staff_id': staff, 'previous_staff_id': previous, 'author_order': author['order']}
        try:
            answer = call('prepare_reassignment', {**base, 'owner': OWNER, 'authorization': authorization})
        except BrowserRejected as exc:
            if '已发出写入' in str(exc):
                raise
            rejected.append({'order': author['order'], 'reason': str(exc)[-120:]})
            continue
        evidence = answer.get('prepared') or {}
        if (answer.get('verified') is True and answer.get('stage') == 'original'
                and evidence.get('previous_person', {}).get('wno') == previous
                and evidence.get('person', {}).get('wno') == staff):
            return {**entry, 'action': 'reassign', 'author_order': author['order'], 'author': author['fullname'],
                    'previous_scholar_id': evidence['previous_person']['id'], 'scholar_id': evidence['person']['id']}
        rejected.append({'order': author['order'], 'reason': '准备回执不一致'})
    return manual('未找到原工号认领的作者行', rejected=rejected)


def make_plan(bridge, roster, ids, progress):
    rows = []
    for index, sa_id in enumerate(ids, 1):
        record = unique_record(roster, sa_id)
        try:
            entry = plan_one(bridge, record)
        except SafetyStop as exc:
            # Only read-only actions run here. A lost reply or a lost page
            # halts the batch; an explicit refusal is this row's finding.
            if '已发出写入' in str(exc) or not isinstance(exc, BrowserRejected) and (
                    '超时' in str(exc) or '未连接' in str(exc)):
                raise
            entry = {'sa_id': sa_id, 'list_row': record.row, 'staff_id': record.staff_id,
                     'action': 'manual', 'reason': '只读核对被拒绝：' + str(exc)[-200:]}
        try:
            released(bridge.call, sa_id)
        except BrowserRejected:
            pass
        rows.append(entry)
        progress(f'只读核对 {index}/{len(ids)}：{sa_id} → {entry["action"]}')
    return {'created': datetime.now().isoformat(timespec='seconds'), 'roster_sha256': roster.sha256, 'rows': rows}


def apply_plan(bridge, roster_path, plan, only, progress, stop):
    log = OperationLog(BASE / 'log.txt')
    roster = read_roster(roster_path)
    todo = [e for e in plan['rows'] if e['action'] == 'reassign' and (not only or e['sa_id'] in only)]
    results = []
    for index, entry in enumerate(todo, 1):
        authorization = {'version': 1, 'approved': True, 'sa_id': entry['sa_id'], 'item_id': entry['item_id'],
                         'staff_id': entry['staff_id'], 'previous_staff_id': entry['library_staff_id'],
                         'author_order': entry['author_order']}
        try:
            record = unique_record(roster, entry['sa_id'])
            roster = run_reassignment(BASE, roster, record, bridge, authorization, log.record, stop.is_set, rework=True)
            after = bridge.call('search', {'sa_id': entry['sa_id'], 'close_detail': True})
            identity = after.get('claim_identity') or {}
            if identity.get('state') != '已认领' or identity.get('library_staff_id') != entry['staff_id']:
                raise SafetyStop('纠正后详情未显示按 SA 工号已认领。')
            results.append({'sa_id': entry['sa_id'], 'status': 'done', 'remark': after['row'].get('remark'),
                            'library_staff_id': identity.get('library_staff_id')})
            progress(f'已纠正 {index}/{len(todo)}：{entry["sa_id"]}')
        except Exception as exc:
            results.append({'sa_id': entry['sa_id'], 'status': 'halted', 'reason': str(exc)})
            progress(f'第 {index} 条停止：{exc}')
            break
    return {'finished': datetime.now().isoformat(timespec='seconds'), 'results': results,
            'remaining': [e['sa_id'] for e in todo[len(results):]]}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--roster', required=True, type=Path)
    parser.add_argument('--ids', required=True, type=Path, help='JSON 列表，或含 sa_id 字段的对象列表')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--plan', type=Path, help='只读核对，写出纠正计划')
    group.add_argument('--apply', type=Path, help='执行已批准的纠正计划')
    parser.add_argument('--only', nargs='*', default=[], help='--apply 时只执行这些 SA ID')
    parser.add_argument('--out', type=Path, help='--apply 结果报告路径')
    parser.add_argument('--pair-file', type=Path, help='沿用已配对码的 JSON（含 token）')
    args = parser.parse_args()

    raw = json.loads(args.ids.read_text(encoding='utf-8'))
    ids = [item['sa_id'] if isinstance(item, dict) else item for item in raw]
    roster = read_roster(args.roster)
    for sa_id in ids:
        unique_record(roster, sa_id)
    plan = json.loads(args.apply.read_text(encoding='utf-8')) if args.apply else None
    if plan and plan.get('roster_sha256') != roster.sha256:
        raise SafetyStop('名单在核对后已变化，请重新生成计划。')

    bridge = Bridge()
    if args.pair_file:
        bridge.token = json.loads(args.pair_file.read_text(encoding='utf-8'))['token']
    stop = threading.Event()
    root = tk.Tk()
    root.title(('执行认领返工纠正' if plan else '认领返工 · 只读核对') + f' · {len(ids)} 条')
    root.geometry('560x190')
    status = tk.StringVar(value='等待现有 SA 比对页的扩展连接（扩展需为 0.4.61）')
    ttk.Label(root, textvariable=status, wraplength=530).pack(pady=12)
    code = tk.StringVar(root, bridge.token)
    ttk.Entry(root, width=58, state='readonly', textvariable=code).pack()
    ttk.Button(root, text='复制配对码', command=lambda: copy_pairing_code(root, bridge.token)).pack(pady=10)
    events, started = [], [False]

    def work():
        try:
            if plan:
                report = apply_plan(bridge, args.roster, plan, set(args.only), events.append, stop)
                path = args.out or args.apply.with_name(args.apply.stem + '-applied.json')
            else:
                report = make_plan(bridge, roster, ids, events.append)
                path = args.plan
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            events.append(f'完成，报告：{path}')
            print(json.dumps({'report': str(path)}, ensure_ascii=False), flush=True)
        except Exception as exc:
            events.append(f'已停止：{exc}')
            print(json.dumps({'error': str(exc)}, ensure_ascii=False), flush=True)

    def tick():
        if not started[0] and bridge.online:
            started[0] = True
            threading.Thread(target=work, daemon=True).start()
        while events:
            status.set(events.pop(0))
        root.after(400, tick)

    root.protocol('WM_DELETE_WINDOW', lambda: (stop.set(), root.destroy()))
    root.after(400, tick)
    try:
        root.mainloop()
    finally:
        bridge.close()


if __name__ == '__main__':
    main()
