"""Offline pilot tests; all workbook rows and browser replies are synthetic."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from openpyxl import load_workbook

from core import SafetyStop, read_roster
from pilot import pilot_scope, precheck_pilot
from tests.test_roster_write import make_roster


class PilotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "list.xlsx"
        self.manifest = Path(self.tmp.name) / "pilot-100.json"
        make_roster(self.path)
        book = load_workbook(self.path)
        book.active["B2"] = "谭勋策"
        book.active["B4"] = "谭勋策"
        book.save(self.path)
        book.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_fixed_scope_and_precheck_only_sync_processed(self):
        roster = read_roster(self.path)
        ids = pilot_scope(roster, self.manifest, limit=2)
        self.assertEqual(ids, ["demo-001", "demo-003"])
        browser = Mock()
        def reply(action, payload):
            self.assertEqual(action, "search")
            sa_id = payload["sa_id"]
            return {"row": {"saLzkId": sa_id,
                            "markStatus": "已处理" if sa_id == "demo-001" else "待处理",
                            "matchCount": 1, "itemId": "1234567890123456789"}}
        browser.call.side_effect = reply
        result = precheck_pilot(roster, ids, browser)
        self.assertEqual(result.checked, 2)
        self.assertEqual(result.synced_ids, ["demo-001"])
        self.assertEqual(result.routes, {"manual": 1})
        self.assertTrue(read_roster(self.path).records[0].done)
        self.assertFalse(read_roster(self.path).records[2].done)
        self.assertEqual(pilot_scope(read_roster(self.path), self.manifest, limit=2), ids)
        self.assertEqual(browser.call.call_count, 2)

    def test_other_owner_and_changed_id_stop_before_browser_access(self):
        roster = read_roster(self.path)
        browser = Mock()
        with self.assertRaises(SafetyStop):
            precheck_pilot(roster, ["demo-005"], browser)
        with self.assertRaises(SafetyStop):
            precheck_pilot(roster, ["unknown"], browser)
        browser.call.assert_not_called()

    def test_cancel_stops_before_next_record(self):
        roster = read_roster(self.path)
        ids = pilot_scope(roster, self.manifest, limit=2)
        browser = Mock()
        result = precheck_pilot(roster, ids, browser, cancelled=lambda: True)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.checked, 0)
        browser.call.assert_not_called()


if __name__ == "__main__":
    unittest.main()
