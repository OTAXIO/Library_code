import queue
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import App
from tests.test_wos_batch import FakeRoster, record


class TrialTests(unittest.TestCase):
    def make_app(self,records):
        app=App.__new__(App)
        app.root=Mock()
        app.roster=FakeRoster(records)
        app.classifier=Mock()
        app.classifier.owner.get.return_value='测试员'
        app.classifier.export_generation=1
        app.classifier.queue=queue.Queue()
        app.automation_panel=Mock(progress=queue.Queue())
        app.operation_log=Mock()
        app.bridge=Mock()
        app.wos_import_panel=Mock()
        app.tabs=Mock()
        app.last_wos_owner,app.last_wos_scope='原负责人','pending'
        app._wos_export_ready=Mock(return_value={})
        return app

    def test_selected_trial_allows_matched_done_record_without_changing_batch_scope(self):
        item=record(done=True,matches=1)
        app=self.make_app([item,record(row=3,sa_id='other',owner='other')])
        app._start_wos_export=Mock()
        app.export_selected_wos_metadata({'rows':[2],'title':item.title,'doi':item.doi})
        self.assertEqual(app._start_wos_export.call_args.args[0],[item])
        self.assertTrue(app._start_wos_export.call_args.kwargs['trial'])
        self.assertEqual((app.last_wos_owner,app.last_wos_scope),('原负责人','pending'))

    def test_trial_must_still_map_exact_title_doi_row_and_owner(self):
        app=self.make_app([record()])
        app._start_wos_export=Mock()
        for item in ({'rows':[3],'title':'Synthetic paper','doi':'10.1234/test'},
                     {'rows':[2],'title':'Changed','doi':'10.1234/test'},
                     {'rows':[2],'title':'Synthetic paper','doi':'10.1234/other'}):
            with patch('app.messagebox.showinfo'):
                app.export_selected_wos_metadata(item)
        app._start_wos_export.assert_not_called()

    def test_trial_worker_never_writes_roster_or_enters_intake(self):
        app=self.make_app([record(done=True,matches=1)])
        jobs=[]
        app.run=lambda job,done,*args,**kwargs:jobs.append((job,done))
        result={'total':1,'attempted':1,'remaining':0,'exported':[{'row':2,'sa_id':'demo-001','file':'synthetic.txt'}],
                'failed':{},'unconfirmed':[]}
        with tempfile.TemporaryDirectory() as folder, patch('app.BASE',Path(folder)), \
             patch('wos_browser.select_transport',return_value=Mock()), \
             patch('wos_batch.preflight',return_value={'extension_version':'0.3.29'}), \
             patch('wos_batch.export',return_value=result) as exported, \
             patch('wos_batch.persist_download_outcomes') as persisted, \
             patch('wos_reports.save_download_report',return_value=Path(folder)/'report.md'), \
             patch('app.messagebox.showinfo'):
            app._start_wos_export(app.roster.records,'试下载',trial=True)
            saved=jobs[0][0]()
            jobs[0][1](saved)
        self.assertFalse(exported.call_args.kwargs['eligible_only'])
        self.assertEqual(exported.call_args.args[3].parts[-3:],('runtime','wos-trials','files'))
        persisted.assert_not_called()
        app.wos_import_panel.receive_downloads.assert_not_called()
        app.tabs.select.assert_not_called()
        self.assertEqual((app.last_wos_owner,app.last_wos_scope),('原负责人','pending'))


if __name__=='__main__':
    unittest.main()
