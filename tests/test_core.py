import json
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from openpyxl import Workbook
from bridge import Bridge
from core import HEADERS, QUERY_HEADER, Journal, SafetyStop, guide, latest_roster, read_roster


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def roster(self, changes=None, duplicate=False):
        # Synthetic records only: no real names, IDs or source workbooks in Git.
        values = dict(owner="测试员", sa_id="demo-001", title="Synthetic paper", doi="", wos="",
                      staff_id="00001", matches=1, item_ids="1234567890123456789", mark="待处理", reason="作者不一致", query="1")
        values.update(changes or {})
        book = Workbook()
        book.active.append(list(HEADERS.values()) + [QUERY_HEADER])
        row = [values[k] for k in HEADERS] + [values["query"]]
        book.active.append(row)
        if duplicate:
            book.active.append(row)
        path = self.root / "数据比对结果-test.xlsx"
        book.save(path)
        return path

    def test_read_preserves_long_id_and_zeros(self):
        r = read_roster(self.roster()).records[0]
        self.assertEqual(r.item_ids, "1234567890123456789")
        self.assertEqual(r.staff_id, "00001")

    def test_duplicate_id_stops(self):
        with self.assertRaises(SafetyStop):
            read_roster(self.roster(duplicate=True))

    def test_numeric_id_stops(self):
        with self.assertRaises(SafetyStop):
            read_roster(self.roster({"item_ids": 1234567890123456789}))

    def test_formula_stops(self):
        with self.assertRaises(SafetyStop):
            read_roster(self.roster({"title": "=1+1"}))

    def test_mismatched_count_stops(self):
        with self.assertRaises(SafetyStop):
            read_roster(self.roster({"matches": 2}))

    def test_unknown_query_is_manual(self):
        self.assertIn("未知情况", guide(read_roster(self.roster({"query": "3"})).records[0]))

    def test_modified_roster_stops(self):
        roster = read_roster(self.roster())
        self.roster({"title": "Changed"})
        with self.assertRaises(SafetyStop):
            roster.assert_unchanged()

    def test_latest_ignores_lockfile(self):
        path = self.roster()
        (self.root / "~$数据比对结果-test.xlsx").touch()
        self.assertEqual(latest_roster(self.root), path)

    def test_checkpoint_survives_restart(self):
        record = read_roster(self.roster()).records[0]
        path = self.root / "journal.db"
        j = Journal(path)
        j.save(record, "写入结果待核验", "synthetic")
        j.close()
        j = Journal(path)
        self.assertEqual(j.state(record), "写入结果待核验")
        j.close()


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.bridge = Bridge(0)

    def tearDown(self):
        self.bridge.close()

    def post(self, route, data, token=None):
        request = urllib.request.Request(f"http://127.0.0.1:{self.bridge.port}{route}",
            json.dumps(data).encode(), headers={"Authorization": "Bearer " + (token or self.bridge.token)})
        with urllib.request.urlopen(request, timeout=3) as response:
            return json.load(response)

    def test_auth_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/poll", {"client": "1"}, "wrong")
        self.assertEqual(ctx.exception.code, 401)

    def test_second_tab_rejected(self):
        self.post("/poll", {"client": "1"})
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self.post("/poll", {"client": "2"})
        self.assertEqual(ctx.exception.code, 409)

    def test_delivered_once(self):
        self.post("/poll", {"client": "1"})
        responses = []
        worker = threading.Thread(target=lambda: responses.append(self.bridge.call("search", {"sa_id": "demo"}, 3)))
        worker.start()
        deadline = time.monotonic() + 2
        command = None
        while not command and time.monotonic() < deadline:
            command = self.post("/poll", {"client": "1"})["command"]
        self.assertIsNotNone(command)
        self.assertIsNone(self.post("/poll", {"client": "1"})["command"])
        self.post("/result", {"id": command["id"], "client": "1", "result": {"ok": True, "data": {"verified": True}}})
        worker.join(4)
        self.assertEqual(responses, [{"verified": True}])

    def test_offline_stops(self):
        with self.assertRaises(SafetyStop):
            self.bridge.call("search", {})


if __name__ == "__main__":
    unittest.main()
