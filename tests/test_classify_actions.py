"""The classification footer separates whole-owner workflows from one-paper review."""
import tempfile
import time
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from classify_app import ClassifyApp


class ClassifyActionsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=tk.Tk()
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        self.export=Mock()
        self.skipped=Mock()
        self.review=Mock()
        self.import_files=Mock()
        self.selected_export=Mock()
        source={'sha256':'fixture','owner':'测试员','pending_only':False,
                'papers':[{'rows':[2,3]}]}
        with patch('classify_app.BASE',Path(self.tmp.name)), \
             patch('classify_app.list_owners',return_value=['测试员']), \
             patch('classify_app.read_papers',return_value=source):
            self.app=ClassifyApp(self.root,on_export=self.export,on_export_skipped=self.skipped,
                                 on_review=self.review,on_import=self.import_files,on_export_selected=self.selected_export)
            self.app.owner.set('测试员')
            self.app.change_owner()
        self.addCleanup(self.app.dispose)
        self.root.update_idletasks()

    def test_batch_buttons_explain_scope_without_changing_callbacks(self):
        self.assertIn('不受表格筛选或选中行影响',self.app.batch_scope.get())
        self.assertIn('“是否识别”为 2',self.app.batch_scope.get())
        self.assertNotIn('备注为 2',self.app.batch_scope.get())
        self.assertIn('TXT',self.app.export_button['text'])
        self.assertIn('跳过',self.app.skipped_export_button['text'])
        self.assertIn('所选论文',self.app.review_button['text'])
        self.app.export_wos()
        self.app.export_skipped_wos()
        self.export.assert_called_once_with()
        self.skipped.assert_called_once_with()

    def test_import_navigation_does_not_require_classification_or_mutate_data(self):
        self.assertEqual(self.app.records,[])
        self.assertEqual(str(self.app.import_button['state']),'normal')
        self.app.import_button.invoke()
        self.import_files.assert_called_once_with()
        self.export.assert_not_called()
        self.skipped.assert_not_called()
        self.review.assert_not_called()

    def test_one_paper_download_is_separate_from_batch_and_obeys_busy_state(self):
        self.app.export_selected_wos()
        self.selected_export.assert_not_called()
        item={'rows':[2,3],'title':'Synthetic paper','doi':'10.1234/test','classification':{}}
        self.app.records=[item]
        self.app.filter_results()
        self.app.tree.selection_set('0')
        self.app.set_external_busy(True)
        self.app.export_selected_wos()
        self.selected_export.assert_not_called()
        self.app.set_external_busy(False)
        self.app.export_selected_wos()
        self.selected_export.assert_called_once_with(item)
        self.export.assert_not_called()
        self.skipped.assert_not_called()

    def test_download_does_not_require_ai_classification(self):
        self.assertEqual(self.app.records, [])
        self.assertEqual(str(self.app.export_button['state']), 'normal')
        self.assertEqual(str(self.app.skipped_export_button['state']), 'normal')
        self.assertIn('不限 AI 推荐数据库', self.app.batch_scope.get())

    def test_import_navigation_obeys_owner_and_shared_busy_state(self):
        self.app.set_external_busy(True)
        self.assertEqual(str(self.app.import_button['state']),'disabled')
        self.app.open_import()
        self.import_files.assert_not_called()
        self.app.set_external_busy(False)
        self.app.set_exporting(True)
        self.assertEqual(str(self.app.import_button['state']),'disabled')
        self.app.open_import()
        self.import_files.assert_not_called()
        self.app.set_exporting(False)
        self.app.owner.set('')
        self.app._sync_export_button()
        self.assertEqual(str(self.app.import_button['state']),'disabled')
        self.app.open_import()
        self.import_files.assert_not_called()

    def test_download_updates_do_not_reload_ai_results_or_release_busy_state(self):
        self.app.bar['value']=60
        self.app.set_exporting(True)
        self.app.set_external_busy(True)
        generation=self.app.export_generation
        self.app.queue.put(('download_progress',(generation,'WOS TXT · 第 1/20 篇 · 检索',time.monotonic()-7)))
        with patch.object(self.app,'load_results') as load:
            self.app.poll()
        load.assert_not_called()
        self.assertTrue(self.app.exporting)
        self.assertTrue(self.app.external_busy)
        self.assertEqual(str(self.app.bar['mode']),'indeterminate')
        self.assertIn('第 1/20 篇',self.app.status.get())
        self.assertRegex(self.app.status.get(),r'已等待 [7-9] 秒')
        self.assertEqual(str(self.app.export_button['state']),'disabled')
        self.app.request_stop()
        self.assertIn('当前步骤返回后停止',self.app.status.get())
        self.assertEqual(str(self.app.stop_button['state']),'disabled')
        self.app.set_external_busy(False)
        self.app.status.set('下载已暂停')
        self.app.queue.put(('download_progress',(generation,'旧消息',time.monotonic())))
        self.app.poll()
        self.assertEqual(self.app.status.get(),'下载已暂停')
        self.assertEqual(str(self.app.bar['mode']),'determinate')
        self.assertEqual(float(self.app.bar['value']),60)
        self.app.stop.clear()
        self.app.set_exporting(True)
        self.app.queue.put(('download_progress',(generation,'上一轮迟到消息',time.monotonic())))
        self.app.poll()
        self.assertNotIn('上一轮',self.app.status.get())
        self.app.set_exporting(False)

    def test_actions_remain_visible_in_compact_window(self):
        self.root.deiconify()
        self.root.geometry('960x740')
        self.app.set_exporting(True)
        self.app._export_message='WOS TXT · 第 135/135 篇 · 原表第 3472 行 · 检索并核验唯一文献（本步最多 120 秒，不重复检索）'
        self.app.request_stop()
        for _ in range(3):
            self.root.update()
        self.assertTrue(self.app.status_label.winfo_viewable())
        self.assertLessEqual(self.app.status_label.winfo_rootx()+self.app.status_label.winfo_width(),
                             self.root.winfo_rootx()+self.root.winfo_width())
        for button in (self.app.export_button,self.app.skipped_export_button,self.app.import_button,
                       self.app.report_button,self.app.channel_button,self.app.output_button,
                       self.app.review_button,self.app.selected_export_button):
            with self.subTest(button=button['text']):
                self.assertTrue(button.winfo_viewable())
                self.assertLessEqual(button.winfo_rootx()+button.winfo_width(),
                                     self.root.winfo_rootx()+self.root.winfo_width())
                self.assertLessEqual(button.winfo_rooty()+button.winfo_height(),
                                     self.root.winfo_rooty()+self.root.winfo_height())
        self.app.set_exporting(False)


if __name__=='__main__':
    unittest.main()
