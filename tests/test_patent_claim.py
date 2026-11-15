"""Patent roster and multi-item workflow tests; entirely synthetic and offline."""
import copy
import tempfile
import threading
import unittest
from pathlib import Path
from openpyxl import Workbook, load_workbook
from core import PATENT_HEADERS, SafetyStop, read_roster
from patent_claim import run_patent_batch
from roster_write import mark_skipped_many
from workflow_service import WorkflowService


def make_patent(path, count=2):
    book = Workbook(); sheet = book.active; sheet.title = '专利'
    sheet.append(['备注'] + list(PATENT_HEADERS.values()) + ['是否识别'])
    values = dict(owner='测试员', sa_id='patent-001', title='合成专利', doi='CN202110613751.0',
                  wos='CN113221193B', staff_id='00001', matches=count,
                  item_ids=','.join(str(101+i) for i in range(count)), mark='待处理', reason='匹配数据未认领')
    sheet.append([None] + [values[k] for k in PATENT_HEADERS] + [None])
    book.save(path); book.close()


class PatentPage:
    online = True
    def __init__(self, record):
        self.record = record; self.calls = []; self.claimed = set(); self.fail = None; self.lost = False
        self.page_kind = 'patent'
        self.row = dict(saPatentId=record.sa_id, gh=record.staff_id, matchCount=record.matches,
                        itemId=record.item_ids, markStatus='待处理', remark='')
    def call(self, action, payload, **kwargs):
        self.calls.append((action, copy.deepcopy(payload)))
        row = copy.deepcopy(self.row)
        source = '测试员(00001)'
        comparison = [{'label':'认领状态','sa':source,'library':'未认领'}]
        if action == 'status':
            return dict(row=row, page_kind=self.page_kind, need_import_protocol=1)
        if action == 'search':
            return dict(row=row, left=dict(id=self.record.sa_id, xm='测试员', gh='00001'),
                        cards=[dict(itemId=x, claimed=x in self.claimed, staff_id='00001' if x in self.claimed else '')
                               for x in self.record.item_ids.split(',') if x])
        item = payload.get('item_id')
        if action == 'inspect_claim':
            return dict(row=row, comparison=comparison, verified=True, claimed=False, staff_id='00001')
        if action == 'prepare_claim':
            authors = [dict(index=0, order=1, fullname='另一个人' if item == self.fail else '测试员',
                            eligible=True, scholarId='', relations=[])]
            return dict(row=row, comparison=comparison, suggested_index=None,
                        prepared=dict(item_id=item, staff_id='00001', sa_text=source, authors=authors,
                                      person=dict(id='scholar-1',wno='00001',name='测试员',names=['测试员'])))
        if action == 'submit_claim':
            if self.lost: raise SafetyStop('合成丢失回执')
            self.claimed.add(item)
            return dict(row=row, verified=True, claimed=True, staff_id='00001',scholar_id='scholar-1',author='测试员',order=1)
        if action == 'complete':
            self.row.update(markStatus='已处理', remark=payload['note'])
            return dict(row=copy.deepcopy(self.row), verified=True)
        raise AssertionError(action)


class PatentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)/'list.xlsx'; make_patent(self.path)
        self.roster = read_roster(self.path, kind='patent'); self.page = PatentPage(self.roster.records[0])
    def run_batch(self, **kw):
        return run_patent_batch(self.roster, self.roster.records, self.page, '测试员', **kw)
    def reset_count(self, count):
        make_patent(self.path, count); self.roster=read_roster(self.path, kind='patent');self.page=PatentPage(self.roster.records[0])
    def test_header_aliases_and_optional_query(self):
        book=load_workbook(self.path); sheet=book.active
        for cell in sheet[1]:
            if cell.value=='匹配到的条目数量':cell.value=' 匹配条目数 '
            if cell.value=='标记为待处理原因':cell.value='待处理原因'
            if cell.value=='申请(专利)号':cell.value='申请（专利）号'
        book.save(self.path);book.close()
        roster=read_roster(self.path, kind='patent')
        self.assertEqual(roster.records[0].query,'');self.assertEqual(roster.kind,'patent')
    def test_missing_column_and_ambiguous_workbooks(self):
        book=load_workbook(self.path);book.active.cell(1,3,'坏表头');book.save(self.path);book.close()
        with self.assertRaisesRegex(SafetyStop,'sa_patent表ID|专利名单'):read_roster(self.path, kind='patent')
        make_patent(self.path);make_patent(self.path.with_name('patent.xlsx'))
        with self.assertRaisesRegex(SafetyStop,'不唯一'):read_roster(self.path, kind='patent')
    def test_missing_title_reports_column(self):
        book=load_workbook(self.path);book.active.cell(1,4,'坏表头');book.save(self.path);book.close()
        with self.assertRaisesRegex(SafetyStop,'专利名称'):read_roster(self.path, kind='patent')
    def test_separate_patent_file_and_shared_workbook_write(self):
        self.path.rename(self.path.with_name('patent.xlsx'))
        roster=read_roster(self.path,kind='patent');self.assertEqual(roster.path.name,'patent.xlsx')
        result=mark_skipped_many(roster,roster.records,reasons={'patent-001':'合成跳过'}).roster
        self.assertTrue(result.records[0].skipped);self.assertEqual(result.kind,'patent')
    def test_two_items_claim_then_close(self):
        result=self.run_batch()
        self.assertFalse(result.halted,result.reason);self.assertEqual(result.completed_ids,('patent-001',))
        self.assertEqual([p['item_id'] for a,p in self.page.calls if a=='submit_claim'],['101','102'])
        self.assertEqual([p['note'] for a,p in self.page.calls if a=='complete'],['已认领'])
        self.assertEqual(result.roster.records[0].remark,'已认领');self.assertTrue(result.roster.records[0].done)
    def test_second_item_failure_keeps_first_without_completion(self):
        self.page.fail='102'; result=self.run_batch()
        self.assertFalse(result.halted,result.reason);self.assertIn('第 2 个条目',result.skipped['patent-001'])
        self.assertEqual(self.page.claimed,{'101'});self.assertNotIn('complete',[a for a,p in self.page.calls])
    def test_retry_partial_failure_only_claims_remaining_item(self):
        self.page.fail='102';first=self.run_batch();self.roster=first.roster
        self.page.fail=None;self.page.calls=[]
        result=self.run_batch(retry_skipped=True)
        self.assertFalse(result.halted,result.reason);self.assertTrue(result.roster.records[0].done)
        self.assertEqual([p['item_id'] for a,p in self.page.calls if a=='submit_claim'],['102'])
    def test_web_staff_trailing_space_is_trimmed_for_identity(self):
        self.page.row['gh']='00001              '
        result=self.run_batch();self.assertFalse(result.halted,result.reason)
        self.assertEqual(result.completed_ids,('patent-001',))
    def test_same_staff_card_is_not_submitted_again(self):
        self.page.claimed={'101'};result=self.run_batch();self.assertFalse(result.halted,result.reason)
        self.assertEqual([p['item_id'] for a,p in self.page.calls if a=='prepare_claim'],['102'])
    def test_zero_reuses_need_import(self):
        self.reset_count(0);result=self.run_batch();self.assertFalse(result.halted,result.reason)
        self.assertEqual([p['note'] for a,p in self.page.calls if a=='complete'],['需导入'])
    def test_three_is_local_only(self):
        self.reset_count(3);result=self.run_batch();self.assertEqual(self.page.calls,[])
        self.assertEqual(result.roster.records[0].remark,'匹配到 3 条，未结项')
    def test_mismatch_only_status(self):
        for key,value in [('saPatentId','other'),('gh','00002'),('matchCount',1),('itemId','101,999')]:
            old=self.page.row[key];self.page.row[key]=value
            result=self.run_batch();self.assertEqual(result.skipped['patent-001'],'名单与网页不一致')
            self.assertEqual([a for a,p in self.page.calls],['status']);self.page.calls=[]
            self.roster=result.roster
            self.page.row[key]=old
            # Use a fresh fixture so the next mismatch is pending.
            make_patent(self.path);self.roster=read_roster(self.path,kind='patent')
    def test_wrong_page_sends_only_status(self):
        self.page.page_kind='paper';result=self.run_batch();self.assertTrue(result.halted)
        self.assertEqual([a for a,p in self.page.calls],['status'])
    def test_lost_claim_receipt_persists_intent_no_resubmit(self):
        self.page.lost=True;result=self.run_batch();self.assertTrue(result.halted)
        result=self.run_batch();self.assertTrue(result.halted)
        self.assertEqual(len([a for a,p in self.page.calls if a=='submit_claim']),1)
    def test_cancel_after_preparation_prevents_submit(self):
        result=self.run_batch(cancel=lambda:any(a=='prepare_claim' for a,p in self.page.calls))
        self.assertTrue(result.cancelled);self.assertNotIn('submit_claim',[a for a,p in self.page.calls])
    def test_processed_syncs_without_writes(self):
        self.page.row.update(markStatus='已处理',remark='已认领');result=self.run_batch()
        self.assertEqual(result.synced_ids,('patent-001',));self.assertEqual([a for a,p in self.page.calls],['status'])
    def test_service_selection_scope_and_wrong_page(self):
        service=WorkflowService(self.path.parent)
        self.assertEqual(service.select(self.roster,'patent','测试员',1,'pending'),self.roster.records)
        self.page.page_kind='paper'
        result=service.run(self.roster,self.roster.records,'patent','pending',self.page,threading.Event(),lambda _:None,owner='测试员')
        self.assertTrue(result.halted);self.assertEqual([a for a,p in self.page.calls],['status'])


class PatentUITests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from app import App
        from unittest.mock import Mock
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.root=tk.Tk();self.root.withdraw()
        self.app=App(self.root,service=WorkflowService(self.base),auto_load=False,bridge=Mock(online=False))
        self.addCleanup(self.app.close)
    def test_patent_file_works_without_paper_roster(self):
        make_patent(self.base/'patent.xlsx')
        self.app.reload_roster();self.app.notebook.select(self.app.pages['patent']);self.app.render()
        self.assertEqual(self.app.trees['patent'].get_children(),('patent-001',))
        self.assertEqual(self.app.start_button['text'],'开始专利认领')
        self.assertEqual(str(self.app.start_button['state']),'normal')
        self.assertEqual(self.app.trees['patent'].heading('title')['text'],'专利名称')
        self.app.bridge.call.assert_not_called()
    def test_missing_roster_disables_start_and_explains_in_page(self):
        self.app.reload_roster();self.app.notebook.select(self.app.pages['patent']);self.app.render()
        self.assertEqual(str(self.app.start_button['state']),'disabled')
        self.assertIn('未找到专利名单',self.app.details['patent'].get('1.0','end'))
        self.app.start();self.app.bridge.call.assert_not_called()
    def test_shared_workbook_migration_and_write_preserve_paper_sheet(self):
        from tests.test_need_import import make_roster
        from core import file_hash
        make_roster(self.base/'list.xlsx',[('测试员',0)])
        make_patent(self.base/'patent.xlsx')
        source=load_workbook(self.base/'patent.xlsx');book=load_workbook(self.base/'list.xlsx')
        target=book.create_sheet('专利')
        for row in source.active.iter_rows(values_only=True):target.append(row)
        # Exercise migration after the paper has been loaded.
        target.delete_cols(target.max_column)
        book.save(self.base/'list.xlsx');book.close();source.close();(self.base/'patent.xlsx').unlink()
        self.app.reload_roster()
        self.app.rosters['claim'].assert_unchanged();self.app.rosters['patent'].assert_unchanged()
        before=read_roster(self.base/'list.xlsx').records
        roster=self.app.rosters['patent']
        result=mark_skipped_many(roster,roster.records,reasons={'patent-001':'合成跳过'}).roster
        self.assertEqual(result.kind,'patent');self.assertTrue(result.records[0].skipped)
        self.assertEqual(read_roster(self.base/'list.xlsx').records,before)
