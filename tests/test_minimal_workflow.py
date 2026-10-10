"""Two-task facade + native UI, using only synthetic rosters and fake pages."""
import ast
import tempfile
import threading
import time
import tkinter as tk
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import App
from core import Record, SafetyStop
from tests import test_zero_match as zero_fixtures
from workflow_service import TaskResult, WorkflowService


def records():
    base = Record(2, "谭勋策", "claim-pending", "Test", "", "", "00001", 1, "100", "待处理", "作者不一致", "1")
    return [base, replace(base, sa_id="claim-skip", row=3, skipped=True),
            replace(base, sa_id="claim-done", row=4, done=True),
            replace(base, sa_id="zero-pending", row=5, matches=0, reason="", item_ids=""),
            replace(base, sa_id="zero-skip", row=6, matches=0, reason="", skipped=True),
            replace(base, sa_id="other-owner", row=7, owner="其他人"),
            replace(base, sa_id="role-issue", row=8, reason="通讯作者不一致")]


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.service = WorkflowService(self.base)
        self.roster = SimpleNamespace(records=records(), assert_unchanged=Mock())

    def test_modes_scopes_and_owner_are_separate(self):
        visible = self.service.visible
        self.assertEqual([r.sa_id for r in visible(self.roster, "claim", "谭勋策", "pending")], ["claim-pending"])
        self.assertEqual([r.sa_id for r in visible(self.roster, "claim", "谭勋策", "skipped")], ["claim-skip"])
        self.assertEqual([r.sa_id for r in visible(self.roster, "claim", "谭勋策", "done")], ["claim-done"])
        self.assertEqual([r.sa_id for r in visible(self.roster, "import", "谭勋策", "pending")], ["zero-pending"])
        self.assertEqual([r.sa_id for r in visible(self.roster, "import", "谭勋策", "skipped")], ["zero-skip"])

    def test_invalid_range_or_other_owner_never_runs(self):
        for owner, count, scope in (("", 1, "pending"), ("其他人", 1, "pending"),
                                    ("谭勋策", 0, "pending"), ("谭勋策", True, "pending"),
                                    ("谭勋策", 101, "pending"), ("谭勋策", 1, "done")):
            with self.assertRaises(SafetyStop):
                self.service.select(self.roster, "claim", owner, count, scope)
        with self.assertRaisesRegex(SafetyStop, "请选择负责人"):
            self.service.select(self.roster, "claim", "", 1, "pending")

    def test_stale_roster_blocked(self):
        self.roster.assert_unchanged.side_effect = SafetyStop("名单已改变")
        with self.assertRaisesRegex(SafetyStop, "名单已改变"):
            self.service.select(self.roster, "claim", "谭勋策", 1, "pending")

    def test_claim_dispatch_has_only_extension_bridge(self):
        bridge = Mock(online=True)
        selected = self.service.select(self.roster, "claim", "谭勋策", 1, "pending")
        result = SimpleNamespace(roster=self.roster, completed_ids=("claim-pending",), synced_ids=(),
                                 skipped={}, halted=False, cancelled=False)
        with patch("workflow_service.run_claim_batch", return_value=result) as run:
            saved = self.service.run(self.roster, selected, "claim", "pending", bridge, threading.Event(), Mock())
        self.assertIs(run.call_args.args[2], bridge)
        self.assertIn("完成 1", saved.summary)

    def test_offline_or_tampered_queue_cannot_dispatch(self):
        for selected, online in (([self.roster.records[5]], True), ([self.roster.records[0]], False)):
            with patch("workflow_service.run_claim_batch") as run, self.assertRaises(SafetyStop):
                self.service.run(self.roster, selected, "claim", "pending", Mock(online=online), threading.Event(), Mock())
            run.assert_not_called()

    def test_all_production_python_has_no_retired_dependency(self):
        root = Path(__file__).resolve().parents[1]
        forbidden = {"wos_browser", "paper_classify", "model_review", "submission_prepare", "settings_panel",
                     "classify_app", "wos_import_panel", "automation_panel", "pilot", "import_channels"}
        for path in root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                names = [node.module] if isinstance(node, ast.ImportFrom) else [n.name for n in node.names] if isinstance(node, ast.Import) else []
                self.assertFalse(forbidden.intersection(names), path.name)
            self.assertNotIn("bsk.exe", path.read_text(encoding="utf-8"))


class ExtensionOnlyImportTests(unittest.TestCase):
    def setUp(self):
        zero_fixtures.ZeroMatchTests.setUp(self)

    def test_full_facade_downloads_imports_and_writes_both_notes_without_bsk(self):
        self.sa.online = True
        original = self.sa.call
        def call(action, payload, timeout=75):
            if action == "wos_diagnose":
                from tests.test_wos_batch import PreflightTests
                return PreflightTests().capabilities()
            if action.startswith("wos_"):
                return self.download.call(action, payload, timeout)
            return original(action, payload, timeout)
        self.sa.call = call
        service = WorkflowService(self.folder)
        with patch("workflow_service.default_store", return_value=self.download.store):
            result = service.run(self.roster, self.roster.records[:1], "import", "pending", self.sa, self.stop, Mock())
        self.assertIn("完成 1", result.summary)
        self.assertTrue(result.roster.records[0].done)
        self.assertEqual(result.roster.records[0].source, "WOS")
        self.assertEqual(result.roster.records[0].remark, self.sa.rows[self.roster.records[0].sa_id]["remark"])
        self.assertTrue((self.folder / "runtime" / "wos-imports" / "zero-match.sqlite3").exists())
        self.assertGreater(len(service.operation_log.read()), 0)


class MinimalUITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.service = WorkflowService(self.temp.name)
        self.root = tk.Tk()
        self.root.withdraw()
        self.app = App(self.root, service=self.service, auto_load=False, bridge=Mock(online=True))
        self.addCleanup(lambda: self.app.close() if not self.app.closed and not self.app.busy else self.root.destroy())
        self.app.roster = SimpleNamespace(records=records(), assert_unchanged=Mock())
        self.app.owner.set("谭勋策")
        self.app.render()
        self.root.update_idletasks()

    def test_only_two_tabs_topmost_and_no_runtime_created_by_opening(self):
        self.assertEqual([self.app.notebook.tab(t, "text") for t in self.app.notebook.tabs()], ["自动认领", "自动导入"])
        self.assertTrue(self.root.attributes("-topmost"))
        self.assertFalse((self.service.base / "runtime").exists())

    def test_completed_view_is_readonly_and_skipped_retry_explicit(self):
        self.app.scope.set("已完成")
        self.app.render()
        self.assertEqual(self.app.trees["claim"].get_children(), ("claim-done",))
        self.assertEqual(str(self.app.start_button["state"]), "disabled")
        self.app.scope.set("重试跳过项")
        self.app.render()
        self.assertEqual(self.app.trees["claim"].get_children(), ("claim-skip",))
        self.assertEqual(self.app.trees["import"].get_children(), ("zero-skip",))

    def test_busy_locks_controls_and_close_requests_safe_pause(self):
        self.app.set_busy(True)
        self.assertEqual(str(self.app.owner_box["state"]), "disabled")
        self.assertEqual(self.app.notebook.tab(self.app.pages["import"], "state"), "disabled")
        with patch("app.messages.showwarning"):
            self.app.close()
        self.assertTrue(self.app.stop.is_set())
        self.assertFalse(self.app.closed)
        self.app.set_busy(False)

    def test_threaded_dispatch_finishes_on_main_thread_without_second_start(self):
        held = threading.Event()
        def run(*args):
            held.wait(2)
            return TaskResult(self.app.roster, {"claim-pending": "fixture complete"}, "完成 1")
        with patch.object(self.service, "run", side_effect=run) as job:
            self.app.start()
            self.app.start()
            held.set()
            deadline = time.monotonic() + 3
            while self.app.busy and time.monotonic() < deadline:
                self.root.update()
                time.sleep(.01)
        self.assertEqual(job.call_count, 1)
        self.assertFalse(self.app.busy)
        self.assertEqual(self.app.status.get(), "完成 1")

    def test_other_owner_and_invalid_count_never_start(self):
        for owner, count in (("其他人", "5"), ("", "5"), ("谭勋策", "0"), ("谭勋策", "101")):
            self.app.owner.set(owner)
            self.app.limit.set(count)
            with patch("app.messages.showwarning"), patch.object(self.service, "run") as run:
                self.app.start()
            run.assert_not_called()
            self.assertFalse(self.app.busy)

    def test_small_window_keeps_actions_and_selected_detail_visible(self):
        self.root.attributes("-alpha", 0)
        self.root.deiconify()
        self.root.geometry("660x430")
        self.root.update()
        for widget in (self.app.start_button, self.app.pause_button, self.app.details["claim"], self.app.scope_box):
            self.assertTrue(widget.winfo_ismapped())
            bottom = widget.winfo_rooty() - self.root.winfo_rooty() + widget.winfo_height()
            right = widget.winfo_rootx() - self.root.winfo_rootx() + widget.winfo_width()
            self.assertLessEqual(bottom, self.root.winfo_height())
            self.assertLessEqual(right, self.root.winfo_width())
        self.root.withdraw()
