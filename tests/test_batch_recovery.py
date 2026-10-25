"""Synthetic queue recovery: no production browser, records or network."""
from pathlib import Path
import copy,json,sqlite3,tempfile,threading,unittest
from unittest.mock import Mock
from unittest.mock import patch
from openpyxl import load_workbook
from bridge import BrowserRejected
from core import SafetyStop,read_roster,file_hash
from roster_write import migrate_status_column
from batch_recovery import RecordReadback
from metadata_sources import MetadataClient,MetadataSkip,merge_metadata
from metadata_pipeline import run_metadata_batch
from zero_match import WorkflowStore
from workflow_service import WorkflowService
from tests.test_roster_write import make_roster
from tests.test_metadata_sources import example
from tests.test_metadata_pipeline import Backend

MISSING='[扩展 0.4.40] [已暂停] 未找到本条导入批次（完整扫描 17 条，同说明 0 条）；原提交结果不明，保留断点，未重传'


class RecoveryTests(unittest.TestCase):
    @patch('batch_recovery.time.sleep')
    def test_acknowledged_readonly_close_timeout_gets_one_bounded_read(self,sleep):
        tracker=RecordReadback();bridge=Mock(online=True,pending=None)
        error=BrowserRejected('search','[扩展 0.4.53] 【已暂停】关闭只读详情超时。本次命令未提交写入；请检查网页窗口后重新定位')
        bridge.call.side_effect=[error,{'detail_released':True}]
        self.assertEqual(tracker.call(bridge,'search',{'close_detail':True},90),{'detail_released':True})
        self.assertEqual(bridge.call.call_count,2);sleep.assert_called_once_with(3)
        self.assertEqual(bridge.call.call_args_list[0],bridge.call.call_args_list[1])
        self.assertTrue(tracker.returned)

    @patch('batch_recovery.time.sleep')
    def test_read_recovery_never_replays_writes_unknowns_or_more_than_once(self,sleep):
        message='关闭只读详情超时。本次命令未提交写入；请检查网页窗口后重新定位'
        for action,error,pending in [('complete',BrowserRejected('complete',message),None),
                ('search',SafetyStop('命令超时'),None),
                ('search',BrowserRejected('search',message),{}),
                ('search',BrowserRejected('search',message+'；请重新登录'),None)]:
            bridge=Mock(online=True,pending=pending);bridge.call.side_effect=error
            with self.assertRaises(SafetyStop):RecordReadback().call(bridge,action,{},90)
            self.assertEqual(bridge.call.call_count,1)
        bridge=Mock(online=True,pending=None);bridge.call.side_effect=BrowserRejected('search',message)
        with self.assertRaises(BrowserRejected):RecordReadback().call(bridge,'search',{},90)
        self.assertEqual(bridge.call.call_count,2)

    def roster(self,count=3,skipped=True):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup);base=Path(temp.name)
        path=base/'list.xlsx';make_roster(path,flags=((2 if skipped else None),)*count)
        book=load_workbook(path)
        for i in range(2,count+2):
            for col,value in {'B':'谭勋策','D':'Synthetic verified paper','E':'10.1234/example',
                              'H':0,'I':'','K':'','L':''}.items():book.active[col+str(i)]=value
        book.save(path);book.close()
        return base,migrate_status_column(read_roster(path))

    @staticmethod
    def builder(folder,candidate):
        p=folder/'paper.xlsx';p.write_bytes(b'PK synthetic workbook');return p,file_hash(p)

    def test_only_completed_read_failures_can_be_isolated(self):
        t=RecordReadback()
        bridge=Mock();bridge.call.side_effect=BrowserRejected('import_excel_check',MISSING)
        with self.assertRaises(BrowserRejected) as exc:t.call(bridge,'import_excel_check',{},90)
        self.assertEqual(t.isolated_note(exc.exception),'批次待核')
        for action,error in [('import_excel_submit',BrowserRejected('import_excel_submit',MISSING)),
                             ('import_excel_check',SafetyStop('命令超时（网页已接收命令但未返回结果）')),
                             ('status',BrowserRejected('status','请重新登录'))]:
            bridge.call.side_effect=error
            with self.assertRaises(SafetyStop) as raised:t.call(bridge,action,{},90)
            self.assertIsNone(t.isolated_note(raised.exception,True))

    def test_first_missing_batch_preserved_then_later_paper_completes(self):
        for retry,fast in ((False,False),(True,False),(False,True),(True,True)):
            with self.subTest(retry=retry,fast=fast):
                base,roster=self.roster(skipped=retry);records=roster.records
                _,cr,oa=example();candidate=merge_metadata(records[0],cr,oa)
                client=MetadataClient(base/'runtime/metadata-imports/papers')
                client.get=Mock(side_effect=[{'message':cr},oa]*3)
                c,folder=client.prepare(records[0]);path,checksum=self.builder(folder,c)
                store=WorkflowStore(base/'runtime/metadata-imports')
                saved={'phase':'import_intent','candidate':c,'path':str(path),'xlsx_sha256':checksum,
                       'instructions':'SA补充-'+records[0].sa_id,'model_id':'journal'}
                store.save(records[0],saved)
                original_prepare=client.prepare
                def prepare(r):
                    if r.sa_id==records[1].sa_id:raise MetadataSkip('书目信息冲突')
                    return original_prepare(r)
                client.prepare=prepare
                backends={r.sa_id:Backend(r,candidate) for r in records}
                calls=[]
                class Mux:
                    online=True;pending=None
                    def call(self,action,payload,timeout=90):
                        sid=payload['sa_id'];calls.append((sid,action))
                        if sid==records[0].sa_id and action=='import_excel_scan' and fast:
                            return {'batches':[],'scanned_count':17}
                        if sid==records[0].sa_id and action=='import_excel_check':raise BrowserRejected(action,MISSING)
                        return backends[sid].call(action,payload,timeout)
                result=run_metadata_batch(base,roster,records,Mux(),threading.Event(),retry_skipped=retry,
                                          client=client,excel_builder=self.builder)
                self.assertFalse(result.halted,result.reason);self.assertEqual(result.remaining,0)
                self.assertEqual([x['status'] for x in result.outcomes],['deferred','skip','done'])
                self.assertEqual(store.get(result.roster.records[0]),saved)
                self.assertEqual(result.roster.records[0].remark,'批次待核')
                self.assertEqual(result.roster.records[0].skipped,retry)
                self.assertTrue(result.roster.records[2].done)
                self.assertNotIn((records[0].sa_id,'import_excel_submit'),calls)
                if fast:self.assertNotIn((records[0].sa_id,'import_excel_check'),calls)
                self.assertEqual(backends[records[2].sa_id].calls.count('import_excel_submit'),1)

    def test_retry_all_72_rows_exactly_once_despite_read_rejections(self):
        base,roster=self.roster(72)
        chosen=WorkflowService(base).select(roster,'import','谭勋策',1,'skipped')
        self.assertEqual(len(chosen),72)
        calls=[]
        class ReadOnlyErrors:
            online=True;pending=None
            def call(self,action,payload,timeout=90):
                calls.append((action,payload['sa_id']))
                raise BrowserRejected(action,'[已暂停] 名单 ID 未返回唯一结果')
        result=run_metadata_batch(base,roster,chosen,ReadOnlyErrors(),threading.Event(),retry_skipped=True)
        self.assertFalse(result.halted,result.reason);self.assertEqual(result.remaining,0)
        self.assertEqual(len(result.outcomes),72);self.assertEqual(len({sid for _,sid in calls}),72)
        self.assertEqual(len(calls),72);self.assertTrue(all(action=='status' for action,_ in calls))
        self.assertTrue(all(r.skipped and not r.done for r in result.roster.records))

    def test_transport_timeout_stops_without_touching_tail(self):
        base,roster=self.roster();calls=[]
        class Lost:
            online=True;pending=None
            def call(self,action,payload,timeout=90):
                calls.append(payload['sa_id']);raise SafetyStop('命令超时（扩展没有取得命令），结果不明')
        result=run_metadata_batch(base,roster,roster.records,Lost(),threading.Event(),retry_skipped=True)
        self.assertTrue(result.halted);self.assertEqual(len(calls),1)
        self.assertEqual(result.roster.records,roster.records)

    def test_service_report_distinguishes_checked_deferred_from_unexecuted(self):
        from zero_match import BatchResult
        base,roster=self.roster();records=roster.records
        outcomes=[{'sa_id':r.sa_id,'status':'deferred','message':'批次待核'} for r in records]
        with patch('metadata_pipeline.run_metadata_batch',return_value=BatchResult(roster,outcomes=outcomes,remaining=0)):
            result=WorkflowService(base).run(roster,records,'import','skipped',Mock(online=True),threading.Event(),Mock(),metadata_import=True)
        self.assertFalse(result.halted);self.assertEqual(result.deferred_count,3)
        self.assertIn('待核验 3',result.summary);self.assertIn('未执行 0',result.summary)
        self.assertEqual(json.loads(Path(result.report_path).read_text(encoding='utf-8'))['status'],'finished_with_review')


if __name__=='__main__':unittest.main()
