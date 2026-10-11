"""Disposable fake Windows APIs only; no user clipboard reads or writes."""
import unittest
from unittest.mock import Mock, patch

from pairing_ui import copy_pairing_code


class PairingCopyTests(unittest.TestCase):
    def setUp(self):
        self.window = Mock(winfo_id=Mock(return_value=123))
        self.user = Mock()
        self.user.OpenClipboard.return_value = 1
        self.user.EmptyClipboard.return_value = 1
        self.user.SetClipboardData.return_value = 456
        self.kernel = Mock()
        self.kernel.GlobalAlloc.return_value = 456
        self.kernel.GlobalLock.return_value = 789

    def invoke(self):
        with patch("pairing_ui.sys.platform", "win32"), patch("ctypes.WinDLL", create=True,
                side_effect=[self.user, self.kernel]), patch("ctypes.memmove") as memory, \
                patch("ctypes.get_last_error", create=True, return_value=5), \
                patch("ctypes.WinError", create=True, side_effect=lambda _: OSError("synthetic clipboard failure")):
            copy_pairing_code(self.window, "synthetic-local-code")
        return memory

    def test_immediate_unicode_transfer_not_delayed_tk_clipboard(self):
        memory = self.invoke()
        data = "synthetic-local-code\0".encode("utf-16-le")
        memory.assert_called_once_with(789, data, len(data))
        self.user.OpenClipboard.assert_called_once_with(123)
        self.user.SetClipboardData.assert_called_once_with(13, 456)
        self.user.CloseClipboard.assert_called_once()
        self.kernel.GlobalFree.assert_not_called()
        self.window.clipboard_clear.assert_not_called()
        self.window.clipboard_append.assert_not_called()

    def test_busy_clipboard_does_not_empty_and_releases_own_buffer(self):
        self.user.OpenClipboard.return_value = 0
        with self.assertRaises(OSError):
            self.invoke()
        self.user.EmptyClipboard.assert_not_called()
        self.user.CloseClipboard.assert_not_called()
        self.kernel.GlobalFree.assert_called_once_with(456)

    def test_transfer_failure_never_reports_success_or_leaks_buffer(self):
        self.user.SetClipboardData.return_value = 0
        with self.assertRaises(OSError):
            self.invoke()
        self.user.CloseClipboard.assert_called_once()
        self.kernel.GlobalFree.assert_called_once_with(456)

    def test_non_windows_tk_copy_and_invalid_values(self):
        with patch("pairing_ui.sys.platform", "linux"):
            copy_pairing_code(self.window, "synthetic-local-code")
        self.window.clipboard_append.assert_called_once_with("synthetic-local-code")
        for value in (None, "", 123, "x" * 201):
            with self.assertRaises(ValueError):
                copy_pairing_code(self.window, value)
