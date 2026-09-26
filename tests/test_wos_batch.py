import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import ANY, Mock, patch

from automation import ImportStore
from core import Record, SafetyStop
from submission_prepare import matches_paper
from tests.test_automation import sample
from wos_batch import export, plan, safe_name, wos_targets


def record(**kwargs):
    base = Record(2, "测试员", "demo-001", "Synthetic paper", "10.1234/test", "", "00001", 0, "", "待处理", "", "1")
    return replace(base, **kwargs)


class FakeRoster:
    def __init__(self, records):
        self.records = records

    def assert_unchanged(self):
        return None


class TargetsTests(unittest.TestCase):
    def test_only_zero_match_unfinished_wos_records(self):
        records = [record(), record(row=3, sa_id="done-001", done=True),
                   record(row=4, sa_id="matched-001", matches=2), record(row=5, sa_id="cnki-001")]
        classification = {f"p{n}": {"import_route": {"recommended_channel": "WOS"}} for n in (1, 2, 3)}
        classification["p4"] = {"import_route": {"recommended_channel": "CNKI"}}
        papers = [{"id": f"p{n}", "rows": [n + 1]} for n in (1, 2, 3, 4)]
        self.assertEqual([r.sa_id for r in wos_targets(FakeRoster(records), classification, papers)],
                         ["demo-001"])

    def test_unclassified_records_are_skipped(self):
        records = [record(), record(row=3, sa_id="unknown-001")]
        papers = [{"id": "p1", "rows": [2]}, {"id": "p2", "rows": [3]}]
        classification = {"p1": {"import_route": {"recommended_channel": "WOS"}}, "p2": {}}
        self.assertEqual([r.sa_id for r in wos_targets(FakeRoster(records), classification, papers)],
                         ["demo-001"])
        self.assertEqual(wos_targets(FakeRoster(records), {}, papers), [])

    def test_safe_name_has_no_path_or_separator_characters(self):
        name = safe_name(record(sa_id="../../etc/pa ss:wd*"), "a" * 64)
        self.assertTrue(name.startswith("WOS-"))
        self.assertNotIn("/", name)
        self.assertNotIn("\\", name)
        self.assertNotIn(" ", name)
        self.assertNotIn(":", name)
        self.assertTrue(name.endswith("-" + "a" * 8 + ".txt"))


class PlanTests(unittest.TestCase):
    def test_plan_keys_on_the_fingerprint_and_returns_the_papers(self):
        document = {"sha256": "abc", "papers": [{"id": "p", "rows": [2]}]}
        with patch("submission_prepare.saved_classification", return_value={"p": {}}) as saved:
            classification, papers = plan(document)
        saved.assert_called_once_with("abc", ANY)
        self.assertEqual(classification, {"p": {}})
        self.assertEqual(papers, document["papers"])

    def test_plan_requires_a_matching_saved_classification(self):
        with patch("submission_prepare.saved_classification", return_value={}):
            with self.assertRaises(SafetyStop):
                plan({"sha256": "abc", "papers": []})

    def test_plan_rejects_a_roster_object(self):
        # Regression: a core.Roster has .sha256 but is not subscriptable, and passing it
        # here is exactly the wiring bug this helper exists to prevent.
        class FakeRoster:
            sha256 = "abc"

        with self.assertRaises(TypeError):
            plan(FakeRoster())


class ExportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inbox = self.root / "inbox"
        self.record = record()

    def bridge_for(self, raw):
        download = self.root / "download.txt"
        download.write_bytes(raw)

        def call(action, payload, timeout=75):
            if action == "wos_export":
                return {"path": str(download), "sa_id": payload["sa_id"]}
            return {}

        return Mock(call=Mock(side_effect=call))

    def classification(self, sa_id="demo-001", rows=(2,)):
        return (FakeRoster([self.record]), {f"p{n}": {"import_route": {"recommended_channel": "WOS"}}
                                            for n in rows}, [{"id": f"p{n}", "rows": [n]} for n in rows])

    def test_export_copies_the_full_record_into_the_intake_folder(self):
        raw = sample()
        roster, classification, papers = self.classification()
        result = export(roster, classification, papers, self.bridge_for(raw),
                        ImportStore(self.root / "imports"), self.inbox)
        self.assertEqual((result["total"], len(result["exported"]), result["failed"]), (1, 1, {}))
        written = Path(result["exported"][0]["file"])
        self.assertEqual(written.parent, self.inbox)
        self.assertEqual(written.read_bytes(), raw)
        # The intake scan must be able to match what we just wrote.
        self.assertTrue(matches_paper(written.read_text(encoding="utf-8"),
                                      {"title": self.record.title, "doi": self.record.doi}))

    def test_second_run_reuses_the_archive_without_touching_the_browser(self):
        raw = sample()
        roster, classification, papers = self.classification()
        bridge = self.bridge_for(raw)
        store = ImportStore(self.root / "imports")
        export(roster, classification, papers, bridge, store, self.inbox)
        calls = bridge.call.call_count
        self.assertGreater(calls, 0)
        export(roster, classification, papers, bridge, store, self.inbox)
        self.assertEqual(bridge.call.call_count, calls)

    def test_repeated_failures_stop_the_batch(self):
        records = [record(row=n, sa_id=f"demo-{n:03d}") for n in (2, 3, 4, 5)]
        classification = {f"p{n}": {"import_route": {"recommended_channel": "WOS"}} for n in (2, 3, 4, 5)}
        papers = [{"id": f"p{n}", "rows": [n]} for n in (2, 3, 4, 5)]
        # A relative path means the extension never reported a usable download.
        bridge = Mock(call=Mock(return_value={"path": "relative.txt", "sa_id": "demo-002"}))
        with self.assertRaises(SafetyStop):
            export(FakeRoster(records), classification, papers, bridge,
                   ImportStore(self.root / "imports"), self.inbox)
        self.assertEqual(list(self.inbox.iterdir()), [])

    def test_weak_identity_export_is_not_dropped_into_the_auto_adopted_folder(self):
        # The roster has neither DOI nor WOS ID, so identity() cannot confirm strongly.
        weak = record(doi="", wos="")
        inbox = self.root / "inbox"
        result = export(FakeRoster([weak]),
                        {"p1": {"import_route": {"recommended_channel": "WOS"}}},
                        [{"id": "p1", "rows": [2]}],
                        self.bridge_for(sample()), ImportStore(self.root / "imports"), inbox)
        self.assertEqual(result["exported"], [])
        self.assertEqual(len(result["unconfirmed"]), 1)
        self.assertEqual(result["unconfirmed"][0]["sa_id"], weak.sa_id)
        self.assertTrue(Path(result["unconfirmed"][0]["archive"]).is_file())
        # The intake folder is adopted automatically, so nothing may land there unverified.
        self.assertEqual(list(inbox.iterdir()), [])

    def test_stop_before_start_exports_nothing(self):
        import threading
        stop = threading.Event()
        stop.set()
        roster, classification, papers = self.classification()
        bridge = self.bridge_for(sample())
        result = export(roster, classification, papers, bridge,
                        ImportStore(self.root / "imports"), self.inbox, stop=stop)
        self.assertEqual(result["exported"], [])
        self.assertTrue(result["stopped"])
        bridge.call.assert_not_called()

    def test_bridge_failure_is_not_silently_swallowed(self):
        roster, classification, papers = self.classification()
        bridge = Mock(call=Mock(side_effect=RuntimeError("桥接断开")))
        with self.assertRaises(RuntimeError):
            export(roster, classification, papers, bridge,
                   ImportStore(self.root / "imports"), self.inbox)


if __name__ == "__main__":
    unittest.main()
