"""Synthetic offline regressions for the third-round field findings."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import load_workbook, Workbook
from bridge import BrowserRejected
from core import SafetyStop
from cross_review import CrossReview, accession_key, assess, doi_key, title_key
import feedback_write
from test_cross_review import clean_detail, set_field, make_feedback, HEADERS


class IdentityV3Tests(unittest.TestCase):
    def test_comparison_keys_and_empty_placeholders(self):
        self.assertEqual(title_key('ＡＢＣ_《汉字》123—'), 'abc汉字123')
        self.assertEqual(doi_key(' ＤＯＩ１０．１２３４／Ａ .;,'), '10.1234/a')
        self.assertEqual(doi_key('invalid'), '')
        for value in ('', '—', '-', '无', ' NULL '):
            self.assertEqual(accession_key(value), '')
        self.assertEqual(accession_key(' rc: 123_S24 '), 'RC:123_S24')

    def test_shared_doi_overrides_title_and_journal_differences(self):
        for left, right, jl, jr in (
            ('Photoredox Catalyzed Picoc-SPPS', 'Photoredox-Catalyzed Picoc-SPPS', 'J. Am. Chem. Soc.', 'JOURNAL OF THE AMERICAN CHEMICAL SOCIETY'),
            ('Photocatalytic C–X Bond', 'Photocatalytic C-X Bond.', 'J.', 'Journal'),
            ('中文题名', 'English title', '核动力工程', 'Hedongli Gongcheng/Nuclear Power Engineering'),
        ):
            with self.subTest(left=left):
                d = clean_detail()
                set_field(d, '题名', left, right)
                set_field(d, '期刊', jl, jr)
                set_field(d, 'DOI', '10.1234/example', '10.1234/example')
                self.assertEqual(assess(d), ([], []))

    def test_title_punctuation(self):
        for left, right in (
            ('Jahn‐Teller', 'Jahn-Teller'), ('What’s', "What's"),
            ('赤字：生成', '赤字: 生成'),
            ('Zn-2Cu-0.8Li- (0.2-0.8)Mg', 'Zn-2Cu-0.8Li-(0.2-0.8)Mg'),
            ('融合科学精神与实践能力：统计应用课程', '融合科学精神与实践能力——《统计应用》课程'),
            ('ＡＢＣ_123', 'abc 123'), ('C–X Bond.', 'C-X Bond'),
        ):
            with self.subTest(left=left):
                d = clean_detail()
                set_field(d, 'DOI', '', '')
                set_field(d, '题名', left, right)
                self.assertEqual(assess(d), ([], []))

    def test_identifiers_and_missing_values_do_not_add_noise(self):
        for label, left, right in (
            ('WOS记录号', 'INSPEC:29631410', 'inspec: 29631410'),
            ('WOS记录号', 'RC:165755816_S24', 'rc:165755816_S24'),
            ('WOS记录号', 'PPRN:91241766', 'PPRN:91241766'),
            ('WOS记录号', '000123456789012', 'WOS:000123456789012'),
            ('DOI', 'DOI10.1016/j.engappai.2026.113818', '10.1016/j.engappai.2026.113818'),
            ('DOI', 'doi.org/10.1016/j.marpolbul.2026.119682', '10.1016/j.marpolbul.2026.119682.;,'),
        ):
            with self.subTest(left=left):
                d = clean_detail()
                set_field(d, '题名', 'A', 'B')
                set_field(d, label, left, right)
                self.assertEqual(assess(d), ([], []))
        for label in ('DOI', 'WOS记录号'):
            d = clean_detail()
            set_field(d, label, 'invalid', '')
            self.assertEqual(assess(d), ([], []))

    def test_identity_conflicts_and_title_only_doi_exception(self):
        d = clean_detail()
        set_field(d, 'DOI', '10.1234/a', '10.1234/b')
        self.assertEqual(assess(d), ([], ['题名相同但 DOI 不同，待核版本']))
        set_field(d, 'WOS记录号', 'RC:123', 'RC:123')
        self.assertEqual(assess(d), ([], []))
        set_field(d, 'WOS记录号', '', '')
        set_field(d, '题名', 'A', 'B')
        self.assertEqual(assess(d), (['两边不是同一篇论文'], []))
        set_field(d, 'DOI', '', '')
        self.assertEqual(assess(d), ([], ['题名不同且无共同 DOI/WOS，待核是否同一篇']))
        set_field(d, '期刊', 'A journal', 'B journal')
        self.assertEqual(assess(d), (['两边不是同一篇论文'], []))

    def test_year_tolerance(self):
        for left, right in ((2026, 2025), (2025, 2026), (2026, 2018), (2026, 2016), (2026, 2023)):
            with self.subTest(right=right):
                d = clean_detail()
                set_field(d, '年份', str(left), str(right))
                gap = abs(left-right)
                self.assertEqual(assess(d), ([], [f'发表年份相差 {gap} 年'] if gap >= 2 else []))


class OfflineV3Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.review = CrossReview(self.temp.name, '复核对象')
        d = clean_detail(remark='')
        self.entry = dict(status='有问题', rule=2, comparison=d['comparison'],
                          claim_identity=d['claim_identity'], remark='')
        self.entry['原文证据定位'] = {key:d['row'].get(key) for key in
            ('saLzkId', 'itemId', 'titleValue', 'doiValue', 'wosValue', 'gh')}
        self.review.data.update(ready=True, rows={'001':self.entry})
        self.bridge = SimpleNamespace(call=lambda *a: self.fail('unexpected browser command'))

    def run_review(self, **kw):
        self.review.run(self.bridge, Event(), lambda _:None, **kw)

    def auto_row(self):
        self.entry.update(confirmed='旧问题', auto_confirmed=True, reviewer='校对员', feedback_synced=True)
        make_feedback(self.review.feedback_path, [('other', '保留', '别人', '别人校对员'),
                                                 ('001', '旧问题', '复核对象', '校对员'),
                                                 ('last', '末行', '别人', '别人校对员')])

    def test_offline_upgrade_and_progress(self):
        updates=[]
        self.review.run(self.bridge, Event(), updates.append)
        self.assertEqual(self.entry['status'], '通过')
        self.assertEqual(self.entry['rule'], 3)
        self.assertEqual(updates, ['按新规则重判 1/1 001'])

    def test_missing_remark_or_locator_or_rejected_requires_browser(self):
        for change in ('remark', 'gh', 'rejected', 'comparison'):
            with self.subTest(change=change):
                entry=copy.deepcopy(self.entry)
                if change == 'gh': entry['原文证据定位'].pop('gh')
                elif change == 'rejected': entry['rejected']=True
                else: entry.pop(change)
                self.review.data['rows']['001']=entry
                with patch.object(self.review, '_inspect', return_value=([], [])) as inspect:
                    self.run_review()
                inspect.assert_called_once()

    def test_offline_uses_verified_original(self):
        self.review.original_path('001').parent.mkdir(parents=True)
        original=dict(self.entry['原文证据定位'], fulltext_verified=True, source='synthetic',
                      staff_id='001', author_count=2, author_index=2, first_authors=[1],
                      corresponding=[1], mark_source='全文')
        self.review.original_path('001').write_text(json.dumps(original), encoding='utf-8')
        self.run_review()
        self.assertEqual(self.entry['issues'], ['误标第一作者', '误标通讯作者'])

    def test_auto_row_removed_other_rows_sheets_preserved_and_fields_cleared(self):
        self.auto_row()
        self.run_review()
        book=load_workbook(self.review.feedback_path)
        try:
            self.assertEqual(list(book.worksheets[0].values), [HEADERS,
                ('other', '保留', '别人', '别人校对员'), ('last', '末行', '别人', '别人校对员')])
            self.assertEqual(list(book['保留页'].values), [('=1+1', '保留文字')])
        finally: book.close()
        for key in ('confirmed', 'auto_confirmed', 'reviewer', 'feedback_synced', 'feedback_remove_pending'):
            self.assertNotIn(key, self.entry)
        self.assertEqual(self.entry['status'], '通过')

    def test_modified_row_preserved_and_warned(self):
        self.auto_row()
        book=load_workbook(self.review.feedback_path)
        book.worksheets[0]['B3']='人工修改'
        book.save(self.review.feedback_path)
        book.close()
        before=self.review.feedback_path.read_bytes()
        self.run_review()
        self.assertEqual(self.review.feedback_path.read_bytes(), before)
        self.assertIn('反馈表里该行已被人工修改，请人工处理', self.entry['pending'])
        self.assertNotIn('confirmed', self.entry)

    def test_locked_removal_retries_on_load_sync(self):
        self.auto_row()
        lock=self.review.feedback_path.with_name('~$复核反馈.xlsx')
        lock.touch()
        with self.assertRaisesRegex(SafetyStop, 'Excel'):
            self.run_review()
        self.assertTrue(self.entry['auto_confirmed'])
        self.assertTrue(self.entry['feedback_remove_pending'])
        lock.unlink()
        resumed=CrossReview(self.temp.name, '复核对象')
        resumed.sync_pending()
        self.assertNotIn('auto_confirmed', resumed.data['rows']['001'])
        book=load_workbook(self.review.feedback_path)
        try: self.assertNotIn('001', [r[0] for r in book.worksheets[0].values])
        finally: book.close()

    def test_failed_removal_does_not_interrupt_other_entries_and_next_run_retries(self):
        self.auto_row()
        second = copy.deepcopy(self.entry)
        for key in ('confirmed', 'auto_confirmed', 'reviewer', 'feedback_synced'):
            second.pop(key)
        second['原文证据定位']['saLzkId'] = '002'
        self.review.data['rows']['002'] = second
        lock = self.review.feedback_path.with_name('~$复核反馈.xlsx')
        lock.touch()
        with self.assertRaisesRegex(SafetyStop, 'Excel'):
            self.run_review()
        self.assertEqual(second['status'], '通过')
        lock.unlink()
        self.run_review()
        self.assertNotIn('confirmed', self.entry)

    def test_completed_current_rule_does_not_issue_browser_commands(self):
        self.entry.update(rule=3, status='通过', issues=[], pending=[])
        before = copy.deepcopy(self.entry)
        self.run_review()
        self.assertEqual(self.entry, before)

    def test_remaining_issue_updates_auto_note_without_new_reviewer(self):
        self.auto_row()
        self.entry['claim_identity']['library_staff_id']='002'
        self.run_review()
        self.assertEqual(self.entry['confirmed'], '认领工号不一致')
        self.assertTrue(self.entry['feedback_synced'])
        book=load_workbook(self.review.feedback_path)
        try: self.assertEqual(book.worksheets[0]['B3'].value, '认领工号不一致')
        finally: book.close()

    def test_manual_confirmation_untouched(self):
        self.auto_row()
        self.entry.pop('auto_confirmed')
        before=copy.deepcopy(self.entry)
        contents=self.review.feedback_path.read_bytes()
        self.run_review(reviewer='新校对员')
        self.assertEqual(self.entry, before)
        self.assertEqual(self.review.feedback_path.read_bytes(), contents)

    def test_ordinary_browser_rejection_does_not_retract_an_auto_row(self):
        self.auto_row()
        self.entry.pop('remark')
        before = self.review.feedback_path.read_bytes()
        def reject(action, payload):
            raise BrowserRejected(action, '请登录')
        self.bridge.call = reject
        self.run_review()
        self.assertTrue(self.entry['rejected'])
        self.assertTrue(self.entry['auto_confirmed'])
        self.assertNotIn('feedback_remove_pending', self.entry)
        self.assertEqual(self.review.feedback_path.read_bytes(), before)


class RemoveFeedbackTests(unittest.TestCase):
    def test_each_modified_column_is_recognizable_and_preserved(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / '复核反馈.xlsx'
            for index in (1, 2, 3):
                values = ['001', 'note', 'owner', 'reviewer']
                values[index] = 'human edit'
                make_feedback(path, [values])
                before = path.read_bytes()
                with self.assertRaises(feedback_write.FeedbackModified):
                    feedback_write.remove_feedback(path, Path(temp)/'backup', '001', 'note', 'owner', 'reviewer')
                self.assertEqual(path.read_bytes(), before)

    def test_lock_backup_replace_and_external_change_guards(self):
        import hashlib
        import os
        for failure in ('excel', 'assistant', 'backup', 'replace', 'external'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                path = Path(temp) / '复核反馈.xlsx'
                backup_dir = Path(temp) / 'backup'
                make_feedback(path, [('001', 'note', 'owner', 'reviewer')])
                before = path.read_bytes()
                if failure in ('excel', 'assistant'):
                    path.with_name('~$' + path.name if failure == 'excel' else '.' + path.name + '.assistant.lock').touch()
                if failure == 'backup':
                    backup_dir.mkdir()
                    (backup_dir / ('复核反馈-' + hashlib.sha256(before).hexdigest() + '.xlsx')).write_bytes(b'bad')
                save, replace = Workbook.save, os.replace
                def external(book, target):
                    save(book, target)
                    path.write_bytes(b'external change')
                def fail_replace(source, target):
                    if Path(target) == path:
                        raise PermissionError('sharing violation')
                    return replace(source, target)
                with patch.object(Workbook, 'save', external if failure == 'external' else save), patch(
                        'feedback_write.os.replace', side_effect=fail_replace if failure == 'replace' else replace):
                    with self.assertRaises(SafetyStop):
                        feedback_write.remove_feedback(path, backup_dir, '001', 'note', 'owner', 'reviewer')
                self.assertEqual(path.read_bytes(), b'external change' if failure == 'external' else before)
                self.assertEqual(list(Path(temp).glob('.feedback-*.xlsx')), [])

    def test_safe_validation_and_idempotency(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'复核反馈.xlsx'
            for rows, headers in (([('001', 'note', 'owner', 'reviewer')]*2, HEADERS),
                                  ([], ('bad',)+HEADERS[1:])):
                make_feedback(path, rows, headers)
                before=path.read_bytes()
                with self.assertRaises(SafetyStop):
                    feedback_write.remove_feedback(path, Path(temp)/'backup', '001', 'note', 'owner', 'reviewer')
                self.assertEqual(path.read_bytes(), before)
            make_feedback(path, [('001', 'note', 'owner', 'reviewer')])
            for _ in range(2):
                feedback_write.remove_feedback(path, Path(temp)/'backup', '001', 'note', 'owner', 'reviewer')
            book=load_workbook(path)
            try: self.assertEqual(list(book.worksheets[0].values), [HEADERS])
            finally: book.close()

    def test_readback_corruption_leaves_original(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/'复核反馈.xlsx'
            make_feedback(path, [('001', 'note', 'owner', 'reviewer'), ('other', '保留', 'o', 'r')])
            before=path.read_bytes()
            save=Workbook.save
            def corrupt(book, target):
                book.worksheets[0]['B2']='corrupted'
                save(book, target)
            with patch.object(Workbook, 'save', corrupt), self.assertRaisesRegex(SafetyStop, '校验'):
                feedback_write.remove_feedback(path, Path(temp)/'backup', '001', 'note', 'owner', 'reviewer')
            self.assertEqual(path.read_bytes(), before)


class TransientV3Tests(unittest.TestCase):
    def test_search_timeout_keeps_partial_evidence_out_of_interrupted_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            review = CrossReview(temp, '复核对象')
            entry = dict(status='有问题', rule=2, remark='saved remark', issues=['旧问题'])
            review.data.update(ready=True, rows={'001': entry})
            review.save()
            before = copy.deepcopy(entry)
            calls = []
            def call(action, payload):
                calls.append(action)
                if action == 'status':
                    return {'row': clean_detail(remark='new remark')['row']}
                raise BrowserRejected(action, '查询记录超时')
            with patch('cross_review.TRANSIENT_WAIT_SECONDS', 0), self.assertRaises(SafetyStop):
                review.run(SimpleNamespace(call=call), Event(), lambda _: None)
            self.assertEqual(entry, before)
            self.assertEqual(CrossReview(temp, '复核对象').data['rows']['001'], before)
            self.assertEqual(calls, ['status', 'search'] * 5)

    def test_retries_success_exhaustion_and_stop(self):
        for mode in ('success', 'exhausted', 'stop'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temp:
                review=CrossReview(temp, '复核对象')
                entry=dict(status='待核', rule=2, pending=['旧待核'])
                review.data.update(ready=True, rows={'001':entry})
                before=copy.deepcopy(entry)
                review.save()
                stop=Event()
                calls=[]
                def call(action, payload):
                    calls.append(action)
                    if mode != 'success' or len(calls) <= 2:
                        raise BrowserRejected(action, '列表仍在加载' if len(calls)%2 else '查询记录超时')
                    d=clean_detail()
                    return {'row':d['row']} if action == 'status' else d
                def wait(seconds):
                    stop.set()
                    return True
                with patch('cross_review.TRANSIENT_WAIT_SECONDS', 0, create=True):
                    if mode == 'stop':
                        with patch.object(stop, 'wait', side_effect=wait):
                            review.run(SimpleNamespace(call=call), stop, lambda _:None)
                    elif mode == 'exhausted':
                        with self.assertRaisesRegex(SafetyStop, '网页持续卡在加载中，请刷新 SA 比对页后重新开始，进度已保存'):
                            review.run(SimpleNamespace(call=call), stop, lambda _:None)
                    else:
                        review.run(SimpleNamespace(call=call), stop, lambda _:None)
                if mode == 'success':
                    self.assertEqual(entry['status'], '通过')
                    self.assertNotIn('rejected', entry)
                    self.assertEqual(calls, ['status']*3+['search'])
                else:
                    self.assertEqual(entry, before)
                    self.assertEqual(CrossReview(temp, '复核对象').data['rows']['001'], before)
                    self.assertEqual(len(calls), 5 if mode == 'exhausted' else 1)
