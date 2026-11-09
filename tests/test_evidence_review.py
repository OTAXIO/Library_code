import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest

from evidence_review import paper_identity, role_suggestion, author_suggestion
from tests.test_need_import import make_roster, web_row
from workflow_service import WorkflowService


class EvidenceRulesTests(unittest.TestCase):
    def test_same_wos_does_not_override_title_conflict(self):
        self.assertEqual(paper_identity([
            dict(label="题名", sa="Paper A", library="Paper B"),
            dict(label="WOS记录号", sa="WOS:000000000000001", library="WOS:000000000000001"),
        ]), "错配待核")

    def test_role_priority_and_partial_email(self):
        self.assertEqual(role_suggestion(3, explicit=[1, 3], emails=[1, 2, 3])[1], [1, 3])
        self.assertEqual(role_suggestion(3, explicit=None, emails=[1, 2, 3])[1], [])
        self.assertEqual(role_suggestion(3, explicit=None, emails=[2])[1], [2])
        self.assertEqual(role_suggestion(1, explicit=None, emails=[])[1], [1])
        self.assertIsNone(role_suggestion(3, explicit=None, emails=[])[1])
        self.assertIsNone(role_suggestion(3, explicit=None, emails=[4])[1])
        self.assertIsNone(role_suggestion(3, explicit=[2], emails=[2], complete=False)[1])

    def test_author_suggestion_requires_positive_evidence(self):
        self.assertEqual(author_suggestion(["Alice"], ["Bob"], ["Alice"], [["Other University"]]), "库内作者待修正")
        self.assertEqual(author_suggestion(["Bob"], ["Bob"], ["Alice"], [["Other University"]]), "SA数据有误")
        self.assertEqual(author_suggestion(["Alice"], ["Alice"], ["Alice"], [["Other University"]]), "作者署名非交大")
        self.assertEqual(author_suggestion(["Alice"], ["Alice"], ["Alice"],
                         [["Other University", "Shanghai Jiao Tong University"]]), "待核")
        self.assertEqual(author_suggestion(["Alice"], ["Alice"], ["Alice"], [[]]), "待核")

    def test_read_only_workflow_displays_front_title_and_never_changes_workbook(self):
        with TemporaryDirectory() as folder:
            base = Path(folder)
            path = base / "list.xlsx"
            make_roster(path, [("测试员", 0), ("其他人", 0)])
            service = WorkflowService(base)
            roster = service.load()
            record = roster.records[0]
            before = path.read_bytes()
            calls = []
            class Bridge:
                def call(self, action, payload):
                    calls.append(action)
                    if action == "status":
                        assert payload["sa_id"] == record.sa_id
                        return {"row": web_row(record)}
                    assert action == "front_search"
                    return {"front_search_protocol": 1, "verified": True, **payload,
                            "complete": True, "total": 1, "items": [
                                dict(id="123", title="前台原题名", doi="", wos="", deleted=False)]}
            result = service.review_evidence(roster, "测试员", "pending", 10, Bridge(), Event(), lambda _: None)
            report = json.loads(Path(result.report_path).read_text(encoding="utf-8"))
            self.assertEqual(calls, ["status", "front_search"])
            self.assertEqual(report[0]["重复管理检索题名"], ["前台原题名"])
            self.assertEqual(report[0]["待核原因"], "前台检索有结果，文献身份待核")
            self.assertEqual(path.read_bytes(), before)
