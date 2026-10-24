"""Synthetic publisher journal pages/PDFs only; no real data or network."""
import copy
from dataclasses import replace
import hashlib
import html
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import quote

from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
from openpyxl import load_workbook
from core import SafetyStop,file_hash,read_roster
from metadata_identity import title_key
from metadata_sources import MetadataClient
from metadata_pipeline import run_metadata_batch
from journal_metadata import rebuild_journal
from tests.test_metadata_sources import example
from tests.test_metadata_pipeline import Backend
from tests.test_roster_write import make_roster
from roster_write import migrate_status_column


def write_pdf(path,lines):
    writer=PdfWriter();page=writer.add_blank_page(width=612,height=792)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
    stream=DecodedStreamObject()
    stream.set_data(('BT /F1 10 Tf 20 760 Td '+' '.join(('0 -20 Td ' if i else '')+'('+v+') Tj' for i,v in enumerate(lines))+' ET').encode('ascii'))
    page[NameObject('/Contents')]=writer._add_object(stream);writer.write(path)


def bundle(folder,source='nature'):
    record=replace(example()[0],doi='')
    identifier='10.1038/s99999-025-00001-1' if source=='nature' else '10.26599/test.2025.1'
    page='https://www.nature.com/articles/'+identifier[8:] if source=='nature' else 'https://www.sciopen.com/article/10.26599/TEST.2025.1'
    online='2025/04/28' if source=='nature' else '2025/7/7'
    values=[('title',record.title),('doi',identifier),('journal_title','Synthetic Journal'),('volume','2'),('issue','5'),
            ('publication_date','2025/09/01'),('online_date',online),('firstpage','10'),('lastpage','19'),('issn','1234-5678'),
            ('author','Alice Test'),('author','Bob Other')]
    if source=='nature':
        body='<ol class="c-article-author-affiliation__list"><li id="Aff1"><p class="c-article-author-affiliation__address">Shanghai Jiao Tong University</p></li></ol>'
        body+='<ol><li id="auth-Alice-Test-Aff1"><span class="c-article-authors-search__title">Alice Test</span></li><li id="auth-Bob-Other-Aff1"><span class="c-article-authors-search__title">Bob Other</span></li></ol>'
    else:
        values.append(('pdf_url','https://www.sciopen.com/local/article_pdf/10.26599/TEST.2025.1.pdf'))
        body='<div class="art-authors-unit-item"><label>1</label>Shanghai Jiao Tong University</div>'
        body+='<span class="art-authors"><span>Alice Test</span><sup>1</sup></span><span class="art-authors"><span>Bob Other</span><sup>1</sup></span><span class="art-authors">Show Author Information</span>'
        write_pdf(folder/'source.pdf',[record.title,identifier,'Alice Test1, Bob Other1','1Shanghai Jiao Tong University','Published date: 7 July 2025'])
    raw=''.join(f'<meta name="citation_{k}" content="{html.escape(v)}">' for k,v in values)+body
    (folder/'source.html').write_text(raw,encoding='utf-8')
    cr={'status':'ok','message':{'DOI':identifier,'title':[record.title],'type':'journal-article','container-title':['Synthetic Journal'],
        'author':[{'given':'Alice','family':'Test'},{'given':'Bob','family':'Other'}],'volume':'2','issue':'5','page':'10-19','ISSN':['1234-5678']}}
    if source=='nature':cr['message']['published-online']={'date-parts':[[2025,4,28]]}
    else:cr['message']['published-print']={'date-parts':[[2025,9]]}
    (folder/'crossref.json').write_text(json.dumps(cr),encoding='utf-8')
    files=[('source.html',page),('crossref.json','https://api.crossref.org/works/'+quote(identifier,safe=''))]
    if source!='nature':files.append(('source.pdf',dict(values)['pdf_url']))
    evidence={'version':2,'evidence_type':'publisher-journal-v1','reviewed':True,
              'documents':[{'path':n,'url':u,'sha256':file_hash(folder/n)} for n,u in files]}
    return record,evidence


class JournalTests(unittest.TestCase):
    def test_two_publishers_use_original_byline_and_online_date_not_api_affiliation(self):
        for source in ('nature','sciopen'):
            with self.subTest(source=source),tempfile.TemporaryDirectory() as temp:
                folder=Path(temp);record,evidence=bundle(folder,source);c=rebuild_journal(record,folder,evidence)
                self.assertEqual(c['author_names'],['Alice Test','Bob Other'])
                self.assertEqual(c['fields']['作者'],'Alice Test(1);Bob Other(1)')
                self.assertEqual(c['model_name'],'期刊论文');self.assertEqual(c['fields']['URL'],evidence['documents'][0]['url'])
                self.assertEqual(c['fields']['发表日期'],'2025-04-28' if source=='nature' else '2025-07-07')
                self.assertEqual(c['publication_status'],'published-online')
                for label in ('第一作者','通讯作者','WOS记录号','会议名称'):self.assertNotIn(label,c['fields'])
                self.assertNotIn('openalex_url',c)

    def test_missing_review_hash_path_and_wrong_canonical_doi_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);record,evidence=bundle(folder)
            for modify in [lambda e:e.update(reviewed=False),lambda e:e.update(version=True),
                           lambda e:e['documents'][0].update(path='../source.html'),
                           lambda e:e['documents'][0].update(url='https://www.nature.com/articles/s99999-025-00002-1'),
                           lambda e:e['documents'][1].update(url='https://api.crossref.org/works/wrong')]:
                bad=copy.deepcopy(evidence);modify(bad)
                with self.assertRaises(SafetyStop):rebuild_journal(record,folder,bad)
            with (folder/'source.html').open('a') as out:out.write('tampered')
            with self.assertRaises(SafetyStop):rebuild_journal(record,folder,evidence)

    def test_changed_authors_affiliations_dates_and_bibliography_fail_closed(self):
        for token,new in [('auth-Alice-Test-Aff1','auth-Alice-Test-Aff2'),('Bob Other','Other Bob Person'),
                          ('2025/04/28','2025/04/29'),('content="2"','content="3"'),('1234-5678','1234-567X')]:
            with self.subTest(token=token),tempfile.TemporaryDirectory() as temp:
                folder=Path(temp);record,evidence=bundle(folder)
                page=folder/'source.html';page.write_text(page.read_text().replace(token,new),encoding='utf-8')
                evidence['documents'][0]['sha256']=file_hash(page)
                with self.assertRaises(SafetyStop):rebuild_journal(record,folder,evidence)

    def test_pdf_is_required_and_checked_for_sciopen(self):
        with tempfile.TemporaryDirectory() as temp:
            folder=Path(temp);record,evidence=bundle(folder,'sciopen')
            no_pdf=copy.deepcopy(evidence);no_pdf['documents'].pop()
            with self.assertRaises(SafetyStop):rebuild_journal(record,folder,no_pdf)
            write_pdf(folder/'source.pdf',['A different paper'])
            evidence['documents'][-1]['sha256']=file_hash(folder/'source.pdf')
            with self.assertRaises(SafetyStop):rebuild_journal(record,folder,evidence)

    def test_acceptance_alone_or_a_future_online_date_does_not_prove_published(self):
        for operation in ('accepted-only','future'):
            with self.subTest(operation=operation),tempfile.TemporaryDirectory() as temp:
                folder=Path(temp);record,evidence=bundle(folder)
                path=folder/'source.html';raw=path.read_text()
                raw=(raw.replace('citation_online_date','citation_accepted_date') if operation=='accepted-only'
                     else raw.replace('2025/04/28','2099/04/28'))
                path.write_text(raw,encoding='utf-8');evidence['documents'][0]['sha256']=file_hash(path)
                if operation=='future':
                    cr=folder/'crossref.json';value=json.loads(cr.read_text())
                    value['message']['published-online']['date-parts']=[[2099,4,28]]
                    cr.write_text(json.dumps(value),encoding='utf-8');evidence['documents'][1]['sha256']=file_hash(cr)
                with self.assertRaises(SafetyStop):rebuild_journal(record,folder,evidence)

    def test_three_original_files_survive_cache_and_resume_without_network(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);source=root/'primary-evidence'/hashlib.sha256(title_key(example()[0].title).encode()).hexdigest()
            source.mkdir(parents=True);record,evidence=bundle(source,'sciopen')
            (source/'evidence.json').write_text(json.dumps(evidence),encoding='utf-8')
            client=MetadataClient(root/'papers',transport='urllib')
            with patch.object(client.discovery,'discover',side_effect=AssertionError('No network')):
                candidate,folder=client.prepare(record)
                self.assertTrue((folder/'crossref.json').is_file());self.assertTrue((folder/'source.pdf').is_file())
                self.assertEqual(client.prepare(record),(candidate,folder))

    def test_full_journal_pipeline_writes_true_source_and_never_replays_done(self):
        with tempfile.TemporaryDirectory() as temp:
            base=Path(temp);path=base/'list.xlsx';make_roster(path,flags=(None,None))
            book=load_workbook(path)
            for cell,value in {'B2':'谭勋策','D2':example()[0].title,'E2':'','H2':0,'I2':'','K2':''}.items():book.active[cell]=value
            book.save(path);book.close();roster=migrate_status_column(read_roster(path));record=roster.records[0]
            folder=base/'runtime/metadata-imports/primary-evidence'/hashlib.sha256(title_key(record.title).encode()).hexdigest()
            folder.mkdir(parents=True);_,evidence=bundle(folder)
            (folder/'evidence.json').write_text(json.dumps(evidence),encoding='utf-8')
            candidate=rebuild_journal(record,folder,evidence)
            class JournalBackend(Backend):
                def call(self,action,p,timeout=90):
                    result=super().call(action,p,timeout)
                    if action in ('status','search'):result.update(publisher_metadata_protocol=1,publisher_journal_protocol=2)
                    if action=='import_excel_resolve':
                        case.assertEqual(set(p['primary_fields']),{'URL','年份','发表期刊'})
                        if result['items']:result['items'][0]['primary_identity']={'url':candidate['primary_url'],
                          'year':candidate['fields']['年份'],'venue':candidate['fields']['发表期刊'],'authors':candidate['author_names']}
                    return result
            case=self;backend=JournalBackend(record,candidate);client=MetadataClient(base/'runtime/metadata-imports/papers')
            def builder(folder,c):
                file=folder/'paper.xlsx';file.write_bytes(b'PK test');return file,file_hash(file)
            result=run_metadata_batch(base,roster,[record],backend,threading.Event(),client=client,excel_builder=builder)
            self.assertFalse(result.halted,result.reason);self.assertTrue(result.roster.records[0].done)
            self.assertEqual(result.roster.records[0].remark,'已入库');self.assertEqual(result.roster.records[0].source,'Nature出版社')
            self.assertEqual(result.roster.records[1],roster.records[1])
            after=run_metadata_batch(base,result.roster,[result.roster.records[0]],backend,threading.Event(),client=client,excel_builder=builder)
            self.assertFalse(after.halted,after.reason)
            for action in ('import_excel_upload','import_excel_submit','import_excel_push','complete'):self.assertEqual(backend.calls.count(action),1)


if __name__=='__main__':unittest.main()
