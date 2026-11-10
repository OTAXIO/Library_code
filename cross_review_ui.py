"""Cross-review tab, with no business-write controls."""
import json
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk

import core
from core import SafetyStop
from cross_review import CrossReview
from notices import messages


class CrossReviewPanel(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent, padding=6)
        self.app = app
        self.owner = tk.StringVar(value="")
        self.reviewer_path = Path(app.service.base) / "runtime/cross-review/reviewer.json"
        self.reviewer = tk.StringVar(value=self.saved_reviewer())
        self.note = tk.StringVar()
        self.review = None
        controls = ttk.Frame(self)
        controls.pack(fill="x")
        ttk.Label(controls, text="复核对象").pack(side="left")
        self.owner_entry = ttk.Entry(controls, textvariable=self.owner, width=13)
        self.owner_entry.pack(side="left", padx=6)
        self.owner_entry.bind("<Return>", lambda _: self.load())
        self.load_button = ttk.Button(controls, text="加载", command=self.load)
        self.load_button.pack(side="left")
        ttk.Label(controls, text="校对人").pack(side="left", padx=(12, 0))
        self.reviewer_entry = ttk.Entry(controls, textvariable=self.reviewer, width=13)
        self.reviewer_entry.pack(side="left", padx=6)
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

    @staticmethod
    def validated_name(value, label):
        name = value.strip()
        if not core.is_assigned_owner(name) or len(name) > 40:
            raise SafetyStop(f"请输入 1–40 字的{label}，不能使用未分配占位名。")
        return name

    def saved_reviewer(self):
        try:
            data = json.loads(self.reviewer_path.read_text(encoding="utf-8"))
            return self.validated_name(data["reviewer"], "校对人")
        except (OSError, ValueError, KeyError, TypeError, AttributeError, SafetyStop):
            return ""

    def controls(self):
        for widget in (self.owner_entry, self.load_button, self.reviewer_entry):
            widget.configure(state="disabled" if self.app.busy else "normal")
        idle = not self.app.busy and self.review is not None
        self.confirm_button.configure(state="normal" if idle else "disabled")
        self.note_entry.configure(state="normal" if idle else "disabled")

    def load(self):
        if self.app.busy:
            return
        try:
            owner = self.validated_name(self.owner.get(), "复核对象")
            self.owner.set(owner)
            try:
                directory = core.fixed_roster_path().parent
            except (SafetyStop, OSError):
                directory = Path(self.app.service.base)
            self.review = CrossReview(self.app.service.base, owner,
                                      feedback_path=directory / "复核反馈.xlsx")
        except (SafetyStop, OSError, ValueError) as exc:
            self.review = None
            messages.showwarning("无法加载复核进度", str(exc), parent=self)
        if self.review:
            try:
                self.review.sync_pending()
            except (SafetyStop, OSError) as exc:
                messages.showwarning("复核反馈未同步", str(exc), parent=self)
        self.render()
        self.app.configure_actions()

    def render(self):
        self.controls()
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        if self.review:
            for sid, entry in self.review.data["rows"].items():
                status = ("有问题·未同步" if entry.get("confirmed") and entry.get("feedback_synced") is not True
                          else entry["status"])
                self.tree.insert("", "end", iid=sid, values=(sid, entry.get("title", ""), status))
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
                            "输入复核对象并加载后读取网页已处理队列。确认问题备注即同步到复核反馈.xlsx。")
        self.details.configure(state="disabled")

    def start(self):
        if self.app.busy or not self.review or self.owner.get() != self.review.owner:
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
        preference_error = None
        try:
            reviewer = self.validated_name(self.reviewer.get(), "校对人")
            if reviewer == self.review.owner:
                raise SafetyStop("校对人不能是被复核人。")
            self.reviewer.set(reviewer)
            try:
                self.reviewer_path.parent.mkdir(parents=True, exist_ok=True)
                self.reviewer_path.write_text(json.dumps({"reviewer": reviewer}, ensure_ascii=False),
                                              encoding="utf-8")
            except OSError as exc:
                preference_error = exc
            self.review.confirm(selected[0], self.note.get(), reviewer)
        except (SafetyStop, OSError) as exc:
            messages.showwarning("确认或同步未完成", str(exc), parent=self)
        else:
            self.app.status.set("已确认并同步到复核反馈.xlsx")
        finally:
            self.render()
            if preference_error is not None:
                self.app.status.set(self.app.status.get() + f"；校对人保存失败：{preference_error}")
