import tempfile
from pathlib import Path
from threading import Event
import unittest

from openpyxl import load_workbook

from core import SafetyStop
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

    def test_scan_resume_and_export_only_confirmed_as_text(self):
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
            review.confirm("001", "认领工号不一致")
            resumed = CrossReview(folder, owner)
            stop.clear()
            actions.clear()
            resumed.run(bridge, stop, lambda _: None)
            self.assertEqual(actions, ["status", "search"])
            path = Path(folder) / "feedback.xlsx"
            self.assertEqual(resumed.export(path), 1)
            book = load_workbook(path)
            try:
                self.assertEqual(list(book.active.values), [("Said", "问题备注"), ("001", "认领工号不一致")])
                self.assertEqual(book.active["A2"].data_type, "s")
            finally:
                book.close()
            with self.assertRaises(FileExistsError):
                resumed.export(path)

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
