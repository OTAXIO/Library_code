"""Discovery broadens recall but must never weaken import acceptance."""
import copy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import ssl
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from core import SafetyStop
from metadata_sources import MetadataClient, MetadataSkip
from tests.test_metadata_sources import example


EMPTY = {'message': {'items': []}}


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.client = MetadataClient(self.temp.name)
        self.record, self.cr, self.oa = example()
        self.record = replace(self.record, doi='')

    def responses(self, *answers):
        self.client.get = Mock(side_effect=copy.deepcopy(answers))

    def oa_hit(self):
        return {'results': [{k: self.oa[k] for k in ('id', 'doi', 'title')}]}

    def test_expanded_crossref_search_gets_full_doi_record_before_using_selected_fields(self):
        near = copy.deepcopy(self.cr); near['title'] = ['Synthetic verified papers']
        found = {'DOI': self.cr['DOI'], 'title': self.cr['title']}
        self.responses({'message': {'items': [near]}}, {'message': {'items': [near]*11+[found]}},
                       {'message': self.cr}, self.oa)
        candidate, _ = self.client.prepare(self.record)
        self.assertEqual(candidate['doi'], self.cr['DOI'])
        self.assertEqual(self.client.get.call_count, 4)
        query = parse_qs(urlsplit(self.client.get.call_args_list[1].args[0]).query)
        self.assertEqual(query['rows'], ['50'])
        self.assertEqual(query['query.bibliographic'], [self.record.title])
        self.assertEqual(query['select'], ['DOI,title'])

    def test_openalex_title_fallback_rechecks_both_singletons_and_archives_discovery(self):
        self.responses(EMPTY, EMPTY, self.oa_hit(), {'message': self.cr}, self.oa)
        candidate, folder = self.client.prepare(self.record)
        self.assertEqual(candidate['doi'], self.cr['DOI'])
        query = parse_qs(urlsplit(self.client.get.call_args_list[2].args[0]).query)
        self.assertEqual(query['search'], [self.record.title])
        self.assertEqual(query['per_page'], ['25'])
        self.assertEqual(self.client.get.call_count, 5)
        saved = json.loads((folder/'metadata.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['record_key'], self.record.key)
        self.assertEqual(len(saved['lookup']), 5)
        self.assertTrue(all(step['returned'] for step in saved['lookup']))

    def test_all_sources_without_exact_hit_preserve_candidate_evidence_not_false_noncoverage(self):
        self.responses(EMPTY, EMPTY, {'results': []})
        with self.assertRaisesRegex(MetadataSkip, '两源未精确匹配'):
            self.client.prepare(self.record)
        self.assertEqual(self.client.get.call_count, 3)
        self.assertEqual(list(Path(self.temp.name).rglob('metadata.json')), [])
        evidence = list(Path(self.temp.name).rglob('lookup-*.json'))
        self.assertEqual(len(evidence), 1)
        saved = json.loads(evidence[0].read_text(encoding='utf-8'))
        self.assertEqual([s['exact_count'] for s in saved['steps']], [0, 0, 0])
        self.assertNotIn('wos未收录', saved['reason'])

    def test_near_title_is_not_an_identity_match_even_when_doi_has_complete_sjtu_data(self):
        hit = self.oa_hit(); hit['results'][0]['title'] += ' II'
        self.responses(EMPTY, EMPTY, hit)
        with self.assertRaises(MetadataSkip):
            self.client.prepare(self.record)
        self.assertEqual(self.client.get.call_count, 3)

    def test_ambiguous_expanded_crossref_never_uses_first_or_asks_another_source_to_guess(self):
        self.responses(EMPTY, {'message': {'items': [self.cr, self.cr]}})
        with self.assertRaisesRegex(MetadataSkip, '未唯一'):
            self.client.prepare(self.record)
        self.assertEqual(self.client.get.call_count, 2)

    def test_ambiguous_openalex_never_uses_first(self):
        hit = self.oa_hit(); hit['results'].append(copy.deepcopy(hit['results'][0]))
        self.responses(EMPTY, EMPTY, hit)
        with self.assertRaisesRegex(MetadataSkip, '未唯一'):
            self.client.prepare(self.record)
        self.assertEqual(self.client.get.call_count, 3)

    def test_explicit_doi_404_does_not_substitute_title_search(self):
        self.responses(None)
        with self.assertRaisesRegex(MetadataSkip, 'Crossref未查询到'):
            self.client.prepare(replace(self.record, doi=self.cr['DOI']))
        self.assertEqual(self.client.get.call_count, 1)

    def test_singleton_wrong_doi_or_changed_openalex_id_stops_without_import_evidence(self):
        changed = copy.deepcopy(self.cr); changed['DOI'] = '10.1234/other'
        self.responses({'message': changed})
        with self.assertRaisesRegex(SafetyStop, '未对应请求标识'):
            self.client.prepare(replace(self.record, doi=self.cr['DOI']))
        self.assertEqual(len(self.client.discovery.memo), 0)
        changed_oa = copy.deepcopy(self.oa); changed_oa['id'] = 'https://openalex.org/W999'
        self.responses(EMPTY, EMPTY, self.oa_hit(), {'message': self.cr}, changed_oa)
        with self.assertRaisesRegex(SafetyStop, '回读标识不一致'):
            self.client.prepare(self.record)
        self.assertEqual(list(Path(self.temp.name).rglob('metadata.json')), [])

    def test_openalex_hit_cannot_replace_missing_printed_affiliation_or_conflicting_author(self):
        for change in ('affiliation', 'author'):
            with self.subTest(change=change):
                changed = copy.deepcopy(self.oa)
                if change == 'affiliation': changed['authorships'][0]['raw_affiliation_strings'] = []
                else: changed['authorships'][0]['raw_author_name'] = 'Another Person'
                self.client.discovery.memo.clear()
                self.responses(EMPTY, EMPTY, self.oa_hit(), {'message': self.cr}, changed)
                with self.assertRaises(MetadataSkip):
                    self.client.prepare(self.record)

    def test_same_title_negative_is_queried_once_per_batch_but_evidence_is_task_specific(self):
        self.responses(EMPTY, EMPTY, {'results': []})
        for i in range(5):
            record = replace(self.record, row=i+2, sa_id='same-title-'+str(i), staff_id=str(i))
            with self.assertRaisesRegex(MetadataSkip, '两源未精确匹配'):
                self.client.prepare(record)
        self.assertEqual(self.client.get.call_count, 3)
        evidence = list(Path(self.temp.name).rglob('lookup-*.json'))
        self.assertEqual(len(evidence), 5)
        self.assertEqual(len({json.loads(p.read_text(encoding='utf-8'))['record_key'] for p in evidence}), 5)

    def test_success_reuses_public_responses_not_record_keys_or_mutable_archives(self):
        self.responses({'message': self.cr}, self.oa)
        first = replace(self.record, doi=self.cr['DOI'])
        candidate, folder = self.client.prepare(first)
        candidate['fields']['题名'] = 'mutated local result'
        second = replace(first, sa_id='another-task', row=9, staff_id='00002')
        candidate2, folder2 = self.client.prepare(second)
        self.assertNotEqual(folder, folder2)
        self.assertEqual(candidate2['title'], self.record.title)
        self.assertEqual(self.client.get.call_count, 2)
        saved = json.loads((folder2/'metadata.json').read_text(encoding='utf-8'))
        prior = json.loads((folder/'metadata.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['record_key'], second.key)
        self.assertEqual(saved['fetched_utc'], prior['fetched_utc'])
        self.assertTrue(all(s['reused'] for s in saved['lookup']))
        self.assertLessEqual(saved['fetched_utc'], saved['archived_utc'])

    def test_memo_expires_and_cancellation_is_respected_even_for_hits(self):
        self.responses(EMPTY, EMPTY, {'results': []}, EMPTY, EMPTY, {'results': []})
        with patch('metadata_discovery.time.monotonic', return_value=0):
            with self.assertRaises(MetadataSkip): self.client.prepare(self.record)
        with patch('metadata_discovery.time.monotonic', return_value=301):
            with self.assertRaises(MetadataSkip): self.client.prepare(replace(self.record, sa_id='later'))
        self.assertEqual(self.client.get.call_count, 6)
        self.client.discovery.cancelled = lambda: True
        with self.assertRaisesRegex(SafetyStop, '已请求暂停'):
            self.client.prepare(replace(self.record, sa_id='cancelled'))
        self.assertEqual(self.client.get.call_count, 6)

    def test_network_error_is_not_cached_as_absence_or_automatically_retried(self):
        self.client.get = Mock(side_effect=[SafetyStop('Crossref HTTP 429'), {'message': self.cr}, self.oa])
        record = replace(self.record, doi=self.cr['DOI'])
        with self.assertRaisesRegex(SafetyStop, '429'): self.client.prepare(record)
        self.assertEqual(self.client.get.call_count, 1)
        self.assertTrue(self.client.prepare(record)[0]['sjtu'])
        self.assertEqual(self.client.get.call_count, 3)

    def test_malformed_search_response_is_not_a_zero_result(self):
        for bad in (None, {'message': None}, {'message': {'items': 'not-list'}}):
            with self.subTest(response=bad):
                self.client.discovery.memo.clear(); self.responses(bad)
                with self.assertRaises(SafetyStop) as caught: self.client.prepare(self.record)
                self.assertNotIsInstance(caught.exception, MetadataSkip)

    def test_diagnostic_write_failure_preserves_original_lookup_stop(self):
        self.responses(EMPTY, EMPTY, {'results': []})
        with patch('metadata_sources.os.rename', side_effect=PermissionError('synthetic filesystem')):
            with self.assertRaisesRegex(MetadataSkip, '两源未精确匹配'):
                self.client.prepare(self.record)
        self.assertEqual(list(Path(self.temp.name).rglob('*.tmp')), [])

    def test_tls_and_connection_failures_name_inner_type_without_exposing_error_payload(self):
        from urllib.error import URLError
        for inner, expected in ((ssl.SSLEOFError(8, 'PRIVATE_PROXY_DETAIL'), 'TLS 连接'),
                                (ConnectionResetError(10054, 'PRIVATE_PROXY_DETAIL'), '连接被重置'),
                                (ConnectionRefusedError(10061, 'PRIVATE_PROXY_DETAIL'), '连接被拒绝')):
            with self.subTest(kind=type(inner).__name__), patch('metadata_sources.urlopen', side_effect=URLError(inner)):
                self.client.last_request = 0
                with self.assertRaises(SafetyStop) as caught:
                    self.client.get('https://api.crossref.org/works/test')
                self.assertIn(expected, str(caught.exception))
                self.assertIn(type(inner).__name__, str(caught.exception))
                self.assertNotIn('PRIVATE_PROXY_DETAIL', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
