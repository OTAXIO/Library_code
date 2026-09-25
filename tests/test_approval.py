"""Synthetic claim closure states; no browser or workbook is opened."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from approval import complete_claim
from core import SafetyStop


class ClaimCompletionTests(unittest.TestCase):
    def setUp(self):
        self.record = SimpleNamespace(owner="谭勋策", sa_id="demo-001", done=False,
                                      matches=1, reason="作者不一致", staff_id="00001")
        self.roster = SimpleNamespace(records=[self.record], assert_unchanged=Mock())
        self.row = {"saLzkId": "demo-001", "markStatus": "待处理", "matchCount": 1,
                    "reason": "作者不一致", "gh": "00001", "remark": ""}
        self.comparison = [{"label": "认领状态", "sa": "测试员(00001)①", "library": "已认领"},
                           {"label": "作者信息", "sa": "测试员", "library": "Tester"}]
        self.latest = {"row": copy.deepcopy(self.row), "comparison": copy.deepcopy(self.comparison)}
        self.done = {**self.row, "markStatus": "已处理", "remark": "已认领"}
        self.bridge = Mock()
        self.bridge.call.side_effect = [self.latest, {"row": self.done, "verified": True}]
        self.writer = patch("roster_write.mark_complete", return_value=object())
        self.write = self.writer.start()
        self.addCleanup(self.writer.stop)

    def run_close(self, **kwargs):
        return complete_claim(self.roster, self.record, self.bridge, self.row,
                              self.comparison, reviewed=kwargs.get("reviewed", True))

    def test_backend_verified_before_one_excel_write(self):
        result = self.run_close()
        self.assertFalse(result.already_processed)
        self.assertEqual([c.args[0] for c in self.bridge.call.call_args_list], ["search", "complete"])
        payload = self.bridge.call.call_args.args[1]
        self.assertEqual(payload["note"], "已认领")
        self.assertEqual(payload["expected_comparison"], self.comparison)
        self.write.assert_called_once_with(self.roster, self.record)

    def test_already_processed_only_syncs_without_toggling(self):
        self.bridge.call.side_effect = [{"row": self.done}]
        result = self.run_close()
        self.assertTrue(result.already_processed)
        self.bridge.call.assert_called_once_with("search", {"sa_id": "demo-001"})
        self.write.assert_called_once()

    def test_wrong_owner_multi_match_and_missing_review_stop_before_browser(self):
        for attr, value in (("owner", "其他人"), ("matches", 2), ("reason", "通讯作者不一致"), ("done", True)):
            original = getattr(self.record, attr)
            setattr(self.record, attr, value)
            with self.assertRaises(SafetyStop):
                self.run_close()
            setattr(self.record, attr, original)
        with self.assertRaises(SafetyStop):
            self.run_close(reviewed=False)
        self.bridge.call.assert_not_called()
        self.write.assert_not_called()

    def test_changed_snapshot_or_details_stop_without_write(self):
        for latest in ({**self.latest, "row": {**self.row, "remark": "changed"}},
                       {**self.latest, "comparison": []}):
            self.bridge.call.reset_mock(side_effect=True)
            self.bridge.call.side_effect = [latest]
            with self.assertRaisesRegex(SafetyStop, "发生变化"):
                self.run_close()
            self.assertEqual(self.bridge.call.call_count, 1)
        self.write.assert_not_called()

    def test_unclaimed_or_staff_conflict_cannot_close(self):
        self.comparison[0]["library"] = "未认领"
        self.latest["comparison"] = copy.deepcopy(self.comparison)
        with self.assertRaisesRegex(SafetyStop, "尚未回读"):
            self.run_close()
        self.comparison[0]["library"] = "已认领"
        self.record.staff_id = "00002"
        self.latest["comparison"] = copy.deepcopy(self.comparison)
        self.bridge.call.side_effect = [self.latest]
        with self.assertRaisesRegex(SafetyStop, "人员编号不一致"):
            self.run_close()
        self.write.assert_not_called()

    def test_incomplete_or_wrong_readback_never_marks_excel(self):
        for result in ({"row": self.done, "verified": False},
                       {"row": {**self.done, "remark": ""}, "verified": True},
                       {"row": {**self.done, "saLzkId": "other"}, "verified": True},
                       {"row": self.row, "verified": True}):
            self.bridge.call.side_effect = [self.latest, result]
            with self.assertRaisesRegex(SafetyStop, "禁止重复提交"):
                self.run_close()
        self.write.assert_not_called()

    def test_uncertain_submit_is_never_retried(self):
        self.bridge.call.side_effect = [self.latest, SafetyStop("timeout")]
        with self.assertRaisesRegex(SafetyStop, "timeout"):
            self.run_close()
        self.assertEqual(self.bridge.call.call_count, 2)
        self.write.assert_not_called()

    def test_excel_failure_preserves_verified_backend_result_message(self):
        self.write.side_effect = PermissionError("workbook busy")
        with self.assertRaisesRegex(SafetyStop, "网页已保存为已处理"):
            self.run_close()
        self.assertEqual(self.bridge.call.call_count, 2)


if __name__ == "__main__":
    unittest.main()
