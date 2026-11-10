import tempfile
import hashlib
import json
from pathlib import Path
from threading import Event
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import Workbook, load_workbook
from openpyxl.styles import PatternFill

from core import SafetyStop, UNASSIGNED_OWNER
from cross_review import CrossReview, assess


def row(sid, owner="复核对象", **changes):
    return dict(saLzkId=sid, updateUsername=owner, markStatus="已处理", titleValue="Example",
                matchCount=1, gh="001", **changes)


def detail(item, right="001", state="已认领", title="Example", journal="Journal"):
    return {"row": item, "detail_released": True,
            "claim_identity": {"sa_staff_id": "001", "library_staff_id": right, "state": state},
            "comparison": [{"label": label, "sa": left, "library": other} for label, left, other in (
                ("题名", "Example", title), ("DOI", "10.1234/example", "10.1234/example"),
                ("期刊", "Journal", journal),
                ("作者信息", "是否第一作者：是 是否通讯作者：是", "是否第一作者：否 是否通讯作者：否"),
            )]}


class ReviewTests(unittest.TestCase):
    def test_assess_identity_staff_roles_and_exempt_closure(self):
        self.assertEqual(assess(detail(row("001"), right="002"))[0], ["认领工号不一致"])
        self.assertEqual(assess(detail(row("001"), right="002", title="Other", journal="Other"))[0],
                         ["两边不是同一篇论文"])
        self.assertEqual(assess(detail(row("001")))[0], [])
        self.assertEqual(assess(detail(row("001", reason="作者不一致", remark="非交大"),
                                       right="", state="未认领"))[0], [])
        self.assertEqual(assess(detail(row("001", remark="已认领"), right="", state="未认领"))[0],
                         ["未认领，但已处理"])
        original = dict(staff_id="001", author_count=2, author_index=1, first_authors=[1, 2],
                        corresponding=[1], mark_source="全文")
        self.assertEqual(assess(detail(row("001")), original)[0], ["共一未修改", "是通讯未修改"])

    def test_scan_resume_skips_confirmed(self):
        with tempfile.TemporaryDirectory() as folder:
            owner = "复核对象"
            review = CrossReview(folder, owner)
            stop = Event()
            entries = {"001": row("001"), "002": row("002")}
            actions = []
            class Bridge:
                def call(self, action, payload):
                    actions.append(action)
                    if action == "cross_review_page":
                        page = payload["page"]
                        return dict(protocol=1, owner=owner, page=page, total=2, page_size=1,
                                    rows=[entries[f"00{page}"]])
                    sid = payload["sa_id"]
                    if action == "status":
                        return {"row": entries[sid]}
                    if action == "search":
                        if sid == "001":
                            stop.set()
                        return detail(entries[sid], right="002")
                    raise AssertionError(action)
            bridge = Bridge()
            review.run(bridge, stop, lambda _: None)
            self.assertEqual(review.data["rows"]["002"]["status"], "未核验")
            make_feedback(Path(folder) / "复核反馈.xlsx")
            review.confirm("001", "认领工号不一致", "校对员")
            resumed = CrossReview(folder, owner)
            stop.clear()
            actions.clear()
            resumed.run(bridge, stop, lambda _: None)
            self.assertEqual(actions, ["status", "search"])

    def test_scan_rejects_mixed_owner_duplicate_and_status_drift(self):
        for mixed in (True, False):
            with self.subTest(mixed=mixed), tempfile.TemporaryDirectory() as folder:
                owner = "复核对象"
                review = CrossReview(folder, owner)
                def page(action, payload):
                    return dict(protocol=1, owner=owner, page=payload["page"], total=2, page_size=1,
                                rows=[row("001", "其他人" if mixed else owner)])
                with self.assertRaises(SafetyStop):
                    review.scan(page, Event(), lambda _: None)
                self.assertFalse(review.data["ready"])
        with tempfile.TemporaryDirectory() as folder:
            review = CrossReview(folder, "复核对象")
            review.data.update(ready=True, rows={"001": {"status": "未核验"}})
            class Bridge:
                def call(self, action, payload):
                    assert action == "status"
                    return {"row": {**row("001"), "markStatus": "待处理"}}
            with self.assertRaises(SafetyStop):
                review.run(Bridge(), Event(), lambda _: None)
            self.assertEqual(CrossReview(folder, "复核对象").data["rows"]["001"]["status"], "待核")


HEADERS = ("Said", "问题备注", "负责人", "校对人")


def make_feedback(path, rows=(), headers=HEADERS):
    book = Workbook()
    sheet = book.active
    sheet.title = "反馈"
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    sheet.column_dimensions["B"].width = 48
    sheet["B1"].fill = PatternFill("solid", fgColor="FFF4C2")
    extra = book.create_sheet("保留页")
    extra["A1"] = "=1+1"
    extra["B1"] = "保留文字"
    book.active = 1  # Write the first sheet, even when another sheet is active.
    book.save(path)
    book.close()


class FeedbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.path = self.base / "roster" / "复核反馈.xlsx"
        self.path.parent.mkdir()
        make_feedback(self.path)
        self.review = CrossReview(self.base, "复核对象", feedback_path=self.path)
        self.review.data["rows"] = {"001": {"status": "待核"}, "002": {"status": "待核"}}

    def values(self):
        book = load_workbook(self.path)
        try:
            return list(book.worksheets[0].values)
        finally:
            book.close()

    def persisted(self):
        return json.loads(self.review.path.read_text(encoding="utf-8"))["rows"]

    def panel(self):
        import tkinter as tk
        from cross_review_ui import CrossReviewPanel
        root = tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        app = SimpleNamespace(
            busy=False, owner=tk.StringVar(root, value="主界面校对员"),
            status=tk.StringVar(root, value="就绪"),
            service=SimpleNamespace(base=self.base), configure_actions=lambda: None,
            roster=SimpleNamespace(path=self.path.with_name("list.xlsx"),
                                   records=[SimpleNamespace(owner="复核对象")]))
        panel = CrossReviewPanel(root, app)
        panel.owner.set("复核对象")
        return panel

    def test_panel_load_without_roster_warns_and_clears_review(self):
        panel = self.panel()
        panel.review = self.review
        panel.app.roster = None
        with patch("cross_review_ui.messages.showwarning") as warning:
            panel.load()
        self.assertIsNone(panel.review)
        self.assertEqual(panel.tree.get_children(), ())
        self.assertEqual(panel.owner.get(), "")
        self.assertIn("名册", warning.call_args.args[1])

    def test_panel_confirm_reports_success_only_after_sync(self):
        panel = self.panel()
        panel.review = self.review
        panel.render()
        panel.tree.selection_set("001")
        panel.note.set("界面确认")
        panel.confirm()
        self.assertEqual(panel.app.status.get(), "已确认并同步到复核反馈.xlsx")
        self.assertEqual(self.values()[1], ("001", "界面确认", "复核对象", "主界面校对员"))
        panel.app.status.set("等待同步")
        self.path.with_name("~$复核反馈.xlsx").touch()
        with patch("cross_review_ui.messages.showwarning") as warning:
            panel.confirm()
        self.assertEqual(panel.app.status.get(), "等待同步")
        self.assertEqual(warning.call_args.args[0], "确认或同步未完成")

    def test_confirm_appends_text_and_backs_up_original_version(self):
        before = self.path.read_bytes()
        self.review.confirm("001", "=问题备注", "校对员")
        self.assertEqual(self.values(), [HEADERS, ("001", "=问题备注", "复核对象", "校对员")])
        book = load_workbook(self.path)
        try:
            for cell in book.worksheets[0][2]:
                self.assertEqual(cell.data_type, "s")
                self.assertEqual(cell.number_format, "@")
        finally:
            book.close()
        self.assertTrue(self.persisted()["001"]["feedback_synced"])
        backup = self.base / "runtime/backups" / ("复核反馈-" + hashlib.sha256(before).hexdigest() + ".xlsx")
        self.assertEqual(backup.read_bytes(), before)

    def test_confirm_again_updates_existing_row_and_reviewer(self):
        self.review.confirm("001", "原备注", "校对员")
        self.review.confirm("001", "修正备注", "另一校对员")
        self.assertEqual(self.values(), [HEADERS, ("001", "修正备注", "复核对象", "另一校对员")])

    def test_owner_conflict_and_duplicate_ids_leave_file_unchanged(self):
        for rows in ([("001", "原备注", "别人", "校对员")],
                     [("001", "一", "复核对象", "校对员"), ("001", "二", "复核对象", "校对员")]):
            with self.subTest(rows=rows):
                make_feedback(self.path, rows)
                before = self.path.read_bytes()
                with self.assertRaises(SafetyStop):
                    self.review.confirm("001", "新备注", "校对员")
                self.assertEqual(self.path.read_bytes(), before)
                self.assertFalse(self.persisted()["001"]["feedback_synced"])

    def test_missing_and_wrong_header_stop_without_creating_or_overwriting(self):
        for headers in (("Said", "问题备注", "校对人", "负责人"), HEADERS + ("额外列",)):
            with self.subTest(headers=headers):
                make_feedback(self.path, headers=headers)
                before = self.path.read_bytes()
                with self.assertRaisesRegex(SafetyStop, "表头"):
                    self.review.confirm("001", "问题", "校对员")
                self.assertEqual(self.path.read_bytes(), before)
        self.path.unlink()
        with self.assertRaisesRegex(SafetyStop, "不存在"):
            self.review.confirm("001", "问题", "校对员")
        self.assertFalse(self.path.exists())

    def test_excel_and_assistant_locks_stop_without_writing(self):
        for name in ("~$复核反馈.xlsx", ".复核反馈.xlsx.assistant.lock"):
            with self.subTest(name=name):
                lock = self.path.with_name(name)
                lock.touch()
                before = self.path.read_bytes()
                try:
                    with self.assertRaises(SafetyStop):
                        self.review.confirm("001", "问题", "校对员")
                    self.assertEqual(self.path.read_bytes(), before)
                finally:
                    lock.unlink()

    def test_preserves_other_rows_sheets_and_formatting(self):
        make_feedback(self.path, [("other", "别人的备注", "别人", "原校对员"),
                                  ("001", "旧备注", "复核对象", "原校对员")])
        self.review.confirm("001", "修改", "校对员")
        self.review.confirm("002", "追加", "校对员")
        self.assertEqual(self.values(), [HEADERS, ("other", "别人的备注", "别人", "原校对员"),
                                        ("001", "修改", "复核对象", "校对员"),
                                        ("002", "追加", "复核对象", "校对员")])
        book = load_workbook(self.path)
        try:
            self.assertEqual(book.sheetnames, ["反馈", "保留页"])
            self.assertEqual(list(book["保留页"].values), [("=1+1", "保留文字")])
            self.assertEqual(book.worksheets[0].column_dimensions["B"].width, 48)
            self.assertEqual(book.worksheets[0]["B1"].fill.fgColor.rgb, "00FFF4C2")
        finally:
            book.close()

    def test_invalid_reviewer_does_not_confirm_or_write_json(self):
        for reviewer in ("", UNASSIGNED_OWNER, " 空格 ", "复核对象", None):
            with self.subTest(reviewer=reviewer):
                before = self.path.read_bytes()
                with self.assertRaisesRegex(SafetyStop, "主界面"):
                    self.review.confirm("001", "问题", reviewer)
                self.assertNotIn("confirmed", self.review.data["rows"]["001"])
                self.assertFalse(self.review.path.exists())
                self.assertEqual(self.path.read_bytes(), before)

    def test_failed_write_retains_confirmation_and_next_confirm_retries_all(self):
        lock = self.path.with_name("~$复核反馈.xlsx")
        lock.touch()
        with self.assertRaises(SafetyStop):
            self.review.confirm("001", "第一次确认", "原校对员")
        entry = self.persisted()["001"]
        self.assertEqual(entry["confirmed"], "第一次确认")
        self.assertFalse(entry["feedback_synced"])
        lock.unlink()
        self.review.confirm("002", "第二条确认", "新校对员")
        self.assertTrue(all(entry["feedback_synced"] for entry in self.persisted().values()))
        self.assertEqual(self.values(), [HEADERS, ("001", "第一次确认", "复核对象", "原校对员"),
                                        ("002", "第二条确认", "复核对象", "新校对员")])

    def test_pending_retries_after_reload(self):
        lock = self.path.with_name("~$复核反馈.xlsx")
        lock.touch()
        with self.assertRaises(SafetyStop):
            self.review.confirm("001", "待补写", "原校对员")
        lock.unlink()
        panel = self.panel()
        panel.load()
        self.assertTrue(self.persisted()["001"]["feedback_synced"])
        self.assertEqual(self.values()[1], ("001", "待补写", "复核对象", "原校对员"))
        self.assertEqual(panel.tree.item("001", "values")[2], "有问题")

    def test_panel_uses_main_owner_and_keeps_failed_confirmation_visible(self):
        self.review.save()
        panel = self.panel()
        panel.load()
        panel.tree.selection_set("001")
        panel.note.set("从界面确认")
        lock = self.path.with_name("~$复核反馈.xlsx")
        lock.touch()
        with patch("cross_review_ui.messages.showwarning") as warning:
            panel.confirm()
            self.assertIn("Excel", warning.call_args.args[1])
            self.assertEqual(panel.tree.item("001", "values")[2], "有问题·未同步")
            panel.load()
            self.assertIsNotNone(panel.review)
            self.assertEqual(panel.tree.item("001", "values")[2], "有问题·未同步")
        lock.unlink()
        panel.load()
        self.assertEqual(self.values()[1], ("001", "从界面确认", "复核对象", "主界面校对员"))
        self.assertTrue(self.persisted()["001"]["feedback_synced"])

    def test_readback_rejects_corruption_in_header_other_rows_sheets_or_target(self):
        save = Workbook.save
        for sheet, cell in (("反馈", "B1"), ("反馈", "B2"), ("保留页", "B1"), ("反馈", "B3")):
            with self.subTest(sheet=sheet, cell=cell):
                make_feedback(self.path, [("other", "保留", "别人", "校对员")])
                before = self.path.read_bytes()
                def corrupt(book, target):
                    book[sheet][cell] = "损坏"
                    save(book, target)
                with patch.object(Workbook, "save", corrupt):
                    with self.assertRaisesRegex(SafetyStop, "校验"):
                        self.review.confirm("001", "预期内容", "校对员")
                self.assertEqual(self.path.read_bytes(), before)
                self.assertFalse(self.persisted()["001"]["feedback_synced"])
                self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_concurrent_external_change_is_not_overwritten(self):
        before = self.path.read_bytes()
        save = Workbook.save
        def external_edit(book, target):
            save(book, target)
            other = load_workbook(self.path)
            try:
                other.worksheets[1]["B1"] = "外部编辑"
                save(other, self.path)
            finally:
                other.close()
        with patch.object(Workbook, "save", external_edit):
            with self.assertRaisesRegex(SafetyStop, "发生变化"):
                self.review.confirm("001", "问题", "校对员")
        self.assertNotEqual(self.path.read_bytes(), before)
        self.assertEqual(self.values(), [HEADERS])
        book = load_workbook(self.path)
        try:
            self.assertEqual(book.worksheets[1]["B1"].value, "外部编辑")
        finally:
            book.close()
        self.assertFalse(self.persisted()["001"]["feedback_synced"])

    def test_existing_wrong_backup_is_not_replaced_or_used(self):
        before = self.path.read_bytes()
        backup_dir = self.base / "runtime/backups"
        backup_dir.mkdir(parents=True)
        backup = backup_dir / ("复核反馈-" + hashlib.sha256(before).hexdigest() + ".xlsx")
        backup.write_bytes(b"bad backup")
        with self.assertRaisesRegex(SafetyStop, "备份校验"):
            self.review.confirm("001", "问题", "校对员")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(backup.read_bytes(), b"bad backup")

    def test_old_confirmation_requires_explicit_reviewer_on_reconfirm(self):
        self.review.data["rows"]["001"].update(confirmed="历史确认", status="有问题")
        self.review.save()
        with self.assertRaisesRegex(SafetyStop, "缺少校对人"):
            self.review.sync_pending()
        self.assertFalse(self.persisted()["001"]["feedback_synced"])
        self.assertEqual(self.values(), [HEADERS])
        self.review.confirm("001", "重新确认", "校对员")
        self.assertTrue(self.persisted()["001"]["feedback_synced"])

    def test_json_final_save_failure_remains_pending_and_retry_is_idempotent(self):
        save = self.review.save
        def fail_final_save():
            if self.review.data["rows"]["001"].get("feedback_synced") is True:
                raise OSError("synthetic checkpoint failure")
            save()
        with patch.object(self.review, "save", fail_final_save):
            with self.assertRaises(OSError):
                self.review.confirm("001", "问题", "校对员")
        self.assertFalse(self.persisted()["001"]["feedback_synced"])
        self.assertFalse(self.review.data["rows"]["001"]["feedback_synced"])
        self.review.sync_pending()
        self.assertEqual(self.values(), [HEADERS, ("001", "问题", "复核对象", "校对员")])
        self.assertTrue(self.persisted()["001"]["feedback_synced"])

    def test_atomic_replace_failure_leaves_original_and_cleans_temporary(self):
        import os
        replace = os.replace
        def fail_workbook(source, target):
            if Path(target) == self.path:
                raise PermissionError("synthetic sharing violation")
            return replace(source, target)
        before = self.path.read_bytes()
        with patch("os.replace", side_effect=fail_workbook):
            with self.assertRaises(SafetyStop):
                self.review.confirm("001", "保留确认", "校对员")
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.persisted()["001"]["feedback_synced"])
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        self.review.sync_pending()
        self.assertTrue(self.persisted()["001"]["feedback_synced"])
