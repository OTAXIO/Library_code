"""Cross-review tab, with no business-write controls."""
import json
import threading
import tkinter as tk
from tkinter import filedialog, ttk

from core import SafetyStop
from cross_review import CrossReview, review_owners
from notices import messages


class CrossReviewPanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=6)
        self.app = app
        self.owner = tk.StringVar(value="")
        self.note = tk.StringVar()
        self.review = None
        controls = ttk.Frame(self)
        controls.pack(fill="x")
        ttk.Label(controls, text="复核对象").pack(side="left")
        self.owners = ttk.Combobox(controls, textvariable=self.owner, state="readonly", width=13)
        self.owners.pack(side="left", padx=6)
        self.owners.bind("<<ComboboxSelected>>", lambda _: self.load())
        self.export_button = ttk.Button(controls, text="导出问题", command=self.export)
        self.export_button.pack(side="right")
        self.tree = ttk.Treeview(self, columns=("sid", "title", "status"), show="headings", height=5,
                                 selectmode="browse")
        for key, title, width in (("sid", "SA ID", 190), ("title", "题名", 320), ("status", "复核状态", 90)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, stretch=key == "title")
        self.tree.pack(fill="both", expand=True, pady=4)
        self.tree.bind("<<TreeviewSelect>>", lambda _: self.show())
        self.details = tk.Text(self, height=6, wrap="word", state="disabled")
        self.details.pack(fill="x")
        footer = ttk.Frame(self)
        footer.pack(fill="x", pady=(6, 0))
        self.note_entry = ttk.Entry(footer, textvariable=self.note)
        self.note_entry.pack(side="left", fill="x", expand=True)
        self.confirm_button = ttk.Button(footer, text="确认问题备注", command=self.confirm)
        self.confirm_button.pack(side="right", padx=(6, 0))

    def controls(self):
        names = review_owners(self.app.roster)
        self.owners.configure(values=names, state="disabled" if self.app.busy else "readonly")
        if self.owner.get() not in names:
            self.owner.set("")
            self.review = None
        idle = not self.app.busy and self.review is not None
        self.export_button.configure(state="normal" if idle else "disabled")
        self.confirm_button.configure(state="normal" if idle else "disabled")
        self.note_entry.configure(state="normal" if idle else "disabled")

    def load(self):
        try:
            self.review = CrossReview(self.app.service.base, self.owner.get()) if self.owner.get() else None
        except (SafetyStop, OSError, ValueError) as exc:
            self.review = None
            messages.showwarning("无法加载复核进度", str(exc), parent=self)
        self.render()
        self.app.configure_actions()

    def render(self):
        self.controls()
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        if self.review:
            for sid, entry in self.review.data["rows"].items():
                self.tree.insert("", "end", iid=sid, values=(sid, entry.get("title", ""), entry["status"]))
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected[0])
        self.show()

    def show(self):
        selected = self.tree.selection()
        entry = self.review.data["rows"].get(selected[0], {}) if self.review and selected else {}
        self.note.set(entry.get("confirmed", "；".join(entry.get("issues", []))))
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", json.dumps(entry, ensure_ascii=False, indent=2) if entry else
                            "选择处理人后读取网页已处理队列。确认的问题备注可导出。")
        self.details.configure(state="disabled")

    def start(self):
        if (self.app.busy or not self.review or self.owner.get() != self.review.owner
                or self.owner.get() not in review_owners(self.app.roster)):
            return
        bridge = self.app.bridge
        if not bridge or not bridge.online:
            messages.showwarning("尚未开始", "请连接 SA 比对页。", parent=self)
            return
        review = self.review
        self.app.stop.clear()
        self.app.set_busy(True)
        def work():
            outcome = "本轮交叉核验结束。"
            try:
                review.run(bridge, self.app.stop, lambda value: self.app.events.put(("progress", value)))
                if self.app.stop.is_set():
                    outcome = "复核已暂停，已完成的进度已保存。"
            except Exception as exc:
                from run_diagnostics import safe_text
                outcome = "复核暂停：" + safe_text(exc, (getattr(bridge, "token", ""),))
            self.app.events.put(("cross_result", outcome))
        threading.Thread(target=work, daemon=True).start()

    def confirm(self):
        selected = self.tree.selection()
        if self.app.busy or not self.review or not selected:
            return
        try:
            self.review.confirm(selected[0], self.note.get())
            self.render()
        except (SafetyStop, OSError) as exc:
            messages.showwarning("未保存", str(exc), parent=self)

    def export(self):
        if self.app.busy or not self.review:
            return
        path = filedialog.asksaveasfilename(parent=self, defaultextension=".xlsx",
                                           filetypes=[("Excel 工作簿", "*.xlsx")])
        if path:
            try:
                count = self.review.export(path)
                self.app.status.set(f"已导出 {count} 条确认问题。")
            except (SafetyStop, OSError) as exc:
                messages.showwarning("未导出", str(exc), parent=self)
