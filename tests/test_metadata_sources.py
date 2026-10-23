import copy
import json
from dataclasses import replace
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest.mock import Mock, patch

from core import Record, SafetyStop, digest
from metadata_sources import KIND, MetadataClient, MetadataSkip, merge_metadata
from metadata_pipeline import validate_batch, resolved


def example():
    record = Record(2, '谭勋策', 'meta-001', 'Synthetic verified paper', '10.1234/example', '', '00001', 0, '', '待处理', '', '1')
    cr = {'DOI':record.doi,'title':[record.title],'type':'journal-article','container-title':['Synthetic Journal'],
          'published':{'date-parts':[[2025,2]]},'author':[{'given':'Alice','family':'Test'}]}
    oa = {'doi':'https://doi.org/'+record.doi,'title':record.title,'type':'article','id':'https://openalex.org/W123',
          'authorships':[{'raw_author_name':'Alice Test','raw_affiliation_strings':['Shanghai Jiao Tong University, China']}]}
    return record, cr, oa


class MetadataSourceTests(unittest.TestCase):
    def test_conference_uses_existing_conference_template_not_journal_fields(self):
        r,cr,oa=example()
        cr.update(type='proceedings-article',event={'name':'Synthetic Conference'},
                  **{'container-title':['Synthetic Series','Synthetic Proceedings']})
        oa['type']='conference-paper'
        candidate=merge_metadata(r,cr,oa)
        self.assertEqual(candidate['model_name'],'会议论文')
        self.assertEqual(candidate['fields']['会议名称'],'Synthetic Conference')
        self.assertEqual(candidate['fields']['出处'],'Synthetic Series / Synthetic Proceedings')
        for label in ('发表期刊','出版者','ISSN','URL','通讯作者','WOS记录号','会议录名称'):
            self.assertNotIn(label,candidate['fields'])

    def test_conference_requires_both_sources_and_real_event_name(self):
        for oa_type in ('article','preprint','conference-abstract','book-chapter'):
            r,cr,oa=example();cr['type']='proceedings-article';oa['type']=oa_type
            cr['event']={'name':'Synthetic Conference'}
            with self.assertRaises(MetadataSkip):merge_metadata(r,cr,oa)
        r,cr,oa=example();cr['type']='proceedings-article';oa['type']='conference-paper'
        with self.assertRaisesRegex(MetadataSkip,'会议信息'):merge_metadata(r,cr,oa)

    def test_verified_flat_fields_no_fabricated_wos_or_roles(self):
        r, cr, oa = example(); c = merge_metadata(r,cr,oa)
        self.assertEqual(c['fields']['作者'],'Alice Test(1)')
        self.assertEqual(c['fields']['作者单位'],'(1)Shanghai Jiao Tong University, China')
        self.assertEqual(c['fields']['发表日期'],'2025-02')
        self.assertEqual(c['wos'],''); self.assertEqual(c['kind'],KIND)
        for forbidden in ('WOS记录号','收录类别','通讯作者','交大是否第一单位'):
            self.assertNotIn(forbidden,c['fields'])

    def test_normalized_institution_without_raw_affiliation_cannot_prove_sjtu(self):
        r, cr, oa = example()
        oa['authorships'][0].update(raw_affiliation_strings=[],institutions=[{'display_name':'Shanghai Jiao Tong University'}])
        with self.assertRaises(MetadataSkip): merge_metadata(r,cr,oa)

    def test_author_order_mismatch_and_initials_are_not_guessed(self):
        for raw in ('A Test','Alice Other','Other Alice'):
            r, cr, oa = example(); oa['authorships'][0]['raw_author_name']=raw
            with self.assertRaises(MetadataSkip): merge_metadata(r,cr,oa)

    def test_multiple_authors_have_exact_affiliation_mapping(self):
        r, cr, oa = example()
        cr['author'].append({'given':'Bob','family':'Other'})
        oa['authorships'].append({'raw_author_name':'Other, Bob','raw_affiliation_strings':['Other University, China']})
        c=merge_metadata(r,cr,oa)
        self.assertEqual(c['fields']['作者'],'Alice Test(1);Bob Other(2)')
        self.assertEqual(len(c['addresses']),2)
        oa['authorships'].reverse()
        with self.assertRaises(MetadataSkip): merge_metadata(r,cr,oa)

    def test_identity_type_and_delimiter_conflicts_are_deferred(self):
        patches=[('doi','10.1234/wrong'),('title','Different'),('type','book-chapter')]
        for key,value in patches:
            r, cr, oa = example(); oa[key]=value
            with self.assertRaises(MetadataSkip): merge_metadata(r,cr,oa)
        r, cr, oa=example(); oa['authorships'][0]['raw_affiliation_strings']=['SJTU;Other']
        with self.assertRaises(MetadataSkip): merge_metadata(r,cr,oa)
        r, cr, oa=example()
        with self.assertRaises(MetadataSkip): merge_metadata(replace(r,wos='WOS:000123456789012'),cr,oa)

    def test_missing_sjtu_is_not_false_non_sjtu_closure(self):
        r, cr, oa=example(); oa['authorships'][0]['raw_affiliation_strings']=['Other University, China']
        with self.assertRaisesRegex(MetadataSkip,'未确认交大署名'): merge_metadata(r,cr,oa)

    def test_title_search_ambiguity_never_selects_first(self):
        r, cr, oa=example()
        with tempfile.TemporaryDirectory() as temp:
            client=MetadataClient(temp); client.get=Mock(return_value={'message':{'items':[cr,cr]}})
            with self.assertRaisesRegex(MetadataSkip,'未唯一'): client.prepare(replace(r,doi=''))
            self.assertEqual(client.get.call_count,1)

    def test_http_error_not_a_zero_and_cache_reverified(self):
        r, cr, oa=example()
        with tempfile.TemporaryDirectory() as temp:
            client=MetadataClient(temp); client.get=Mock(side_effect=[{'message':cr},oa])
            c, folder=client.prepare(r)
            self.assertEqual(client.prepare(r)[0],c)
            self.assertEqual(client.get.call_count,2)
            import json
            p=folder/'metadata.json'; data=json.loads(p.read_text(encoding='utf-8'));data['candidate']['doi']='10.1234/changed'
            p.write_text(json.dumps(data),encoding='utf-8')
            with self.assertRaises(SafetyStop): client.prepare(r)

    def test_title_presentation_can_differ_but_words_numbers_and_doi_cannot(self):
        r, cr, oa = example()
        r = replace(r, title='Synthetic–verified “paper”.')
        cr['title'] = ['Synthetic-verified <i>"paper"</i>']
        oa['title'] = 'Synthetic-verified "paper"'
        self.assertEqual(merge_metadata(r, cr, oa)['title'], 'Synthetic-verified "paper"')
        for bad in ('Synthetic-verified "papers"', 'Synthetic-verified "paper" II', 'Synthetic unverified paper'):
            with self.subTest(title=bad), self.assertRaises(MetadataSkip):
                merge_metadata(replace(r, title=bad), cr, oa)

    def test_title_search_uses_same_presentation_rule_without_taking_first_near_match(self):
        r, cr, oa = example()
        r = replace(r, doi='', title='Synthetic verified paper.')
        with tempfile.TemporaryDirectory() as temp:
            client = MetadataClient(temp)
            near = copy.deepcopy(cr); near['title'] = ['Synthetic verified papers']
            client.get = Mock(side_effect=[{'message': {'items': [near, cr]}}, oa])
            candidate, _ = client.prepare(r)
            self.assertEqual(candidate['doi'], cr['DOI'])
            self.assertEqual(client.get.call_count, 2)

    def test_empty_folder_from_interruption_is_recoverable_without_overwriting_evidence(self):
        r, cr, oa = example()
        with tempfile.TemporaryDirectory() as temp:
            client = MetadataClient(temp)
            folder = Path(temp) / digest({'record': r.key, 'protocol': KIND})
            folder.mkdir()
            client.get = Mock(side_effect=[{'message': cr}, oa])
            candidate, actual = client.prepare(r)
            self.assertEqual(actual, folder)
            self.assertEqual(json.loads((folder/'metadata.json').read_text(encoding='utf-8'))['candidate'], candidate)
            self.assertEqual(list(folder.glob('.metadata-*.tmp')), [])

    def test_interrupted_archive_never_publishes_partial_json_and_can_be_retried(self):
        r, cr, oa = example()
        with tempfile.TemporaryDirectory() as temp:
            client = MetadataClient(temp); client.get = Mock(side_effect=[{'message': cr}, oa])
            with patch('metadata_sources.json.dump', side_effect=OSError('disk fixture')):
                with self.assertRaises(OSError): client.prepare(r)
            folder = Path(temp) / digest({'record': r.key, 'protocol': KIND})
            self.assertFalse((folder/'metadata.json').exists())
            self.assertEqual(list(folder.glob('.metadata-*.tmp')), [])
            client.get = Mock(side_effect=[{'message': cr}, oa])
            self.assertTrue(client.prepare(r)[0]['sjtu'])

    def test_corrupt_or_wrong_record_cache_is_preserved_without_network_or_upload(self):
        r, cr, oa = example()
        for raw in ('{"candidate":', json.dumps({'record_key': 'wrong'})):
            with self.subTest(raw=raw), tempfile.TemporaryDirectory() as temp:
                client = MetadataClient(temp); client.get = Mock()
                folder = Path(temp) / digest({'record': r.key, 'protocol': KIND})
                folder.mkdir(); archive = folder/'metadata.json'; archive.write_text(raw, encoding='utf-8')
                with self.assertRaisesRegex(SafetyStop, '缓存不完整或校验失败'): client.prepare(r)
                self.assertEqual(archive.read_text(encoding='utf-8'), raw)
                client.get.assert_not_called()

    def test_racing_cache_is_revalidated_and_never_overwritten(self):
        r, cr, oa = example()
        with tempfile.TemporaryDirectory() as temp:
            client = MetadataClient(temp); client.get = Mock(side_effect=[{'message': cr}, oa])
            _, folder = client.prepare(r)
            before = (folder/'metadata.json').read_bytes()
            client.publish_cache(folder, {'record_key': 'not-this-paper'})
            self.assertEqual((folder/'metadata.json').read_bytes(), before)
            self.assertEqual(client.prepare(r)[0]['doi'], cr['DOI'])

    def test_http_errors_name_service_and_rate_wait_without_becoming_absence(self):
        with tempfile.TemporaryDirectory() as temp:
            client = MetadataClient(temp, transport='urllib')
            for status in (401, 403, 429, 500):
                client.last_request = 0
                error = HTTPError('https://api.openalex.org/works/test', status, 'SECRET BODY', {'Retry-After': '60'}, None)
                with patch('metadata_sources.urlopen', side_effect=error), self.assertRaises(SafetyStop) as caught:
                    client.get('https://api.openalex.org/works/test')
                self.assertIn('OpenAlex', str(caught.exception))
                self.assertIn('HTTP '+str(status), str(caught.exception))
                self.assertNotIn('SECRET BODY', str(caught.exception))
                if status == 429: self.assertIn('60 秒', str(caught.exception))
            with patch('metadata_sources.urlopen', side_effect=HTTPError('url', 404, 'missing', {}, None)):
                client.last_request = 0
                self.assertIsNone(client.get('https://api.crossref.org/works/test'))

    def test_timeout_explains_actual_service_and_does_not_automatically_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            client = MetadataClient(temp, transport='urllib')
            with patch('metadata_sources.urlopen', side_effect=URLError(TimeoutError())) as request:
                with self.assertRaisesRegex(SafetyStop, 'Crossref 请求超时'):
                    client.get('https://api.crossref.org/works/test')
            request.assert_called_once()

    def test_foreign_or_insecure_initial_endpoint_is_rejected_before_request(self):
        with tempfile.TemporaryDirectory() as temp:
            client = MetadataClient(temp)
            for url in ('http://api.crossref.org/works/test', 'https://example.org/works',
                        'https://api.openalex.org.attacker.invalid/works', 'https://user@api.openalex.org/works'):
                with self.subTest(url=url), patch('metadata_sources.urlopen') as request:
                    client.last_request = 0
                    with self.assertRaises(SafetyStop): client.get(url)
                    request.assert_not_called()

    def test_batch_counts_and_source_must_be_exact(self):
        r, cr, oa=example()
        batch={'id':'123','modelId':'article','source':'人工提交','instructions':'SA补充-'+r.sa_id,'status':2,'total':1,'actual':1,'fail':0,
               'increase':1,'duplicateSkip':0,'duplicateIncreaseUpdate':0,'duplicateOverallCoverage':0}
        self.assertEqual(validate_batch(batch,r,pushed=True),batch)
        for patch in ({'source':'WOS'},{'actual':0},{'fail':1},{'total':True},{'instructions':'SA补充-other'},{'status':1},
                      {'increase':0},{'duplicateSkip':1},{'duplicateIncreaseUpdate':1},{'duplicateOverallCoverage':1}):
            with self.assertRaises(SafetyStop): validate_batch({**batch,**patch},r,pushed=True)

    def test_resolve_requires_complete_unique_exact_doi(self):
        r,cr,oa=example(); c=merge_metadata(r,cr,oa)
        item={'id':'1234567890123456789','title':c['title'],'doi':c['doi'],'wos':''}
        self.assertEqual(resolved({'verified':True,'items':[item]},c),item)
        with self.assertRaises(SafetyStop): resolved({'verified':False,'items':[]},c)
        with self.assertRaises(MetadataSkip): resolved({'verified':True,'items':[item,item]},c)


if __name__=='__main__': unittest.main()
