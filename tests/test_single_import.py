import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from core import Record, SafetyStop
from maintenance.single_import import select_one


class SingleImportTests(unittest.TestCase):
    def test_explicit_pending_id_not_first_row_is_allowed(self):
        first = Record(2, "谭勋策", "first", "Title", "10.1/first", "", "001", 0, "", "待处理", "", "1")
        second = replace(first, row=3, sa_id="second", doi="10.1/second")
        roster = SimpleNamespace(records=[first, second], assert_unchanged=Mock())
        self.assertEqual(select_one(roster, "second"), second)

    def test_skipped_done_other_owner_and_nonzero_cannot_start(self):
        record = Record(2, "谭勋策", "first", "Title", "10.1/first", "", "001", 0, "", "待处理", "", "1")
        for row in (replace(record, skipped=True), replace(record, done=True),
                    replace(record, owner="其他人"), replace(record, matches=1)):
            with self.assertRaises(SafetyStop):
                select_one(SimpleNamespace(records=[row], assert_unchanged=Mock()), "first")

    def test_changed_roster_cannot_start(self):
        roster = SimpleNamespace(records=[], assert_unchanged=Mock(side_effect=SafetyStop("changed")))
        with self.assertRaises(SafetyStop):
            select_one(roster, "first")
