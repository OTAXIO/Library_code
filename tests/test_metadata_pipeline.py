"""Durability and owner/zero-match guard tests; synthetic data only."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from openpyxl import load_workbook
from core import SafetyStop, file_hash, read_roster
from metadata_sources import MetadataClient, MetadataSkip, merge_metadata
from metadata_pipeline import run_metadata_batch
from roster_write import migrate_status_column
from tests.test_metadata_sources import example
from tests.test_roster_write import make_roster


class Backend:
    def __init__(self, record, candidate):
        self.candidate=candidate;self.row={'saLzkId':record.sa_id,'titleValue':record.title,'doiValue':record.doi,
          'wosValue':'','gh':record.staff_id,'markStatus':'待处理','matchCount':0,'itemId':'','reason':'','remark':''}
        self.phase=0;self.calls=[];self.lost='';self.validation_error=False;self.claimed=True;self.roles_match=True

    def call(self, action, p, timeout=90):
        self.calls.append(action);c=self.candidate
        if action in ('status','search'):
            if action=='search' and not self.row['itemId']:
                raise SafetyStop('零匹配详情为空；关联前只能读取列表状态')
            comparison=[] if not self.row['itemId'] else [{'label':label,'sa':self.row[k],'library':c[f]}
              for label,k,f in [('题名','titleValue','title'),('DOI','doiValue','doi'),('WOS记录号','wosValue','wos')]]
            if comparison:
                comparison.extend([
                    {'label':'认领状态','sa':'Alice Test(00001)','library':'已认领' if self.claimed else '未认领'},
                    {'label':'作者信息','sa':'是否第一作者：否\n是否通讯作者：否',
                     'library':'是否第一作者：否\n是否通讯作者：'+('否' if self.roles_match else '是')},
                    {'label':'交大是否第一单位','sa':'是','library':'是'}])
            return {'metadata_completion_protocol':2,'row':copy.deepcopy(self.row),'comparison':comparison}
        if action=='import_excel_resolve':
            return {'verified':True,'items':[] if self.phase!=2 else [{'id':'1234567890123456789',
              'title':c['title'],'doi':c['doi'],'wos':''}]}
        if action=='import_excel_probe':return {'metadata_excel_protocol':1,'source':'人工提交','model_id':'journal'}
        if action=='import_excel_scan':return {'batches':[]}
        if action in ('import_excel_upload','import_excel_upload_status'):
            if self.validation_error:raise SafetyStop('格式校验待核验')
            result={'uploaded':True,'validated':True,'sha256':p['xlsx_sha256'],'dataset_id':'sjtu','model_id':'journal'}
        elif action=='import_excel_submit':self.phase=1;result={'submitted':True}
        elif action=='import_excel_check':
            if not self.phase:raise SafetyStop('无批次；未重试提交')
            return {'verified':True,'batch':{'id':'batch-one','modelId':'journal','source':'人工提交',
              'instructions':p['instructions'],'total':1,'actual':1 if self.phase==2 else 0,'fail':0,'status':self.phase,
              'increase':1 if self.phase==2 else 0,'duplicateSkip':0,'duplicateIncreaseUpdate':0,'duplicateOverallCoverage':0}}
        elif action=='import_excel_push':self.phase=2;result={'submitted':True}
        elif action=='link':self.row.update(itemId=p['item_id'],matchCount=1);result={'verified':True,'row':copy.deepcopy(self.row)}
        elif action=='prepare_claim':
            return {**self.call('search',p), 'suggested_index':0, 'prepared':{
                'item_id':self.row['itemId'],'staff_id':'00001','sa_text':'Alice Test(00001)',
                'person':{'id':'person-one','wno':'00001','name':'Alice Test','names':['Alice Test']},
                'authors':[{'index':0,'id':'author-one','order':1,'fullname':'Alice Test',
                            'eligible':True,'scholarId':'','relations':[]}]}}
        elif action=='submit_claim':
            self.claimed=True
            result={'row':copy.deepcopy(self.row),'verified':True,'claimed':True,'staff_id':'00001',
                    'scholar_id':'person-one','author':'Alice Test','order':1}
        elif action=='complete':self.row.update(markStatus='已处理',remark=p['note']);result={'verified':True,'row':copy.deepcopy(self.row)}
        else:raise AssertionError(action)
        if self.lost==action:
            self.lost='';raise SafetyStop('模拟回执丢失')
        return result


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name)
        path=self.base/'list.xlsx';make_roster(path,flags=(None,None))
        book=load_workbook(path);s=book.active
        for col,value in {'B2':'谭勋策','D2':'Synthetic verified paper','E2':'10.1234/example','H2':0,'I2':'','K2':''}.items():s[col]=value
        book.save(path);book.close()
        self.roster=migrate_status_column(read_roster(path));self.record=self.roster.records[0]
        _,cr,oa=example();self.client=MetadataClient(self.base/'runtime/metadata-imports/papers')
        self.client.get=Mock(side_effect=[{'message':cr},oa])
        self.candidate=merge_metadata(self.record,cr,oa);self.backend=Backend(self.record,self.candidate)
        self.stop=threading.Event()

    @staticmethod
    def builder(folder,candidate):
        path=folder/'paper.xlsx';path.write_bytes(b'PK synthetic test workbook')
        return path,file_hash(path)

    def run_once(self):
        return run_metadata_batch(self.base,self.roster,[self.record],self.backend,self.stop,
                                  client=self.client,excel_builder=self.builder)

    def test_success_exact_note_source_and_flags_other_owner_unchanged(self):
        other=self.roster.records[1]
        result=self.run_once();self.assertFalse(result.halted,result.reason)
        current=result.roster.records[0]
        self.assertTrue(current.done);self.assertEqual(current.remark,'已入库');self.assertEqual(current.source,'Crossref+OpenAlex')
        self.assertEqual(result.roster.records[1],other)
        self.assertEqual(self.backend.calls.count('import_excel_upload'),1)
        self.assertEqual(self.backend.calls.count('complete'),1)
        self.assertFalse(any(a.startswith('wos_') or 'claim' in a for a in self.backend.calls))

    def test_lost_ack_all_stages_resume_without_duplicate_writes(self):
        for action in ('import_excel_upload','import_excel_submit','import_excel_push','link','complete'):
            with self.subTest(action=action):
                # Independent synthetic folder and transport per uncertainty.
                self.setUp();self.backend.lost=action
                first=self.run_once();self.assertTrue(first.halted)
                self.roster=first.roster;self.record=self.roster.records[0]
                second=self.run_once();self.assertFalse(second.halted,second.reason)
                self.assertTrue(second.roster.records[0].done)
                for write in ('import_excel_upload','import_excel_submit','import_excel_push','link','complete'):
                    self.assertEqual(self.backend.calls.count(write),1,(action,write))

    def test_nonzero_backend_only_flags_skip_and_does_not_add_note(self):
        self.backend.row.update(matchCount=1,itemId='111',remark='')
        result=self.run_once();self.assertTrue(result.roster.records[0].skipped)
        self.assertEqual(result.roster.records[0].remark,'');self.assertEqual(self.backend.calls,['status'])

    def test_skipped_is_not_repeated_in_normal_batch(self):
        self.record=replace(self.record,skipped=True)
        self.roster.records[0]=self.record
        result=self.run_once();self.assertEqual(self.backend.calls,[])
        self.assertEqual(result.remaining,0)

    def test_other_owner_or_nonzero_roster_rejected(self):
        for bad in (replace(self.record,owner='其他人'),replace(self.record,matches=1)):
            self.roster.records[0]=bad
            with self.assertRaises(SafetyStop):run_metadata_batch(self.base,self.roster,[bad],self.backend,self.stop)
        self.assertEqual(self.backend.calls,[])

    def test_old_protocol_stops_before_source_queries_or_any_write(self):
        original=self.backend.call
        def call(action,payload,timeout=90):
            result=original(action,payload,timeout)
            if action=='status':result['metadata_completion_protocol']=1
            return result
        self.backend.call=call
        result=self.run_once();self.assertTrue(result.halted);self.assertIn('0.4.28',result.reason)
        self.assertEqual(self.backend.calls,['status']);self.client.get.assert_not_called()
        self.assertEqual(result.roster.sha256,self.roster.sha256)

    def test_pause_after_upload_saves_receipt_and_never_submits(self):
        original=self.backend.call
        def call(action,payload,timeout=90):
            result=original(action,payload,timeout)
            if action=='import_excel_upload':self.stop.set()
            return result
        self.backend.call=call
        result=self.run_once();self.assertTrue(result.halted)
        self.assertNotIn('import_excel_submit',self.backend.calls)
        self.stop.clear();self.roster=result.roster;self.record=self.roster.records[0]
        second=self.run_once();self.assertFalse(second.halted,second.reason)
        self.assertEqual(self.backend.calls.count('import_excel_upload'),1)

    def test_newly_linked_unclaimed_author_is_claimed_before_closure(self):
        self.backend.claimed=False
        result=self.run_once();self.assertFalse(result.halted,result.reason)
        self.assertTrue(result.roster.records[0].done)
        calls=self.backend.calls
        self.assertEqual(calls.count('submit_claim'),1)
        self.assertLess(calls.index('link'),calls.index('submit_claim'))
        self.assertLess(calls.index('submit_claim'),calls.index('complete'))

    def test_unknown_claim_ack_never_reclaims_or_closes(self):
        self.backend.claimed=False;self.backend.lost='submit_claim'
        first=self.run_once();self.assertTrue(first.halted)
        self.roster=first.roster;self.record=self.roster.records[0]
        second=self.run_once();self.assertTrue(second.halted)
        self.assertFalse(second.roster.records[0].done)
        self.assertEqual(self.backend.calls.count('submit_claim'),1)
        self.assertNotIn('complete',self.backend.calls)
        self.assertEqual(self.backend.calls.count('import_excel_submit'),1)

    def test_role_disagreement_never_claims_or_closes(self):
        self.backend.claimed=False;self.backend.roles_match=False
        result=self.run_once();self.assertTrue(result.halted)
        self.assertIn('作者角色',result.reason)
        self.assertNotIn('prepare_claim',self.backend.calls)
        self.assertNotIn('complete',self.backend.calls)
        self.assertFalse(result.roster.records[0].done)

    def test_resume_completed_then_pending_paper_only_reads_previous_batch(self):
        for index,pause_at in enumerate(('status','import_excel_upload','import_excel_submit')):
            with self.subTest(pause_at=pause_at):
                if index:self.setUp()
                book=load_workbook(self.roster.path);sheet=book.active
                for cell,value in {'B3':'谭勋策','D3':'Second synthetic paper','E3':'10.1234/second',
                                   'G3':'00001','H3':0,'I3':'','K3':''}.items():sheet[cell]=value
                book.save(self.roster.path);book.close()
                current=read_roster(self.roster.path);first,second=current.records
                _,cr1,oa1=example();cr2=copy.deepcopy(cr1);oa2=copy.deepcopy(oa1)
                cr2.update(DOI=second.doi,title=[second.title]);oa2.update(doi='https://doi.org/'+second.doi,title=second.title)
                self.client.get=Mock(side_effect=[{'message':cr1},oa1,{'message':cr2},oa2])
                other=Backend(second,merge_metadata(second,cr2,oa2));backends={first.sa_id:self.backend,second.sa_id:other}
                if pause_at!='status':other.lost=pause_at
                class Queue:
                    pause=pause_at=='status'
                    def call(queue,action,payload,timeout=90):
                        if queue.pause and payload['sa_id']==second.sa_id:
                            queue.pause=False;raise SafetyStop('第二篇启动前暂停')
                        return backends[payload['sa_id']].call(action,payload,timeout)
                queue=Queue()
                before=run_metadata_batch(self.base,current,[first,second],queue,self.stop,client=self.client,excel_builder=self.builder)
                self.assertTrue(before.halted);self.assertTrue(before.roster.records[0].done)
                previous=len(self.backend.calls)
                after=run_metadata_batch(self.base,before.roster,before.roster.records,queue,self.stop,
                                         client=self.client,excel_builder=self.builder)
                self.assertFalse(after.halted,after.reason);self.assertTrue(all(r.done for r in after.roster.records))
                self.assertEqual(self.backend.calls[previous:],['import_excel_check'] if pause_at=='status' else [])
                for backend in (self.backend,other):
                    for action in ('import_excel_upload','import_excel_submit','import_excel_push','link','complete'):
                        self.assertEqual(backend.calls.count(action),1,(action,backend.calls))


if __name__=='__main__':unittest.main()
