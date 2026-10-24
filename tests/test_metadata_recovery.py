"""One-time operator recovery with synthetic files and no business writes."""
import copy,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from core import SafetyStop,digest,file_hash
from metadata_sources import merge_metadata
from metadata_recovery import recover_missing_import
from tests.test_metadata_sources import example
from zero_match import WorkflowStore


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.base=Path(self.temp.name)
        self.record,self.cr,self.oa=example();self.roster=SimpleNamespace(records=[self.record],assert_unchanged=Mock())
        self.oa['authorships'][0]['raw_affiliation_strings'].append('These authors contributed equally')
        self.corrected=merge_metadata(self.record,self.cr,self.oa)
        old=copy.deepcopy(self.corrected);old['addresses'].append('These authors contributed equally')
        old['affiliation']=';'.join(old['addresses']);old['author_mapping']=[[1,2]]
        old['fields']['作者']='Alice Test(1,2)';old['fields']['作者单位']+=';(2)These authors contributed equally'
        old['sha256']=digest({k:v for k,v in old.items() if k!='sha256'})
        self.root=self.base/'runtime/metadata-imports';folder=self.root/'papers/original';folder.mkdir(parents=True)
        path=folder/'paper.xlsx';path.write_bytes(b'PK synthetic old workbook')
        (folder/'metadata.json').write_text(json.dumps({'record_key':self.record.key,'candidate':old,
            'crossref':self.cr,'openalex':self.oa}),encoding='utf8')
        self.original={'phase':'import_intent','candidate':old,'path':str(path),'xlsx_sha256':file_hash(path),
            'model_id':'journal','instructions':'SA补充-'+self.record.sa_id,'upload':{'uploaded':True,'validated':True}}
        self.store=WorkflowStore(self.root);self.store.save(self.record,self.original)
        self.calls=[];self.scans=0;self.has_batch=False;self.has_paper=False;self.protocol=1;self.change_total=False
        self.bridge=SimpleNamespace(call=self.call,online=True,pending=None)

    def call(self,action,p,timeout=90):
        self.calls.append(action)
        if action=='status':return {'row':{'saLzkId':self.record.sa_id,'titleValue':self.record.title,
            'doiValue':self.record.doi,'wosValue':'','gh':self.record.staff_id,'markStatus':'待处理','matchCount':0,'itemId':''}}
        if action=='import_excel_scan':
            self.scans+=1
            return {'batches':[{'id':'old-batch'}] if self.has_batch else [],'scan_protocol':self.protocol,
                'all_sources':True,'instructions':self.original['instructions'],
                'scanned_count':17+(1 if self.change_total and self.scans==2 else 0)}
        if action=='import_excel_resolve':return {'verified':True,'library_resolution_protocol':2,
            'items':[{'id':'existing'}] if self.has_paper else []}
        if action=='import_excel_probe':return {'metadata_excel_protocol':1,'source':'人工提交','model_id':'journal'}
        raise AssertionError('A recovery check must not write: '+action)

    @staticmethod
    def build(folder,candidate):
        path=folder/'paper.xlsx';path.write_bytes(b'PK repaired synthetic workbook');return path,file_hash(path)

    def recover(self,**changes):
        return recover_missing_import(self.base,self.roster,self.record,self.bridge,
            **{'explicitly_requested':True,'builder':self.build,**changes})

    def test_immutable_old_intent_is_archived_before_one_new_prepared_attempt(self):
        original_bytes=Path(self.original['path']).read_bytes();new=self.recover()
        self.assertEqual(new['phase'],'prepared');self.assertEqual(new['candidate'],self.corrected)
        self.assertEqual(Path(self.original['path']).read_bytes(),original_bytes)
        self.assertNotEqual(new['path'],self.original['path']);self.assertNotIn('upload',new)
        report=Path(new['recovery']['evidence_path']);archive=json.loads(report.read_text(encoding='utf8'))
        self.assertEqual(archive['old_state'],self.original)
        self.assertEqual(archive['evidence']['old_request_outcome'],'unknown')
        self.assertTrue((report.parent/'original-journal.sqlite3').is_file())
        self.assertEqual(self.calls,['status','import_excel_scan','import_excel_resolve','import_excel_probe',
                                     'import_excel_scan','status'])

    def test_no_explicit_request_leaves_original_and_calls_nothing(self):
        with self.assertRaises(SafetyStop):self.recover(explicitly_requested=False)
        self.assertEqual(self.store.get(self.record),self.original);self.assertEqual(self.calls,[])

    def test_any_existing_batch_or_library_paper_blocks_new_attempt(self):
        for kind in ('has_batch','has_paper'):
            with self.subTest(kind=kind):
                setattr(self,kind,True)
                with self.assertRaises(SafetyStop):self.recover()
                self.assertEqual(self.store.get(self.record),self.original)
                setattr(self,kind,False)

    def test_incomplete_or_changing_all_sources_scan_is_not_absence(self):
        self.protocol=True
        with self.assertRaises(SafetyStop):self.recover()
        self.assertEqual(self.store.get(self.record),self.original)
        self.protocol=1;self.scans=0;self.change_total=True
        with self.assertRaises(SafetyStop):self.recover()
        self.assertEqual(self.store.get(self.record),self.original)

    def test_pending_command_offline_and_pause_never_reset_original(self):
        for changes in ({'online':False},{'pending':{'id':'in-flight'}}):
            with self.subTest(changes=changes):
                for k,v in changes.items():setattr(self.bridge,k,v)
                with self.assertRaises(SafetyStop):self.recover()
                self.assertEqual(self.store.get(self.record),self.original)
                self.bridge.online=True;self.bridge.pending=None
        with self.assertRaises(SafetyStop):self.recover(cancel=lambda:True)
        self.assertEqual(self.store.get(self.record),self.original)

    def test_repeated_flag_resumes_new_unknown_intent_and_never_resets_it_again(self):
        state=self.recover();state['phase']='import_intent';self.store.save(self.record,state);self.calls.clear()
        self.assertEqual(self.recover(),state);self.assertEqual(self.calls,[])

    def test_altered_original_excel_and_journal_batch_block_recovery(self):
        p=Path(self.original['path']);old=p.read_bytes();p.write_bytes(b'changed')
        with self.assertRaises(SafetyStop):self.recover()
        p.write_bytes(old);state={**self.original,'batch':{'id':'known'}};self.store.save(self.record,state)
        with self.assertRaises(SafetyStop):self.recover()
        self.assertEqual(self.store.get(self.record),state)

    def test_concurrent_journal_update_is_never_overwritten(self):
        def build(folder,candidate):
            self.store.save(self.record,{**self.original,'phase':'linked'})
            return self.build(folder,candidate)
        with self.assertRaises(SafetyStop):self.recover(builder=build)
        self.assertEqual(self.store.get(self.record)['phase'],'linked')
