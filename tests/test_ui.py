"""Native widget/queue smoke test; no browser and no live mutations."""
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch
from pathlib import Path

from app import App
from bridge import Bridge
from core import Journal, Record, Roster
from remarks import CLAIMED


class UITests(unittest.TestCase):
    def test_layout_and_owner_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = tk.Tk()
            root.withdraw()
            app = App(root, Bridge(0), Journal(Path(tmp) / "local.db"), auto_load=False)
            try:
                record = Record(2, "测试员", "demo-001", "Synthetic", "", "", "00001", 0, "", "待处理", "", "3")
                other = Record(3, "另一位", "demo-002", "Synthetic", "", "", "00002", 0, "", "待处理", "", "1")
                app.roster = Roster(Path(tmp), "synthetic", [record, other], 0)
                app.owner.set("测试员")
                app.select_owner()
                root.update_idletasks()
                self.assertEqual(len(app.records), 1)
                self.assertEqual(app.tree.get_children(), ("demo-001",))
                app.tree.selection_set("demo-001")
                app.select_record()
                self.assertIn("未知情况", app.details.get("1.0", "end"))
                self.assertFalse(app.reviewed.get())
                self.assertIsNone(app.snapshot)
                app.set_busy(True)
                self.assertEqual(str(app.owner_box['state']), "disabled")
                app.set_busy(False)
                self.assertEqual(str(app.owner_box['state']), "readonly")
                self.assertLessEqual(root.winfo_reqheight(), 900)
                claimed_record = Record(2, "测试员", "demo-001", "Synthetic", "", "", "00001", 1, "123", "待处理", "作者不一致", "1")
                app.current = claimed_record
                app.snapshot = {"matchCount": 1, "reason": "作者不一致"}
                app.comparison = [{"label": "认领状态", "sa": "测试员", "library": "已认领"}]
                with patch.object(app, "require_record", return_value=claimed_record), patch("app.messagebox.askyesno", return_value=True):
                    app.use_remark(CLAIMED)
                    app.use_remark(CLAIMED)
                    self.assertEqual(app.note.get("1.0", "end").strip(), "已认领")
                    self.assertFalse(app.reviewed.get())
                    app.reviewed.set(True)
                    self.assertEqual(app.reviewed_note(), "已认领")
            finally:
                app.bridge.close()
                app.journal.close()
                root.destroy()


if __name__ == '__main__':
    unittest.main()
