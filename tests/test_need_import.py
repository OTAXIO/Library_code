"""Zero-match 需导入 hand-off on a synthetic workbook and a fake SA page."""
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from openpyxl import Workbook

from bridge import BrowserRejected
from core import HEADERS, QUERY_HEADER, STATUS_HEADER, SafetyStop, read_roster
from need_import import NOTE, PENDING_NOTE, run_need_import_batch
from roster_write import mark_skipped_many


def make_roster(path, rows):
    book = Workbook()
    sheet = book.active
    sheet.title = "名单"
    sheet.append(["备注"] + list(HEADERS.values()) + [QUERY_HEADER, STATUS_HEADER])
    for i, (owner, matches) in enumerate(rows, 1):
        values = dict(owner=owner, sa_id=f"zero-{i:03}", title=f"Synthetic {i}", doi="", wos="",
                      staff_id="00001", matches=matches, item_ids="1234567890" if matches else "",
                      mark="待处理", reason="")
        sheet.append([None] + [values[k] for k in HEADERS] + ["2", None])
    book.save(path)
    book.close()


def web_row(record, **changes):
    row = {"saLzkId": record.sa_id, "itemId": "", "matchCount": 0, "markStatus": "待处理",
           "reason": "", "titleValue": record.title, "doiValue": "", "wosValue": "",
           "gh": record.staff_id, "remark": ""}
    row.update(changes)
    return row


class FakePage:
    """Status reads return the live row; complete flips it like the native dialog."""
    def __init__(self, roster, protocol=1):
        self.rows = {r.sa_id: web_row(r) for r in roster.records}
        self.protocol, self.calls, self.fail = protocol, [], {}

    def call(self, action, payload):
        self.calls.append((action, payload))
        error = self.fail.get((action, payload["sa_id"]))
        if error:
            raise error
        row = self.rows[payload["sa_id"]]
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
        self.page.protocol = 0
        before = self.path.read_bytes()
        result = self.run_batch()
        self.assertTrue(result.halted)
        self.assertIn("0.4.57", result.reason)
        self.assertEqual([action for action, _ in self.page.calls], ["status"])
        self.assertEqual(self.path.read_bytes(), before)

    def test_prewrite_rejection_skips_one_row_and_continues(self):
        self.page.fail[("complete", "zero-001")] = BrowserRejected(
            "complete", "[扩展 0.4.57] 【已暂停】网页数据在核验后已变化")
        result = self.run_batch()
        self.assertFalse(result.halted)
        self.assertEqual(result.completed_ids, ("zero-002",))
        saved = read_roster(self.path)
        self.assertEqual((saved.records[0].skipped, saved.records[0].remark), (True, "需导入未提交"))
        self.assertTrue(saved.records[1].done)
        self.page.fail.clear()
        retry = self.run_batch(records=saved.records[:1], roster=saved, retry_skipped=True)
        self.assertEqual(retry.completed_ids, ("zero-001",))

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
        self.page.calls.clear()
        pending = self.run_batch(records=saved.records[:1], roster=saved, retry_skipped=True)
        self.assertEqual([action for action, _ in self.page.calls], ["status"])
        self.assertEqual(pending.roster.records[0].remark, PENDING_NOTE)
        saved = pending.roster
        self.page.rows["zero-001"].update(markStatus="已处理", remark=NOTE)
        self.page.calls.clear()
        retry = self.run_batch(records=saved.records[:1], roster=saved, retry_skipped=True)
        self.assertEqual(retry.synced_ids, ("zero-001",))
        self.assertEqual([action for action, _ in self.page.calls], ["status"])
        final = read_roster(self.path).records[0]
        self.assertTrue(final.done)
        self.assertEqual(final.remark, NOTE)

    def test_old_import_intents_block_only_the_affected_record(self):
        for relative, table, phase in (
            ("wos-imports/imports.sqlite3", "imports", "push_intent"),
            ("wos-imports/zero-match.sqlite3", "jobs", "link_intent"),
            ("metadata-imports/zero-match.sqlite3", "jobs", "import_intent"),
        ):
            with self.subTest(phase=phase):
                base = self.path.parent / phase
                base.mkdir()
                roster_path = base / "list.xlsx"
                make_roster(roster_path, [("测试员", 0), ("测试员", 0)])
                roster = read_roster(roster_path)
                first, second = roster.records
                path = base / "runtime" / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(path)) as db, db:
                    db.execute(f"CREATE TABLE {table}(sa_id, record_key, data)")
                    db.execute(f"INSERT INTO {table} VALUES(?,?,?)", (first.sa_id, first.key, json.dumps({"phase": phase})))
                original = path.read_bytes()
                page = FakePage(roster)
                result = run_need_import_batch(roster, roster.records, page, "测试员")
                self.assertEqual([a for a, _ in page.calls], ["status", "status", "complete"])
                self.assertEqual(result.completed_ids, (second.sa_id,))
                self.assertTrue(result.roster.records[0].skipped)
                self.assertEqual(path.read_bytes(), original)

    def test_interrupted_write_is_blocked_after_restarting_with_original_roster(self):
        first = self.roster.records[0]
        self.page.fail[("complete", first.sa_id)] = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_batch(records=[first])
        self.page = FakePage(self.roster)
        reloaded = read_roster(self.path)
        retry = self.run_batch(roster=reloaded, records=reloaded.records[:1])
        self.assertEqual([action for action, _ in self.page.calls], ["status"])
        self.assertEqual(retry.roster.records[0].remark, PENDING_NOTE)

    def test_legacy_unknown_remark_without_journal_blocks_submission(self):
        first = self.roster.records[0]
        saved = mark_skipped_many(self.roster, [first], reasons={first.sa_id: PENDING_NOTE}).roster
        self.run_batch(roster=saved, records=saved.records[:1], retry_skipped=True)
        self.assertEqual([action for action, _ in self.page.calls], ["status"])

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
