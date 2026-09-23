"""Manual desk tests with native widgets and synthetic files; no website calls."""
import tempfile
import tkinter as tk
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

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

    def test_layout_tabs_topmost_and_no_automation(self):
        self.root.deiconify()
        self.root.update()
        self.assertTrue(self.root.attributes('-topmost'))
        self.assertEqual([self.app.tabs.tab(tab, 'text') for tab in self.app.tabs.tabs()], ['人工处理', '自动化'])
        self.assertFalse(hasattr(self.app, 'bridge'))
        for size in ('560x700', '520x600'):
            self.root.geometry(size)
            self.root.update()
            for widget in (self.app.tree, self.app.details, self.app.complete_button, self.app.status_label, self.app.check):
                self.assertGreater(widget.winfo_height(), 10)
                self.assertGreaterEqual(widget.winfo_rootx(), self.root.winfo_rootx())
                self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), self.root.winfo_rootx() + self.root.winfo_width())
                self.assertLessEqual(widget.winfo_rooty() + widget.winfo_height(), self.root.winfo_rooty() + self.root.winfo_height())
        self.app.tabs.select(self.app.automation_page)
        self.assertFalse(hasattr(self.app, 'bridge'))

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


if __name__ == '__main__':
    unittest.main()
