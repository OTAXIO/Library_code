import copy
from html import escape
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core import SafetyStop, digest
from publisher_roles import parse_correspondence, correspondence_evidence, repair_correspondence


class PublisherRoleTests(unittest.TestCase):
    def setUp(self):
        self.c = {'doi': '10.20517/demo.2026.1', 'title': 'An exact synthetic paper',
                  'author_names': ['Alice Test', 'Bob Example'], 'sha256': 'c'*64}
        self.url = 'https://www.oaepublish.com/articles/demo.2026.1'
        self.notes = 'Correspondence to: Dr. Bob Example, Shanghai Jiao Tong University, China. E-mail: example@example.org'

    def html(self, **changed):
        meta = {'citation_doi': [self.c['doi']], 'citation_title': [self.c['title']],
                'citation_fulltext_html_url': [self.url], 'citation_author': self.c['author_names'],
                'citation_author_institution': [self.notes]}
        meta.update(changed)
        return ''.join(f'<meta name="{k}" content="{escape(v, quote=True)}">'
                       for k, values in meta.items() for v in values).encode()

    def test_positive_correspondence_never_assigns_first_or_joint_role(self):
        proof = parse_correspondence(self.html(), self.url, self.c)
        self.assertEqual(proof['correspondent_names'], ['Bob Example'])
        self.assertNotIn('first', proof); self.assertNotIn('commonCorrespondent', proof)

    def test_wrong_doi_title_author_order_or_url_is_rejected(self):
        for changes in ({'citation_doi': ['10.20517/other']}, {'citation_title': ['Different paper']},
                        {'citation_author': list(reversed(self.c['author_names']))},
                        {'citation_fulltext_html_url': ['https://untrusted.example/paper']}):
            with self.subTest(changes=changes), self.assertRaises(SafetyStop):
                parse_correspondence(self.html(**changes), self.url, self.c)
        with self.assertRaises(SafetyStop):
            parse_correspondence(self.html(), self.url+'?changed=1', self.c)

    def test_ambiguous_missing_or_nonbyline_correspondence_is_not_guessed(self):
        for notes in ([], [self.notes, self.notes], ['These authors contributed equally.'],
                      ['Correspondence to: Dr. Unknown Person, University.']):
            with self.subTest(notes=notes), self.assertRaises(SafetyStop):
                parse_correspondence(self.html(citation_author_institution=notes), self.url, self.c)

    def test_archived_source_is_reparsed_and_tamper_is_rejected_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp); raw=self.html(); evidence=parse_correspondence(raw,self.url,self.c)
            (folder/'publisher-correspondence.html').write_bytes(raw)
            (folder/'publisher-correspondence.json').write_text(json.dumps(evidence))
            with patch('publisher_roles.public_open',side_effect=AssertionError('must not refetch')):
                self.assertEqual(correspondence_evidence(folder,self.c),evidence)
                (folder/'publisher-correspondence.html').write_bytes(raw+b' changed')
                with self.assertRaises(SafetyStop):correspondence_evidence(folder,self.c)

    def test_only_exact_claimed_staff_can_prepare_role_write_and_unknown_is_not_replayed(self):
        from types import SimpleNamespace
        record=SimpleNamespace(staff_id='00123'); evidence=parse_correspondence(self.html(),self.url,self.c)
        row={'id':'sa'}; state={'path':'synthetic/paper.xlsx','candidate':self.c,'item_id':'item'}
        person={'id':'scholar','wno':'00123'}
        response={'verified':True,'claimed':True,'staff_id':'00123','scholar_id':'scholar',
                  'author':'Bob Example','order':2,'prepared':{'person':person}}
        saved=[]; calls=[]
        def call(action,payload):
            calls.append(action)
            if action=='status':return {'row':row,'publisher_correspondent_protocol':1}
            if action=='inspect_claim':return response
            self.assertEqual(saved[-1]['correspondence_repair']['phase'],'intent')
            return {'verified':True,'staff_id':'00123','scholar_id':'scholar',
                    'order':2,'item_id':'item','correspondent':True}
        store=SimpleNamespace(save=lambda r,s:saved.append(copy.deepcopy(s)))
        comparison=[{'label':'认领状态','sa':'Test(00123)'},
            {'label':'作者信息','sa':'是否通讯作者：是','library':'是否通讯作者：否'}]
        with patch('publisher_roles.correspondence_evidence',return_value=evidence):
            self.assertTrue(repair_correspondence(record,comparison,row,state,store,call))
            self.assertEqual(calls,['status','inspect_claim','repair_correspondent'])
            self.assertEqual(state['correspondence_repair']['phase'],'verified')
            calls.clear();self.assertFalse(repair_correspondence(record,comparison,row,state,store,call))
            self.assertEqual(calls,['status','inspect_claim'])
            del state['correspondence_repair'];response['staff_id']='123'
            calls.clear();self.assertFalse(repair_correspondence(record,comparison,row,state,store,call))
            self.assertEqual(calls,['status','inspect_claim']);self.assertNotIn('correspondence_repair',state)

    def test_negative_sa_or_unknown_role_does_not_fetch_or_write(self):
        from types import SimpleNamespace
        for roles in ([], [{'label':'作者信息','sa':'是否通讯作者：否','library':'是否通讯作者：是'}],
                      [{'label':'作者信息','sa':'是否通讯作者：未知','library':'是否通讯作者：否'}]):
            with patch('publisher_roles.correspondence_evidence',side_effect=AssertionError('must not fetch')):
                self.assertFalse(repair_correspondence(SimpleNamespace(),roles,{}, {},None,None))


if __name__=='__main__':unittest.main()
