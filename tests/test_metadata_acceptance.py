"""Acceptance runner safety, without a UI, bridge or real workbook."""
from pathlib import Path
import tempfile
import unittest

from maintenance.metadata_acceptance import code_fingerprint, control_stamp


class AcceptanceSourceTests(unittest.TestCase):
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

    def test_dependency_extension_and_runner_changes_require_restart(self):
        for name in ('zero_match.py', 'extension/library-adapter.js',
                     'extension/manifest.json', 'maintenance/metadata_acceptance.py'):
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


if __name__ == '__main__':
    unittest.main()
