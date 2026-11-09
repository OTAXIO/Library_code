"""Synthetic rows and transport only. Never touches the real list or site."""
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bridge import BrowserRejected
from core import Record, SafetyStop
from maintenance import claim_rework

WRITES = {'cancel_reassignment', 'submit_reassignment', 'submit_claim', 'complete', 'edit'}


def record(**changes):
    values = dict(row=7, owner='谭勋策', sa_id='sa1', title='Paper', doi='', wos='', staff_id='00001',
                  matches=1, item_ids='123', mark='已处理', reason='', query='1',
                  done=True, skipped=False, remark='已认领')
    return Record(**{**values, **changes})


class Site:
    def __init__(self, state='认领不一致', remark='DOI和WOSID SA未提交；已认领', protocol=1, refuse=()):
        self.state, self.protocol, self.refuse, self.calls = state, protocol, set(refuse), []
        self.row = dict(saLzkId='sa1', itemId='123', matchCount=1, gh='00001', titleValue='Paper',
                        doiValue='', wosValue='', markStatus='已处理', remark=remark, updateUsername='谭勋策')

    def call(self, action, payload, timeout=90):
        self.calls.append(action)
        if action in WRITES:
            raise AssertionError('read-only plan sent ' + action)
        found = dict(row=dict(self.row), closed_rework_protocol=self.protocol,
                     claim_identity=dict(state=self.state, sa_staff_id='00001',
                                         library_staff_id='00002' if self.state == '认领不一致' else ''),
                     comparison=[dict(label='认领状态', sa='Same Name(00001)', library=self.state)])
        if action == 'search':
            return found
        if action == 'inspect_claim':
            return {**found, 'verified': True, 'claimed': False, 'prepared': dict(
                person=dict(id='new', wno='00001', names=['Same Name', '同名']),
                authors=[dict(order=1, fullname='Other', scholarId='x', eligible=True),
                         dict(order=3, fullname='Name, Same', scholarId='old', eligible=True),
                         dict(order=4, fullname='Unclaimed', scholarId='', eligible=True)])}
        if action == 'prepare_reassignment':
            order = payload['authorization']['author_order']
            if order != 3 or order in self.refuse:
                raise BrowserRejected(action, '【已暂停】授权的旧工号与这行现有认领不一致，未撤销任何关系')
            return {**found, 'verified': True, 'stage': 'original', 'prepared': dict(
                person=dict(id='new', wno='00001'), previous_person=dict(id='old', wno='00002'))}
        raise AssertionError(action)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.verify = patch.object(claim_rework, 'verify_sa', lambda *_: None)
        self.verify.start()
        self.addCleanup(self.verify.stop)

    def test_homonym_claim_is_located_by_verified_prepare_only(self):
        site = Site()
        entry = claim_rework.plan_one(site, record())
        self.assertEqual((entry['action'], entry['author_order'], entry['library_staff_id']), ('reassign', 3, '00002'))
        # The name-matching author is tried first; the unclaimed one never is.
        self.assertEqual(site.calls, ['search', 'inspect_claim', 'prepare_reassignment'])

    def test_wrong_candidates_are_refused_without_writes(self):
        site = Site(refuse={3})
        entry = claim_rework.plan_one(site, record())
        self.assertEqual(entry['action'], 'manual')
        self.assertEqual([r['order'] for r in entry['rejected']], [3, 1])

    def test_unclaimed_or_other_notes_are_manual(self):
        for site, rec in ((Site(state='未认领'), record()), (Site(remark='非交大作者'), record()),
                          (Site(), record(remark='非交大作者')), (Site(protocol=None), record())):
            with self.subTest(remark=site.row['remark'], state=site.state):
                entry = claim_rework.plan_one(site, rec)
                self.assertEqual(entry['action'], 'manual')
                self.assertNotIn('prepare_reassignment', site.calls)

    def test_refused_read_is_reported_and_batch_continues(self):
        site = Site()
        def call(action, payload, timeout=90):
            if action == 'search' and len(site.calls) == 0:
                site.calls.append(action)
                raise BrowserRejected(action, '【已暂停】本库已认领人员编号与 SA 不一致，不能结案')
            return Site.call(site, action, payload, timeout)
        site.call = call
        roster = SimpleNamespace(records=[record()], sha256='h')
        plan = claim_rework.make_plan(site, roster, ['sa1'], lambda *_: None)
        self.assertEqual(plan['rows'][0]['action'], 'manual')
        self.assertIn('不能结案', plan['rows'][0]['reason'])

    def test_lost_reply_halts_the_plan(self):
        site = Site()
        def call(action, payload, timeout=90):
            raise SafetyStop('命令超时（扩展没有取得命令），结果不明。')
        site.call = call
        roster = SimpleNamespace(records=[record()], sha256='h')
        with self.assertRaises(SafetyStop):
            claim_rework.make_plan(site, roster, ['sa1'], lambda *_: None)


class ApplyTests(unittest.TestCase):
    def test_apply_runs_only_planned_rework_and_stops_on_first_failure(self):
        rows = [record(sa_id=f'sa{i}') for i in range(3)]
        roster = SimpleNamespace(records=rows, sha256='h')
        plan = {'rows': [dict(sa_id=f'sa{i}', action='reassign' if i != 1 else 'manual', item_id='123',
                              staff_id='00001', library_staff_id='00002', author_order=3) for i in range(3)]
                + [dict(sa_id='sa9', action='reassign', item_id='1', staff_id='1', library_staff_id='2', author_order=1)]}
        seen = []
        def fake(base, current, rec, bridge, authorization, audit, cancel, rework):
            seen.append((rec.sa_id, rework, authorization['previous_staff_id'], authorization['approved']))
            return current
        site = SimpleNamespace(call=lambda action, payload, timeout=90: dict(
            row=dict(remark='已认领'), claim_identity=dict(state='已认领', library_staff_id='00001')))
        with patch.object(claim_rework, 'read_roster', return_value=roster), \
                patch.object(claim_rework, 'run_reassignment', side_effect=fake), \
                patch.object(claim_rework, 'OperationLog'):
            report = claim_rework.apply_plan(site, 'x.xlsx', plan, {'sa0', 'sa2', 'sa9'}, lambda *_: None,
                                             threading.Event())
        self.assertEqual(seen, [('sa0', True, '00002', True), ('sa2', True, '00002', True)])
        self.assertEqual([r['status'] for r in report['results']], ['done', 'done', 'halted'])


if __name__ == '__main__':
    unittest.main()
