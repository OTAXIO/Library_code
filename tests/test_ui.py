"""Manual desk tests with native widgets and synthetic files; no website calls."""
import tempfile
import tkinter as tk
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch, Mock
from openpyxl import load_workbook

from app import App, BASE, GREEN, YELLOW
from core import Journal, SafetyStop, file_hash, read_roster
from remarks import CLAIMED
from notices import messages as quiet_messages
from tests.test_roster_write import make_roster


class UITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'list.xlsx'
        make_roster(self.path)
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, Journal(Path(self.tmp.name) / 'local.db'), auto_load=False)
        self.app.loaded(read_roster(self.path))
        self.app.owner.set('测试员')
        self.app.select_owner()
        self.root.update_idletasks()

    def tearDown(self):
        self.app.set_busy(False)
        # Drain queued ttk theme notifications before destroying this test root.
        self.root.update_idletasks()
        self.app.close()
        self.tmp.cleanup()

    def select_first(self):
        self.app.next_record()
        self.root.update_idletasks()

    def select_tan_first(self):
        book = load_workbook(self.path)
        book.active['B2'] = '谭勋策'
        book.save(self.path)
        book.close()
        self.app.loaded(read_roster(self.path))
        self.app.owner.set('谭勋策')
        self.app.select_owner()
        self.select_first()

    def sync_run(self, job, callback, status):
        self.app.set_busy(True)
        try:
            result = job()
        except Exception:
            self.app.set_busy(False)
            raise
        self.app.set_busy(False)
        callback(result)

    def complete_first(self):
        self.select_first()
        self.app.reviewed.set(True)
        with patch('app.messagebox.askyesno', return_value=True), patch.object(self.app, 'run', side_effect=self.sync_run):
            self.app.confirm_manual_done()

    def test_numeric_done_hidden_and_color(self):
        self.assertNotIn('demo-002', self.app.tree.get_children())
        self.assertIn('demo-003', self.app.tree.get_children())
        self.assertEqual(len(self.app.records), 4)
        self.assertEqual(self.app.done_count.get(), '已完成 1')
        self.assertEqual(self.app.tree.tag_configure('pending')['background'], YELLOW)

    def test_warning_is_quiet_non_modal_and_can_be_updated(self):
        self.root.deiconify()
        self.root.update()
        with patch('notices.native_messages.showwarning') as native:
            quiet_messages.showwarning('测试暂停', '第一次', parent=self.root)
            window = self.root._sa_notice
            self.assertIsNone(window.grab_current())
            quiet_messages.showwarning('测试暂停', '第二次', parent=self.root)
            self.assertIs(self.root._sa_notice, window)
            self.assertEqual(window.body.get('1.0', 'end-1c'), '第二次')
            native.assert_not_called()
        self.root.update_idletasks()
        self.assertGreater(window.winfo_height(), 200)
        window.destroy()

    def test_warning_can_reopen_after_close_without_affecting_approval(self):
        self.select_first()
        quiet_messages.showwarning('暂停', '测试', parent=self.root)
        self.root._sa_notice.destroy()
        quiet_messages.showwarning('暂停', '再次测试', parent=self.root)
        self.assertTrue(self.root._sa_notice.winfo_exists())
        self.assertFalse(self.app.reviewed.get())
        self.assertIsNone(self.root.grab_current())

    def test_owner_scope_and_legacy_log_not_authoritative(self):
        record = self.app.records[0]
        self.app.journal.save(record, '已完成')
        self.app.roster.records[-1] = replace(self.app.roster.records[-1], owner='另一位')
        self.app.select_owner()
        self.assertIn(record.sa_id, self.app.tree.get_children())
        self.assertNotIn('demo-005', self.app.tree.get_children())

    def test_layout_both_tabs_topmost_and_no_background_actions(self):
        self.root.deiconify()
        self.root.update()
        self.assertTrue(self.root.attributes('-topmost'))
        self.assertEqual([self.app.tabs.tab(tab, 'text') for tab in self.app.tabs.tabs()], ['人工处理', '自动化', '模型辅助'])
        self.assertIsNone(self.app.bridge)
        for size in ('560x700', '520x600'):
            self.root.geometry(size)
            self.root.update()
            for tab in self.app.tabs.tabs():
                self.app.tabs.select(tab)
                self.root.update()
                for widget in (self.app.tree, self.app.details, self.app.complete_button, self.app.status_label, self.app.check,
                               self.app.claim_author_box, self.app.model_panel.output, self.app.model_panel.allow,
                               self.app.model_panel.evidence, self.app.model_panel.info_label,
                               self.app.model_panel.model_box, self.app.model_panel.cancel_button,
                               *self.app.buttons, *self.app.view_buttons):
                    if not widget.winfo_viewable():
                        continue
                    self.assertGreater(widget.winfo_height(), 10)
                    self.assertGreaterEqual(widget.winfo_rootx(), self.root.winfo_rootx())
                    self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), self.root.winfo_rootx() + self.root.winfo_width())
                    self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), self.root.winfo_rooty() + self.root.winfo_height())
                for button in self.app.buttons:
                    if button.winfo_viewable():
                        self.assertGreaterEqual(button.winfo_width(), button.winfo_reqwidth(), button.cget('text'))
            self.app.tabs.select(self.app.automation_page)
            for child in self.app.automation_panel.subtabs.tabs():
                self.app.automation_panel.subtabs.select(child)
                self.root.update()
                for widget in (*self.app.buttons, self.app.automation_panel.output):
                    if widget.winfo_viewable():
                        self.assertGreater(widget.winfo_height(), 10)
                        self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), self.root.winfo_rooty() + self.root.winfo_height())
        self.app.tabs.select(self.app.automation_page)
        self.assertIsNone(self.app.bridge)

    def test_pairing_is_lazy_and_dialog_controls_fit(self):
        self.root.deiconify()
        self.root.update()
        browser = Mock(online=False, token='a' * 43)
        with patch('app.Bridge', return_value=browser) as create:
            self.assertIsNone(self.app.bridge)
            self.app.pair()
            create.assert_called_once_with()
        self.assertIs(self.app.bridge, browser)
        self.root.update()
        popup = next(w for w in self.root.winfo_children() if isinstance(w, tk.Toplevel))
        buttons = [w for frame in popup.winfo_children() for w in frame.winfo_children() if w.winfo_class() == 'TButton']
        self.assertEqual(len(buttons), 3)
        for button in buttons:
            self.assertGreater(button.winfo_height(), 10)
            self.assertLessEqual(button.winfo_rooty() + button.winfo_height(), popup.winfo_rooty() + popup.winfo_height())
        popup.destroy()

    def test_completed_record_can_locate_but_cannot_open_editor(self):
        self.app.task_view.set('done')
        self.app.switch_view()
        self.select_first()
        browser = Mock(online=True)
        browser.call.return_value = {'row': {'saLzkId': 'demo-002'}}
        self.app.bridge = browser
        with patch.object(self.app, 'run', side_effect=self.sync_run):
            self.app.locate()
        browser.call.assert_called_once_with('search', {'sa_id': 'demo-002'})
        with patch('app.messagebox.showwarning'):
            self.app.open_browser_panel('open_metadata')
        self.assertEqual(browser.call.call_count, 1)

    def test_wrong_browser_id_rejected(self):
        self.select_first()
        self.app.bridge = Mock(online=True)
        self.app.bridge.call.return_value = {'row': {'saLzkId': 'wrong'}}
        with patch.object(self.app, 'run', side_effect=self.sync_run):
            with self.assertRaisesRegex(SafetyStop, 'ID 不一致'):
                self.app.locate()
        self.assertIsNone(self.app.snapshot)

    def test_completed_view_is_green_and_cannot_approve_again(self):
        self.app.task_view.set('done')
        self.app.switch_view()
        self.assertEqual(self.app.tree.get_children(), ('demo-002',))
        self.select_first()
        self.assertEqual(self.app.badge.cget('background'), GREEN)
        self.assertEqual(str(self.app.complete_button['state']), 'disabled')
        with patch('app.messagebox.showwarning'), patch('app.mark_complete') as write:
            self.app.confirm_manual_done()
            write.assert_not_called()
        self.app.task_view.set('pending')
        self.app.switch_view()
        self.assertNotIn('demo-002', self.app.tree.get_children())

    def test_manual_browser_navigation_uses_selected_id_and_snapshot(self):
        self.select_first()
        browser = Mock(online=True)
        browser.call.return_value = {'row': {'saLzkId': self.app.current.sa_id, 'itemId': '123'}}
        self.app.bridge = browser
        with patch.object(self.app, 'run', side_effect=self.sync_run):
            self.app.locate()
            browser.call.assert_called_with('search', {'sa_id': 'demo-001'})
            self.app.open_browser_panel('open_metadata')
            browser.call.assert_called_with('open_metadata', {'sa_id': 'demo-001', 'expected': {'saLzkId': 'demo-001', 'itemId': '123'}})
        self.assertIsNone(self.app.snapshot)
        before = browser.call.call_count
        with patch('app.messagebox.showwarning'):
            self.app.open_browser_panel('complete')
        self.assertEqual(browser.call.call_count, before)

    def test_browser_navigation_requires_connection(self):
        self.select_first()
        with patch('app.messagebox.showwarning') as warning, patch.object(self.app, 'run') as run:
            self.app.locate()
            warning.assert_called_once()
            run.assert_not_called()

    def test_claim_closure_requires_review_snapshot_and_exact_remark(self):
        self.select_tan_first()
        self.app.bridge = Mock(online=True)
        with patch('app.messagebox.showwarning'), patch.object(self.app, 'run') as run:
            self.app.confirm_claim_done()
            self.app.reviewed.set(True)
            self.app.confirm_claim_done()
            self.app.snapshot = {'saLzkId': self.app.current.sa_id}
            self.app.comparison = [{'label': '认领状态', 'library': '已认领'}]
            self.app.confirm_claim_done()
            run.assert_not_called()

    def test_claim_closure_confirmation_passes_scoped_evidence(self):
        self.select_tan_first()
        self.app.bridge = Mock(online=True)
        self.app.snapshot = {'saLzkId': self.app.current.sa_id}
        self.app.comparison = [{'label': '认领状态', 'library': '已认领'}]
        self.app.note.insert('1.0', '已认领')
        self.app.reviewed.set(True)
        with patch('app.messagebox.askyesno', return_value=True), patch.object(self.app, 'run') as run:
            self.app.confirm_claim_done()
            run.assert_called_once()
            with patch('app.complete_claim') as close:
                run.call_args.args[0]()
                self.assertEqual(close.call_args.args[1].owner, '谭勋策')
                self.assertTrue(close.call_args.kwargs['reviewed'])

    def test_automation_precheck_syncs_remote_done_without_browser_writes(self):
        self.select_tan_first()
        browser = Mock(online=True)
        browser.call.return_value = {'row': {'saLzkId': 'demo-001', 'markStatus': '已处理'}}
        self.app.bridge = browser
        with patch.object(self.app, 'run', side_effect=self.sync_run):
            self.app.automation_panel.start()
        browser.call.assert_called_once_with('status', {'sa_id': 'demo-001'})
        self.assertTrue(read_roster(self.path).records[0].done)
        self.assertNotIn('demo-001', self.app.tree.get_children())
        self.assertIsNone(self.app.current)
        self.assertIn('跳过', self.app.automation_panel.route.get())

    def test_automation_pending_status_requires_fresh_detail_before_routing(self):
        self.select_tan_first()
        browser = Mock(online=True)
        row = {'saLzkId': 'demo-001', 'markStatus': '待处理',
               'matchCount': 1, 'itemId': '1234567890123456789'}
        browser.call.side_effect = [{'row': row}, {'row': row, 'comparison': []}]
        self.app.bridge = browser
        with patch.object(self.app, 'run', side_effect=self.sync_run):
            self.app.automation_panel.start()
        self.assertEqual([call.args[0] for call in browser.call.call_args_list],
                         ['status', 'search'])
        self.assertFalse(read_roster(self.path).records[0].done)

    def test_resume_precheck_skips_import_when_remote_is_done(self):
        self.select_tan_first()
        browser = Mock(online=True)
        browser.call.return_value = {'row': {'saLzkId': 'demo-001', 'markStatus': '已处理'}}
        self.app.bridge = browser
        panel = self.app.automation_panel
        panel.store = Mock()
        panel.store.get.return_value = {
            'candidate': {'title': 'Synthetic 1', 'authors': 'Alice', 'journal': 'Journal',
                          'year': '2026', 'doi': '', 'wos': '', 'affiliation': '上海交通大学'},
            'phase': 'exported', 'identity_confirmed': True,
            'instructions': 'SA补充-demo-001', 'batch': None}
        flow = Mock()
        with patch.object(panel, 'engine', return_value=flow), \
             patch.object(self.app, 'run', side_effect=self.sync_run):
            panel.resume()
        browser.call.assert_called_once_with('status', {'sa_id': 'demo-001'})
        flow.proceed.assert_not_called()
        self.assertTrue(read_roster(self.path).records[0].done)

    def test_approval_button_enabled_only_after_review(self):
        self.select_first()
        self.assertEqual(str(self.app.complete_button['state']), 'disabled')
        self.app.reviewed.set(True)
        self.assertEqual(str(self.app.complete_button['state']), 'normal')
        self.app.set_busy(True)
        self.assertEqual(str(self.app.complete_button['state']), 'disabled')

    def test_explicit_approval_and_confirmation_required(self):
        self.select_first()
        before = file_hash(self.path)
        with patch('app.messagebox.showwarning') as warning, patch('app.mark_complete') as write:
            self.app.confirm_manual_done()
            warning.assert_called_once()
            write.assert_not_called()
        self.app.reviewed.set(True)
        with patch('app.messagebox.askyesno', return_value=False), patch('app.mark_complete') as write:
            self.app.confirm_manual_done()
            write.assert_not_called()
        self.assertEqual(file_hash(self.path), before)

    def test_completion_persists_green_and_stays_on_current(self):
        self.complete_first()
        self.assertTrue(read_roster(self.path).records[0].done)
        self.assertNotIn('demo-001', self.app.tree.get_children())
        self.assertEqual(self.app.current.sa_id, 'demo-001')
        self.assertEqual(self.app.approval.get(), '已完成')
        self.assertEqual(self.app.badge.cget('background'), GREEN)
        self.assertFalse(self.app.reviewed.get())
        self.app.next_record()
        self.assertEqual(self.app.approval.get(), '未完成')
        self.assertEqual(self.app.badge.cget('background'), YELLOW)
        self.assertNotEqual(self.app.current.sa_id, 'demo-001')

    def test_reload_keeps_completed_hidden(self):
        self.complete_first()
        with patch('app.fixed_roster_path', return_value=self.path) as fixed, patch.object(self.app, 'run', side_effect=self.sync_run):
            self.app.reload_roster()
        fixed.assert_called_once_with(BASE)
        self.assertEqual(self.app.owner.get(), '')
        self.app.owner.set('测试员')
        self.app.select_owner()
        self.assertNotIn('demo-001', self.app.tree.get_children())

    def test_missing_reload_clears_previous_queue(self):
        self.select_first()
        with patch('app.fixed_roster_path', side_effect=SafetyStop('missing')), patch.object(self.app, 'run', side_effect=self.sync_run):
            with self.assertRaises(SafetyStop):
                self.app.reload_roster()
        self.assertIsNone(self.app.roster)
        self.assertIsNone(self.app.current)
        self.assertEqual(self.app.tree.get_children(), ())
        self.assertEqual(self.app.records, [])

    def test_busy_blocks_completion_and_selection(self):
        self.select_first()
        record = self.app.current
        self.app.set_busy(True)
        self.assertEqual(str(self.app.owner_box['state']), 'disabled')
        self.app.next_record()
        self.assertEqual(self.app.current, record)
        with patch('app.mark_complete') as write:
            self.app.confirm_manual_done()
            write.assert_not_called()

    def test_note_presets_and_edits_clear_approval(self):
        self.select_first()
        self.app.reviewed.set(True)
        self.app.use_remark(CLAIMED)
        self.app.use_remark(CLAIMED)
        self.assertEqual(self.app.note.get('1.0', 'end').strip(), CLAIMED)
        self.assertFalse(self.app.reviewed.get())
        self.root.update_idletasks()
        self.app.reviewed.set(True)
        self.app.note.insert('end', ' 人工核对')
        self.root.update()
        self.assertFalse(self.app.reviewed.get())

    def test_failed_save_remains_yellow_and_pending(self):
        self.select_first()
        self.app.reviewed.set(True)
        before = file_hash(self.path)
        with patch('app.messagebox.askyesno', return_value=True), patch('app.mark_complete', side_effect=SafetyStop('locked')):
            self.app.confirm_manual_done()
        import time
        deadline = time.monotonic() + 3
        with patch('app.messagebox.showwarning'):
            while self.app.busy and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.02)
        self.assertFalse(self.app.busy)
        self.assertEqual(self.app.approval.get(), '未完成')
        self.assertIn('demo-001', self.app.tree.get_children())
        self.assertFalse(self.app.reviewed.get())
        self.assertEqual(file_hash(self.path), before)

    def test_log_failure_after_save_does_not_undo_completion(self):
        self.select_first()
        self.app.reviewed.set(True)
        save = self.app.journal.save
        def fail_when_done(record, state, *args):
            if state == '已完成':
                raise OSError('disk failure')
            return save(record, state, *args)
        with patch('app.messagebox.askyesno', return_value=True), patch('app.messagebox.showwarning'), patch.object(self.app.journal, 'save', side_effect=fail_when_done), patch.object(self.app, 'run', side_effect=self.sync_run):
            self.app.confirm_manual_done()
        self.assertTrue(self.app.current.done)
        self.assertEqual(self.app.approval.get(), '已完成')
        self.assertIn('日志保存失败', self.app.status.get())

    def test_all_completed_is_valid_empty_queue(self):
        make_roster(self.path, [1, 1])
        self.app.loaded(read_roster(self.path))
        self.app.select_owner()
        self.assertEqual(self.app.records, [])
        self.assertEqual(self.app.done_count.get(), '已完成 2')

    def set_claim_ready(self, suggested=0):
        self.select_first()
        self.app.snapshot = {'saLzkId': self.app.current.sa_id}
        self.app.comparison = [{'label': '认领状态', 'sa': '测试员(00001)①', 'library': '未认领'}]
        prepared = {'staff_id': '00001', 'sa_text': '测试员(00001)①', 'item_id': '1234567890123456789',
                    'person': {'id': 'scholar-1', 'wno': '00001', 'name': '测试员'},
                    'authors': [{'index': 0, 'order': 1, 'fullname': 'Demo', 'scholarId': '', 'eligible': True}]}
        self.app.bridge = Mock(online=True)
        self.app.bridge.call.return_value = {'row': self.app.snapshot, 'prepared': prepared, 'suggested_index': suggested}
        with patch.object(self.app, 'run', side_effect=self.sync_run):
            self.app.prepare_claim()
        return prepared

    def test_sa_copy_preserves_zeroes_not_roster_record_id(self):
        self.set_claim_ready()
        with patch.object(self.app, 'copy') as copy:
            self.app.copy_sa_number()
        self.assertEqual(copy.call_args.args[0], '00001')
        self.app.next_record()
        with patch.object(self.app, 'copy') as copy, patch('app.messagebox.showwarning'):
            self.app.copy_sa_number()
            copy.assert_not_called()
        self.assertIsNone(self.app.prepared_claim)

    def test_claim_preparation_does_not_write_or_mark_done(self):
        before = file_hash(self.path)
        self.set_claim_ready()
        self.assertEqual(self.app.bridge.call.call_args.args[0], 'prepare_claim')
        self.assertEqual(self.app.claim_author_box.current(), 0)
        self.assertEqual(str(self.app.claim_button['state']), 'normal')
        self.assertFalse(self.app.current.done)
        self.assertFalse(self.app.reviewed.get())
        self.assertEqual(file_hash(self.path), before)

    def test_unknown_author_requires_explicit_selection(self):
        self.set_claim_ready(suggested=None)
        self.assertEqual(self.app.claim_author_box.current(), -1)
        self.assertEqual(str(self.app.claim_button['state']), 'disabled')
        with patch('app.messagebox.showwarning'), patch.object(self.app, 'run') as run:
            self.app.submit_claim()
            run.assert_not_called()

    def test_cancelled_claim_never_submits(self):
        self.set_claim_ready()
        with patch('app.messagebox.askyesno', return_value=False), patch.object(self.app, 'run') as run:
            self.app.submit_claim()
            run.assert_not_called()
        self.assertEqual(self.app.bridge.call.call_count, 1)

    def test_verified_claim_adds_note_but_leaves_excel_pending(self):
        self.set_claim_ready()
        before = file_hash(self.path)
        self.app.bridge.call.return_value = {'row': self.app.snapshot, 'verified': True, 'claimed': True,
                                            'staff_id': '00001', 'scholar_id': 'scholar-1', 'author': 'Demo', 'order': 1}
        with patch('app.messagebox.askyesno', return_value=True), patch.object(self.app, 'run', side_effect=self.sync_run):
            self.app.submit_claim()
        action, payload = self.app.bridge.call.call_args.args
        self.assertEqual(action, 'submit_claim')
        self.assertTrue(payload['confirmed'])
        self.assertEqual(payload['author_index'], 0)
        self.assertEqual(self.app.note.get('1.0', 'end').strip(), '已认领')
        self.assertFalse(self.app.reviewed.get())
        self.assertFalse(self.app.current.done)
        self.assertEqual(file_hash(self.path), before)
        self.assertIsNone(self.app.snapshot)
        self.assertIsNone(self.app.prepared_claim)

    def test_unverified_claim_does_not_add_success_note(self):
        self.set_claim_ready()
        self.app.bridge.call.return_value = {'row': self.app.snapshot, 'verified': False}
        with patch('app.messagebox.askyesno', return_value=True), patch.object(self.app, 'run', side_effect=self.sync_run):
            with self.assertRaisesRegex(SafetyStop, '禁止直接重试'):
                self.app.submit_claim()
        self.assertEqual(self.app.note.get('1.0', 'end').strip(), '')
        self.assertFalse(self.app.current.done)
        self.assertIsNone(self.app.prepared_claim)

    def test_claim_lookup_requires_matching_staff_and_pending_record(self):
        self.set_claim_ready()
        self.app.comparison[0]['sa'] = '测试员(00002)'
        with patch('app.messagebox.showwarning'), patch.object(self.app, 'run') as run:
            self.app.prepare_claim()
            run.assert_not_called()

        self.app.current = replace(self.app.current, done=True)
        with patch('app.messagebox.showwarning'), patch.object(self.app, 'run') as run:
            self.app.submit_claim()
            run.assert_not_called()

    def test_model_requires_per_record_send_consent(self):
        self.select_first()
        self.app.model_panel.client = Mock()
        with patch('model_panel.messagebox.showwarning'), patch.object(self.app, 'run') as run:
            self.app.model_panel.review()
            run.assert_not_called()
        self.app.model_panel.consent.set(True)
        self.app.next_record()
        self.assertFalse(self.app.model_panel.consent.get())

    def test_model_advice_cannot_write_or_approve(self):
        from tests.test_model_review import advice
        self.select_first()
        before = file_hash(self.path)
        panel = self.app.model_panel
        panel.client = Mock()
        panel.client.review.return_value = {'advice': advice(), 'model': 'deepseek-reasoner', 'usage': {}}
        panel.consent.set(True)
        self.app.reviewed.set(True)
        with patch.object(self.app, 'run', side_effect=self.sync_run), patch('app.mark_complete') as write:
            panel.review()
            write.assert_not_called()
        self.assertIn('未经人工批准', panel.result_text)
        self.assertFalse(self.app.reviewed.get())
        self.assertFalse(self.app.current.done)
        self.assertEqual(file_hash(self.path), before)
        self.assertIsNone(self.app.bridge)
        self.app.next_record()
        self.assertEqual(panel.result_text, '')

    def test_model_changed_evidence_invalidates_advice(self):
        self.select_first()
        panel = self.app.model_panel
        panel.result_text = 'old advice'
        panel.consent.set(True)
        panel.evidence.insert('1.0', 'New evidence')
        self.root.update()
        self.assertEqual(panel.result_text, '')
        self.assertFalse(panel.consent.get())
        self.app.next_record()
        self.assertEqual(panel.evidence.get('1.0', 'end').strip(), '')

    def test_model_rejects_stale_result(self):
        from tests.test_model_review import advice
        self.select_first()
        panel = self.app.model_panel
        panel.client = Mock()
        panel.client.review.return_value = {'advice': advice(), 'model': 'deepseek-reasoner', 'usage': {}}
        panel.consent.set(True)
        def changed(job, callback, status):
            result = job()
            panel.evidence.insert('1.0', 'changed')
            callback(result)
        with patch.object(self.app, 'run', side_effect=changed):
            with self.assertRaisesRegex(SafetyStop, '旧模型意见'):
                panel.review()
        self.assertEqual(panel.result_text, '')

    def test_model_busy_disables_edits_but_allows_cancel(self):
        panel = self.app.model_panel
        panel.client = Mock()
        panel.running = True
        self.app.set_busy(True)
        self.assertEqual(str(panel.evidence['state']), 'disabled')
        self.assertEqual(str(panel.cancel_button['state']), 'normal')
        panel.cancel()
        panel.client.cancel.assert_called_once()
        self.assertTrue(panel.cancelled)


if __name__ == '__main__':
    unittest.main()
