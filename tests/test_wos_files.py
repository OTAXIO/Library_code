"""Disposable download directories only; never mutate Downloads or list.xlsx."""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from automation import ImportStore, parse_wos
from core import Record, SafetyStop
from tests.test_automation import sample
from wos_files import archive_export, download_paths, find_export, read_export, record_ut, main


class IntakeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.raw = sample()
        self.record = Record(2, "谭勋策", "demo-1", "Synthetic paper", "10.1234/test", "", "1", 0, "", "待处理", "", "1")
        self.path = self.root / "savedrecs.txt"
        self.path.write_bytes(self.raw)
        self.url = "https://www.webofscience.com/wos/woscc/full-record/WOS:000123456789012"

    def test_read_exact_single_record_and_keep_original(self):
        raw, candidate = read_export(self.path)
        self.assertEqual(raw, self.raw)
        store = ImportStore(self.root / "code" / "runtime" / "wos-downloads")
        target = Path(archive_export(store, self.path, raw, candidate, self.record))
        self.assertEqual(target.name, f"demo-1__WOS-000123456789012__{candidate['sha256'][:12]}.txt")
        self.assertEqual(self.path.read_bytes(), self.raw)
        self.assertEqual(target.read_bytes(), self.raw)
        manifest = json.loads(target.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["sa_id"], self.record.sa_id)
        self.assertEqual(manifest["record_key"], self.record.key)
        self.assertEqual(manifest["sha256"], candidate["sha256"])
        self.assertTrue(manifest["identity_confirmed"])
        self.assertEqual(archive_export(store, self.path, raw, candidate, self.record), str(target))
        target.write_bytes(b"tampered")
        with self.assertRaisesRegex(SafetyStop, "拒绝覆盖"):
            archive_export(store, self.path, raw, candidate, self.record)

    def test_match_contents_not_name_or_recency(self):
        newer = self.root / "savedrecs (99).txt"
        newer.write_bytes(sample(TI="Wrong paper", DI="10.1234/other"))
        self.assertEqual(find_export(self.record, [self.root], self.url)[0], self.path)

    def test_filename_counter_and_duplicate_exact_copy_are_allowed(self):
        duplicate = self.root / "savedrecs (1).txt"
        duplicate.write_bytes(self.raw)
        self.assertEqual(len(download_paths(self.root)), 2)
        self.assertIsNotNone(find_export(self.record, [self.root]))

    def test_partial_and_arbitrary_files_not_scanned(self):
        for name in ("savedrecs.txt.crdownload", "random.txt", "savedrecs.txt.exe", "savedrecs (3).txt.tmp"):
            (self.root / name).write_bytes(self.raw)
        self.assertEqual(download_paths(self.root), [self.path])

    def test_any_identifier_or_title_contradiction_rejects(self):
        for changed in (replace(self.record, doi="10.1234/other"),
                        replace(self.record, wos="WOS:000999999999999"),
                        replace(self.record, title="Other title"), replace(self.record, doi="")):
            with self.subTest(changed=changed):
                self.assertIsNone(find_export(changed, [self.root]))

    def test_search_record_ut_also_required(self):
        self.assertIsNone(find_export(self.record, [self.root], self.url.replace("000123456789012", "000999999999999")))

    def test_title_only_recovery_needs_explicit_journal_correlation_and_exact_ut(self):
        record = replace(self.record, doi="")
        self.assertIsNone(find_export(record, [self.root], self.url))
        self.assertIsNone(find_export(record, [self.root], correlated=True))
        self.assertEqual(find_export(record, [self.root], self.url, correlated=True)[0], self.path)
        self.assertIsNone(find_export(record, [self.root], self.url.replace("000123456789012", "000999999999999"), correlated=True))

    def test_conflicting_exports_stop_even_if_same_title_and_doi(self):
        (self.root / "savedrecs (1).txt").write_bytes(sample(PY="2025"))
        with self.assertRaisesRegex(SafetyStop, "内容不同"):
            find_export(self.record, [self.root])

    def test_multi_record_and_invalid_bytes_refused(self):
        for raw in (b"invalid", self.raw + self.raw.splitlines()[1] + b"\n", b"\xffbad"):
            self.path.write_bytes(raw)
            with self.assertRaises(SafetyStop):
                read_export(self.path)
            self.assertIsNone(find_export(self.record, [self.root]))

    def test_other_owner_done_nonzero_unsafe_id_never_linked(self):
        for change in ({"owner": "其他人"}, {"done": True}, {"matches": 1}, {"sa_id": "../../escape"}):
            store = ImportStore(self.root / str(len(change)) / next(iter(change)))
            with self.assertRaises(SafetyStop):
                archive_export(store, self.path, self.raw, parse_wos(self.raw), replace(self.record, **change))
            self.assertFalse(list(store.root.rglob("*.txt")))

    def test_unlinked_copy_does_not_invent_association_or_sjtu(self):
        raw = sample(AF="Alice Test", C1="[Alice Test] East China Normal Univ, Shanghai, China")
        self.path.write_bytes(raw)
        store = ImportStore(self.root / "archive")
        copied = Path(archive_export(store, self.path, raw, parse_wos(raw)))
        info = json.loads(copied.with_suffix(".json").read_text(encoding="utf-8"))
        self.assertIsNone(info["sa_id"])
        self.assertEqual(info["affiliation_status"], "non_sjtu")
        self.assertFalse(info["linked_to_roster"])
        self.assertIsNone(store.get(self.record))

    def test_cli_default_is_read_only_preview(self):
        with patch("builtins.print") as out:
            self.assertEqual(main(["--source-dir", str(self.root)]), 0)
        report = json.loads(out.call_args.args[0])
        self.assertEqual(report["mode"], "preview")
        self.assertEqual(report["files"][0]["wos"], "WOS:000123456789012")
        self.assertEqual(list(self.root.iterdir()), [self.path])

    def test_only_exact_wos_core_record_urls(self):
        self.assertEqual(record_ut(self.url), "WOS:000123456789012")
        self.assertEqual(record_ut(self.url.replace("WOS:", "WOS%3A")), "WOS:000123456789012")
        for bad in ("", None, self.url.replace("https:", "http:"), self.url + "?other=1",
                    self.url.replace("www.webofscience.com", "evil.example"),
                    self.url.replace("/WOS:", "/%2fWOS:"), self.url + "/more"):
            with self.subTest(bad=bad), self.assertRaises(SafetyStop):
                record_ut(bad)
