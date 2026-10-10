import tempfile
import unittest
from pathlib import Path

from wos_reports import save_download_report


class DownloadReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_all_failures_are_retained_not_just_dialog_preview(self):
        result = {'total': 100, 'attempted': 74, 'remaining': 26, 'disconnected': True,
                  'stopped': True, 'failed': {f'test-{i}': {'row': i + 2, 'per_record': i % 2 == 0,
                  'error': f'synthetic failure {i}'} for i in range(74)}}
        report = save_download_report(result, '谭勋策', 'skipped', self.root)
        text = report.read_text(encoding='utf-8')
        self.assertEqual(text.count('名单 ID：'), 74)
        self.assertIn('synthetic failure 73', text)
        self.assertIn('尚未执行：26', text)
        self.assertIn('是否识别为 2', text)
        self.assertIn('通信不可继续：是', text)

    def test_minimal_result_and_multiple_reports_do_not_overwrite(self):
        first = save_download_report({'exported': []}, 'Test', 'pending', self.root / 'nested')
        second = save_download_report({}, 'Test', 'pending', self.root / 'nested')
        self.assertNotEqual(first, second)
        self.assertTrue(first.is_absolute())
        self.assertIn('论文总数：0', first.read_text(encoding='utf-8'))

    def test_paths_and_all_successful_and_unconfirmed_items_are_saved(self):
        result = {'extension_version':'0.3.27',
                  'exported': [{'row': 2, 'sa_id': 'one', 'title': '论文一', 'file': 'D:/test/one.txt'}],
                  'unconfirmed': [{'row': 3, 'sa_id': 'two', 'title': '论文二', 'archive': 'D:/test/hash.txt'}]}
        report = save_download_report(result, 'Test', 'pending', self.root)
        text = report.read_text(encoding='utf-8')
        self.assertIn('D:/test/one.txt', text)
        self.assertIn('D:/test/hash.txt', text)
        self.assertIn('论文总数：2', text)
        self.assertIn('原插件 0.3.27', text)

    def test_trial_and_browser_halt_explain_no_intake_or_bulk_red_marking(self):
        result={'transport':'browser-skill','browser_skill_version':'0.3.2',
                'page_blocked':True,'halt_reason':'WOS 页面未就绪','attempted':1,'remaining':9}
        report=save_download_report(result,'Test','trial',self.root)
        text=report.read_text(encoding='utf-8')
        self.assertIn('Browser Skill 0.3.2',text)
        self.assertIn('纯试下载',text)
        self.assertIn('未执行的后续论文保持原样',text)
        self.assertNotIn('确认上传入库',text)

    def test_unknown_objects_secrets_and_markdown_are_not_exposed(self):
        result = {'api_key': 'never-serialize-this', '_source_update': object(),
                  'failed': {'id': {'error': '```\nBearer syntheticsecretvalue\nsk-synthetic123456789', 'row': 2}}}
        report = save_download_report(result, 'Test', 'pending', self.root)
        text = report.read_text(encoding='utf-8')
        for secret in ('never-serialize-this', 'syntheticsecretvalue', 'sk-synthetic123456789'):
            self.assertNotIn(secret, text)
        self.assertIn('[已隐藏凭据]', text)
        self.assertEqual(text.count('```') % 2, 0)


if __name__ == '__main__':
    unittest.main()
