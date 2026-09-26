import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import patch
from app import App
from core import Journal, file_hash, read_roster
from tests.test_roster_write import make_roster


class UnifiedTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'list.xlsx'
        make_roster(self.path)
        self.root=tk.Tk()
        self.root.withdraw()
        self.reader=patch('classify_app.read_papers',return_value={'papers':[{'rows':[2]}]})
        self.reader.start()
        self.addCleanup(self.reader.stop)
        self.app=App(self.root,Journal(Path(self.tmp.name)/'unified.db'),auto_load=False,unified=True)
        self.app.loaded(read_roster(self.path))

    def tearDown(self):
        self.app.closing=False
        self.app.classifier.busy=False
        self.app.set_busy(False)
        self.root.update_idletasks()
        self.app.close()

    def test_all_features_in_one_notebook_and_shared_busy_state(self):
        self.assertEqual([self.app.tabs.tab(t,'text') for t in self.app.tabs.tabs()],['人工处理','自动化 / 认领','模型辅助','批量分类 / 导入渠道','零匹配提交准备'])
        self.assertIs(self.app.classifier.root,self.root)
        self.app.classifier.set_busy(True)
        self.assertTrue(self.app.busy)
        self.assertEqual(str(self.app.complete_button['state']),'disabled')
        self.app.classifier.set_busy(False)
        self.assertFalse(self.app.busy)
        self.app.set_busy(True)
        self.assertEqual(str(self.app.classifier.start_button['state']),'disabled')
        self.assertTrue(self.app.classifier.external_busy)

    def test_classified_record_navigates_without_web_or_write(self):
        record=self.app.roster.records[0]
        before=file_hash(self.path)
        self.app.review_classified({'rows':[record.row],'title':record.title,'doi':record.doi})
        self.assertEqual(self.app.current.sa_id,record.sa_id)
        self.assertEqual(str(self.app.tabs.select()),str(self.app.manual_page))
        self.assertIsNone(self.app.bridge)
        self.assertEqual(file_hash(self.path),before)

    def test_close_waits_for_batch(self):
        self.app.classifier.set_busy(True)
        self.app.close()
        self.assertTrue(self.app.closing)
        self.assertTrue(self.app.classifier.stop.is_set())
        self.assertTrue(self.root.winfo_exists())

    def test_integrated_layout_controls_within_window(self):
        self.root.deiconify()
        self.root.geometry('960x740')
        self.root.update()
        for tab in self.app.tabs.tabs():
            self.app.tabs.select(tab)
            self.root.update()
            for widget in (self.app.complete_button,self.app.status_label,self.app.model_panel.copy_button,
                           self.app.classifier.start_button,self.app.classifier.channel_button,self.app.classifier.review_button):
                if widget.winfo_viewable():
                    self.assertGreater(widget.winfo_height(),10)
                    self.assertLessEqual(widget.winfo_rootx()+widget.winfo_width(),self.root.winfo_rootx()+self.root.winfo_width())
                    self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(),self.root.winfo_rooty()+self.root.winfo_height())
