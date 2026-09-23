"""Manual desk tests with native widgets and synthetic files; no website calls."""
import tempfile
import tkinter as tk
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch, Mock

from app import App, BASE, GREEN, YELLOW
from core import Journal, SafetyStop, file_hash, read_roster
from remarks import CLAIMED
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
        self.assertEqual([self.app.tabs.tab(tab, 'text') for tab in self.app.tabs.tabs()], ['人工处理', '自动化'])
        self.assertIsNone(self.app.bridge)
        for size in ('560x700', '520x600'):
            self.root.geometry(size)
            self.root.update()
            for tab in self.app.tabs.tabs():
                self.app.tabs.select(tab)
                self.root.update()
                for widget in (self.app.tree, self.app.details, self.app.complete_button, self.app.status_label, self.app.check,
                               self.app.claim_author_box, *self.app.buttons, *self.app.view_buttons):
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


if __name__ == '__main__':
    unittest.main()
