import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

from core import Record, SafetyStop
from maintenance.recover_rejected_export import validated_recovery, REJECTED, BLOCKED


class RejectedExportRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.record = Record(2, "谭勋策", "synthetic", "Paper", "10.1/a", "", "001", 0, "", "待处理", "", "1")
        self.roster = SimpleNamespace(sha256="a" * 64, assert_unchanged=Mock())
        self.state = {"phase": "export_intent", "record_url":
                      "https://www.webofscience.com/wos/woscc/full-record/WOS:000123456789012"}
        self.report = {"sa_id": self.record.sa_id, "roster_before_sha256": self.roster.sha256,
            "roster_after_sha256": self.roster.sha256, "halted": True, "reason": REJECTED,
            "import_phase_before": "", "import_phase": "", "batch": {}, "workflow_phase": "",
            "excel_done": False, "excel_skipped": False, "imported_and_closed": False,
            "new_import_and_closed": False, "started_utc": "2026-01-01T01:00:00+00:00",
            "finished_utc": "2026-01-01T01:01:00+00:00",
            "outcomes": [{"sa_id": self.record.sa_id, "status": "halted", "message": REJECTED}]}

    def test_exact_pre_click_receipt_can_restore_only_preparation(self):
        result = validated_recovery(self.roster, self.record, self.state, [self.report])
        self.assertEqual(result["phase"], "export_preparing")
        self.assertEqual(result["record_url"], self.state["record_url"])

    def test_later_read_only_block_must_also_be_reviewed_in_order(self):
        later = deepcopy(self.report)
        later.update(reason=BLOCKED, started_utc="2026-01-01T01:02:00+00:00")
        later["outcomes"][0]["message"] = BLOCKED
        self.assertEqual(validated_recovery(self.roster, self.record, self.state,
                                          [self.report, later])["phase"], "export_preparing")
        with self.assertRaises(SafetyStop):
            validated_recovery(self.roster, self.record, self.state, [later, self.report])

    def test_unknown_clicked_changed_or_wrong_record_never_rewind(self):
        for changes in [{"sa_id": "other"}, {"reason": "命令超时"}, {"import_phase": "pushed"},
                        {"excel_done": True}, {"roster_after_sha256": "b" * 64},
                        {"outcomes": []}, {"workflow_phase": "unknown"}]:
            with self.subTest(changes=changes), self.assertRaises(SafetyStop):
                validated_recovery(self.roster, self.record, self.state, [{**self.report, **changes}])
        for state in [{**self.state, "phase": "downloaded"}, {**self.state, "new_attempt": "unknown"},
                      {**self.state, "record_url": "https://example.test/anything"}]:
            with self.subTest(state=state), self.assertRaises(SafetyStop):
                validated_recovery(self.roster, self.record, state, [self.report])
