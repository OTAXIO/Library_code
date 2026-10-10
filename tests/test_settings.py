import json
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import App
from core import Journal, SafetyStop
from operation_log import OperationLog
from settings_panel import DEFAULTS, read_preferences


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.root = tk.Tk()
        self.root.withdraw()
        self.client = Mock()
        self.client.key_store.configured.return_value = False
        with patch('settings_panel.read_preferences', return_value=dict(DEFAULTS)):
            self.app = App(self.root, Journal(self.folder / 'journal.db'), auto_load=False,
                           model_client=self.client, operation_log=OperationLog(self.folder / 'log.txt'))
        self.panel = self.app.settings_panel
        self.panel.path = self.folder / 'preferences.json'

    def tearDown(self):
        self.app.set_busy(False)
        self.root.update_idletasks()
        self.app.close()

    def test_configuration_entry_routes_to_last_tab_without_reading_secret(self):
        self.app.model_panel.configure_key()
        self.assertEqual(self.app.tabs.select(), str(self.app.settings_page))
        self.assertEqual(self.app.tabs.tabs()[-1], str(self.app.settings_page))
        self.client.key_store.load.assert_not_called()
        self.client.models.assert_not_called()

    def test_key_saved_only_to_existing_secure_store_then_cleared(self):
        self.panel.key.set('synthetic-test-only')
        self.panel.save_key()
        self.client.key_store.save.assert_called_once_with('synthetic-test-only')
        self.assertEqual(self.panel.key.get(), '')
        self.assertFalse(self.panel.path.exists())

    def test_models_only_preferences_and_invalid_file(self):
        self.panel.models['review'].set('qwen')
        self.panel.save_models()
        self.assertEqual(read_preferences(self.panel.path)['review'], 'qwen')
        self.assertEqual(self.app.model_panel.model.get(), 'qwen')
        self.assertEqual(set(json.loads(self.panel.path.read_text())), set(DEFAULTS))
        self.panel.path.write_text('{"api_key":"not-accepted"}')
        with self.assertRaises(SafetyStop):
            read_preferences(self.panel.path)

    def test_busy_blocks_configuration_and_connection(self):
        self.app.set_busy(True)
        self.panel.key.set('synthetic-test-only')
        self.panel.save_key()
        self.panel.save_models()
        self.panel.test_connection()
        self.panel.check_browser()
        self.client.key_store.save.assert_not_called()
        self.client.models.assert_not_called()
        self.assertFalse(self.panel.path.exists())

    def test_browser_check_is_read_only_uses_explicit_profile_and_does_not_save(self):
        self.panel.browser_mode.set('浏览器技能（无需配对）')
        self.panel.browser_instance.set('synthetic-profile')
        self.panel.browser_path=self.folder/'runtime/wos_browser.json'
        def sync(job,ready,*args,**kwargs):
            ready(job())
        with patch.object(self.app,'run',side_effect=sync), patch('wos_browser.BrowserSkillWOS') as factory:
            client=factory.return_value
            client.check_connection.return_value={'browser_skill_version':'0.3.2'}
            self.panel.check_browser()
            self.assertEqual(factory.call_args.args[1],'synthetic-profile')
            client.check_connection.assert_called_once()
            client.call.assert_not_called()
            client._start.assert_not_called()
        self.assertIn('尚未检索或下载',self.panel.status.get())
        self.assertFalse(self.panel.browser_path.exists())
        self.client.key_store.load.assert_not_called()

    def test_original_plugin_connection_check_does_not_call_browser_skill(self):
        self.panel.browser_mode.set('原插件配对')
        with patch('wos_browser.BrowserSkillWOS') as factory, patch.object(self.app,'run') as run:
            self.panel.check_browser()
            factory.assert_not_called()
            run.assert_not_called()
        self.assertIn('原插件',self.panel.status.get())

    def test_empty_owner_message_is_generic(self):
        panel = self.app.automation_panel
        with self.assertRaisesRegex(SafetyStop, '^请选择负责人。$'):
            panel.guard()
        with patch('automation_panel.messagebox.showwarning') as notice:
            panel.claim_batch()
            self.assertEqual(notice.call_args.args[1], '请选择负责人。')
            panel.pilot()
            self.assertEqual(notice.call_args.args[1], '请选择负责人。')
        with patch('app.messagebox.showwarning') as notice:
            self.app.confirm_claim_done()
            self.assertEqual(notice.call_args.args[1], '请选择负责人。')
        with patch('wos_import_panel.messagebox.showwarning') as notice:
            self.app.wos_import_panel.preview()
            self.assertEqual(notice.call_args.args[1], '请选择负责人。')
            self.app.wos_import_panel.start()
            self.assertEqual(notice.call_args.args[1], '请选择负责人。')
