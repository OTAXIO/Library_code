import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import ANY, Mock, patch

from automation import ImportStore
from core import Record, SafetyStop
from submission_prepare import matches_paper
from tests.test_automation import sample
from wos_batch import (all_targets, disconnected_outcome, export, plan, preflight, roster_rows,
                       safe_name, skipped_roster_rows, skipped_targets, wos_targets)


def record(**kwargs):
    base = Record(2, "测试员", "demo-001", "Synthetic paper", "10.1234/test", "", "00001", 0, "", "待处理", "", "1")
    return replace(base, **kwargs)


class FakeRoster:
    def __init__(self, records):
        self.records = records

    def assert_unchanged(self):
        return None


class TargetsTests(unittest.TestCase):
    def test_only_zero_match_unfinished_wos_records(self):
        records = [record(), record(row=3, sa_id="done-001", done=True),
                   record(row=4, sa_id="matched-001", matches=2), record(row=5, sa_id="cnki-001")]
        classification = {f"p{n}": {"import_route": {"recommended_channel": "WOS"}} for n in (1, 2, 3)}
        classification["p4"] = {"import_route": {"recommended_channel": "CNKI"}}
        papers = [{"id": f"p{n}", "rows": [n + 1]} for n in (1, 2, 3, 4)]
        self.assertEqual([r.sa_id for r in wos_targets(FakeRoster(records), classification, papers)],
                         ["demo-001"])

    def test_unclassified_records_are_skipped(self):
        records = [record(), record(row=3, sa_id="unknown-001")]
        papers = [{"id": "p1", "rows": [2]}, {"id": "p2", "rows": [3]}]
        classification = {"p1": {"import_route": {"recommended_channel": "WOS"}}, "p2": {}}
        self.assertEqual([r.sa_id for r in wos_targets(FakeRoster(records), classification, papers)],
                         ["demo-001"])
        self.assertEqual(wos_targets(FakeRoster(records), {}, papers), [])

    def test_all_targets_merges_rows_of_the_same_paper(self):
        # The roster lists one row per SA record, each with its own SA ID. Exporting
        # every row would write identical TXT files, so one row represents the paper.
        records = [record(),
                   record(row=3, sa_id="same-paper-002"),
                   record(row=4, sa_id="done-001", done=True),
                   record(row=5, sa_id="matched-001", matches=2),
                   record(row=6, sa_id="other-001", title="Another paper", doi="10.1234/other")]
        self.assertEqual([r.sa_id for r in all_targets(FakeRoster(records))],
                         ["demo-001", "other-001"])

    def test_roster_rows_counts_before_merging(self):
        records = [record(), record(row=3, sa_id="same-paper-002"),
                   record(row=4, sa_id="done-001", done=True)]
        self.assertEqual(len(roster_rows(FakeRoster(records))), 2)
        self.assertEqual(len(all_targets(FakeRoster(records))), 1)

    def test_owner_scope_and_skipped_rows_are_excluded(self):
        records = [record(), record(row=3,sa_id='other-owner',owner='Other'),
                   record(row=4,sa_id='skipped',skipped=True)]
        roster=FakeRoster(records)
        self.assertEqual([r.sa_id for r in roster_rows(roster,'测试员')],['demo-001'])
        self.assertEqual([r.sa_id for r in all_targets(roster,'测试员')],['demo-001'])

    def test_skipped_targets_only_include_numeric_two_scope_and_merge_papers(self):
        records = [record(), record(row=3,sa_id='skip-a',skipped=True),
                   record(row=4,sa_id='skip-a-copy',skipped=True),
                   record(row=5,sa_id='skip-other',owner='Other',skipped=True),
                   record(row=6,sa_id='skip-matched',skipped=True,matches=1),
                   record(row=7,sa_id='skip-done',skipped=True,done=True)]
        roster=FakeRoster(records)
        self.assertEqual([r.sa_id for r in skipped_roster_rows(roster,'测试员')],
                         ['skip-a','skip-a-copy'])
        self.assertEqual([r.sa_id for r in skipped_targets(roster,'测试员')],['skip-a'])

    def test_all_targets_ignores_the_classification_hint(self):
        # The roster decides the scope: a record the model did not label WOS is still
        # searched, because that is exactly the record that needs the lookup.
        records = [record(sa_id="unlabelled-001", title="Unlabelled paper")]
        self.assertEqual([r.sa_id for r in all_targets(FakeRoster(records))], ["unlabelled-001"])

    def test_different_doi_keeps_same_titled_rows_apart(self):
        # Same title with a different DOI are different papers for the intake, so they
        # must not collapse into a single file.
        records = [record(), record(row=3, sa_id="same-title-002", doi="10.1234/other")]
        self.assertEqual([r.sa_id for r in all_targets(FakeRoster(records))],
                         ["demo-001", "same-title-002"])

    def test_safe_name_is_title_plus_sa_id(self):
        name = safe_name(record(sa_id="sa-001", title="A Push and a Pull: Dual Pathways"))
        self.assertEqual(name, "A Push and a Pull Dual Pathways+sa-001.txt")

    def test_safe_name_is_filesystem_safe_and_keeps_the_identifier(self):
        hostile = safe_name(record(sa_id="../../etc/pa ss:wd*", title="Bad/Name:with*chars?"))
        for forbidden in ("/", "\\", ":", "*", "?", "<", ">", '"', "|"):
            self.assertNotIn(forbidden, hostile)
        self.assertTrue(hostile.endswith("+.._.._etc_pa ss_wd_.txt"), hostile)
        # The identifier survives an absurd title, and the whole name stays importable.
        long_title = safe_name(record(sa_id="sa-002", title="T" * 500))
        self.assertTrue(long_title.endswith("+sa-002.txt"))
        self.assertLessEqual(len(long_title), 190)
        self.assertEqual(safe_name(record(sa_id="sa-003", title="   ")), "sa-003.txt")

    def test_safe_name_with_a_long_title_still_differs_per_record(self):
        first = safe_name(record(sa_id="sa-100", title="Same title " * 40))
        second = safe_name(record(sa_id="sa-200", title="Same title " * 40))
        self.assertNotEqual(first, second)
        self.assertTrue(first.endswith("+sa-100.txt"))
        self.assertTrue(second.endswith("+sa-200.txt"))


class PlanTests(unittest.TestCase):
    def test_plan_keys_on_the_fingerprint_and_returns_the_papers(self):
        document = {"sha256": "abc", "papers": [{"id": "p", "rows": [2]}]}
        with patch("submission_prepare.saved_classification", return_value={"p": {}}) as saved:
            classification, papers = plan(document)
        saved.assert_called_once_with("abc", ANY)
        self.assertEqual(classification, {"p": {}})
        self.assertEqual(papers, document["papers"])

    def test_plan_requires_a_matching_saved_classification(self):
        with patch("submission_prepare.saved_classification", return_value={}):
            with self.assertRaises(SafetyStop):
                plan({"sha256": "abc", "papers": []})

    def test_plan_rejects_a_roster_object(self):
        # Regression: a core.Roster has .sha256 but is not subscriptable, and passing it
        # here is exactly the wiring bug this helper exists to prevent.
        class FakeRoster:
            sha256 = "abc"

        with self.assertRaises(TypeError):
            plan(FakeRoster())


class PreflightTests(unittest.TestCase):
    def test_running_capabilities_are_checked_before_search(self):
        bridge=Mock(call=Mock(return_value={'extension_version':'0.3.28','wos_download_protocol':1,
                    'result_reader':'shared-diagnostic','read_results_world':'ISOLATED'}))
        self.assertEqual(preflight(bridge),{'extension_version':'0.3.28'})
        bridge.call.assert_called_once_with('wos_diagnose',{},timeout=15)

    def test_old_or_incompatible_extension_fails_without_a_search(self):
        results=({}, {'extension_version':'0.3.27','wos_download_protocol':1,
                      'result_reader':'shared-diagnostic','read_results_world':'ISOLATED'},
                 {'extension_version':'0.3.26','wos_download_protocol':1,
                      'result_reader':'other','read_results_world':'ISOLATED'},
                 {'extension_version':'0.3.27','wos_download_protocol':True,
                  'result_reader':'shared-diagnostic','read_results_world':'ISOLATED'})
        for result in results:
            bridge=Mock(call=Mock(return_value=result))
            with self.assertRaisesRegex(SafetyStop,'重载'):
                preflight(bridge)
            self.assertEqual(bridge.call.call_count,1)
        bridge=Mock(call=Mock(side_effect=SafetyStop('[扩展 0.3.25] 未知 WOS 调度命令')))
        with self.assertRaisesRegex(SafetyStop,'仍是旧版本'):
            preflight(bridge)
        self.assertEqual(bridge.call.call_count,1)

    def test_genuine_connection_failure_is_not_hidden_as_a_version_problem(self):
        bridge=Mock(call=Mock(side_effect=SafetyStop('浏览器未连接')))
        with self.assertRaisesRegex(SafetyStop,'浏览器未连接'):
            preflight(bridge)


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inbox = self.root / "inbox"
        self.record = record()

    def bridge_for(self, raw):
        download = self.root / "download.txt"
        download.write_bytes(raw)

        def call(action, payload, timeout=75):
            if action == "wos_export":
                return {"path": str(download), "sa_id": payload["sa_id"]}
            return {}

        return Mock(call=Mock(side_effect=call))

    def classification(self, sa_id="demo-001", rows=(2,)):
        return (FakeRoster([self.record]), {f"p{n}": {"import_route": {"recommended_channel": "WOS"}}
                                            for n in rows}, [{"id": f"p{n}", "rows": [n]} for n in rows])

    def selected(self):
        """The classification-driven scope, as the app computes it."""
        roster, classification, papers = self.classification()
        return wos_targets(roster, classification, papers)

    def distinct(self, count, start=2):
        """Distinct papers: all_targets merges rows sharing a title and DOI."""
        return [record(row=start+i, sa_id=f"demo-{start+i:03d}",
                       title=f"Synthetic paper {start+i}", doi=f"10.1234/test{start+i}")
                for i in range(count)]

    def test_export_copies_the_full_record_into_the_intake_folder(self):
        raw = sample()
        result = export(self.selected(), self.bridge_for(raw),
                        ImportStore(self.root / "imports"), self.inbox)
        self.assertEqual((result["total"], len(result["exported"]), result["failed"]), (1, 1, {}))
        written = Path(result["exported"][0]["file"])
        self.assertEqual(written.parent, self.inbox)
        self.assertEqual(written.read_bytes(), raw)
        # The intake scan must be able to match what we just wrote.
        self.assertTrue(matches_paper(written.read_text(encoding="utf-8"),
                                      {"title": self.record.title, "doi": self.record.doi}))

    def test_second_run_reuses_the_archive_without_touching_the_browser(self):
        raw = sample()
        targets = self.selected()
        bridge = self.bridge_for(raw)
        store = ImportStore(self.root / "imports")
        export(targets, bridge, store, self.inbox)
        calls = bridge.call.call_count
        self.assertGreater(calls, 0)
        export(targets, bridge, store, self.inbox)
        self.assertEqual(bridge.call.call_count, calls)

    def test_progress_reports_each_stage_before_waiting_for_the_browser(self):
        updates=[]
        bridge=self.bridge_for(sample())
        original=bridge.call.side_effect
        def call(action,payload,timeout=75):
            self.assertIn('第 1/1 篇',updates[-1])
            self.assertIn('原表第 2 行',updates[-1])
            self.assertIn('检索并核验' if action=='wos_search' else '导出完整记录',updates[-1])
            self.assertIn(f'{timeout} 秒',updates[-1])
            return original(action,payload,timeout)
        bridge.call.side_effect=call
        store=ImportStore(self.root/'downloads')
        result=export(self.selected(),bridge,store,self.inbox,progress=updates.append)
        self.assertEqual(len(result['exported']),1)
        self.assertTrue(any('核验下载文件' in update for update in updates))
        self.assertIn('TXT 已保存',updates[-1])
        calls=bridge.call.call_count
        updates.clear()
        export(self.selected(),bridge,store,self.inbox,progress=updates.append)
        self.assertEqual(bridge.call.call_count,calls)
        self.assertTrue(any('复用已保存' in update for update in updates))
        self.assertFalse(any('本步最多' in update for update in updates))

    def test_download_does_not_require_sjtu_affiliation_or_call_import(self):
        bridge=self.bridge_for(sample(C1='Another University'))
        result=export(self.selected(),bridge,ImportStore(self.root/'downloads'),self.inbox)
        self.assertEqual(len(result['exported']),1)
        self.assertEqual([(c.args[0], c.kwargs['timeout']) for c in bridge.call.call_args_list],
                         [('wos_search', 120), ('wos_export', 75)])

    def test_conflicting_doi_is_not_adopted(self):
        result=export(self.selected(),self.bridge_for(sample(DI='10.1234/other')),
                      ImportStore(self.root/'downloads'),self.inbox)
        self.assertEqual(result['exported'],[])
        self.assertEqual(result['not_exported'],1)

    def test_repeated_download_failures_visit_every_record(self):
        records = self.distinct(4)
        # A relative path means the extension never reported a usable download.
        bridge = Mock(call=Mock(return_value={"path": "relative.txt", "sa_id": "demo-002"}))
        result=export(all_targets(FakeRoster(records)), bridge,
                      ImportStore(self.root / "imports"), self.inbox)
        self.assertEqual(len(result['failed']),4)
        self.assertEqual(bridge.call.call_count,8)
        self.assertFalse(result['stopped'])
        self.assertEqual(list(self.inbox.iterdir()), [])

    def test_weak_identity_export_is_not_dropped_into_the_auto_adopted_folder(self):
        # The roster has neither DOI nor WOS ID, so identity() cannot confirm strongly.
        weak = record(doi="", wos="")
        inbox = self.root / "inbox"
        result = export(all_targets(FakeRoster([weak])), self.bridge_for(sample()),
                        ImportStore(self.root / "imports"), inbox)
        self.assertEqual(result["exported"], [])
        self.assertEqual(len(result["unconfirmed"]), 1)
        self.assertEqual(result["unconfirmed"][0]["sa_id"], weak.sa_id)
        self.assertTrue(Path(result["unconfirmed"][0]["archive"]).is_file())
        # The intake folder is adopted automatically, so nothing may land there unverified.
        self.assertEqual(list(inbox.iterdir()), [])

    def test_stop_before_start_exports_nothing(self):
        import threading
        stop = threading.Event()
        stop.set()
        bridge = self.bridge_for(sample())
        result = export(self.selected(), bridge,
                        ImportStore(self.root / "imports"), self.inbox, stop=stop)
        self.assertEqual(result["exported"], [])
        self.assertTrue(result["stopped"])
        bridge.call.assert_not_called()

    def test_expected_lookup_misses_do_not_trip_the_circuit_breaker(self):
        # A zero-result page is a completed result for this paper. It must continue
        # without attempting export, and must not be treated as a lost browser.
        records = self.distinct(8)
        def call(action, payload, timeout=75):
            if action != "wos_search":
                raise AssertionError('zero-result search must not attempt export')
            raise SafetyStop('WOS 未找到记录；这不等于未发表，也不自动标记完成')
        bridge = Mock(call=Mock(side_effect=call))
        result = export(all_targets(FakeRoster(records)), bridge,
                        ImportStore(self.root / "imports"), self.inbox)
        self.assertEqual(len(result["failed"]), len(records))
        self.assertEqual(result["not_exported"], len(records))
        self.assertEqual(result["session_failures"], 0)
        self.assertEqual(bridge.call.call_count,len(records))
        self.assertFalse(result['stopped'])

    def test_non_connection_page_failures_visit_every_record(self):
        records = self.distinct(8)
        bridge = Mock(call=Mock(side_effect=SafetyStop('绑定的工作标签页已切换或未登录，请人工返回')))
        result=export(all_targets(FakeRoster(records)), bridge,
                      ImportStore(self.root / "imports"), self.inbox)
        self.assertEqual(result['session_failures'],8)
        self.assertEqual(bridge.call.call_count,8)
        self.assertFalse(result['stopped'])

    def test_field_selector_page_failures_visit_every_record(self):
        records = self.distinct(8)
        bridge = Mock(call=Mock(side_effect=SafetyStop(
            '[扩展 0.3.20] [WOS 已暂停] 检索字段选择器未唯一识别（识别到 0 个）')))
        result = export(all_targets(FakeRoster(records)), bridge,
                        ImportStore(self.root / 'imports'), self.inbox)
        self.assertEqual(result['session_failures'], 8)
        self.assertEqual(result['remaining'], 0)
        self.assertEqual(bridge.call.call_count, 8)
        self.assertFalse(result['disconnected'])
        self.assertFalse(result['stopped'])

    def test_wos_page_timeout_does_not_stop_the_whole_batch(self):
        records = self.distinct(4)
        message = 'WOS 等待检索结果超时，未自动重复操作'
        bridge = Mock(call=Mock(side_effect=SafetyStop(message)))
        result = export(all_targets(FakeRoster(records)), bridge,
                        ImportStore(self.root / 'imports'), self.inbox)
        self.assertFalse(disconnected_outcome(message))
        self.assertEqual(result['attempted'], 4)
        self.assertEqual(result['remaining'], 0)
        self.assertEqual(bridge.call.call_count, 4)
        self.assertFalse(result['disconnected'])
        self.assertFalse(result['stopped'])

    def test_unavailable_bridge_states_stop_the_whole_batch(self):
        records = self.distinct(8)
        messages = (
            '命令超时（网页已接收命令但未返回结果），结果不明。',
            '上一条浏览器命令仍在执行，暂停等待人工核验',
        )
        for index, message in enumerate(messages):
            with self.subTest(message=message):
                bridge = Mock(call=Mock(side_effect=SafetyStop(message)))
                result = export(all_targets(FakeRoster(records)), bridge,
                                ImportStore(self.root / f'imports-{index}'),
                                self.root / f'inbox-{index}')
                self.assertEqual(bridge.call.call_count, 1)
                self.assertEqual(result['attempted'], 1)
                self.assertEqual(len(result['failed']), 1)
                self.assertEqual(result['remaining'], 7)
                self.assertTrue(result['disconnected'])
                self.assertTrue(result['stopped'])
                self.assertTrue(disconnected_outcome(message))

    def test_true_browser_disconnect_stops_the_whole_batch(self):
        records = self.distinct(8)
        bridge = Mock(call=Mock(side_effect=SafetyStop(
            '浏览器未连接。请在已登录的 WOS 或比对页打开扩展并配对。')))
        result=export(all_targets(FakeRoster(records)),bridge,
                      ImportStore(self.root/'imports'),self.inbox)
        self.assertEqual(bridge.call.call_count,1)
        self.assertEqual(result['attempted'],1)
        self.assertEqual(len(result['failed']),1)
        self.assertEqual(result['session_failures'],1)
        self.assertEqual(result['remaining'],7)
        self.assertTrue(result['disconnected'])
        self.assertTrue(result['stopped'])
        self.assertTrue(disconnected_outcome(result['failed'][records[0].sa_id]['error']))

    def test_success_after_three_failures_is_still_downloaded(self):
        records=self.distinct(3)+[self.record]
        bridge=self.bridge_for(sample())
        original=bridge.call.side_effect
        def call(action,payload,timeout=75):
            if payload['sa_id']!=self.record.sa_id:
                raise SafetyStop('WOS 下载中断')
            return original(action,payload,timeout)
        bridge.call.side_effect=call
        result=export(records,bridge,ImportStore(self.root/'downloads'),self.inbox)
        self.assertEqual(len(result['failed']),3)
        self.assertEqual(len(result['exported']),1)
        self.assertEqual(result['exported'][0]['sa_id'],self.record.sa_id)

    def test_bridge_failure_is_not_silently_swallowed(self):
        bridge = Mock(call=Mock(side_effect=RuntimeError("桥接断开")))
        with self.assertRaises(RuntimeError):
            export(self.selected(), bridge, ImportStore(self.root / "imports"), self.inbox)


if __name__ == "__main__":
    unittest.main()
