"""Synthetic publisher evidence only; no real sources, roster or network."""
import copy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from core import SafetyStop, file_hash, read_roster
from metadata_identity import MetadataSkip, title_key, legacy_title_key
from metadata_sources import MetadataClient
from metadata_pipeline import resolved, run_metadata_batch
from primary_metadata import rebuild_primary, trusted_page
from tests.test_metadata_sources import example
from tests.test_metadata_pipeline import Backend
from tests.test_roster_write import make_roster
from roster_write import migrate_status_column
from openpyxl import load_workbook


def synthetic(folder):
    record, _, _ = example()
    record = replace(record, doi='')
    page='https://proceedings.iclr.cc/paper_files/paper/2025/hash/'+'a'*32+'-Abstract-Conference.html'
    pdf=page.replace('/hash/','/file/').replace('-Abstract-Conference.html','-Paper-Conference.pdf')
    metas={'title':record.title,'author':'Test, Alice','journal_title':'International Conference on Learning Representations',
           'publication_date':'2025-04-24','pdf_url':pdf,'firstpage':'100','lastpage':'109'}
    (folder/'source.html').write_text(''.join(f'<meta name="citation_{key}" content="{value}">' for key,value in metas.items()),encoding='utf-8')
    writer=PdfWriter();page_obj=writer.add_blank_page(width=612,height=792)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),
                           NameObject('/BaseFont'):NameObject('/Helvetica')})
    page_obj[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
    content=DecodedStreamObject()
    lines=['Published as a conference paper at ICLR 2025',record.title,'Alice Test1','1Shanghai Jiao Tong University']
    content.set_data(('BT /F1 12 Tf 40 760 Td '+' '.join(('0 -20 Td ' if i else '')+'('+line+') Tj' for i,line in enumerate(lines))+' ET').encode('ascii'))
    page_obj[NameObject('/Contents')]=writer._add_object(content)
    writer.write(folder/'source.pdf')
    evidence={'version':1,'reviewed':True,'documents':[
        {'path':name,'url':url,'sha256':file_hash(folder/name)} for name,url in [('source.html',page),('source.pdf',pdf)]],
        'facts':{'authors':[{'name':'Alice Test','byline':'Alice Test1','affiliations':[1]}],
                 'affiliations':['Shanghai Jiao Tong University']}}
    return record,evidence


class PrimaryTests(unittest.TestCase):
    def test_original_registry_keys_still_resolve_after_whitespace_policy_update(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'primary-evidence'/hashlib.sha256(legacy_title_key(example()[0].title).encode()).hexdigest()
            source.mkdir(parents=True);record,evidence=synthetic(source)
            (source/'evidence.json').write_text(json.dumps(evidence),encoding='utf8')
            client=MetadataClient(root/'papers')
            with patch.object(client.discovery,'discover',side_effect=AssertionError('must reuse original')):
                self.assertEqual(client.prepare(record)[0]['source'],'ICLR官方论文集')
    def test_no_doi_keeps_actual_url_and_order_without_roles(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);record,evidence=synthetic(folder)
            candidate=rebuild_primary(record,folder,evidence)
            self.assertEqual(candidate['doi'],'')
            self.assertEqual(candidate['fields']['相关网址'],evidence['documents'][0]['url'])
            self.assertEqual(candidate['fields']['作者'],'Alice Test(1)')
            self.assertEqual(candidate['source'],'ICLR官方论文集')
            for label in ('DOI','WOS记录号','通讯作者','第一作者','发表期刊'):
                self.assertNotIn(label,candidate['fields'])

    def test_originals_rechecked_not_just_reviewed_boolean(self):
        changes=[lambda e:e.update(reviewed=False),lambda e:e.update(version=True),
                 lambda e:e['facts']['authors'][0].update(name='Alice Other'),
                 lambda e:e['facts']['authors'][0].update(byline=None),
                 lambda e:e['facts']['authors'][0].update(affiliations=[2]),
                 lambda e:e['facts']['affiliations'].__setitem__(0,'Invented Shanghai Jiao Tong University'),
                 lambda e:e['documents'][1].update(url='https://arxiv.org/pdf/0000.00000')]
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);record,evidence=synthetic(folder)
            for change in changes:
                altered=copy.deepcopy(evidence);change(altered)
                with self.subTest(change=change),self.assertRaises(SafetyStop):
                    rebuild_primary(record,folder,altered)
            with (folder/'source.pdf').open('ab') as out:out.write(b'changed')
            with self.assertRaisesRegex(SafetyStop,'哈希'):rebuild_primary(record,folder,evidence)

    def test_record_doi_and_title_conflicts_never_filled_from_preprint(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);record,evidence=synthetic(folder)
            for altered in (replace(record,doi='10.1234/other'),replace(record,title='Different paper'),
                            replace(record,wos='WOS:000123456789012')):
                with self.assertRaises(MetadataSkip):rebuild_primary(altered,folder,evidence)

    def test_registry_is_explicit_no_network_and_cache_rebuilt_on_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);papers=root/'papers';source=root/'primary-evidence'/hashlib.sha256(title_key(example()[0].title).encode()).hexdigest()
            source.mkdir(parents=True);record,evidence=synthetic(source)
            (source/'evidence.json').write_text(json.dumps(evidence),encoding='utf-8')
            client=MetadataClient(papers,transport='urllib')
            with patch.object(client.discovery,'discover',side_effect=AssertionError('No network')):
                candidate,folder=client.prepare(record)
                self.assertEqual(client.prepare(record),(candidate,folder))
            with (folder/'source.html').open('a') as out:out.write('changed')
            with self.assertRaises(SafetyStop):client.prepare(record)

    def test_only_real_official_canonical_pages(self):
        for url in ('http://proceedings.mlr.press/v306/test26a.html',
                    'https://proceedings.mlr.press.evil/v306/test26a.html',
                    'https://proceedings.mlr.press/v306/test26a.html?q=1',
                    'https://arxiv.org/abs/1234.56789','https://proceedings.iclr.cc/paper'):
            with self.assertRaises(SafetyStop):trusted_page(url)

    def test_no_doi_resolution_needs_exact_url_author_order_year_and_venue(self):
        with tempfile.TemporaryDirectory() as temp:
            record,evidence=synthetic(Path(temp));c=rebuild_primary(record,Path(temp),evidence)
            identity={'url':c['primary_url'],'year':'2025','venue':c['fields']['会议名称'],'authors':c['author_names']}
            item={'id':'123','title':c['title'],'doi':'','wos':'','primary_identity':identity}
            def answer(value):return {'verified':True,'library_resolution_protocol':2,'items':[value]}
            self.assertEqual(resolved(answer(item),c),item)
            for key in identity:
                bad=copy.deepcopy(item);bad['primary_identity'][key]='different'
                with self.assertRaises(SafetyStop):resolved(answer(bad),c)
            bad=copy.deepcopy(item);del bad['primary_identity']
            with self.assertRaises(SafetyStop):resolved(answer(bad),c)

    def test_pipeline_probes_native_fields_before_dedupe_and_closes_once(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);path=base/'list.xlsx';make_roster(path,flags=(None,None))
            book=load_workbook(path);sheet=book.active
            for cell,value in {'B2':'谭勋策','D2':example()[0].title,'E2':'','H2':0,'I2':'','K2':''}.items():sheet[cell]=value
            book.save(path);book.close()
            roster=migrate_status_column(read_roster(path));record=roster.records[0]
            source=base/'runtime/metadata-imports/primary-evidence'/hashlib.sha256(title_key(record.title).encode()).hexdigest()
            source.mkdir(parents=True);_,evidence=synthetic(source)
            (source/'evidence.json').write_text(json.dumps(evidence),encoding='utf-8')
            client=MetadataClient(base/'runtime/metadata-imports/papers',transport='urllib')
            candidate=rebuild_primary(record,source,evidence)
            class PrimaryBackend(Backend):
                def call(self,action,p,timeout=90):
                    result=super().call(action,p,timeout)
                    if action in ('status','search'):result['publisher_metadata_protocol']=1
                    if action=='import_excel_resolve':
                        self_case.assertEqual(set(p['primary_fields']),{'相关网址','年份','会议名称'})
                        if result['items']:result['items'][0]['primary_identity']={
                            'url':candidate['primary_url'],'year':candidate['fields']['年份'],
                            'venue':candidate['fields']['会议名称'],'authors':candidate['author_names']}
                    return result
            self_case=self;backend=PrimaryBackend(record,candidate)
            def builder(folder,candidate):
                path=folder/'paper.xlsx';path.write_bytes(b'PK synthetic test workbook')
                return path,file_hash(path)
            result=run_metadata_batch(base,roster,[record],backend,threading.Event(),client=client,excel_builder=builder)
            self.assertFalse(result.halted,result.reason)
            done=result.roster.records[0]
            self.assertTrue(done.done);self.assertEqual(done.remark,'已入库');self.assertEqual(done.source,'ICLR官方论文集')
            self.assertEqual(result.roster.records[1],roster.records[1])
            self.assertLess(backend.calls.index('import_excel_probe'),backend.calls.index('import_excel_resolve'))
            again=run_metadata_batch(base,result.roster,[done],backend,threading.Event(),client=client,excel_builder=builder)
            self.assertFalse(again.halted,again.reason)
            for action in ('import_excel_upload','import_excel_submit','import_excel_push','complete'):
                self.assertEqual(backend.calls.count(action),1,action)


if __name__ == '__main__':unittest.main()
