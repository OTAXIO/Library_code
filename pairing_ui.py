"""Copy the local pairing code; never log or persist authentication secrets."""
import sys


def copy_pairing_code(window, value):
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ValueError("配对码无效")
    window.update_idletasks()
    if sys.platform != "win32":
        window.clipboard_clear()
        window.clipboard_append(value)
        return
    # Tk's delayed clipboard rendering is unreliable in some Windows hosts.
    # Transfer an owned Unicode buffer immediately. This is the app's Copy
    # button implementation, not screen control or reading another clipboard.
    import ctypes
    from ctypes import wintypes
    user = ctypes.WinDLL("user32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    user.OpenClipboard.argtypes = [wintypes.HWND]
    user.OpenClipboard.restype = wintypes.BOOL
    user.EmptyClipboard.argtypes = []
    user.EmptyClipboard.restype = wintypes.BOOL
    user.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user.SetClipboardData.restype = wintypes.HANDLE
    user.CloseClipboard.argtypes = []
    user.CloseClipboard.restype = wintypes.BOOL
    kernel.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel.GlobalAlloc.restype = wintypes.HANDLE
    kernel.GlobalLock.argtypes = [wintypes.HANDLE]
    kernel.GlobalLock.restype = ctypes.c_void_p
    kernel.GlobalUnlock.argtypes = [wintypes.HANDLE]
    kernel.GlobalUnlock.restype = wintypes.BOOL
    kernel.GlobalFree.argtypes = [wintypes.HANDLE]
    kernel.GlobalFree.restype = wintypes.HANDLE
    content = (value + "\0").encode("utf-16-le")
    handle = kernel.GlobalAlloc(0x0002, len(content))
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    transferred = False
    opened = False
    try:
        pointer = kernel.GlobalLock(handle)
        if not pointer:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            ctypes.memmove(pointer, content, len(content))
        finally:
            kernel.GlobalUnlock(handle)
        if not user.OpenClipboard(window.winfo_id()):
            raise ctypes.WinError(ctypes.get_last_error())
        opened = True
        if not user.EmptyClipboard() or not user.SetClipboardData(13, handle):
            raise ctypes.WinError(ctypes.get_last_error())
        transferred = True  # Windows owns this buffer after SetClipboardData.
    finally:
        if opened:
            user.CloseClipboard()
        if not transferred:
            kernel.GlobalFree(handle)
