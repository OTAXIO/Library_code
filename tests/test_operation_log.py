"""Count completed operations, rather than rendered lines, in log.txt."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from operation_log import OperationLog


class OperationLogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "log.txt"
        self.log = OperationLog(self.path)

    def test_last_hundred_operations_survive_reopen_and_multiline_display(self):
        for index in range(105):
            self.log.record(f"第 {index} 次操作\n第二行说明", "已执行", f"demo-{index:03}")
        reopened = OperationLog(self.path).read()
        self.assertEqual(len(reopened), 100)
        self.assertEqual(reopened[0]["sa_id"], "demo-005")
        self.assertEqual(reopened[-1]["sa_id"], "demo-104")
        self.assertEqual(reopened[-1]["action"], "第 104 次操作\n第二行说明")
        self.assertEqual(reopened[-1]["result"], "已执行")
        self.assertGreater(len(self.path.read_text(encoding="utf-8").splitlines()), 100)
        self.assertEqual(len(json.loads(self.path.read_text(encoding="utf-8"))["operations"]), 100)

    def test_paused_result_is_recorded_without_exception_text(self):
        self.log.record("网页认领", "已暂停", "demo-001")
        self.assertEqual(self.log.read()[0]["result"], "已暂停")
        self.assertEqual(set(self.log.read()[0]), {"time", "action", "sa_id", "result"})

    def test_corrupt_existing_log_is_preserved(self):
        self.path.write_text("old unparseable evidence", encoding="utf-8")
        with self.assertRaises(ValueError):
            self.log.record("定位", "已执行")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "old unparseable evidence")

    def test_failed_replace_preserves_previous_log(self):
        self.log.record("定位", "已执行")
        previous = self.path.read_bytes()
        with patch("operation_log.os.replace", side_effect=OSError("locked")):
            with self.assertRaises(OSError):
                self.log.record("认领", "已暂停")
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(len(list(self.path.parent.glob(".log-*.tmp"))), 0)


if __name__ == "__main__":
    unittest.main()
