import csv
import io
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

from automation import ImportStore, WOSFlow, classify, identity, parse_wos
from core import Record, SafetyStop


def sample(**changes):
    data = dict(TI="Synthetic paper", AU="Test, A", AF="Alice Test", SO="Synthetic Journal", PY="2026",
                C1="[Test, Alice] Shanghai Jiao Tong Univ, Shanghai, China", UT="WOS:000123456789012", DI="10.1234/test")
    data.update(changes)
    text = io.StringIO()
    writer = csv.writer(text, delimiter="\t", lineterminator="\r\n")
    writer.writerow(data)
    writer.writerow(data.values())
    return text.getvalue().encode("utf-8")


def record(**kwargs):
    base = Record(2, "测试员", "demo-001", "Synthetic paper", "10.1234/test", "", "00001", 0, "", "待处理", "", "1")
    return replace(base, **kwargs)


def result(matches=0, fields=None, **changes):
    row = dict(saLzkId="demo-001", matchCount=matches, itemId="" if matches == 0 else ",".join(str(i) for i in range(matches)), markStatus="待处理",
               titleValue="Synthetic paper", doiValue="10.1234/test", wosValue="")
    row.update(changes)
    return dict(row=row, comparison=fields or [])


class RoutingTests(unittest.TestCase):
    def test_missing_and_multiple(self):
        self.assertEqual(classify(record(), result()).route, "wos")
        self.assertEqual(classify(record(), result(2)).route, "manual")

    def test_wrong_identity_and_count(self):
        for row in (result(saLzkId="another"), result(matchCount=True), result(0, itemId="1")):
            with self.assertRaises(SafetyStop):
                classify(record(), row)

    def test_updated_sa_metadata_stops_old_roster_import(self):
        for data in (result(titleValue="Changed"), result(doiValue="10.1234/changed"), result(wosValue="WOS:000123456789012")):
            with self.assertRaises(SafetyStop): classify(record(), data)

    def test_done_never_routes_to_write(self):
        self.assertEqual(classify(record(done=True), result()).route, "manual")
        self.assertEqual(classify(record(), result(markStatus="已处理")).route, "manual")

    def test_claim_preserves_id_and_other_issues(self):
        fields = [{"label": "认领状态", "sa": "测试员(00001)", "library": "未认领"},
                  {"label": "DOI", "sa": "", "library": "10.1234/test"}]
        plan = classify(record(), result(1, fields))
        self.assertEqual(plan.route, "claim")
        self.assertIn("DOI", plan.issues)
        self.assertEqual(classify(record(staff_id="1"), result(1, fields)).route, "manual")

    def test_corresponding_author_is_never_blindly_fixed(self):
        fields = [{"label": "通讯作者", "sa": "Alice", "library": "Bob"}]
        self.assertEqual(classify(record(), result(1, fields)).route, "metadata")


class ExportTests(unittest.TestCase):
    def test_tab_full_record_utf8_bom_and_utf16(self):
        raw = sample(TI="Synthetic paper\nwith second line")
        for content in (raw, b"\xef\xbb\xbf" + raw, raw.decode().encode("utf-16")):
            parsed = parse_wos(content)
            self.assertEqual(parsed["wos"], "WOS:000123456789012")
            self.assertTrue(parsed["sjtu"])

    def test_reject_multiple_wrong_format_and_incomplete(self):
        for content in (b"<html>login</html>", sample() + sample().split(b"\r\n")[1] + b"\r\n",
                        b"PT J\nTI Fake\nER\nEF", sample(UT=""), sample(PY="unknown"), b"x" * 524289):
            with self.assertRaises(SafetyStop):
                parse_wos(content)

    def test_identifier_conflict_wins(self):
        with self.assertRaises(SafetyStop):
            identity(record(doi="10.1234/other"), parse_wos(sample()))
        with self.assertRaises(SafetyStop):
            identity(record(wos="WOS:000000000000000"), parse_wos(sample()))

    def test_short_title_requires_human_even_with_matching_doi(self):
        self.assertFalse(identity(record(title="Synthetic"), parse_wos(sample())))
        self.assertFalse(identity(record(doi=""), parse_wos(sample())))
        self.assertTrue(identity(record(), parse_wos(sample())))

    def test_affiliation_cannot_be_inferred_from_title(self):
        with self.assertRaises(SafetyStop):
            identity(record(), parse_wos(sample(C1="Other University")))


class FlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = ImportStore(Path(self.tmp.name) / "imports")
        self.download = Path(self.tmp.name) / "wos.txt"
        self.download.write_bytes(sample())
        self.batch = dict(id="batch-001", status=1, batchNumber="TEST", modelId="m")
        self.calls = []
        self.uploads = self.submits = self.pushes = 0
        self.fail = None
        def call(action, payload, timeout=75):
            self.calls.append(action)
            if action == self.fail:
                raise SafetyStop("simulated timeout")
            if action == "search": return result()
            if action == "wos_search": return {}
            if action == "wos_export": return {"path": str(self.download), "sa_id": "demo-001"}
            if action == "import_scan": return {"batches": []}
            if action == "import_upload":
                self.uploads += 1
                return {"uploaded": True, "sha256": payload["candidate"]["sha256"]}
            if action == "import_submit":
                self.submits += 1
                return {"submitted": True}
            if action == "import_check": return {"verified": True, "batch": dict(self.batch)}
            if action == "import_push":
                self.pushes += 1
                self.batch["status"] = 2
                return {"submitted": True}
            raise AssertionError(action)
        self.bridge = Mock()
        self.bridge.call.side_effect = call
        self.flow = WOSFlow(self.bridge, self.store)

    def tearDown(self):
        self.tmp.cleanup()

    def test_full_flow_one_upload_import_push_no_excel_actions(self):
        self.flow.prepare(record())
        self.assertEqual(self.flow.proceed(record())["phase"], "pushed")
        self.flow.proceed(record())
        self.assertEqual((self.uploads, self.submits, self.pushes), (1, 1, 1))
        self.assertNotIn("complete", self.calls)
        self.assertNotIn("submit_claim", self.calls)

    def test_each_wos_call_reports_one_operation_even_when_paused(self):
        events = []
        self.flow.audit = lambda action, outcome, sa_id: events.append((action, outcome, sa_id))
        self.flow.prepare(record())
        self.assertEqual(events[:2], [("wos_search", "已执行", "demo-001"),
                                      ("wos_export", "已执行", "demo-001")])
        self.fail = "import_upload"
        with self.assertRaises(SafetyStop):
            self.flow.proceed(record())
        self.assertEqual(events[-1], ("import_upload", "已暂停", "demo-001"))
        self.assertEqual(sum(action == "import_upload" for action, _, _ in events), 1)

    def test_title_only_pauses_before_any_upload(self):
        rec = record(doi="")
        original = self.bridge.call.side_effect
        self.bridge.call.side_effect = lambda action, payload, timeout=75: result(doiValue="") if action == "search" else original(action, payload, timeout)
        self.flow.prepare(rec)
        self.assertEqual(self.flow.proceed(rec)["phase"], "exported")
        self.assertEqual(self.uploads, 0)
        self.assertEqual(self.flow.proceed(rec, confirm_identity=True)["phase"], "pushed")

    def test_import_intent_survives_failure_and_restart(self):
        self.flow.prepare(record())
        self.fail = "import_submit"
        with self.assertRaises(SafetyStop): self.flow.proceed(record())
        self.assertEqual(self.store.get(record())["phase"], "import_intent")
        self.fail = None
        restarted = WOSFlow(self.bridge, ImportStore(self.store.root))
        restarted.proceed(record())
        self.assertEqual(self.calls.count("import_submit"), 1)
        self.assertEqual(self.uploads, 1)

    def test_upload_uncertainty_never_retries(self):
        self.flow.prepare(record())
        self.fail = "import_upload"
        with self.assertRaises(SafetyStop): self.flow.proceed(record())
        self.fail = None
        with self.assertRaisesRegex(SafetyStop, "上传结果不明"): self.flow.proceed(record())
        self.assertEqual(self.calls.count("import_upload"), 1)

    def test_push_uncertainty_never_retries(self):
        self.flow.prepare(record())
        self.fail = "import_push"
        with self.assertRaises(SafetyStop): self.flow.proceed(record())
        self.fail = None
        with self.assertRaisesRegex(SafetyStop, "推送已提交"): self.flow.proceed(record())
        self.assertEqual(self.calls.count("import_push"), 1)

    def test_file_tampering_and_changed_roster_block(self):
        state = self.flow.prepare(record())
        (self.store.root / (state["candidate"]["sha256"] + ".txt")).write_bytes(b"changed")
        with self.assertRaises(SafetyStop): self.flow.proceed(record())
        with self.assertRaises(SafetyStop): self.store.get(record(title="changed"))
        self.assertEqual(self.uploads, 0)

    def test_existing_remote_batch_blocks_upload(self):
        self.flow.prepare(record())
        original = self.bridge.call.side_effect
        self.bridge.call.side_effect = lambda action, payload, timeout=75: {"batches": [self.batch]} if action == "import_scan" else original(action, payload, timeout)
        with self.assertRaisesRegex(SafetyStop, "已有同说明"): self.flow.proceed(record())
        self.assertEqual(self.uploads, 0)

    def test_cancel_between_steps_is_safe(self):
        self.flow.prepare(record())
        def stop(): raise SafetyStop("用户暂停")
        self.flow.unchanged = stop
        with self.assertRaises(SafetyStop): self.flow.proceed(record())
        self.assertEqual(self.uploads, 0)

    def test_current_wos_export_does_not_search(self):
        self.flow.prepare(record(), current_wos=True)
        self.assertNotIn("wos_search", self.calls)


if __name__ == "__main__":
    unittest.main()
