import tempfile,copy
from pathlib import Path
from unittest import TestCase
from unittest.mock import Mock
from openpyxl import load_workbook
from core import read_roster,SafetyStop
from approval import current_claim_record,complete_claim
from roster_write import migrate_status_column
from tests.test_roster_write import make_roster


class CurrentMatchTests(TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'list.xlsx';make_roster(self.path,flags=(2,))
        book=load_workbook(self.path);s=book.active
        for col,value in {'B2':'谭勋策','H2':0,'I2':'','K2':'','L2':''}.items():s[col]=value
        book.save(self.path);book.close()
        self.roster=migrate_status_column(read_roster(self.path));self.record=self.roster.records[0]
        self.row={'saLzkId':self.record.sa_id,'gh':self.record.staff_id,'titleValue':self.record.title,
            'doiValue':'','wosValue':'','matchCount':1,'itemId':'1234567890123456789','markStatus':'待处理','reason':'作者不一致','remark':''}

    def test_live_route_does_not_change_original_key_or_exported_fields(self):
        live=current_claim_record(self.record,self.row,True)
        self.assertEqual(live.matches,1);self.assertEqual(live.reason,'作者不一致')
        self.assertEqual(self.record.matches,0)
        comp=[{'label':'认领状态','sa':'测试员(00001)','library':'已认领'},{'label':'作者信息','sa':'一致','library':'一致'}]
        bridge=Mock();bridge.call.side_effect=[{'row':self.row,'comparison':comp},{'verified':True,'row':{**self.row,'markStatus':'已处理','remark':'已认领'}}]
        with self.assertRaises(SafetyStop):complete_claim(self.roster,self.record,bridge,self.row,comp,reviewed=True)
        result=complete_claim(self.roster,self.record,bridge,self.row,comp,reviewed=True,current_match=True)
        after=result.completion.roster.records[0]
        self.assertTrue(after.done);self.assertEqual(after.matches,0);self.assertEqual(after.reason,'');self.assertEqual(after.key,self.record.key)

    def test_changed_identity_or_nonunique_match_cannot_enable_claim(self):
        for key,value in [('gh','99999'),('titleValue','another title'),('doiValue','10.1234/changed'),('matchCount',2),('itemId','1,2')]:
            with self.subTest(key=key),self.assertRaises(SafetyStop):current_claim_record(self.record,{**self.row,key:value},True)
