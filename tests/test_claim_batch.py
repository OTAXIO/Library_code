"""Automatic claim queue tests use synthetic records and a mocked browser."""
import copy
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch

from approval import COMBINED_MISSING_IDS_CLAIM
from claim_batch import automatic_claim_selection, run_claim_batch
from core import Record, SafetyStop


def record(sa_id="demo-001", **changes):
    base = Record(2, "谭勋策", sa_id, "Synthetic", "", "", "00001", 1, "item-1",
                  "待处理", "作者不一致", "2")
    return replace(base, **changes)


def found(rec):
    row = {"saLzkId": rec.sa_id, "itemId": "item-1", "matchCount": 1,
           "markStatus": "待处理", "reason": "作者不一致", "gh": "00001", "remark": ""}
    comparison = [{"label": "认领状态", "sa": "测试员(00001)①", "library": "未认领"},
                  {"label": "作者信息", "sa": "测试员", "library": "Tester"}]
    return {"row": row, "comparison": comparison}


def prepared(search):
    data = {"staff_id": "00001", "sa_text": "测试员(00001)①", "item_id": "item-1",
            "person": {"id": "scholar-1", "wno": "00001", "name": "测试员",
                       "names": ["测试员", "Tester"]},
            "authors": [{"index": 0, "order": 1, "fullname": "Tester", "scholarId": "",
                         "eligible": True, "relations": []}]}
    return {"row": copy.deepcopy(search["row"]), "comparison": copy.deepcopy(search["comparison"]),
            "prepared": data, "suggested_index": 0}


class FakeRoster:
    def __init__(self, records):
        self.records = records
        self.assert_unchanged = Mock()


def persist_skips(roster, records):
    ids = {item.sa_id for item in records}
    updated = [replace(item, skipped=True, remark="2") if item.sa_id in ids else item
               for item in roster.records]
    return SimpleNamespace(roster=FakeRoster(updated))


class ClaimBatchTests(unittest.TestCase):
    def test_selection_requires_one_exact_suggested_signature(self):
        rec = record()
        search = found(rec)
        result = prepared(search)
        selected = automatic_claim_selection(rec, search["row"], search["comparison"], result)
        self.assertEqual(selected[2]["fullname"], "Tester")
        for change in (None, True, 9):
            bad = copy.deepcopy(result)
            bad["suggested_index"] = change
            with self.assertRaises(SafetyStop):
                automatic_claim_selection(rec, search["row"], search["comparison"], bad)
        bad = copy.deepcopy(result)
        bad["prepared"]["person"]["names"] = ["Another"]
        with self.assertRaises(SafetyStop):
            automatic_claim_selection(rec, search["row"], search["comparison"], bad)

    def test_skips_red_candidate_then_completes_next_without_manual_approval(self):
        skipped = record("demo-skip", reason="通讯作者不一致")
        target = record("demo-ok")
        roster = FakeRoster([skipped, target])
        search = found(target)
        search["row"]["saLzkId"] = target.sa_id
        prep = prepared(search)
        proof = {"row": search["row"], "verified": True, "claimed": True, "staff_id": "00001",
                 "scholar_id": "scholar-1", "author": "Tester", "order": 1}
        bridge = Mock()
        bridge.call.side_effect = [search, prep, proof]
        completed_roster = FakeRoster([replace(skipped), replace(target, done=True)])
        closure = SimpleNamespace(completion=SimpleNamespace(roster=completed_roster))
        audit = Mock()
        with patch("claim_batch.auto_complete_claim", return_value=closure) as close:
            result = run_claim_batch(roster, [skipped, target], bridge, audit=audit,
                                     skip_writer=persist_skips)
        self.assertEqual(result.completed_ids, ("demo-ok",))
        self.assertIn("demo-skip", result.skipped)
        self.assertFalse(result.halted)
        self.assertEqual([call.args[0] for call in bridge.call.call_args_list],
                         ["search", "prepare_claim", "submit_claim"])
        self.assertTrue(bridge.call.call_args_list[-1].args[1]["confirmed"])
        close.assert_called_once()
        self.assertIn(("自动认领列表条目", "已跳过", "demo-skip"),
                      [call.args for call in audit.call_args_list])
        self.assertTrue(next(item for item in result.roster.records
                             if item.sa_id == "demo-skip").skipped)

    def test_ambiguous_author_closes_owned_drawer_and_continues(self):
        first, second = record("demo-a"), record("demo-b")
        roster = FakeRoster([first, second])
        search_a, search_b = found(first), found(second)
        ambiguous = prepared(search_a)
        ambiguous["suggested_index"] = None
        cleanup = copy.deepcopy(search_a)
        prep_b = prepared(search_b)
        proof = {"row": search_b["row"], "verified": True, "claimed": True, "staff_id": "00001",
                 "scholar_id": "scholar-1", "author": "Tester", "order": 1}
        bridge = Mock()
        bridge.call.side_effect = [search_a, ambiguous, cleanup, search_b, prep_b, proof]
        closed_roster = FakeRoster([first, replace(second, done=True)])
        with patch("claim_batch.auto_complete_claim",
                   return_value=SimpleNamespace(completion=SimpleNamespace(roster=closed_roster))):
            result = run_claim_batch(roster, [first, second], bridge,
                                     skip_writer=persist_skips)
        self.assertIn(first.sa_id, result.skipped)
        self.assertEqual(result.completed_ids, (second.sa_id,))
        self.assertEqual([call.args[0] for call in bridge.call.call_args_list],
                         ["search", "prepare_claim", "search", "search", "prepare_claim", "submit_claim"])

    def test_missing_doi_and_wos_unclaimed_uses_combined_note(self):
        target = record("demo-combined", reason="DOI和WOSID都不一致")
        roster = FakeRoster([target])
        search = found(target)
        search["row"]["reason"] = target.reason
        search["comparison"].extend([
            {"label": "DOI", "sa": "", "library": "10.1000/example"},
            {"label": "WOS记录号", "sa": "", "library": "WOS:000000000000001"},
        ])
        prep = prepared(search)
        proof = {"row": search["row"], "verified": True, "claimed": True, "staff_id": "00001",
                 "scholar_id": "scholar-1", "author": "Tester", "order": 1}
        bridge = Mock()
        bridge.call.side_effect = [search, prep, proof]
        closed_roster = FakeRoster([replace(target, done=True)])
        closure = SimpleNamespace(completion=SimpleNamespace(roster=closed_roster))
        with patch("claim_batch.auto_complete_claim", return_value=closure) as close:
            result = run_claim_batch(roster, [target], bridge)
        self.assertEqual(result.completed_ids, (target.sa_id,))
        self.assertEqual(close.call_args.kwargs["note"], COMBINED_MISSING_IDS_CLAIM)

    def test_combined_claim_allows_auxiliary_display_differences(self):
        target = record("demo-combined-extra", reason="DOI和WOSID都不一致;作者不一致")
        roster = FakeRoster([target])
        search = found(target)
        search["row"]["reason"] = target.reason
        search["comparison"].extend([
            {"label": "DOI", "sa": "", "library": "10.1016/j.oceaneng.2025.122998"},
            {"label": "WOS记录号", "sa": "", "library": "WOS:001609529300006"},
            {"label": "发表年月", "sa": "2026-01", "library": "2026"},
            {"label": "作者单位", "sa": "船建", "library": "Shanghai Jiao Tong Univ"},
        ])
        prep = prepared(search)
        proof = {"row": search["row"], "verified": True, "claimed": True, "staff_id": "00001",
                 "scholar_id": "scholar-1", "author": "Tester", "order": 1}
        bridge = Mock()
        bridge.call.side_effect = [search, prep, proof]
        closed_roster = FakeRoster([replace(target, done=True)])
        closure = SimpleNamespace(completion=SimpleNamespace(roster=closed_roster))
        with patch("claim_batch.auto_complete_claim", return_value=closure) as close:
            result = run_claim_batch(roster, [target], bridge)

        self.assertEqual(result.completed_ids, (target.sa_id,))
        self.assertEqual(close.call_args.kwargs["note"], COMBINED_MISSING_IDS_CLAIM)

    def test_combined_case_requires_both_sa_identifiers_blank(self):
        target = record("demo-incomplete", reason="DOI和WOSID都不一致")
        roster = FakeRoster([target])
        search = found(target)
        search["row"]["reason"] = target.reason
        search["comparison"].extend([
            {"label": "DOI", "sa": "10.1000/present", "library": "10.1000/example"},
            {"label": "WOS记录号", "sa": "", "library": "WOS:000000000000001"},
        ])
        bridge = Mock()
        bridge.call.return_value = search
        result = run_claim_batch(roster, [target], bridge, skip_writer=persist_skips)
        self.assertIn(target.sa_id, result.skipped)
        self.assertIn("并非同时为空", result.skipped[target.sa_id])
        bridge.call.assert_called_once_with("search", {"sa_id": target.sa_id})

    def test_missing_claim_window_is_red_skip_and_next_record_continues(self):
        first, second = record("demo-no-window"), record("demo-next")
        roster = FakeRoster([first, second])
        search_a, search_b = found(first), found(second)
        prep_b = prepared(search_b)
        proof = {"row": search_b["row"], "verified": True, "claimed": True, "staff_id": "00001",
                 "scholar_id": "scholar-1", "author": "Tester", "order": 1}
        bridge = Mock()
        bridge.call.side_effect = [search_a, SafetyStop("[扩展 0.3.17] 作者认领页面结构已变化，请更新扩展"),
                                   search_b, prep_b, proof]
        closed_roster = FakeRoster([first, replace(second, done=True)])
        with patch("claim_batch.auto_complete_claim",
                   return_value=SimpleNamespace(completion=SimpleNamespace(roster=closed_roster))):
            result = run_claim_batch(roster, [first, second], bridge,
                                     skip_writer=persist_skips)
        self.assertIn(first.sa_id, result.skipped)
        self.assertIn("没有可用的作者认领窗口", result.skipped[first.sa_id])
        self.assertEqual(result.completed_ids, (second.sa_id,))
        self.assertFalse(result.halted)
        self.assertEqual([call.args[0] for call in bridge.call.call_args_list],
                         ["search", "prepare_claim", "search", "prepare_claim", "submit_claim"])

    def test_uncertain_submit_marks_red_and_halts_without_next_record(self):
        first, second = record("demo-a"), record("demo-b")
        roster = FakeRoster([first, second])
        search = found(first)
        bridge = Mock()
        bridge.call.side_effect = [search, prepared(search), SafetyStop("timeout after submit")]
        result = run_claim_batch(roster, [first, second], bridge,
                                 skip_writer=persist_skips)
        self.assertTrue(result.halted)
        self.assertIn(first.sa_id, result.skipped)
        self.assertEqual(result.checked, 1)
        self.assertEqual(result.completed_ids, ())
        self.assertEqual([call.args[0] for call in bridge.call.call_args_list],
                         ["search", "prepare_claim", "submit_claim"])

    def test_skip_marker_write_failure_halts_and_preserves_reason(self):
        target = record("demo-write-two", reason="通讯作者不一致")
        roster = FakeRoster([target])
        writer = Mock(side_effect=SafetyStop("Excel 正在使用名单"))
        result = run_claim_batch(roster, [target], Mock(), skip_writer=writer)
        self.assertTrue(result.halted)
        self.assertFalse(result.roster.records[0].skipped)
        self.assertIn("Excel 数字 2 写入失败", result.skipped[target.sa_id])
        writer.assert_called_once()

    def test_normal_mode_ignores_numeric_two_and_retry_mode_accepts_it(self):
        target = record("demo-red", skipped=True, remark="2")
        roster = FakeRoster([target])
        bridge = Mock()
        normal = run_claim_batch(roster, [target], bridge)
        self.assertEqual(normal.checked, 0)
        bridge.call.assert_not_called()

        search = found(target)
        bridge.call.side_effect = [search, SafetyStop("作者认领页面结构已变化")]
        retry = run_claim_batch(roster, [target], bridge, retry_skipped=True,
                                skip_writer=persist_skips)
        self.assertEqual(retry.checked, 1)
        self.assertIn(target.sa_id, retry.skipped)
        self.assertEqual([call.args[0] for call in bridge.call.call_args_list],
                         ["search", "prepare_claim"])

    def test_already_claimed_pending_row_is_closed_without_duplicate_claim(self):
        target = record("demo-claimed")
        roster = FakeRoster([target])
        search = found(target)
        search["comparison"][0]["library"] = "已认领"
        bridge = Mock()
        bridge.call.return_value = search
        closed_roster = FakeRoster([replace(target, done=True)])
        closure = SimpleNamespace(completion=SimpleNamespace(roster=closed_roster))
        with patch("claim_batch.complete_claim", return_value=closure) as close, \
                patch("claim_batch.auto_complete_claim") as new_claim:
            result = run_claim_batch(roster, [target], bridge)
        self.assertEqual(result.completed_ids, (target.sa_id,))
        self.assertFalse(result.halted)
        bridge.call.assert_called_once_with("search", {"sa_id": target.sa_id})
        close.assert_called_once()
        self.assertEqual(close.call_args.kwargs["note"], "已认领")
        new_claim.assert_not_called()

    def test_cancel_and_limit_are_enforced(self):
        records = [record(f"demo-{index:03}") for index in range(101)]
        roster = FakeRoster(records)
        result = run_claim_batch(roster, records, Mock(), cancel=lambda: True)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.checked, 0)
        with self.assertRaises(SafetyStop):
            run_claim_batch(roster, records, Mock(), limit=101)

    def test_pause_during_person_lookup_prevents_claim_and_does_not_skip(self):
        rec = record()
        roster, bridge = FakeRoster([rec]), Mock()
        search = found(rec)
        stopped = False
        def reply(action, payload):
            nonlocal stopped
            if action == "search":
                return search
            if action == "prepare_claim":
                stopped = True
                return prepared(search)
            raise AssertionError("No write allowed after pause: " + action)
        bridge.call.side_effect = reply
        writer = Mock()
        result = run_claim_batch(roster, [rec], bridge, cancel=lambda: stopped, skip_writer=writer)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.skipped, {})
        self.assertEqual([c.args[0] for c in bridge.call.call_args_list], ["search", "prepare_claim"])
        writer.assert_not_called()

    def test_roster_change_during_person_lookup_blocks_claim_without_skip(self):
        rec = record()
        roster, bridge = FakeRoster([rec]), Mock()
        search = found(rec)
        def reply(action, payload):
            if action == "search":
                return search
            if action == "prepare_claim":
                roster.assert_unchanged.side_effect = SafetyStop("名单已改变")
                return prepared(search)
            raise AssertionError("Stale roster must not be submitted: " + action)
        bridge.call.side_effect = reply
        writer = Mock()
        result = run_claim_batch(roster, [rec], bridge, skip_writer=writer)
        self.assertTrue(result.halted)
        self.assertEqual(result.skipped, {})
        self.assertIn("名单已改变", result.reason)
        self.assertEqual([c.args[0] for c in bridge.call.call_args_list], ["search", "prepare_claim"])
        writer.assert_not_called()

    def test_pause_after_confirmed_claim_does_not_start_closure_or_mark_skip(self):
        rec = record()
        roster, bridge = FakeRoster([rec]), Mock()
        search = found(rec)
        stopped = False
        def reply(action, payload):
            nonlocal stopped
            if action == "search":
                return search
            if action == "prepare_claim":
                return prepared(search)
            if action == "submit_claim":
                stopped = True
                return {"row": search["row"], "verified": True, "claimed": True, "staff_id": "00001",
                        "scholar_id": "scholar-1", "author": "Tester", "order": 1}
            raise AssertionError(action)
        bridge.call.side_effect = reply
        writer = Mock()
        with patch("claim_batch.auto_complete_claim") as close:
            result = run_claim_batch(roster, [rec], bridge, cancel=lambda: stopped, skip_writer=writer)
        self.assertTrue(result.cancelled)
        self.assertEqual(result.skipped, {})
        close.assert_not_called()
        writer.assert_not_called()


if __name__ == "__main__":
    unittest.main()
