import copy
from dataclasses import replace
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from core import Record, SafetyStop
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
