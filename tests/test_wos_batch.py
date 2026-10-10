"""Extension-only download boundary; all TXT files are disposable fixtures."""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from automation import ImportStore
from core import Record, SafetyStop
from tests.test_automation import sample
from wos_batch import WOSDownload, default_store, preflight


class PreflightTests(unittest.TestCase):
    def capabilities(self, **page):
        return {"extension_version": "0.4.1", "wos_download_protocol": 1,
                "result_reader": "shared-diagnostic", "read_results_world": "ISOLATED",
                "page": {"core_search_route": True, "query_input_count": 1,
                         "wos_error": False, "login_required": False, "dialog_count": 0, "busy": False, **page}}

    def test_extension_protocol_checked_before_search(self):
        bridge = Mock(call=Mock(return_value=self.capabilities()))
        self.assertEqual(preflight(bridge), {"extension_version": "0.4.1"})
        bridge.call.assert_called_once_with("wos_diagnose", {}, timeout=25)

    def test_old_incompatible_and_alternate_transport_fail_without_search(self):
        for result in ({}, {**self.capabilities(), "extension_version": "0.3.28"},
                       {**self.capabilities(), "extension_version": "0.4.0"},
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

    def test_site_error_login_dialog_and_missing_controls_are_not_paper_misses(self):
        for page, expected in (({"wos_error": True}, "不是论文零结果"),
                               ({"site_timeout": True}, "不是论文零结果"),
                               ({"login_required": True}, "登录或验证码"),
                               ({"dialog_count": 1}, "操作弹窗"),
                               ({"query_input_count": 0}, "未将任何论文标为跳过")):
            bridge = Mock(call=Mock(return_value=self.capabilities(**page)))
            with self.assertRaisesRegex(SafetyStop, expected):
                preflight(bridge)
            self.assertEqual(bridge.call.call_count, 1)

    def test_untyped_readiness_fails(self):
        for page in (None, {}, self.capabilities(query_input_count=True)["page"],
                     self.capabilities(dialog_count=-1)["page"]):
            result = {**self.capabilities(), "page": page}
            with self.assertRaisesRegex(SafetyStop, "完整的页面就绪"):
                preflight(Mock(call=Mock(return_value=result)))

    def test_result_page_does_not_require_search_inputs(self):
        self.assertEqual(preflight(Mock(call=Mock(return_value=self.capabilities(
            core_search_route=False, query_input_count=0)))), {"extension_version": "0.4.1"})


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.record = Record(2, "谭勋策", "demo-1", "Synthetic paper", "10.1234/test", "", "00001", 0, "", "待处理", "", "1")
        self.path = self.root / "real.txt"
        self.raw = sample()
        self.path.write_bytes(self.raw)
        self.bridge = Mock(call=Mock(return_value={"sa_id": "demo-1", "path": str(self.path)}))
        self.store = ImportStore(self.root / "downloads")
        self.stop = threading.Event()
        self.check = Mock()
        self.audit = Mock()
        self.flow = WOSDownload(self.bridge, self.store, self.check, self.stop, self.audit)

    def test_download_archives_exact_reported_bytes_then_reuses_without_browser(self):
        result = self.flow.prepare(self.record)
        self.assertTrue(result["identity_confirmed"])
        self.assertEqual(self.store.bytes(result), self.raw)
        self.assertEqual([c.args[0] for c in self.bridge.call.call_args_list], ["wos_search", "wos_export"])
        self.assertEqual([c.kwargs["timeout"] for c in self.bridge.call.call_args_list], [120, 75])
        self.flow.prepare(self.record)
        self.assertEqual(self.bridge.call.call_count, 2)

    def test_wrong_id_relative_or_missing_path_not_adopted(self):
        for result in (None, {"sa_id": "other", "path": str(self.path)},
                       {"sa_id": "demo-1", "path": "relative.txt"},
                       {"sa_id": "demo-1", "path": str(self.root / "missing.txt")}):
            self.bridge.call.return_value = result
            with self.assertRaises(SafetyStop):
                self.flow.prepare(self.record)
            self.assertIsNone(self.store.get(self.record))

    def test_conflicting_doi_not_adopted(self):
        self.path.write_bytes(sample(DI="10.1234/other"))
        with self.assertRaisesRegex(SafetyStop, "与名单冲突"):
            self.flow.prepare(self.record)
        self.assertIsNone(self.store.get(self.record))

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
