"""Acceptance runner safety, without a UI, bridge or real workbook."""
from pathlib import Path
import tempfile
import unittest

from maintenance.metadata_acceptance import code_fingerprint, control_stamp, claim_outcomes, verify_completed, select_ids, audit_completed_imports
from core import SafetyStop
from types import SimpleNamespace
from unittest.mock import Mock


class AcceptanceSourceTests(unittest.TestCase):
    def test_retarget_is_bounded_and_explicit(self):
        self.assertEqual(select_ids('SELECT one two'),['one','two'])
        for command in ('SELECT','SELECT one one','SELECT a b c d e f','SELECT ../../list.xlsx'):
            with self.assertRaises(SafetyStop):select_ids(command)

    def test_completed_audit_refuses_pending_and_never_sends_write_commands(self):
        from core import read_roster,file_hash
        from metadata_sources import MetadataClient,merge_metadata
        from metadata_pipeline import run_metadata_batch
        from tests.test_batch_recovery import RecoveryTests
        from tests.test_metadata_sources import example
        from tests.test_metadata_pipeline import Backend
        import threading
        case=RecoveryTests();base,roster=case.roster(1)
        try:
            record=roster.records[0];_,cr,oa=example()
            candidate=merge_metadata(record,cr,oa);client=MetadataClient(base/'runtime/metadata-imports/papers')
            client.get=Mock(side_effect=[{'message':cr},oa]);backend=Backend(record,candidate)
            result=run_metadata_batch(base,roster,[record],backend,threading.Event(),retry_skipped=True,
                client=client,excel_builder=case.builder)
            self.assertFalse(result.halted,result.reason)
            before_calls=len(backend.calls)
            verified,errors=audit_completed_imports(base,result.roster,result.roster.records,backend)
            self.assertFalse(errors,errors);self.assertEqual(len(verified),1)
            self.assertEqual(backend.calls[before_calls:],['import_excel_check','import_excel_resolve','search'])
            self.assertEqual(file_hash(result.roster.path),result.roster.sha256)
        finally:case.doCleanups()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        (self.base/'extension').mkdir()
        (self.base/'maintenance').mkdir()
        (self.base/'extension/manifest.json').write_text('{"version":"test"}')
        (self.base/'extension/library-adapter.js').write_text('synthetic adapter')
        (self.base/'maintenance/metadata_acceptance.py').write_text('synthetic runner')
        (self.base/'zero_match.py').write_text('synthetic dependency')
        (self.base/'metadata_excel.mjs').write_text('synthetic workbook serializer')

    def test_dependency_extension_and_runner_changes_require_restart(self):
        for name in ('zero_match.py', 'extension/library-adapter.js',
                     'extension/manifest.json', 'maintenance/metadata_acceptance.py', 'metadata_excel.mjs'):
            with self.subTest(name=name):
                before = code_fingerprint(self.base)
                path = self.base/name
                path.write_bytes(path.read_bytes()+b' changed')
                self.assertNotEqual(before, code_fingerprint(self.base))

    def test_runtime_records_do_not_change_code_identity(self):
        before = code_fingerprint(self.base)
        (self.base/'runtime').mkdir()
        (self.base/'runtime/control.txt').write_text('PAUSE')
        self.assertEqual(before, code_fingerprint(self.base))

    def test_existing_command_has_initial_stamp_not_a_new_run(self):
        path = self.base/'control.txt'
        self.assertIsNone(control_stamp(None))
        self.assertIsNone(control_stamp(path))
        path.write_text('RUN')
        initial = control_stamp(path)
        self.assertEqual(initial, control_stamp(path))

    def test_unexecuted_tail_does_not_inherit_the_preceding_failure(self):
        rows=[SimpleNamespace(sa_id=str(i),done=False) for i in range(5)]
        claim=SimpleNamespace(completed_ids=(),synced_ids=(),checked=2,
            skipped={'0':'后台拒绝认领'},reason='前条窗口待核')
        result=claim_outcomes(rows,claim)
        self.assertEqual([x['status'] for x in result],['review','review','unexecuted','unexecuted','unexecuted'])
        self.assertTrue(all(x['message']=='尚未执行' for x in result[2:]))

    def test_final_readback_can_release_previous_owned_detail_and_retains_partial_results(self):
        records=[SimpleNamespace(sa_id='one',staff_id='001',done=True,remark='非交大作者'),
                 SimpleNamespace(sa_id='two',staff_id='002',done=True,remark='已认领')]
        bridge=Mock();bridge.call.side_effect=[{'row':{'saLzkId':'one','gh':'001',
            'markStatus':'已处理','remark':'非交大作者'}},RuntimeError('read failure')]
        verified,errors=verify_completed(bridge,records)
        self.assertEqual(set(verified),{'one'});self.assertEqual(set(errors),{'two'})
        self.assertTrue(all(c.args[0]=='search' and c.args[1]['close_detail'] is True
            for c in bridge.call.call_args_list))


if __name__ == '__main__':
    unittest.main()
