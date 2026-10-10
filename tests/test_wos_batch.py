"""Extension-only download boundary; all TXT files are disposable fixtures."""
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

from automation import ImportStore
from core import Record, SafetyStop
from bridge import BrowserRejected
from tests.test_automation import sample
from wos_batch import WOSDownload, default_store, preflight


class PreflightTests(unittest.TestCase):
    def capabilities(self, **page):
        return {"extension_version": "0.4.9", "wos_download_protocol": 1, "search_prepare_protocol": 1, "export_prepare_protocol": 1,
                "result_reader": "shared-diagnostic", "read_results_world": "ISOLATED",
                "page": {"core_search_route": True, "query_input_count": 1,
                         "wos_error": False, "site_timeout": False, "login_required": False, "dialog_count": 0, "busy": False, **page}}

    def test_extension_protocol_checked_before_search(self):
        bridge = Mock(call=Mock(return_value=self.capabilities()))
        self.assertEqual(preflight(bridge), {"extension_version": "0.4.9"})
        bridge.call.assert_called_once_with("wos_diagnose", {}, timeout=25)

    def test_old_incompatible_and_alternate_transport_fail_without_search(self):
        for result in ({}, {**self.capabilities(), "extension_version": "0.3.28"},
                       {**self.capabilities(), "extension_version": "0.4.0"},
                       {**self.capabilities(), "extension_version": "0.4.1"},
                       {**self.capabilities(), "extension_version": "0.4.2"},
                       {**self.capabilities(), "extension_version": "0.4.3"},
                       {**self.capabilities(), "extension_version": "0.4.4"},
                       {**self.capabilities(), "extension_version": "0.4.5"},
                       {**self.capabilities(), "extension_version": "0.4.6"},
                       {**self.capabilities(), "extension_version": "0.4.7"},
                       {**self.capabilities(), "extension_version": "0.4.8"},
                       {**self.capabilities(), "export_prepare_protocol": True},
                       {**self.capabilities(), "search_prepare_protocol": True},
                       {**self.capabilities(), "search_prepare_protocol": None},
                       {**self.capabilities(), "wos_download_protocol": True},
                       {**self.capabilities(), "read_results_world": "MAIN"},
                       {"transport": "browser-skill", "wos_download_protocol": 2}):
            with self.subTest(result=result):
                bridge = Mock(call=Mock(return_value=result))
                with self.assertRaisesRegex(SafetyStop, "重载"):
                    preflight(bridge)
                self.assertEqual(bridge.call.call_count, 1)

    def test_old_action_and_disconnect_have_distinct_messages(self):
        for message, expected in (("未知 WOS 调度命令", "仍是旧版本"), ("浏览器未连接", "浏览器未连接")):
            bridge = Mock(call=Mock(side_effect=SafetyStop(message)))
            with self.assertRaisesRegex(SafetyStop, expected):
                preflight(bridge)

    def test_site_error_login_and_dialog_are_not_paper_misses(self):
        for page, expected in (({"wos_error": True}, "不是论文零结果"),
                               ({"site_timeout": True}, "不是论文零结果"),
                               ({"login_required": True}, "登录或验证码"),
                               ({"dialog_count": 1}, "操作弹窗")):
            bridge = Mock(call=Mock(return_value=self.capabilities(**page)))
            with self.assertRaisesRegex(SafetyStop, expected):
                preflight(bridge)
            self.assertEqual(bridge.call.call_count, 1)

    def test_untyped_readiness_fails(self):
        for page in (None, {}, self.capabilities(query_input_count=True)["page"],
                     self.capabilities(site_timeout=None)["page"],
                     self.capabilities(dialog_count=-1)["page"]):
            result = {**self.capabilities(), "page": page}
            with self.assertRaisesRegex(SafetyStop, "完整的页面就绪"):
                preflight(Mock(call=Mock(return_value=result)))

    def test_result_page_does_not_require_search_inputs(self):
        self.assertEqual(preflight(Mock(call=Mock(return_value=self.capabilities(
            core_search_route=False, query_input_count=0)))), {"extension_version": "0.4.9"})

    def test_smart_navigation_only_and_delayed_inputs_do_not_block_preparation(self):
        bridge = Mock(call=Mock(return_value=self.capabilities(query_input_count=0, busy=True)))
        self.assertEqual(preflight(bridge), {"extension_version": "0.4.9"})
        bridge.call.assert_called_once_with("wos_diagnose", {}, timeout=25)

    def test_only_known_tab_delimited_preview_can_pass_dialog_gate(self):
        self.assertEqual(preflight(Mock(call=Mock(return_value=self.capabilities(
            dialog_count=2, export_dialog=True))), resume_export=True), {"extension_version": "0.4.9"})
        with self.assertRaisesRegex(SafetyStop, "操作弹窗"):
            preflight(Mock(call=Mock(return_value=self.capabilities(dialog_count=2, export_dialog=True))))
        for flag in (None, False, "true", 1):
            with self.assertRaisesRegex(SafetyStop, "操作弹窗"):
                preflight(Mock(call=Mock(return_value=self.capabilities(dialog_count=1, export_dialog=flag))), resume_export=True)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.record = Record(2, "谭勋策", "demo-1", "Synthetic paper", "10.1234/test", "", "00001", 0, "", "待处理", "", "1")
        self.path = self.root / "real.txt"
        self.raw = sample()
        self.path.write_bytes(self.raw)
        self.url = "https://webofscience.clarivate.cn/wos/woscc/full-record/WOS:000123456789012"
        self.bridge = Mock(call=Mock(return_value={"sa_id": "demo-1", "path": str(self.path),
                                                 "ready": True, "record_url": self.url}))
        self.store = ImportStore(self.root / "downloads")
        self.stop = threading.Event()
        self.check = Mock()
        self.audit = Mock()
        self.flow = WOSDownload(self.bridge, self.store, self.check, self.stop, self.audit,
                                download_dir=self.root / "browser-downloads")

    def test_download_archives_exact_reported_bytes_then_reuses_without_browser(self):
        result = self.flow.prepare(self.record)
        self.assertTrue(result["identity_confirmed"])
        self.assertEqual(self.store.bytes(result), self.raw)
        self.assertEqual([c.args[0] for c in self.bridge.call.call_args_list], ["wos_search", "wos_export_prepare", "wos_export"])
        self.assertEqual([c.kwargs["timeout"] for c in self.bridge.call.call_args_list], [120, 75, 75])
        self.flow.prepare(self.record)
        self.assertEqual(self.bridge.call.call_count, 3)

    def test_wrong_id_relative_or_missing_path_not_adopted(self):
        for result in (None, {"sa_id": "other", "path": str(self.path)},
                       {"sa_id": "demo-1", "path": "relative.txt"},
                       {"sa_id": "demo-1", "path": str(self.root / "missing.txt")}):
            self.bridge.call.side_effect = [dict(ready=True, record_url=self.url), dict(ready=True, record_url=self.url), result]
            with self.assertRaises(SafetyStop):
                self.flow.prepare(self.record)
            self.assertNotEqual((self.store.get(self.record) or {}).get("phase"), "downloaded")
            # Give each invalid returned-path trial a fresh, independent journal.
            with self.store.connect() as db:
                db.execute("DELETE FROM imports")

    def test_conflicting_doi_not_adopted(self):
        self.path.write_bytes(sample(DI="10.1234/other"))
        with self.assertRaisesRegex(SafetyStop, "与名单冲突"):
            self.flow.prepare(self.record)
        self.assertEqual(self.store.get(self.record)["phase"], "export_intent")

    def test_no_results_does_not_attempt_export(self):
        self.bridge.call.side_effect = SafetyStop("WOS 未找到记录")
        with self.assertRaisesRegex(SafetyStop, "未找到记录"):
            self.flow.prepare(self.record)
        self.assertEqual(self.bridge.call.call_count, 1)

    def test_pause_after_search_never_exports(self):
        def call(*args, **kwargs):
            self.stop.set()
            return {}
        self.bridge.call.side_effect = call
        with self.assertRaisesRegex(SafetyStop, "已暂停"):
            self.flow.prepare(self.record)
        self.assertEqual(self.bridge.call.call_count, 1)

    def test_default_store_is_independent_of_classification(self):
        self.assertEqual(default_store(self.root).root, self.root / "runtime" / "wos-downloads")

    def make_download(self, raw=None):
        folder = self.flow.download_dir
        folder.mkdir(exist_ok=True)
        path = folder / "savedrecs (1).txt"
        path.write_bytes(raw or self.raw)
        return path

    def test_existing_manual_download_reused_without_search_or_export(self):
        path = self.make_download()
        result = self.flow.prepare(self.record)
        self.bridge.call.assert_not_called()
        self.assertEqual(self.store.bytes(result), self.raw)
        self.assertIn(self.record.sa_id, Path(result["saved_path"]).name)
        self.assertEqual(path.read_bytes(), self.raw)

    def test_failed_export_recovers_actual_late_file_without_reclick(self):
        def invoke(action, payload, **kwargs):
            if action == "wos_export":
                self.make_download()
                raise SafetyStop("未取得来源回执")
            return {"ready": True, "record_url": self.url}
        self.bridge.call.side_effect = invoke
        result = self.flow.prepare(self.record)
        self.assertEqual(result["phase"], "downloaded")
        self.assertEqual([c.args[0] for c in self.bridge.call.call_args_list].count("wos_export"), 1)

    def test_uncertain_final_export_is_not_repeated_after_restart(self):
        self.store.save(self.record, {"phase": "export_intent", "record_url": self.url})
        self.bridge.call.return_value = {"state": "submitted"}
        with self.assertRaisesRegex(SafetyStop, "不重复导出"):
            self.flow.prepare(self.record)
        self.assertEqual([c.args[0] for c in self.bridge.call.call_args_list], ["wos_export_status"])
        self.assertEqual(self.store.get(self.record)["phase"], "export_intent")

    def test_explicit_pre_click_rejection_keeps_a_safe_preparing_checkpoint(self):
        error = "[扩展 0.4.9] [WOS 已暂停] 未处于 WOS 核心合集单篇完整记录页"
        self.bridge.call.side_effect = [dict(ready=True, record_url=self.url),
            dict(ready=True, record_url=self.url), BrowserRejected("wos_export", error)]
        with self.assertRaises(BrowserRejected):
            self.flow.prepare(self.record)
        state = self.store.get(self.record)
        self.assertEqual(state["phase"], "export_preparing")
        self.assertEqual(state["rejected_before_click"], error)
        self.bridge.call.side_effect = None
        self.flow.prepare(self.record)
        self.assertEqual([c.args[0] for c in self.bridge.call.call_args_list][-2:],
                         ["wos_export_prepare", "wos_export"])

    def test_unknown_replies_and_timeouts_never_rewind_final_export(self):
        pre_click = "[扩展 0.4.9] [WOS 已暂停] 未处于 WOS 核心合集单篇完整记录页"
        errors = [SafetyStop(pre_click), BrowserRejected("other_action", pre_click),
                  BrowserRejected("wos_export", "导出预览失效或已提交"),
                  BrowserRejected("wos_export", "命令超时"),
                  BrowserRejected("wos_export", pre_click + " extra")]
        for index, error in enumerate(errors):
            with self.subTest(error=str(error)):
                self.flow.store = ImportStore(self.root / f"unknown-{index}")
                self.bridge.call.side_effect = [dict(ready=True, record_url=self.url),
                    dict(ready=True, record_url=self.url), error]
                with patch("wos_batch.time.sleep"), patch.object(self.stop, "wait", return_value=False):
                    with self.assertRaises(SafetyStop):
                        self.flow.prepare(self.record)
                self.assertEqual(self.flow.store.get(self.record)["phase"], "export_intent")

    def test_uncertain_export_then_manual_file_resumes_without_any_browser_action(self):
        self.store.save(self.record, {"phase": "export_intent", "record_url": self.url})
        self.make_download()
        self.assertEqual(self.flow.prepare(self.record)["phase"], "downloaded")
        self.bridge.call.assert_not_called()

    def test_title_only_old_intent_recovers_correlated_txt_on_search_page_without_browser(self):
        record = replace(self.record, doi="")
        self.store.save(record, {"phase": "export_intent", "record_url": self.url})
        self.make_download()
        result = self.flow.prepare(record)
        self.assertEqual(result["phase"], "downloaded")
        self.assertFalse(result["identity_confirmed"], "recovery is not automatic import authorization")
        self.bridge.call.assert_not_called()

    def test_unsubmitted_owned_preview_is_only_permitted_export_resume(self):
        self.store.save(self.record, {"phase": "export_intent", "record_url": self.url})
        self.bridge.call.side_effect = [{"state": "unsubmitted"}, {"sa_id": self.record.sa_id, "path": str(self.path)}]
        self.flow.prepare(self.record)
        self.assertEqual([c.args[0] for c in self.bridge.call.call_args_list], ["wos_export_status", "wos_export"])
        self.assertTrue(self.bridge.call.call_args.args[1]["prepared"])

    def test_preparation_resume_does_not_repeat_search(self):
        self.store.save(self.record, {"phase": "export_preparing", "record_url": self.url})
        self.flow.prepare(self.record)
        self.assertEqual([c.args[0] for c in self.bridge.call.call_args_list], ["wos_export_prepare", "wos_export"])

    def test_lost_preparing_page_can_find_same_ut_once_then_export(self):
        self.store.save(self.record, {"phase": "export_preparing", "record_url": self.url})
        error = BrowserRejected("wos_export_prepare", "[扩展 0.4.9] [WOS 已暂停] 未处于 WOS 核心合集单篇完整记录页")
        self.bridge.call.side_effect = [error, {"record_url": self.url},
            {"ready": True, "record_url": self.url}, {"sa_id": self.record.sa_id, "path": str(self.path)}]
        self.assertEqual(self.flow.prepare(self.record)["phase"], "downloaded")
        self.assertEqual([c.args[0] for c in self.bridge.call.call_args_list],
                         ["wos_export_prepare", "wos_search", "wos_export_prepare", "wos_export"])

    def test_lost_preparation_cannot_select_a_new_ut_or_loop(self):
        error = BrowserRejected("wos_export_prepare", "[WOS 已暂停] 未处于 WOS 核心合集单篇完整记录页")
        for search, last in (({"record_url": self.url.replace('789012', '789013')}, None),
                             ({"record_url": self.url}, error)):
            self.store.save(self.record, {"phase": "export_preparing", "record_url": self.url})
            self.bridge.call.reset_mock()
            self.bridge.call.side_effect = [error, search] + ([last] if last else [])
            with self.assertRaises(SafetyStop):
                self.flow.prepare(self.record)
            calls = [c.args[0] for c in self.bridge.call.call_args_list]
            self.assertEqual(calls.count("wos_search"), 1)
            self.assertNotIn("wos_export", calls)

    def test_unrelated_file_cannot_resolve_unknown_export(self):
        self.store.save(self.record, {"phase": "export_intent", "record_url": self.url})
        self.make_download(sample(UT="WOS:000999999999999"))
        self.bridge.call.return_value = {"state": "unknown"}
        with self.assertRaisesRegex(SafetyStop, "结果不明"):
            self.flow.prepare(self.record)
        self.assertEqual(self.store.get(self.record)["phase"], "export_intent")

    def test_title_only_never_adopts_download_history(self):
        self.make_download()
        record = replace(self.record, doi="")
        result = self.flow.prepare(record)
        self.assertFalse(result["identity_confirmed"])
        self.assertEqual([c.args[0] for c in self.bridge.call.call_args_list][0], "wos_search")

    def test_changed_title_or_search_ut_cannot_save_returned_file(self):
        for index, raw in enumerate((sample(TI="Different paper"), sample(UT="WOS:000999999999999"))):
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                self.store = ImportStore(self.root / ("mismatch-" + str(index)))
                self.flow.store = self.store
                with self.assertRaisesRegex(SafetyStop, "不一致"):
                    self.flow.prepare(self.record)
                self.assertEqual(self.store.get(self.record)["phase"], "export_intent")

    def test_invalid_scope_does_not_even_search_or_adopt_local_file(self):
        self.make_download()
        for changes in ({"owner": "其他人"}, {"done": True}, {"matches": 1}):
            with self.assertRaisesRegex(SafetyStop, "未完成零匹配"):
                self.flow.prepare(replace(self.record, **changes))
        self.bridge.call.assert_not_called()
        self.assertIsNone(self.store.get(self.record))

    def test_missing_hash_archive_restored_from_identical_download_only(self):
        self.make_download()
        state = self.flow.prepare(self.record)
        (self.store.root / (state["candidate"]["sha256"] + ".txt")).unlink()
        restored = self.flow.prepare(self.record)
        self.assertEqual(self.store.bytes(restored), self.raw)
        self.bridge.call.assert_not_called()

    def test_missing_hash_archive_cannot_be_replaced_by_different_content(self):
        path = self.make_download()
        state = self.flow.prepare(self.record)
        (self.store.root / (state["candidate"]["sha256"] + ".txt")).unlink()
        path.write_bytes(sample(PY="2025"))
        with self.assertRaisesRegex(SafetyStop, "原存档校验码不一致"):
            self.flow.prepare(self.record)
        self.bridge.call.assert_not_called()
        self.assertEqual(self.store.get(self.record)["candidate"]["sha256"], state["candidate"]["sha256"])
