"""Quiet, non-modal warnings; never changes Windows volume or sound settings."""
import tkinter as tk
from tkinter import ttk, messagebox as native_messages


class QuietMessages:
    # Consequential confirmations retain explicit yes/no dialogs.
    askyesno = staticmethod(native_messages.askyesno)
    showerror = staticmethod(native_messages.showerror)
    showinfo = staticmethod(native_messages.showinfo)

    @staticmethod
    def showwarning(title, message, *, parent, **_options):
        root = parent.winfo_toplevel()
        window = getattr(root, "_sa_notice", None)
        if window is None or not window.winfo_exists():
            window = tk.Toplevel(root)
            root._sa_notice = window
            window.transient(root)
            window.attributes("-topmost", True)
            window.geometry("490x270")
            window.minsize(390, 230)
            frame = ttk.Frame(window, padding=14)
            frame.pack(fill="both", expand=True)
            ttk.Label(frame, text="已暂停 · 请核对提示", font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w", pady=(0, 8))
            window.body = tk.Text(frame, height=5, width=30, wrap="word", relief="flat", bg="#fff7df",
                                  font=("Microsoft YaHei UI", 10), padx=8, pady=8)
            window.body.pack(fill="both", expand=True)
            tools = ttk.Frame(frame)
            tools.pack(fill="x", pady=(10, 0))
            def copy():
                root.clipboard_clear()
                root.clipboard_append(window.body.get("1.0", "end-1c"))
            ttk.Button(tools, text="复制提示", command=copy).pack(side="left")
            ttk.Button(tools, text="关闭提示", command=window.destroy).pack(side="right")
            window.bind("<Escape>", lambda _e: window.destroy())
            # No grab_set / wait_window / bell / MessageBox warning icon. Users
            # can operate the browser and assistant while this notice is open.
        window.title(title)
        window.body.configure(state="normal")
        window.body.delete("1.0", "end")
        window.body.insert("1.0", str(message))
        window.body.configure(state="disabled")
        return "ok"


messages = QuietMessages()
