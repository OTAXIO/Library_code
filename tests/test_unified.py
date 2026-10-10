import tempfile
import threading
import tkinter as tk
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch
from app import App
from core import Journal, file_hash, read_roster
from operation_log import OperationLog
from tests.test_roster_write import make_roster


class UnifiedTests(unittest.TestCase):
    def setUp(self):
        # Unit/UI tests must never select the user's saved browser transport or
        # open a real browser. Live transport is covered separately.
        transport=patch('wos_browser.select_transport',return_value=Mock())
        transport.start()
        self.addCleanup(transport.stop)
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'list.xlsx'
        make_roster(self.path)
        self.root=tk.Tk()
        self.root.withdraw()
        self.reader=patch('classify_app.read_papers',return_value={'papers':[{'rows':[2]}]})
        self.reader.start()
        self.addCleanup(self.reader.stop)
        self.owners=patch('classify_app.list_owners',return_value=['谭勋策','测试员'])
        self.owners.start()
        self.addCleanup(self.owners.stop)
        self.app=App(self.root,Journal(Path(self.tmp.name)/'unified.db'),auto_load=False,unified=True,
                     operation_log=OperationLog(Path(self.tmp.name)/'preview-log.txt'))
        self.app.loaded(read_roster(self.path))

    def tearDown(self):
        self.app.closing=False
        self.app.classifier.busy=False
        self.app.set_busy(False)
        self.root.update_idletasks()
        self.app.close()

    def test_all_features_in_one_notebook_and_shared_busy_state(self):
        self.assertEqual([self.app.tabs.tab(t,'text') for t in self.app.tabs.tabs()],['人工处理','自动化 / 认领','批量分类 / 核对','零匹配提交准备','WOS 导入','设置'])
        self.assertEqual([self.app.analysis_tabs.tab(t,'text') for t in self.app.analysis_tabs.tabs()],['批量分类 / 导入渠道','单条核对'])
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

    def test_analysis_subtabs_and_settings_share_configuration_without_requests(self):
        self.app.classifier.configure_key()
        self.assertEqual(self.app.tabs.select(), str(self.app.settings_page))
        self.root.deiconify()
        self.root.geometry('960x740')
        self.app.tabs.select(self.app.classification_page)
        for tab in self.app.analysis_tabs.tabs():
            self.app.analysis_tabs.select(tab)
            self.root.update()
            widget = self.app.model_panel.copy_button if tab == str(self.app.model_page) else self.app.classifier.export_button
            self.assertTrue(widget.winfo_viewable())
            self.assertLessEqual(widget.winfo_rooty()+widget.winfo_height(), self.root.winfo_rooty()+self.root.winfo_height())
        self.assertIsNone(self.app.bridge)

    def test_close_waits_for_batch(self):
        self.app.classifier.set_busy(True)
        self.app.close()
        self.assertTrue(self.app.closing)
        self.assertTrue(self.app.classifier.stop.is_set())
        self.assertTrue(self.root.winfo_exists())

    def test_first_batch_initializes_archive_and_next_batch_reuses_it(self):
        self.app.automation_panel.runtime=Path(self.tmp.name)/'wos-imports'
        self.assertIsNone(self.app.automation_panel.store)

    def test_wos_export_uses_classification_owner_scope(self):
        owner=self.app.roster.records[0].owner
        self.app.classifier.owner.set(owner)
        captured=[]
        with patch.object(self.app,'_wos_export_ready',return_value={}) as ready, \
             patch.object(self.app,'_start_wos_export',side_effect=lambda targets,label,rows=None:
                          captured.append((targets,label,rows))):
            self.app.export_wos_metadata()
        ready.assert_called_once_with(owner)
        self.assertEqual(len(captured),1)
        targets,label,rows=captured[0]
        self.assertTrue(all(record.owner==owner and not record.done and not record.skipped and record.matches==0
                            for record in targets))
        self.assertIn(owner,label)
        record=self.app.roster.records[0]
        stores=[]

        def download(targets,bridge,store,inbox,**kwargs):
            # Exercise the same first archive access as WOSFlow.prepare.
            self.assertIsNone(store.get(record))
            self.assertTrue(store.path.is_file())
            stores.append(store)
            return {'exported': []}

        def run(job,callback,status,**kwargs):
            job()

        with patch.object(self.app,'run',side_effect=run), \
             patch('app.BASE',Path(self.tmp.name)), \
             patch('wos_batch.preflight',return_value={'extension_version':'0.3.27'}), \
             patch('wos_batch.export',side_effect=download), \
             patch('wos_batch.default_store',side_effect=lambda: __import__('automation').ImportStore(Path(self.tmp.name)/'downloads')), \
             patch('wos_batch.default_inbox',return_value=Path(self.tmp.name)/'inbox'):
            self.app._start_wos_export([record],'测试下载')
            self.app._start_wos_export([record],'测试下载')
        self.assertEqual(len(stores),2)
        self.assertEqual(stores[0].path,stores[1].path)
        self.assertIsNone(self.app.automation_panel.store)

    def test_search_skipped_items_uses_only_owner_numeric_two_rows(self):
        owner=self.app.roster.records[0].owner
        self.app.roster.records=[replace(record,matches=0,skipped=True)
                                 if index==0 else record
                                 for index,record in enumerate(self.app.roster.records)]
        self.app.classifier.owner.set(owner)
        captured=[]
        with patch.object(self.app,'_wos_export_ready',return_value={}) as ready, \
             patch.object(self.app,'_start_wos_export',side_effect=lambda targets,label,rows=None:
                          captured.append((targets,label,rows))):
            self.app.export_skipped_wos_metadata()
        ready.assert_called_once_with(owner)
        self.assertEqual(len(captured),1)
        targets,label,rows=captured[0]
        self.assertTrue(targets)
        self.assertTrue(all(record.owner==owner and record.skipped and not record.done
                            and record.matches==0 for record in targets))
        self.assertIn('重试跳过论文',label)
        self.assertEqual(rows,sum(record.owner==owner and record.skipped and not record.done
                                  and record.matches==0 for record in self.app.roster.records))

    def test_selected_download_keeps_exact_paper_owner_and_completion_guards(self):
        base=replace(self.app.roster.records[0],matches=0,doi='10.1234/synthetic',done=False)
        self.app.classifier.owner.set(base.owner)
        before=file_hash(self.path)
        original_scope=self.app.last_wos_scope
        for skipped in (False,True):
            with self.subTest(skipped=skipped):
                first=replace(base,skipped=skipped)
                duplicate=replace(first,row=7,sa_id='same-paper-copy')
                self.app.roster.records=[
                    replace(first,row=3,sa_id='another-owner',owner='Other'),
                    replace(first,row=4,sa_id='already-done',done=True),
                    replace(first,row=5,sa_id='matched',matches=1),
                    first,duplicate,
                    replace(first,row=8,sa_id='different-paper',title='Different paper')]
                item={'rows':[record.row for record in self.app.roster.records],
                      'title':first.title,'doi':first.doi}
                with patch.object(self.app,'_wos_export_ready',return_value={}) as ready, \
                     patch.object(self.app,'_start_wos_export') as start:
                    self.app.export_selected_wos_metadata(item)
                ready.assert_called_once_with(base.owner)
                self.assertEqual(start.call_count,1)
                self.assertEqual(start.call_args.args[0],[first])
                self.assertIn('试下载所选论文 TXT',start.call_args.args[1])
                self.assertEqual(start.call_args.kwargs['rows'],4)
                self.assertTrue(start.call_args.kwargs['trial'])
                self.assertEqual(self.app.last_wos_scope,original_scope)
                with patch.object(self.app,'_wos_export_ready',return_value={}), \
                     patch.object(self.app,'_start_wos_export') as start, \
                     patch('app.messagebox.showinfo'):
                    self.app.export_selected_wos_metadata({**item,'title':'Stale title'})
                start.assert_not_called()
        self.assertEqual(file_hash(self.path),before)

    def test_incompatible_extension_prevents_batch_search_and_file_operations(self):
        from core import SafetyStop
        record=self.app.roster.records[0]
        jobs=[]
        before=file_hash(self.path)
        with patch.object(self.app,'run',side_effect=lambda job,*args,**kwargs:jobs.append(job)), \
             patch('wos_batch.preflight',side_effect=SafetyStop('插件下载接口不兼容')), \
             patch('wos_batch.export') as download, patch('wos_batch.default_store') as store:
            self.app._start_wos_export([record],'fixture download')
            with self.assertRaisesRegex(SafetyStop,'插件下载接口不兼容'):
                jobs[0]()
        download.assert_not_called()
        store.assert_not_called()
        self.assertEqual(file_hash(self.path),before)
        self.app.classifier.set_exporting(False)

    def test_worker_download_progress_reaches_its_page_and_finishes_cleanly(self):
        from automation import ImportStore
        record=self.app.roster.records[0]
        jobs=[]
        self.app.classifier.bar['value']=40
        def hold(job,callback,status,**kwargs):
            self.app.set_busy(True)
            jobs.append((job,callback))
        def download(*args,**kwargs):
            kwargs['progress']('WOS TXT · 第 1/1 篇 · 检索并核验唯一文献')
            return {'exported':[],'unconfirmed':[],'remaining':0,'not_exported':1,'session_failures':0}
        values=[]
        with patch.object(self.app,'run',side_effect=hold), \
             patch('wos_batch.preflight',return_value={'extension_version':'0.3.27'}), \
             patch('wos_batch.export',side_effect=download), \
             patch('wos_batch.default_store',return_value=ImportStore(Path(self.tmp.name)/'downloads')), \
             patch('wos_batch.default_inbox',return_value=Path(self.tmp.name)/'inbox'), \
             patch('wos_reports.save_download_report',return_value=Path(self.tmp.name)/'report.md'), \
             patch('app.messagebox.showinfo'):
            self.app._start_wos_export([record],'fixture download')
            job,callback=jobs[0]
            worker=threading.Thread(target=lambda:values.append(job()))
            worker.start()
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.app.classifier.poll()
            self.assertIn('第 1/1 篇',self.app.classifier.status.get())
            self.assertIn('检索并核验',self.app.classifier.status.get())
            self.assertTrue(self.app.busy)
            self.app.events.put((True,callback,values[0],'fixture download',record.sa_id))
            self.root.after_cancel(self.app.pump_id)
            self.app.pump()
        self.app.classifier.poll()
        self.assertIn('下载已结束',self.app.classifier.status.get())
        self.assertIn('无可用记录 1 篇',self.app.classifier.status.get())
        self.assertFalse(self.app.busy)
        self.assertFalse(self.app.classifier.exporting)
        self.assertEqual(float(self.app.classifier.bar['value']),40)

    def test_download_worker_failure_stops_animation_and_keeps_error_on_download_page(self):
        from core import SafetyStop
        self.app.classifier.bar['value']=25
        self.app.classifier.set_exporting(True)
        self.app.set_busy(True)
        generation=self.app.classifier.export_generation
        self.app.classifier.queue.put(('download_progress',(generation,'旧进度',0)))
        self.app.events.put((False,lambda value:None,SafetyStop('浏览器未连接'),
                            'fixture download',''))
        with patch('app.messagebox.showwarning'):
            self.root.after_cancel(self.app.pump_id)
            self.app.pump()
        self.app.classifier.poll()
        self.assertIn('下载已暂停',self.app.classifier.status.get())
        self.assertIn('浏览器未连接',self.app.classifier.status.get())
        self.assertFalse(self.app.busy)
        self.assertFalse(self.app.classifier.exporting)
        self.assertEqual(str(self.app.classifier.bar['mode']),'determinate')
        self.assertEqual(float(self.app.classifier.bar['value']),25)

    def test_download_report_and_skipped_file_handoff_reach_import_preview(self):
        from openpyxl import load_workbook
        from core import HEADERS
        from automation import ImportStore, parse_wos
        from tests.test_automation import sample
        folder = Path(self.tmp.name)
        book = load_workbook(self.path)
        for key, value in {'owner': '谭勋策', 'title': 'Synthetic paper', 'doi': '10.1234/test',
                           'wos': '', 'matches': 0, 'item_ids': ''}.items():
            book.active.cell(2, 2 + list(HEADERS).index(key)).value = value
        book.active.cell(2, 1).value = 2
        book.save(self.path)
        book.close()
        self.app.loaded(read_roster(self.path))
        record = self.app.roster.records[0]
        self.app.automation_panel.runtime = folder / 'imports'
        inbox = folder / 'inbox'
        inbox.mkdir()
        exported = inbox / 'paper.txt'
        exported.write_bytes(sample())
        result = {'total': 1, 'exported': [{'sa_id': record.sa_id, 'row': record.row, 'title': record.title,
                  'file': str(exported), 'sha256': parse_wos(sample())['sha256']}], 'failed': {},
                  'unconfirmed': [], 'inbox': str(inbox), 'attempted': 1, 'remaining': 0}
        def sync(job, callback, status, **kwargs):
            self.app.set_busy(True)
            try:
                value = job()
            finally:
                self.app.set_busy(False)
            callback(value)
        with patch.object(self.app, 'run', side_effect=sync), patch('app.BASE', folder), \
             patch('wos_batch.preflight',return_value={'extension_version':'0.3.27'}), \
             patch('app.messagebox.showinfo'), patch('wos_batch.export', return_value=result), \
             patch('wos_batch.default_inbox', return_value=inbox), \
             patch('wos_batch.default_store', return_value=ImportStore(folder / 'downloads')):
            self.app._start_wos_export([record], 'synthetic download', rows=1)
        panel = self.app.wos_import_panel
        self.assertEqual(panel.plan.scope, 'skipped')
        self.assertEqual(panel.plan.items[0].record.sa_id, record.sa_id)
        self.assertEqual(panel.plan.items[0].status, 'ready')
        self.assertTrue(Path(panel.download_report).is_file())
        self.assertEqual(self.app.tabs.select(), str(self.app.wos_import_page))
        self.assertTrue(self.app.roster.records[0].skipped)
        self.assertFalse(self.app.roster.records[0].done)
        self.assertIsNone(panel.store().get(record), 'preview must not create an import intent')

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

    def test_every_page_keeps_its_controls_on_screen(self):
        # Regression: the WOS download button existed but Tk silently unmapped the whole
        # footer row on a short window. The older check above only inspected widgets that
        # were already viewable, so it skipped exactly the broken case and stayed green.
        self.root.deiconify()
        interactive=('TButton','Button','TCombobox','TEntry','Checkbutton','TCheckbutton')

        def walk(node):
            for child in node.winfo_children():
                yield child
                yield from walk(child)

        def hidden_by_notebook(widget):
            # A Notebook's unselected pane is hidden on purpose, not clipped.
            node=widget
            while node is not None and node is not self.root:
                parent=node.nametowidget(node.winfo_parent())
                if parent.winfo_class()=='TNotebook' and not node.winfo_ismapped():
                    return True
                node=parent
            return False

        for size in ('1180x900','960x740'):
            self.root.geometry(size)
            for _ in range(3):
                self.root.update()
            height=self.root.winfo_height()
            width=self.root.winfo_width()
            for tab in self.app.tabs.tabs():
                self.app.tabs.select(tab)
                for _ in range(3):
                    self.root.update()
                page=self.app.tabs.nametowidget(tab)
                if not page.winfo_ismapped():
                    continue
                for widget in walk(page):
                    if widget.winfo_class() not in interactive or hidden_by_notebook(widget):
                        continue
                    label=widget.cget('text') if 'text' in widget.keys() else widget.winfo_class()
                    where=f'{size} / {self.app.tabs.tab(tab,"text")} / {label}'
                    self.assertTrue(widget.winfo_ismapped(),
                                    f'{where} 没有被布局，控件存在但看不见')
                    top=widget.winfo_rooty()-self.root.winfo_rooty()
                    right=widget.winfo_rootx()-self.root.winfo_rootx()+widget.winfo_width()
                    self.assertGreaterEqual(top,0,f'{where} 顶部越界')
                    self.assertLessEqual(top+widget.winfo_height(),height,f'{where} 底部越界')
                    self.assertLessEqual(right,width,f'{where} 右侧越界')
