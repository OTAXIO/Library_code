"""Immutable synthetic proceedings; no real network, roster or submissions."""
import copy,json,tempfile,unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
from core import SafetyStop,file_hash
from primary_metadata import rebuild_primary
from publisher_roles import correspondence_evidence
from tests.test_metadata_sources import example


def fixture(folder):
    record=replace(example()[0],doi='')
    url='https://aclanthology.org/2025.acl-long.1/'
    meta={'title':[record.title],'doi':['10.18653/v1/2025.acl-long.1'],
        'author':['Bob Example','Alice Test'],
        'conference_title':['Proceedings of the Annual Meeting of the Association for Computational Linguistics'],
        'publication_date':['2025/7'],'firstpage':['100'],'lastpage':['110'],'pdf_url':[url.rstrip('/')+'.pdf']}
    (folder/'source.html').write_text(''.join(f'<meta name="citation_{k}" content="{v}">' for k,vs in meta.items() for v in vs),encoding='utf8')
    writer=PdfWriter();page=writer.add_blank_page(width=612,height=792)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
    stream=DecodedStreamObject();lines=[record.title,'Alice Test1/Letter Bob Example2',
        '1Shanghai Jiao Tong University 2Other University','alicetest@example.invalid',
        'Abstract','Proceedings of the Association for Computational Linguistics 2025 100']
    stream.set_data(('BT /F1 12 Tf 40 760 Td '+' '.join(('0 -20 Td ' if i else '')+'('+v+') Tj' for i,v in enumerate(lines))+' ET').encode())
    page[NameObject('/Contents')]=writer._add_object(stream);writer.write(folder/'source.pdf')
    evidence={'version':2,'reviewed':True,'evidence_type':'publisher-acl-v1','documents':[
        {'path':n,'url':u,'sha256':file_hash(folder/n)} for n,u in [('source.html',url),('source.pdf',url.rstrip('/')+'.pdf')]],
        'facts':{'authors':[{'name':'Alice Test','byline':'Alice Test1/Letter','affiliations':[1]},
            {'name':'Bob Example','byline':'Bob Example2','affiliations':[2]}],
            'affiliations':['Shanghai Jiao Tong University','Other University']}}
    return record,evidence


class ACLTests(unittest.TestCase):
    def test_printed_order_and_positive_envelope_override_index_order_without_guessing_roles(self):
        with tempfile.TemporaryDirectory() as temp:
            f=Path(temp);r,e=fixture(f);c=rebuild_primary(r,f,e)
            self.assertEqual(c['author_names'],['Alice Test','Bob Example'])
            self.assertEqual(c['correspondent_names'],['Alice Test'])
            self.assertTrue(c['publisher_index_order_differs'])
            self.assertEqual(c['fields']['作者'],'Alice Test(1);Bob Example(2)')
            self.assertNotIn('第一作者',c['fields'])
            (f/'metadata.json').write_text(json.dumps({'primary':e,'candidate':c}),encoding='utf8')
            with patch('publisher_roles.public_open',side_effect=AssertionError('no refetch')):
                proof=correspondence_evidence(f,c)
            self.assertEqual(proof['protocol'],2);self.assertEqual(proof['correspondent_names'],['Alice Test'])

    def test_hash_order_mapping_name_doi_and_publisher_conflicts_block(self):
        with tempfile.TemporaryDirectory() as temp:
            f=Path(temp);r,e=fixture(f)
            edits=[lambda a:a.update(reviewed=False),lambda a:a['facts']['authors'].reverse(),
                lambda a:a['facts']['authors'][0].update(affiliations=[2]),
                lambda a:a['facts']['authors'][0].update(name='Different Person'),
                lambda a:a['documents'][1].update(url='https://arxiv.org/pdf/0000.1'),
                lambda a:a['documents'][0].update(sha256='0'*64)]
            for edit in edits:
                changed=copy.deepcopy(e);edit(changed)
                with self.subTest(edit=edit),self.assertRaises(SafetyStop):rebuild_primary(r,f,changed)
            with self.assertRaises(SafetyStop):rebuild_primary(replace(r,doi='10.1234/other'),f,e)


if __name__=='__main__':unittest.main()
