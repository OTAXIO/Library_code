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
    def test_scan_reads_all_in_two_requests(self):
        with tempfile.TemporaryDirectory() as folder:
            review = CrossReview(folder, "复核对象")
            calls, updates = [], []
            def call(action, payload):
                calls.append((action, payload))
                size = payload.get("page_size", 10)
                return dict(protocol=1, owner="复核对象", page=1, total=188,
                            page_size=size, rows=[row(str(i)) for i in range(min(size, 188))])
            review.scan(call, Event(), updates.append)
            self.assertEqual(calls, [("cross_review_page", {"owner": "复核对象", "page": 1}),
                                     ("cross_review_page", {"owner": "复核对象", "page": 1, "page_size": 188})])
            saved = CrossReview(folder, "复核对象").data
            self.assertTrue(saved["ready"])
            self.assertEqual(set(saved["rows"]), {str(i) for i in range(188)})
            self.assertEqual((saved["scanned"], saved["total"], saved["page_size"]), (188, 188, 188))
            self.assertIn("一次读取 188 条", updates[-1])

    def test_scan_rejects_old_extension(self):
        with tempfile.TemporaryDirectory() as folder:
            review = CrossReview(folder, "复核对象")
            def call(action, payload):
                return dict(protocol=1, owner="复核对象", page=1, total=188,
                            page_size=10, rows=[row(str(i)) for i in range(10)])
            with self.assertRaisesRegex(SafetyStop, "扩展未更新到 0.4.63.*重载扩展后重新配对"):
                review.scan(call, Event(), lambda _: None)
            self.assertFalse(CrossReview(folder, "复核对象").data["ready"])

    def test_scan_cap_collects_drifting_pages_or_stops_after_eight_passes(self):
        import random
        for complete in (True, False):
            with self.subTest(complete=complete), tempfile.TemporaryDirectory() as folder:
                review = CrossReview(folder, "复核对象")
                calls, updates = [], []
                rng = random.Random(42)
                def call(action, payload):
                    calls.append(payload)
                    size = payload.get("page_size", 10)
                    if size == 10:
                        ids = list(range(10))
                    elif size == 120:
                        ids = list(range(50))
                    else:
                        self.assertEqual(size, 50)
                        count = min(size, 120 - (payload["page"] - 1) * size)
                        ids = rng.sample(range(120 if complete else 119), count)
                    return dict(protocol=1, owner="复核对象", page=payload["page"], total=120,
                                page_size=size, rows=[row(str(i)) for i in ids])
                if complete:
                    review.scan(call, Event(), updates.append)
                    self.assertEqual(set(review.data["rows"]), {str(i) for i in range(120)})
                    self.assertTrue(review.data["ready"])
                else:
                    with self.assertRaisesRegex(SafetyStop, "读 8 遍后仍只收齐 119/120"):
                        review.scan(call, Event(), updates.append)
                    self.assertEqual([p["page"] for p in calls[2:]], [1, 2, 3] * 8)
                    self.assertFalse(CrossReview(folder, "复核对象").data["ready"])
                self.assertTrue(all(p["page_size"] == 50 for p in calls[2:]))
                self.assertEqual(review.data["page_size"], 50)

    def test_scan_rejects_invalid_full_receipts(self):
        for change in ({"total": 5}, {"rows": [row("a", "其他人"), row("b"), row("c"), row("d")]},
                       {"rows": [row("a"), row("a"), row("c"), row("d")]},
                       {"rows": [row(""), row("b"), row("c"), row("d")]},
                       {"rows": []}, {"rows": [row(str(i)) for i in range(5)]}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as folder:
                review = CrossReview(folder, "复核对象")
                def call(action, payload):
                    size = payload.get("page_size", 2)
                    result = dict(protocol=1, owner="复核对象", page=1, total=4,
                                  page_size=size, rows=[row(sid) for sid in "abcd"[:size]])
                    if "page_size" in payload:
                        result.update(change)
                    return result
                with self.assertRaises(SafetyStop):
                    review.scan(call, Event(), lambda _: None)
                self.assertFalse(CrossReview(folder, "复核对象").data["ready"])

    def test_scan_over_1000_uses_requested_size_and_rejects_receipt_changes(self):
        for change in (None, {"total": 1002}, {"page_size": 10}):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as folder:
                review = CrossReview(folder, "复核对象")
                calls = []
                def call(action, payload):
                    calls.append(payload)
                    size = payload.get("page_size", 10)
                    page = payload["page"]
                    result = dict(protocol=1, owner="复核对象", page=page, total=1001,
                                  page_size=size, rows=[row(str(i)) for i in
                                                       range((page - 1) * size, min(page * size, 1001))])
                    if page == 2 and change:
                        result.update(change)
                    return result
                if change:
                    with self.assertRaises(SafetyStop):
                        review.scan(call, Event(), lambda _: None)
                    self.assertFalse(review.data["ready"])
                else:
                    review.scan(call, Event(), lambda _: None)
                    self.assertTrue(review.data["ready"])
                    self.assertEqual(len(review.data["rows"]), 1001)
                self.assertTrue(all(p["page_size"] == 1000 for p in calls[1:]))

    def test_scan_resume_preserves_results_with_new_total_and_order(self):
        with tempfile.TemporaryDirectory() as folder:
            review = CrossReview(folder, "复核对象")
            entries = {str(i): {"title": "旧题名", "status": "未核验"} for i in range(30)}
            entries["0"].update(status="通过", issues=[], pending=[], confirmed="保留结果")
            review.data.update(rows=entries, scanned=30, total=188, page_size=10)
            review.save()
            review = CrossReview(folder, "复核对象")
            ids = [str(i) for i in reversed(range(31))]
            calls = []
            def call(action, payload):
                page = payload["page"]
                calls.append(payload.copy())
                size = payload.get("page_size", 10)
                return dict(protocol=1, owner="复核对象", page=page, total=31, page_size=size,
                            rows=[row(sid) for sid in ids[:size]])
            review.scan(call, Event(), lambda _: None)
            saved = CrossReview(folder, "复核对象").data
            self.assertEqual(calls, [{"owner": "复核对象", "page": 1},
                                     {"owner": "复核对象", "page": 1, "page_size": 31}])
            self.assertTrue(saved["ready"])
            self.assertEqual(set(saved["rows"]), set(ids))
            self.assertEqual(saved["rows"]["0"], entries["0"])
            self.assertEqual((saved["scanned"], saved["total"]), (31, 31))

    def test_scan_rejects_missing_old_queue_ids_after_collection(self):
        with tempfile.TemporaryDirectory() as folder:
            review = CrossReview(folder, "复核对象")
            review.data["rows"]["old"] = {"status": "通过"}
            review.save()
            def call(action, payload):
                return dict(protocol=1, owner="复核对象", page=1, total=1,
                            page_size=payload.get("page_size", 10), rows=[row("new")])
            with self.assertRaisesRegex(SafetyStop, "已入队的 1 条不在当前已处理列表"):
                review.scan(call, Event(), lambda _: None)
            saved = CrossReview(folder, "复核对象").data
            self.assertFalse(saved["ready"])
            self.assertEqual(set(saved["rows"]), {"old", "new"})
            self.assertEqual(saved["rows"]["old"]["status"], "通过")

    def test_scan_stop_saves_read_pages_and_does_not_read_next_page(self):
        with tempfile.TemporaryDirectory() as folder:
            review = CrossReview(folder, "复核对象")
            stop, calls = Event(), []
            def call(action, payload):
                calls.append(payload["page"])
                stop.set()
                return dict(protocol=1, owner="复核对象", page=1, total=2, page_size=1, rows=[row("a")])
            review.scan(call, stop, lambda _: None)
            saved = CrossReview(folder, "复核对象").data
            self.assertEqual(calls, [1])
            self.assertFalse(saved["ready"])
            self.assertEqual(saved["scanned"], 1)
            self.assertEqual(set(saved["rows"]), {"a"})

    def test_scan_empty_list_never_reads_page_two(self):
        with tempfile.TemporaryDirectory() as folder:
            review = CrossReview(folder, "复核对象")
            calls = []
            def call(action, payload):
                calls.append(payload["page"])
                return dict(protocol=1, owner="复核对象", page=1, total=0, page_size=10, rows=[])
            review.scan(call, Event(), lambda _: None)
            self.assertEqual(calls, [1])
            self.assertTrue(review.data["ready"])
            self.assertEqual(review.data["scanned"], 0)

    def test_scan_rejects_more_unique_ids_than_total(self):
        with tempfile.TemporaryDirectory() as folder:
            review = CrossReview(folder, "复核对象")
            # Probe + capped full read, then two drifting fallback passes.
            batches = iter(("a", "a", "a", "a", "b", "c", "d", "a"))
            def call(action, payload):
                return dict(protocol=1, owner="复核对象", page=payload["page"], total=3,
                            page_size=payload.get("page_size", 1),
                            rows=[row(next(batches))])
            with self.assertRaisesRegex(SafetyStop, "ID 数超过分页总数"):
                review.scan(call, Event(), lambda _: None)
            self.assertFalse(CrossReview(folder, "复核对象").data["ready"])

    def test_scan_rejects_invalid_page_receipts(self):
        for changes in ({"protocol": 2}, {"owner": "其他人"}, {"page": 2},
                        {"total": True}, {"total": -1}, {"total": "1"},
                        {"page_size": True}, {"page_size": 0}, {"page_size": "1"},
                        {"rows": []}, {"rows": {}}, {"rows": [row("a"), row("b")]}):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as folder:
                review = CrossReview(folder, "复核对象")
                def call(action, payload):
                    return {**dict(protocol=1, owner="复核对象", page=1, total=1,
                                   page_size=1, rows=[row("a")]), **changes}
                with self.assertRaises(SafetyStop):
                    review.scan(call, Event(), lambda _: None)
                self.assertFalse(review.data["ready"])
                self.assertEqual(review.data["rows"], {})

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
                        size = payload.get("page_size", 1)
                        return dict(protocol=1, owner=owner, page=page, total=2, page_size=size,
                                    rows=list(entries.values())[:size])
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
        for batch in ([row("001", "其他人")], [{**row("001"), "markStatus": "待处理"}],
                      [row("001"), row("001")], [row("")], [row(123)], [None]):
            with self.subTest(batch=batch), tempfile.TemporaryDirectory() as folder:
                owner = "复核对象"
                review = CrossReview(folder, owner)
                def page(action, payload):
                    return dict(protocol=1, owner=owner, page=payload["page"], total=len(batch),
                                page_size=len(batch), rows=batch)
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

    def panel(self, reviewer="手动校对员"):
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
        resolver = patch("core.fixed_roster_path", return_value=self.path.with_name("list.xlsx"))
        resolver.start()
        self.addCleanup(resolver.stop)
        panel = CrossReviewPanel(root, app)
        if reviewer is not None:
            panel.reviewer.set(reviewer)
        panel.owner.set("复核对象")
        return panel

    def test_panel_without_roster_loads_trims_and_confirms(self):
        self.review.save()
        panel = self.panel()
        panel.app.roster = None
        panel.owner.set("  复核对象  ")
        panel.load()
        self.assertEqual(panel.review.owner, "复核对象")
        panel.tree.selection_set("001")
        panel.note.set("手动确认")
        panel.reviewer.set("  手动校对员  ")
        panel.confirm()
        self.assertEqual(self.values()[1], ("001", "手动确认", "复核对象", "手动校对员"))
        self.assertTrue(self.persisted()["001"]["feedback_synced"])

    def test_panel_invalid_owner_never_loads(self):
        panel = self.panel()
        for name in ("   ", "人" * 41, UNASSIGNED_OWNER):
            with self.subTest(name=name), patch("cross_review_ui.messages.showwarning") as warning:
                panel.owner.set(name)
                panel.load()
                self.assertIsNone(panel.review)
                warning.assert_called_once()
        panel.owner.set("人" * 40)
        panel.load()
        self.assertEqual(panel.review.owner, "人" * 40)

    def test_panel_invalid_reviewer_never_confirms(self):
        panel = self.panel()
        panel.review = self.review
        panel.render()
        panel.tree.selection_set("001")
        for name in ("   ", "复核对象", " 复核对象 ", "人" * 41, UNASSIGNED_OWNER):
            with self.subTest(name=name), patch("cross_review_ui.messages.showwarning") as warning:
                panel.reviewer.set(name)
                panel.note.set("问题")
                panel.confirm()
                self.assertNotIn("confirmed", self.review.data["rows"]["001"])
                self.assertEqual(self.values(), [HEADERS])
                warning.assert_called_once()

    def test_reviewer_prefill_and_corrupt_or_missing_file(self):
        path = self.base / "runtime/cross-review/reviewer.json"
        self.assertEqual(self.panel(None).reviewer.get(), "")
        path.parent.mkdir(parents=True)
        for content in ("{broken", "[]", '"name"', '{"reviewer": 3}',
                        json.dumps({"reviewer": UNASSIGNED_OWNER}),
                        json.dumps({"reviewer": "人" * 41})):
            path.write_text(content, encoding="utf-8")
            self.assertEqual(self.panel(None).reviewer.get(), "")
        path.write_bytes(b"\xff")
        self.assertEqual(self.panel(None).reviewer.get(), "")
        path.write_text(json.dumps({"reviewer": "手动校对员"}), encoding="utf-8")
        self.assertEqual(self.panel(None).reviewer.get(), "手动校对员")

    def test_reviewer_saved_and_save_failure_does_not_block_confirmation(self):
        panel = self.panel()
        panel.review = self.review
        panel.render()
        panel.tree.selection_set("001")
        panel.note.set("问题")
        panel.confirm()
        self.assertEqual(self.panel(None).reviewer.get(), "手动校对员")
        path = self.base / "runtime/cross-review/reviewer.json"
        path.unlink()
        path.mkdir()  # A directory at the file path makes preference saving fail.
        panel.reviewer.set("新校对员")
        panel.note.set("新问题")
        panel.confirm()
        self.assertEqual(self.values()[1], ("001", "新问题", "复核对象", "新校对员"))
        self.assertTrue(self.persisted()["001"]["feedback_synced"])
        self.assertIn("保存失败", panel.app.status.get())
        self.assertIn("已确认并同步", panel.app.status.get())

    def test_feedback_path_uses_fixed_location_or_service_base(self):
        panel = self.panel()
        panel.app.roster = SimpleNamespace(path=self.base / "wrong/list.xlsx")
        panel.load()
        self.assertEqual(panel.review.feedback_path, self.path)
        with patch("core.fixed_roster_path", side_effect=SafetyStop("名单不存在")):
            panel.app.roster = None
            panel.load()
        self.assertEqual(panel.review.feedback_path, self.base / "复核反馈.xlsx")

    def test_app_without_roster_can_start_run_and_confirm(self):
        import tkinter as tk
        from app import App
        from workflow_service import WorkflowService
        root = tk.Tk()
        root.withdraw()
        item = row("001")
        class Bridge:
            online = True

            def call(self, action, payload):
                if action == "cross_review_page":
                    return dict(protocol=1, owner="复核对象", page=1, total=1, page_size=1,
                                rows=[item])
                if action == "status":
                    return {"row": item}
                if action == "search":
                    return detail(item, right="002")
                raise AssertionError(action)

        app = App(root, service=WorkflowService(self.base), auto_load=False, bridge=Bridge())
        self.addCleanup(root.destroy)
        self.addCleanup(lambda: root.after_cancel(app.timer))
        self.assertIsNone(app.roster)
        app.notebook.select(app.pages["cross"])
        panel = app.cross_review
        panel.owner.set("复核对象")
        with patch("core.fixed_roster_path", return_value=self.path.with_name("list.xlsx")):
            panel.load_button.invoke()
        self.assertEqual(str(app.start_button["state"]), "normal")
        app.start_button.invoke()
        while True:
            kind, value = app.events.get(timeout=5)
            if kind == "cross_result":
                self.assertEqual(value, ("本轮交叉核验结束。", False))
                break
        app.set_busy(False)
        panel.render()
        panel.tree.selection_set("001")
        panel.note.set("认领工号不一致")
        panel.reviewer.set("手动校对员")
        panel.confirm_button.invoke()
        self.assertEqual(self.values()[1], ("001", "认领工号不一致", "复核对象", "手动校对员"))
        self.assertTrue(self.persisted()["001"]["feedback_synced"])

    def test_start_requires_input_to_match_loaded_owner(self):
        panel = self.panel()
        panel.app.roster = None
        panel.load()
        panel.app.bridge = SimpleNamespace(online=True)
        panel.app.stop = Event()
        panel.app.set_busy = lambda busy: setattr(panel.app, "busy", busy)
        panel.owner.set("另一个人")
        with patch("cross_review_ui.threading.Thread") as thread:
            panel.start()
            thread.assert_not_called()
            self.assertFalse(panel.app.busy)
            panel.owner.set("复核对象")
            panel.start()
            thread.return_value.start.assert_called_once()
            self.assertTrue(panel.app.busy)

    def test_run_warning_is_redacted_and_dispatched_on_main_thread_only_for_failure(self):
        import tkinter as tk
        from threading import get_ident
        from app import App
        from workflow_service import WorkflowService
        main_thread = get_ident()
        for outcome in ("failure", "complete", "pause"):
            with self.subTest(outcome=outcome):
                root = tk.Tk()
                root.withdraw()
                app = App(root, service=WorkflowService(self.base), auto_load=False,
                          bridge=SimpleNamespace(online=True, token="secret-credential"))
                try:
                    panel = app.cross_review
                    panel.owner.set("复核对象")
                    panel.review = CrossReview(self.base, "复核对象")
                    worker_threads, warning_threads = [], []
                    def run(bridge, stop, progress):
                        worker_threads.append(get_ident())
                        if outcome == "failure":
                            raise SafetyStop("分页不稳定 secret-credential")
                        if outcome == "pause":
                            stop.set()
                    with patch.object(panel.review, "run", side_effect=run), \
                            patch("cross_review_ui.messages.showwarning",
                                  side_effect=lambda *a, **k: warning_threads.append(get_ident())) as warning:
                        panel.start()
                        event = app.events.get(timeout=5)
                        self.assertEqual(event[0], "cross_result")
                        warning.assert_not_called()
                        app.events.put(event)
                        root.after_cancel(app.timer)
                        app.pump()
                        self.assertNotEqual(worker_threads, [main_thread])
                        self.assertFalse(app.busy)
                        if outcome == "failure":
                            warning.assert_called_once_with("交叉核验已暂停", app.status.get(), parent=root)
                            self.assertIn("分页不稳定", app.status.get())
                            self.assertNotIn("secret-credential", app.status.get())
                            self.assertEqual(warning_threads, [main_thread])
                        else:
                            warning.assert_not_called()
                            self.assertIn("已暂停" if outcome == "pause" else "结束", app.status.get())
                finally:
                    root.after_cancel(app.timer)
                    root.destroy()

    def test_panel_confirm_reports_success_only_after_sync(self):
        panel = self.panel()
        panel.review = self.review
        panel.render()
        panel.tree.selection_set("001")
        panel.note.set("界面确认")
        panel.confirm()
        self.assertEqual(panel.app.status.get(), "已确认并同步到复核反馈.xlsx")
        self.assertEqual(self.values()[1], ("001", "界面确认", "复核对象", "手动校对员"))
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
        for reviewer in ("", UNASSIGNED_OWNER, " 空格 ", "复核对象", "人" * 41, None):
            with self.subTest(reviewer=reviewer):
                before = self.path.read_bytes()
                with self.assertRaisesRegex(SafetyStop, "校对人"):
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

    def test_panel_uses_manual_reviewer_and_keeps_failed_confirmation_visible(self):
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
        self.assertEqual(self.values()[1], ("001", "从界面确认", "复核对象", "手动校对员"))
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
