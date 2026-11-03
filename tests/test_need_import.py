"""Zero-match 需导入 hand-off on a synthetic workbook and a fake SA page."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import Workbook

from bridge import BrowserRejected
from core import HEADERS, QUERY_HEADER, STATUS_HEADER, SafetyStop, read_roster
from need_import import LINK_PENDING_NOTE, NOTE, PENDING_NOTE, run_need_import_batch
from roster_write import mark_skipped_many


def make_roster(path, rows):
    book = Workbook()
    sheet = book.active
    sheet.title = "名单"
    sheet.append(["备注"] + list(HEADERS.values()) + [QUERY_HEADER, STATUS_HEADER])
    for i, (owner, matches) in enumerate(rows, 1):
        values = dict(owner=owner, sa_id=f"zero-{i:03}", title=f"Synthetic {i}", doi=f"10.1000/syn.{i}", wos="",
                      staff_id="00001", matches=matches, item_ids="1234567890" if matches else "",
                      mark="待处理", reason="")
        sheet.append([None] + [values[k] for k in HEADERS] + ["2", None])
    book.save(path)
    book.close()


def web_row(record, **changes):
    row = {"saLzkId": record.sa_id, "itemId": "", "matchCount": 0, "markStatus": "待处理",
           "reason": "", "titleValue": record.title, "doiValue": record.doi, "wosValue": "",
           "gh": record.staff_id, "remark": ""}
    row.update(changes)
    return row


def library_item(record, id="2112369564765831170", **changes):
    item = {"id": id, "title": record.title, "doi": record.doi, "wos": "", "model": "期刊论文",
            "deleted": False}
    item.update(changes)
    return item


class FakePage:
    """Status reads return the live row; complete flips it like the native dialog.

    ``library`` maps a frontend (field, text) query to its items; unknown
    queries answer a confirmed zero. ``link`` sets the platform ID like the
    native 编辑 dialog and the comparison shows the linked item's fields.
    """
    def __init__(self, roster, protocol=2):
        self.rows = {r.sa_id: web_row(r) for r in roster.records}
        self.protocol, self.calls, self.fail = protocol, [], {}
        self.library, self.items = {}, {}

    def call(self, action, payload):
        self.calls.append((action, payload))
        error = self.fail.get((action, payload.get("sa_id", payload.get("field"))))
        if error:
            raise error
        if action == "front_search":
            items = self.library.get((payload["field"], payload["text"]), [])
            return {"front_search_protocol": 1, "verified": True, "field": payload["field"],
                    "text": payload["text"], "total": len(items), "complete": True, "items": items}
        row = self.rows[payload["sa_id"]]
        if action == "link":
            assert payload["expected"] == row and row["matchCount"] == 0 and payload["reviewed"] is True
            row.update(itemId=payload["item_id"], matchCount=1, reason="作者不一致")
            return {"row": dict(row), "verified": True}
        if action == "search":
            item = self.items.get(row["itemId"], {})
            comparison = [{"label": "题名", "sa": row["titleValue"], "library": item.get("title", "")},
                          {"label": "DOI", "sa": row["doiValue"], "library": item.get("doi", "")},
                          {"label": "WOS记录号", "sa": row["wosValue"], "library": item.get("wos", "")}]
            return {"row": dict(row), "comparison": comparison}
        if action == "status":
            data = {"row": dict(row)}
            if self.protocol:
                data["need_import_protocol"] = self.protocol
            return data
        assert action == "complete" and payload["expected"] == row and row["markStatus"] == "待处理"
        row.update(markStatus="已处理", remark=payload["note"])
        return {"row": dict(row), "verified": True}


class NeedImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "list.xlsx"
        make_roster(self.path, [("测试员", 0), ("测试员", 0), ("其他人", 0), ("测试员", 1)])
        self.roster = read_roster(self.path)
        self.page = FakePage(self.roster)

    def hold(self, record, query=None, **changes):
        """The library frontend already holds this paper (found by DOI by default)."""
        item = library_item(record, **changes)
        self.page.library[query or ("doi", record.doi)] = [item]
        self.page.items[item["id"]] = item
        return item

    def run_batch(self, records=None, roster=None, **kwargs):
        roster = roster or self.roster
        records = records if records is not None else roster.records[:2]
        return run_need_import_batch(roster, records, self.page, "测试员", **kwargs)

    def test_zero_match_rows_are_closed_on_site_then_marked_done_with_note(self):
        result = self.run_batch()
        self.assertEqual(result.completed_ids, ("zero-001", "zero-002"))
        self.assertFalse(result.halted)
        saved = read_roster(self.path)
        for record in saved.records[:2]:
            self.assertTrue(record.done)
            self.assertFalse(record.skipped)
            self.assertEqual(record.remark, NOTE)
        self.assertEqual(saved.records[2:], self.roster.records[2:])
        completes = [payload for action, payload in self.page.calls if action == "complete"]
        self.assertEqual([p["note"] for p in completes], [NOTE, NOTE])
        self.assertTrue(all(p["owner"] == "测试员" and p["reviewed"] is True for p in completes))
        self.assertEqual(self.page.rows["zero-001"]["remark"], NOTE)

    def test_every_zero_match_row_is_searched_on_the_frontend_before_need_import(self):
        self.run_batch(records=self.roster.records[:1])
        searches = [payload for action, payload in self.page.calls if action == "front_search"]
        self.assertEqual(searches, [{"field": "doi", "text": "10.1000/syn.1"},
                                    {"field": "title", "text": '"Synthetic 1"'}])
        actions = [action for action, _ in self.page.calls]
        self.assertLess(actions.index("front_search"), actions.index("complete"))

    def fake_claim(self, completed=True, **result):
        calls = []

        def claim(roster, records, bridge, **kwargs):
            calls.append((records, kwargs))
            ids = tuple(r.sa_id for r in records) if completed else ()
            return SimpleNamespace(roster=roster, completed_ids=ids, synced_ids=(),
                                   skipped=result.get("skipped", {}), cancelled=False,
                                   halted=result.get("halted", False), reason=result.get("reason", ""))
        return calls, claim

    def test_held_paper_is_linked_verified_on_both_sides_then_claimed_not_need_import(self):
        target = self.roster.records[0]
        item = self.hold(target)
        calls, claim = self.fake_claim()
        with patch("need_import.run_claim_batch", side_effect=claim):
            result = self.run_batch(records=[target])
        self.assertEqual(result.claimed_ids, (target.sa_id,))
        self.assertEqual(result.completed_ids, ())
        links = [payload for action, payload in self.page.calls if action == "link"]
        self.assertEqual([p["item_id"] for p in links], [item["id"]])
        self.assertNotIn("complete", [action for action, _ in self.page.calls])
        self.assertEqual(self.page.rows[target.sa_id]["itemId"], item["id"])
        (records, kwargs), = calls
        self.assertEqual([r.sa_id for r in records], [target.sa_id])
        self.assertIs(kwargs["current_match"], True)
        self.assertEqual(kwargs["owner"], "测试员")

    def test_found_by_exact_title_links_the_same_item(self):
        target = self.roster.records[0]
        self.hold(target, query=("title", '"Synthetic 1"'), title="SYNTHETIC  1")
        calls, claim = self.fake_claim()
        with patch("need_import.run_claim_batch", side_effect=claim):
            result = self.run_batch(records=[target])
        self.assertEqual(result.claimed_ids, (target.sa_id,))

    def test_frontend_ambiguity_or_disagreement_is_red_with_a_reason_and_nothing_written(self):
        first, second = self.roster.records[:2]
        self.hold(first)
        self.page.library[("title", '"Synthetic 1"')] = [library_item(first, id="999")]
        self.hold(second, doi="10.1000/other")      # same title, different DOI
        with patch("need_import.run_claim_batch") as claim:
            result = self.run_batch()
        claim.assert_not_called()
        self.assertEqual(result.skipped, {first.sa_id: "前台有多条同一文献，待人工核验",
                                          second.sa_id: "前台疑似已有，信息不一致待核"})
        self.assertFalse({"link", "complete"} & {action for action, _ in self.page.calls})
        saved = read_roster(self.path).records
        self.assertEqual([(r.skipped, r.remark) for r in saved[:2]],
                         [(True, "前台有多条同一文献，待人工核验"), (True, "前台疑似已有，信息不一致待核")])

    def test_unrelated_frontend_hits_do_not_prevent_need_import(self):
        target = self.roster.records[0]
        self.page.library[("title", '"Synthetic 1"')] = [
            library_item(target, title="Synthetic 1 revisited", doi="")]
        result = self.run_batch(records=[target])
        self.assertEqual(result.completed_ids, (target.sa_id,))

    def test_truncated_frontend_answer_is_not_treated_as_absence(self):
        target = self.roster.records[0]
        original = self.page.call

        def truncated(action, payload):
            answer = original(action, payload)
            if action == "front_search":
                answer.update(total=80, complete=False)
            return answer
        self.page.call = truncated
        result = self.run_batch(records=[target])
        self.assertEqual(result.skipped, {target.sa_id: "前台检索结果过多，待人工核验"})
        self.assertNotIn("complete", [action for action, _ in self.page.calls])

    def test_frontend_failure_halts_before_any_write(self):
        before = self.path.read_bytes()
        self.page.fail[("front_search", "doi")] = BrowserRejected("front_search", "【已暂停】前台未登录")
        result = self.run_batch()
        self.assertTrue(result.halted)
        self.assertIn("前台", result.reason)
        self.assertFalse({"link", "complete"} & {action for action, _ in self.page.calls})
        self.assertEqual(self.path.read_bytes(), before)

    def test_link_receipt_unknown_halts_marks_pending_and_retry_resumes_without_relinking(self):
        target = self.roster.records[0]
        item = self.hold(target)
        self.page.fail[("link", target.sa_id)] = BrowserRejected(
            "link", "【已发出写入，禁止自动重试】保存平台号超时")
        with patch("need_import.run_claim_batch") as claim:
            result = self.run_batch(records=[target])
        claim.assert_not_called()
        self.assertTrue(result.halted)
        saved = read_roster(self.path)
        self.assertEqual((saved.records[0].skipped, saved.records[0].remark), (True, LINK_PENDING_NOTE))
        # The write landed: retry finds the link via the frontend and claims, never relinks.
        self.page.fail.clear()
        self.page.rows[target.sa_id].update(itemId=item["id"], matchCount=1, reason="作者不一致")
        self.page.calls.clear()
        calls, claim = self.fake_claim()
        with patch("need_import.run_claim_batch", side_effect=claim):
            retry = self.run_batch(records=saved.records[:1], roster=saved, retry_skipped=True)
        self.assertEqual(retry.claimed_ids, (target.sa_id,))
        self.assertNotIn("link", [action for action, _ in self.page.calls])
        self.assertIs(calls[0][1]["retry_skipped"], True)

    def test_linked_but_comparison_disagrees_is_red_and_not_claimed(self):
        target = self.roster.records[0]
        item = self.hold(target)
        self.page.items[item["id"]] = {**item, "title": "Something else"}
        with patch("need_import.run_claim_batch") as claim:
            result = self.run_batch(records=[target])
        claim.assert_not_called()
        self.assertEqual(result.skipped, {target.sa_id: "已关联，两边信息待核"})

    def test_claim_halt_stops_the_queue(self):
        first, second = self.roster.records[:2]
        self.hold(first)
        calls, claim = self.fake_claim(completed=False, halted=True, reason="认领结果不确定",
                                       skipped={first.sa_id: "认领结果不确定"})
        with patch("need_import.run_claim_batch", side_effect=claim):
            result = self.run_batch()
        self.assertTrue(result.halted)
        self.assertEqual(result.reason, "认领结果不确定")
        self.assertNotIn(("status", {"sa_id": second.sa_id}), self.page.calls)

    def test_live_match_changed_is_red_with_a_reason_not_an_empty_note(self):
        self.page.rows["zero-001"].update(matchCount=1, itemId="1234567890")
        self.page.rows["zero-002"].update(matchCount=2, itemId="1,2")
        result = self.run_batch()
        self.assertEqual(result.completed_ids, ())
        saved = read_roster(self.path)
        self.assertEqual([(r.skipped, r.remark) for r in saved.records[:2]],
                         [(True, "网页已有匹配，待认领"), (True, "网页已有多条匹配")])
        self.assertNotIn("complete", [action for action, _ in self.page.calls])

    def test_already_processed_on_site_only_syncs_roster(self):
        self.page.rows["zero-001"].update(markStatus="已处理", remark="其他人已处理")
        result = self.run_batch(records=self.roster.records[:1])
        self.assertEqual(result.synced_ids, ("zero-001",))
        saved = read_roster(self.path).records[0]
        self.assertTrue(saved.done)
        self.assertEqual(saved.remark, "其他人已处理")
        self.assertEqual([action for action, _ in self.page.calls], ["status"])

    def test_old_extension_halts_before_any_write(self):
        self.page.protocol = 1   # 0.4.57: 需导入 without frontend lookup
        before = self.path.read_bytes()
        result = self.run_batch()
        self.assertTrue(result.halted)
        self.assertIn("0.4.58", result.reason)
        self.assertEqual([action for action, _ in self.page.calls], ["status"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_prewrite_rejection_skips_one_row_and_continues(self):
        self.page.fail[("complete", "zero-001")] = BrowserRejected("complete", "【已暂停】网页数据在核验后已变化")
        result = self.run_batch()
        self.assertFalse(result.halted)
        self.assertEqual(result.completed_ids, ("zero-002",))
        saved = read_roster(self.path).records
        self.assertEqual((saved[0].skipped, saved[0].remark), (True, "需导入未提交"))
        self.assertTrue(saved[1].done)

    def test_unknown_write_halts_marks_pending_and_retry_syncs_without_resubmitting(self):
        self.page.fail[("complete", "zero-001")] = BrowserRejected(
            "complete", "【已发出写入，禁止自动重试】状态或备注回读不一致")
        result = self.run_batch()
        self.assertTrue(result.halted)
        self.assertEqual(result.skipped, {"zero-001": PENDING_NOTE})
        self.assertNotIn(("status", {"sa_id": "zero-002"}), self.page.calls)
        saved = read_roster(self.path)
        self.assertEqual((saved.records[0].skipped, saved.records[0].remark), (True, PENDING_NOTE))
        self.assertFalse(saved.records[1].done or saved.records[1].skipped)
        # The write actually landed on the site; the retry only reads it back.
        self.page.fail.clear()
        self.page.rows["zero-001"].update(markStatus="已处理", remark=NOTE)
        self.page.calls.clear()
        retry = self.run_batch(records=saved.records[:1], roster=saved, retry_skipped=True)
        self.assertEqual(retry.synced_ids, ("zero-001",))
        self.assertEqual([action for action, _ in self.page.calls], ["status"])
        final = read_roster(self.path).records[0]
        self.assertTrue(final.done)
        self.assertEqual(final.remark, NOTE)

    def test_existing_site_note_or_mismatched_facts_are_not_overwritten(self):
        self.page.rows["zero-001"]["remark"] = "人工备注"
        self.page.rows["zero-002"]["titleValue"] = "Different"
        result = self.run_batch()
        self.assertEqual(result.skipped, {"zero-001": "待人工核验", "zero-002": "名单与网页不一致"})
        self.assertNotIn("complete", [action for action, _ in self.page.calls])

    def test_scope_guards_refuse_before_browser(self):
        records = self.roster.records
        for owner, chosen in (("测试员", [records[2]]), ("测试员", [records[3]]),
                              ("（未分配）", [records[0]]), ("", [records[0]])):
            with self.assertRaises(SafetyStop):
                run_need_import_batch(self.roster, chosen, self.page, owner)
        with self.assertRaises(SafetyStop):
            self.run_batch(retry_skipped=True)
        self.assertEqual(self.page.calls, [])

    def test_retry_scope_only_accepts_skipped_rows(self):
        skipped = mark_skipped_many(self.roster, self.roster.records[:1], reasons={"zero-001": "需导入未提交"}).roster
        with self.assertRaises(SafetyStop):
            run_need_import_batch(skipped, skipped.records[:2], self.page, "测试员", retry_skipped=True)
        result = run_need_import_batch(skipped, skipped.records[:1], self.page, "测试员", retry_skipped=True)
        self.assertEqual(result.completed_ids, ("zero-001",))
        self.assertTrue(read_roster(self.path).records[0].done)

    def test_pause_before_first_read_sends_nothing(self):
        result = self.run_batch(cancel=lambda: True)
        self.assertTrue(result.cancelled)
        self.assertFalse(result.halted)
        self.assertEqual(self.page.calls, [])


if __name__ == "__main__":
    unittest.main()
