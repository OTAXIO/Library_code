"""Paper routing on synthetic workbooks; no server or browser connection."""
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openpyxl import load_workbook
from core import HEADERS, SafetyStop, file_hash, read_roster
from roster_write import mark_complete, mark_skipped_many
from tests.test_need_import import make_roster
from workflow_service import WorkflowService


class PaperClaimTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.path = self.base / 'list.xlsx'
        make_roster(self.path, [('测试员', n) for n in (1, 1, 0, 3)])
        book = load_workbook(self.path)
        reason_col = list(HEADERS).index('reason') + 2
        book.active.cell(2, reason_col, '作者不一致')
        book.active.cell(3, reason_col, '通讯作者标记不一致')
        book.active.cell(5, list(HEADERS).index('item_ids') + 2, '100,200,300')
        book.save(self.path)
        book.close()
        self.roster = read_roster(self.path)
        self.service = WorkflowService(self.base)
        self.stop = threading.Event()
        self.bridge = Mock(online=True)
        self.bridge.call.return_value = {"page_kind": "paper"}

    def run_paper(self, scope='pending', limit=4, bridge=None):
        chosen = self.service.select(self.roster, 'paper', '测试员', limit, scope)
        return self.service.run(self.roster, chosen, 'paper', scope,
                                self.bridge if bridge is None else bridge, self.stop, Mock(), owner='测试员')

    @staticmethod
    def outcome(roster, completed=(), halted=False, reason='', cancelled=False):
        return SimpleNamespace(roster=roster, completed_ids=completed, synced_ids=(), skipped={},
                               halted=halted, reason=reason, cancelled=cancelled)

    def test_visible_all_matches_scope_owner_and_limit(self):
        self.assertEqual([r.matches for r in self.service.visible(self.roster, 'paper', '测试员', 'pending')], [1, 1, 0, 3])
        self.assertEqual(self.service.select(self.roster, 'paper', '测试员', 2, 'pending'), self.roster.records[:2])
        self.assertEqual(self.service.visible(self.roster, 'patent', '测试员', 'pending'), [])
        with self.assertRaisesRegex(SafetyStop, '当前范围没有可执行的论著认领任务'):
            self.service.select(self.roster, 'paper', '其他人', 1, 'pending')

    def test_four_routes_and_fresh_roster_in_subbatches(self):
        seen = []
        def claim(roster, records, bridge, **kw):
            self.assertEqual([r.sa_id for r in records], ['zero-001'])
            self.assertTrue(roster.records[1].skipped)
            self.assertTrue(roster.records[3].skipped)
            seen.append('claim')
            updated = mark_complete(roster, records[0], note='合成认领').roster
            return self.outcome(updated, ('zero-001',))
        def need(roster, records, bridge, owner, **kw):
            self.assertEqual([r.sa_id for r in records], ['zero-003'])
            self.assertTrue(roster.records[0].done)
            seen.append('need')
            return self.outcome(mark_complete(roster, records[0], note='需导入').roster, ('zero-003',))
        with patch('workflow_service.run_claim_batch', side_effect=claim), patch('need_import.run_need_import_batch', side_effect=need):
            result = self.run_paper()
        self.assertEqual(seen, ['claim', 'need'])
        self.assertEqual(result.roster.records[1].remark, '非认领任务：通讯作者标记不一致')
        self.assertEqual(result.roster.records[3].remark, '匹配到 3 条，未结项')
        self.assertFalse(result.roster.records[3].done)
        self.assertIn('认领 1 · 原已处理 0 · 需导入 1 · 多条匹配跳过 1 · 非认领跳过 1 · 其他跳过 0。', result.summary)
        self.assertEqual(len(result.messages), 4)
        self.bridge.call.assert_called_once_with("status", {"sa_id": "zero-001"}, timeout=90)

    def test_halted_claim_prevents_need_import_and_explains_reason(self):
        def claim(roster, *args, **kw):
            return self.outcome(roster, halted=True, reason='回读未确认')
        with patch('workflow_service.run_claim_batch', side_effect=claim), patch('need_import.run_need_import_batch') as need:
            result = self.run_paper()
        need.assert_not_called()
        self.assertTrue(result.halted)
        self.assertIn('回读未确认', result.summary)
        self.assertIn('后续子批未执行', result.summary)

    def test_stop_during_claim_prevents_next_batch(self):
        def claim(roster, *args, **kw):
            self.stop.set()
            return self.outcome(roster)
        with patch('workflow_service.run_claim_batch', side_effect=claim), patch('need_import.run_need_import_batch') as need:
            result = self.run_paper()
        need.assert_not_called()
        self.assertTrue(result.cancelled)

    def only_local(self):
        book = load_workbook(self.path)
        # Keep b/d only, with a long reason and an existing note.
        book.active.delete_rows(4)
        book.active.delete_rows(2)
        reason_col = list(HEADERS).index('reason') + 2
        book.active.cell(2, reason_col, '长' * 50)
        book.active['A3'] = '原备注'
        book.save(self.path)
        book.close()
        self.roster = read_roster(self.path)

    def test_local_only_runs_offline_and_retry_does_not_rewrite(self):
        self.only_local()
        offline = Mock(online=False)
        with patch('workflow_service.run_claim_batch') as claim, patch('need_import.run_need_import_batch') as need:
            result = self.run_paper(bridge=offline)
            claim.assert_not_called()
            need.assert_not_called()
        offline.call.assert_not_called()
        self.assertEqual(result.roster.records[0].remark, '非认领任务：' + '长' * 40)
        self.assertEqual(result.roster.records[1].remark, '原备注')
        self.assertTrue(all(r.skipped and not r.done for r in result.roster.records))
        self.roster = result.roster
        before = file_hash(self.path)
        with patch('workflow_service.mark_skipped_many') as write:
            result = self.run_paper(scope='skipped', bridge=offline)
        write.assert_not_called()
        self.assertEqual(file_hash(self.path), before)
        self.assertIn('保持不动', result.summary)

    def test_preexisting_stop_writes_nothing(self):
        self.only_local()
        before = file_hash(self.path)
        self.stop.set()
        result = self.run_paper(bridge=Mock(online=False))
        self.assertTrue(result.cancelled)
        self.assertEqual(result.messages, {})
        self.assertEqual(file_hash(self.path), before)

    def test_retry_passes_retry_flag_to_both_batches(self):
        self.roster = mark_skipped_many(self.roster, self.roster.records,
                                       reasons={r.sa_id: '合成跳过' for r in self.roster.records}).roster
        with patch('workflow_service.run_claim_batch', side_effect=lambda roster, *a, **k: self.outcome(roster)) as claim, \
                patch('need_import.run_need_import_batch', side_effect=lambda roster, *a, **k: self.outcome(roster)) as need:
            self.run_paper(scope='skipped')
        self.assertTrue(claim.call_args.kwargs['retry_skipped'])
        self.assertTrue(need.call_args.kwargs['retry_skipped'])

    def test_unsafe_queue_and_stale_roster_block_before_writes(self):
        for chosen, owner in ((self.roster.records[::-1], '测试员'), (self.roster.records, '其他人')):
            with patch('workflow_service.mark_skipped_many') as write, self.assertRaises(SafetyStop):
                self.service.run(self.roster, chosen, 'paper', 'pending', self.bridge, self.stop, Mock(), owner=owner)
            write.assert_not_called()
        self.path.write_bytes(b'changed')
        with patch('workflow_service.mark_skipped_many') as write, self.assertRaises(SafetyStop):
            self.run_paper()
        write.assert_not_called()


    def test_wrong_or_missing_page_kind_stops_after_status(self):
        for status in ({'page_kind':'patent'}, {}):
            self.bridge.call.reset_mock(); self.bridge.call.return_value=status
            with self.assertRaisesRegex(SafetyStop,'绑定页类型不符'):
                self.run_paper()
            self.bridge.call.assert_called_once_with('status', {'sa_id':'zero-001'}, timeout=90)
