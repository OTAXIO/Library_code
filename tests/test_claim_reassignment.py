"""Synthetic staff IDs and transport only. Never touches the real list or site."""
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from claim_reassignment import run_reassignment, validate_authorization
from core import Record, SafetyStop
from operation_log import OperationLog
from bridge import BrowserRejected


AUTH = dict(version=1, approved=True, sa_id='demo', item_id='123', staff_id='00001',
            previous_staff_id='00002', author_order=2)
REC = Record(2, '谭勋策', 'demo', 'Paper', '', '', '00001', 0, '', '待处理', '', '2',
             skipped=True, remark='后台匹配已变化')


class Browser:
    def __init__(self):
        self.stage = 'original'
        self.row = dict(saLzkId='demo', itemId='123', matchCount=1, gh='00001',
            titleValue='Paper', doiValue='', wosValue='', markStatus='待处理', remark='')
        self.calls = []
        self.uncertain = None
        self.no_write = False
        self.wrong_staff = False

    def search(self):
        return dict(row=copy.deepcopy(self.row), staff_claim_protocol=1,
            comparison=[dict(label='认领状态', sa='Same Name(00001)',
                library='已认领' if self.stage == 'claimed' else '认领不一致')])

    def proof(self):
        return dict(row=copy.deepcopy(self.row), verified=True, stage=self.stage,
            staff_id='1' if self.wrong_staff else '00001', scholar_id='new-scholar',
            order=2, author='Same Name', claimed=self.stage == 'claimed')

    def authors(self):
        return [dict(index=0,order=1,fullname='Other',scholarId='unchanged',relations=[],eligible=True),
                dict(index=1,order=2,fullname='Same Name',relations=[],eligible=True,
                    scholarId='new-scholar' if self.stage=='claimed' else 'old-scholar' if self.stage=='original' else '')]

    def call(self, action, payload):
        self.calls.append(action)
        if action == 'search':
            return self.search()
        if action == 'prepare_reassignment':
            return {**self.search(), 'verified': True, 'stage': 'original', 'prepared': dict(
                authorization=copy.deepcopy(AUTH), staff_id='00001', item_id='123',
                person=dict(id='new-scholar', wno='00001'),
                previous_person=dict(id='old-scholar', wno='00002'),authors=self.authors())}
        if action == 'inspect_claim':
            return {**self.search(),**self.proof(),'prepared':{'authors':self.authors()}}
        if action == 'inspect_reassignment':
            return self.proof()
        if action == 'cancel_reassignment':
            if not self.no_write: self.stage = 'cancelled'
        elif action == 'submit_reassignment':
            if not self.no_write: self.stage = 'claimed'
        elif action == 'complete':
            self.row.update(markStatus='已处理', remark='已认领')
        else:
            raise AssertionError(action)
        if self.uncertain == action:
            self.uncertain = None
            raise SafetyStop('synthetic ACK lost')
        return self.proof()


class ReassignmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.roster = SimpleNamespace(records=[REC], assert_unchanged=Mock())
        self.browser = Browser()
        self.updated = SimpleNamespace(records=[replace(REC, done=True, skipped=False, remark='已认领')])
        self.writer = patch('claim_reassignment.reconcile_processed', return_value=SimpleNamespace(roster=self.updated))
        self.saved = self.writer.start()

    def tearDown(self):
        self.writer.stop()
        self.temp.cleanup()

    def run_job(self, **kwargs):
        return run_reassignment(self.base, self.roster, REC, self.browser, copy.deepcopy(AUTH), **kwargs)

    def state(self):
        return json.loads(next((self.base/'runtime/claim-reassignments').glob('*.json')).read_text(encoding='utf-8'))

    def test_complete_by_staff_without_changing_original_zero_match_facts(self):
        self.assertIs(self.run_job(), self.updated)
        self.assertEqual(self.browser.calls.count('cancel_reassignment'), 1)
        self.assertEqual(self.browser.calls.count('submit_reassignment'), 1)
        self.assertEqual(self.browser.calls.count('complete'), 1)
        self.saved.assert_called_once_with(self.roster, REC, self.browser.row)
        self.assertEqual(REC.matches, 0)
        self.assertEqual(REC.item_ids, '')
        self.assertEqual(self.state()['phase'], 'done')

    def test_leading_zero_owner_order_and_explicit_authorization_are_required(self):
        for changes in ({'staff_id':'1'}, {'staff_id':1}, {'previous_staff_id':'00001'},
                        {'approved':False}, {'author_order':True}, {'version':True}, {'sa_id':'another'}):
            with self.subTest(changes=changes), self.assertRaises(SafetyStop):
                validate_authorization(REC, {**AUTH, **changes})
        with self.assertRaises(SafetyStop):
            validate_authorization(replace(REC, owner='other'), AUTH)

    def test_lost_cancel_ack_resumes_by_reading_without_second_cancel(self):
        self.browser.uncertain = 'cancel_reassignment'
        with self.assertRaisesRegex(SafetyStop, 'ACK lost'): self.run_job()
        self.assertEqual(self.state()['phase'], 'cancel_intent')
        self.saved.assert_not_called()
        self.run_job()
        self.assertEqual(self.browser.calls.count('cancel_reassignment'), 1)
        self.assertEqual(self.browser.calls.count('submit_reassignment'), 1)

    def test_unknown_cancel_still_original_is_not_repeated(self):
        self.browser.uncertain = 'cancel_reassignment'; self.browser.no_write = True
        with self.assertRaises(SafetyStop): self.run_job()
        with self.assertRaisesRegex(SafetyStop, '禁止自动重复提交'): self.run_job()
        self.assertEqual(self.browser.calls.count('cancel_reassignment'), 1)
        self.assertNotIn('submit_reassignment', self.browser.calls)
        self.saved.assert_not_called()

    def test_lost_claim_ack_does_not_repeat_claim(self):
        self.browser.uncertain = 'submit_reassignment'
        with self.assertRaises(SafetyStop): self.run_job()
        self.assertEqual(self.state()['phase'], 'claim_intent')
        self.run_job()
        self.assertEqual(self.browser.calls.count('submit_reassignment'), 1)

    def test_lost_closure_ack_reads_site_and_writes_list_without_second_closure(self):
        self.browser.uncertain = 'complete'
        with self.assertRaises(SafetyStop): self.run_job()
        self.assertEqual(self.state()['phase'], 'complete_intent')
        self.run_job()
        self.assertEqual(self.browser.calls.count('complete'), 1)
        self.assertEqual(self.browser.calls.count('submit_reassignment'), 1)

    def test_changed_staff_stops_without_marking_list_complete(self):
        self.browser.wrong_staff = True
        with self.assertRaises(SafetyStop): self.run_job()
        self.saved.assert_not_called()
        self.assertNotIn('complete', self.browser.calls)

    def test_done_record_is_not_replayed(self):
        done = replace(REC, done=True)
        roster = SimpleNamespace(records=[done], assert_unchanged=Mock())
        self.assertIs(run_reassignment(self.base, roster, done, self.browser, AUTH), roster)
        self.assertEqual(self.browser.calls, [])

    def test_unknown_existing_note_is_retained(self):
        self.browser.row['remark'] = 'colleague note'
        with self.assertRaisesRegex(SafetyStop, '其他备注'): self.run_job()
        self.assertNotIn('complete', self.browser.calls)
        self.saved.assert_not_called()

    def test_pause_does_not_submit_any_action(self):
        with self.assertRaisesRegex(SafetyStop, '暂停'): self.run_job(cancel=lambda: True)
        self.assertEqual(self.browser.calls, [])

    def test_real_operation_log_accepts_every_stage_and_keeps_bounded_operations(self):
        log = OperationLog(self.base/'log.txt')
        self.run_job(audit=log.record)
        operations = log.read()
        self.assertEqual(len(operations), 5)
        self.assertTrue(all(op['result'] == '已执行' for op in operations))

    def test_logging_failure_before_first_submission_leaves_prepared_not_unknown_intent(self):
        def rejected(*_): raise ValueError('synthetic rejected log')
        with self.assertRaises(ValueError): self.run_job(audit=rejected)
        self.assertEqual(self.state()['phase'], 'prepared')
        self.assertNotIn('cancel_reassignment', self.browser.calls)

    def test_explicit_before_write_rejection_restores_prepared_but_never_retries_in_same_run(self):
        original=self.browser.call
        def call(action,payload):
            if action=='cancel_reassignment':
                self.browser.calls.append(action)
                raise BrowserRejected(action,'[扩展 test] 【已暂停】目标快照变化')
            return original(action,payload)
        self.browser.call=call
        with self.assertRaises(BrowserRejected): self.run_job()
        self.assertEqual(self.state()['phase'],'prepared')
        self.assertEqual(self.browser.calls.count('cancel_reassignment'),1)
        self.assertNotIn('submit_reassignment',self.browser.calls)

    def test_rejected_after_write_keeps_unknown_intent(self):
        original=self.browser.call
        def call(action,payload):
            if action=='cancel_reassignment':
                raise BrowserRejected(action,'【已发出写入，禁止自动重试】回执未确认')
            return original(action,payload)
        self.browser.call=call
        with self.assertRaises(BrowserRejected): self.run_job()
        self.assertEqual(self.state()['phase'],'cancel_intent')

    def test_manual_completion_during_pause_only_reads_staff_and_syncs_list(self):
        def pause_before_write(*_): raise ValueError('pause')
        with self.assertRaises(ValueError): self.run_job(audit=pause_before_write)
        self.browser.stage='claimed';self.browser.row.update(markStatus='已处理',remark='已认领')
        result=self.run_job()
        self.assertIs(result,self.updated)
        self.assertEqual(self.state()['completion_origin'],'verified_existing_claim')
        for action in ('cancel_reassignment','submit_reassignment','complete'):
            self.assertNotIn(action,self.browser.calls)
        self.assertIn('inspect_claim',self.browser.calls)

    def test_manual_completion_with_other_staff_never_syncs_list(self):
        self.browser.stage='claimed';self.browser.row.update(markStatus='已处理',remark='已认领')
        self.browser.wrong_staff=True
        with self.assertRaises(SafetyStop): self.run_job()
        self.saved.assert_not_called()
