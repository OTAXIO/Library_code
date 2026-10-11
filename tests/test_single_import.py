"""Acceptance target selection is read-only and never clears queue flags."""
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


class TargetSelectionTests(unittest.TestCase):
    def row(self, **changes):
        return Mock(**{"sa_id": "fixture-1", "owner": "谭勋策", "matches": 0,
                       "done": False, "skipped": False, **changes})

    def test_only_one_owned_pending_zero_match_can_be_selected(self):
        row = self.row()
        roster = Mock(records=[row], assert_unchanged=Mock())
        self.assertIs(select_one(roster, "fixture-1"), row)
        roster.assert_unchanged.assert_called_once_with()

    def test_other_owner_nonzero_done_and_skipped_are_not_reopened(self):
        for change in ({"owner": "其他负责人"}, {"matches": 1}, {"done": True}, {"skipped": True}):
            row = self.row(**change)
            with self.assertRaises(SafetyStop):
                select_one(Mock(records=[row], assert_unchanged=Mock()), "fixture-1")
            for field, expected in change.items():
                self.assertEqual(getattr(row, field), expected)

    def test_missing_or_duplicate_ids_fail_closed(self):
        for rows in ([], [self.row(), self.row()]):
            with self.assertRaises(SafetyStop):
                select_one(Mock(records=rows, assert_unchanged=Mock()), "fixture-1")
