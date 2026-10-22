"""Synthetic, read-only observers: no real roster, credentials, or browser."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from core import Record, SafetyStop
from operation_log import OperationLog
from run_diagnostics import RunDiagnostics, TracedBridge, safe_text
from workflow_service import TaskResult, WorkflowService
from zero_match import verify_sa


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.log = OperationLog(self.base / "log.txt")
        self.record = Record(2, "谭勋策", "synthetic-001", "Synthetic", "", "", "0001", 0, "", "待处理", "", "2")
        self.roster = SimpleNamespace(records=[self.record], assert_unchanged=Mock())

    def diagnostic(self, bridge=None):
        return RunDiagnostics(self.base, "metadata-import", "pending", [self.record], self.log, Mock(), bridge)

    def test_pending_is_persisted_before_the_only_dispatch_and_payload_is_never_saved(self):
        backend = Mock(online=True, token="synthetic-private-token")
        d = self.diagnostic(backend)
        def call(action, payload, timeout=90):
            saved = json.loads(d.path.read_text(encoding="utf-8"))
            self.assertEqual(saved["pending_command"]["action"], "import_excel_submit")
            self.assertNotIn("content", saved["pending_command"])
            return {"submitted": True, "cookie": "should-never-be-logged"}
        backend.call.side_effect = call
        answer = TracedBridge(backend, d).call("import_excel_submit", {"sa_id": self.record.sa_id, "content": "SECRET XLSX"})
        self.assertTrue(answer["submitted"])
        backend.call.assert_called_once()
        saved = d.path.read_text(encoding="utf-8")
        self.assertNotIn("SECRET", saved)
        self.assertNotIn("should-never-be-logged", saved)
        self.assertNotIn("synthetic-private-token", saved)
        self.assertEqual(json.loads(saved)["commands"][0]["status"], "returned")

    def test_failed_ack_has_error_and_stack_without_replaying_or_logging_secrets(self):
        backend = Mock(online=True, token="synthetic-private-token")
        backend.call.side_effect = SafetyStop("lost ack token=abc Bearer xyz https://example.org/?api_key=foo synthetic-private-token")
        d = self.diagnostic(backend)
        with self.assertRaises(SafetyStop) as stopped:
            TracedBridge(backend, d).call("import_excel_submit", {"sa_id": self.record.sa_id})
        d.failed(stopped.exception)
        backend.call.assert_called_once()
        raw = d.path.read_text(encoding="utf-8")
        for secret in ("synthetic-private-token", "abc", "xyz", "foo"):
            self.assertNotIn(secret, raw)
            self.assertNotIn(secret, self.log.path.read_text(encoding="utf-8"))
        saved = json.loads(raw)
        self.assertEqual(saved["status"], "error")
        self.assertEqual(saved["commands"][0]["error"]["type"], "SafetyStop")
        self.assertTrue(saved["commands"][0]["error"]["stack"])
        self.assertEqual(self.log.read()[-1]["result"], "已暂停")

    def test_report_failure_never_suppresses_original_result_or_creates_a_retry(self):
        backend = Mock(token="", online=True)
        backend.call.return_value = {"verified": True}
        with patch("run_diagnostics.os.replace", side_effect=OSError("locked")):
            d = self.diagnostic(backend)
            self.assertTrue(TracedBridge(backend, d).call("status", {})["verified"])
        backend.call.assert_called_once()
        self.assertFalse(d.saved)
        self.assertEqual(list(d.path.parent.glob(".run-*.tmp")), [])

    def test_service_saves_non_browser_exception_and_keeps_exact_type(self):
        service = WorkflowService(self.base)
        backend = Mock(token="", online=True)
        failure = ValueError("fixture corrupt cache")
        with patch.object(service, "_run", side_effect=failure):
            with self.assertRaises(ValueError) as caught:
                service.run(self.roster, [self.record], "import", "pending", backend, threading.Event(), Mock(), metadata_import=True)
        self.assertIs(caught.exception, failure)
        saved = json.loads(Path(service.last_report_path).read_text(encoding="utf-8"))
        self.assertEqual(saved["error"]["type"], "ValueError")
        self.assertEqual(saved["error"]["message"], "fixture corrupt cache")
        backend.call.assert_not_called()

    def test_validation_pause_after_successful_status_is_not_logged_as_success(self):
        service = WorkflowService(self.base)
        backend = Mock(token="", online=True)
        backend.call.return_value = {"verified": True}
        def work(*args, **kwargs):
            observed = args[4]
            observed.call("status", {"sa_id": self.record.sa_id})
            return TaskResult(self.roster, {}, "暂停", halted=True, reason="后台事实不一致")
        with patch.object(service, "_run", side_effect=work):
            result = service.run(self.roster, [self.record], "import", "pending", backend, threading.Event(), Mock())
        saved = json.loads(Path(result.report_path).read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "paused")
        self.assertEqual(saved["reason"], "后台事实不一致")
        self.assertIn("诊断", result.summary)
        self.assertEqual(service.operation_log.read()[-1]["detail"], "后台事实不一致")
        backend.call.assert_called_once()

    def test_manual_locator_is_also_diagnosed_without_changing_transport(self):
        service = WorkflowService(self.base)
        backend = Mock(token="", online=True)
        with patch.object(service, "_locate_import", side_effect=SafetyStop("fixture WOS timeout")):
            with self.assertRaises(SafetyStop):
                service.locate_import(self.roster, self.record, "谭勋策", "pending", backend, threading.Event(), Mock())
        saved = json.loads(Path(service.last_report_path).read_text(encoding="utf-8"))
        self.assertEqual(saved["mode"], "wos-locate")
        self.assertEqual(saved["error"]["message"], "fixture WOS timeout")

    def test_json_credentials_and_url_queries_are_redacted(self):
        redacted = safe_text('{"api_key":"verysecret", "password":"private"} https://user:pw@api.openalex.org/works?api_key=hidden')
        for secret in ("verysecret", "private", "user:pw", "hidden"):
            self.assertNotIn(secret, redacted)

    def test_sa_mismatch_names_fields_without_disclosing_personnel_values(self):
        row = {'saLzkId': self.record.sa_id, 'markStatus': '待处理', 'titleValue': self.record.title,
               'doiValue': '', 'wosValue': '', 'gh': 'private-wrong-person'}
        with self.assertRaisesRegex(SafetyStop, '人员编号') as caught:
            verify_sa(self.record, row)
        self.assertNotIn('private-wrong-person', str(caught.exception))
        self.assertNotIn(self.record.staff_id, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
