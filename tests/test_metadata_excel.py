"""Observe generator failures without running Node or touching real archives."""
import subprocess
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from core import SafetyStop
from metadata_excel import build_excel


class ExcelBuildDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.modules = self.folder / 'modules'
        (self.modules/'@oai/artifact-tool').mkdir(parents=True)
        self.env = patch.dict('os.environ', {'SA_NODE_EXECUTABLE': 'fixture-node',
            'SA_ARTIFACT_NODE_MODULES': str(self.modules)})
        self.env.start(); self.addCleanup(self.env.stop)

    def test_failed_generator_persists_readable_error_not_secret_or_payload(self):
        response = SimpleNamespace(returncode=1, stdout='', stderr='Module missing token=fixture-secret')
        with patch('metadata_excel.subprocess.run', return_value=response) as run:
            with self.assertRaisesRegex(SafetyStop, 'excel-build.log'): build_excel(self.folder, {})
        run.assert_called_once()
        diagnostic = (self.folder/'excel-build.log').read_text(encoding='utf-8')
        self.assertIn('Module missing', diagnostic)
        self.assertNotIn('fixture-secret', diagnostic)
        self.assertFalse((self.folder/'paper.xlsx').exists())

    def test_timeout_is_not_retried_and_preserves_diagnostic(self):
        timeout = subprocess.TimeoutExpired('fixture-node', 90, stderr='fixture timeout')
        with patch('metadata_excel.subprocess.run', side_effect=timeout) as run:
            with self.assertRaisesRegex(SafetyStop, '90 秒'): build_excel(self.folder, {})
        run.assert_called_once()
        self.assertIn('fixture timeout', (self.folder/'excel-build.log').read_text(encoding='utf-8'))

    def test_launch_error_is_not_retried(self):
        with patch('metadata_excel.subprocess.run', side_effect=FileNotFoundError('fixture missing')) as run:
            with self.assertRaisesRegex(SafetyStop, '无法启动'): build_excel(self.folder, {})
        run.assert_called_once()
        self.assertIn('FileNotFoundError', (self.folder/'excel-build.log').read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
