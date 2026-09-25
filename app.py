"""Manual review desk with optional, explicitly triggered browser navigation."""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from notices import messages as messagebox
from bridge import Bridge
from claim import sa_claim_source
from approval import complete_claim
from model_review import KeyStore, ModelClient
from model_panel import ModelPanel
from automation_panel import AutomationPanel

from core import Journal, SafetyStop, fixed_roster_path, guide, read_roster
from remarks import PRESETS, append_remark
from roster_write import mark_complete

BASE = Path(__file__).resolve().parent
YELLOW = "#fff2bc"
GREEN = "#d9f2df"


class App:
    def __init__(self, root, journal=None, auto_load=True, bridge=None, model_client=None):
        self.root = root
        self.journal = journal or Journal(BASE / "runtime" / "progress.sqlite3")
        self.roster = None
        self.records = []
        self.by_id = {}
        self.current = None
        self.bridge = bridge
        self.model_client = model_client or ModelClient(KeyStore(BASE / "runtime"))
        self.snapshot = None
        self.comparison = None
        self.prepared_claim = None
        self.claim_options = []
        self.busy = False
        self.events = queue.Queue()
        self.buttons = []
        self.owner = tk.StringVar()
        self.status = tk.StringVar(value="选择负责人后，开始人工处理。")
        self.pending_count = tk.StringVar(value="未完成 —")
        self.done_count = tk.StringVar(value="已完成 —")
        self.current_id = tk.StringVar(value="请选择一条记录")
        self.approval = tk.StringVar(value="未完成")
        self.reviewed = tk.BooleanVar(value=False)
        self.preset = tk.StringVar(value="选择备注模板")
        self.task_view = tk.StringVar(value="pending")
        self.connection = tk.StringVar(value="连接浏览器")
        self.sa_number = tk.StringVar(value="先定位网页")
        self.claim_person = tk.StringVar(value="尚未查找人员")
        self.claim_author = tk.StringVar()
        self.build()
        self.reviewed.trace_add("write", lambda *_: self.refresh_approval())
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.pump_id = self.root.after(120, self.pump)
        self.load_id = self.root.after(200, self.reload_roster) if auto_load else None

    def button(self, parent, text, command, **kwargs):
        # Use text-sized buttons; ttk's default nine-character minimum clips the
        # six compact navigation actions at the smallest supported window size.
        kwargs.setdefault("width", 0)
        widget = ttk.Button(parent, text=text, command=command, **kwargs)
        self.buttons.append(widget)
        return widget

    def build(self):
        self.root.title("机构知识库 · 比对助手")
        width, height = 560, min(700, self.root.winfo_screenheight() - 90)
        x = max(0, self.root.winfo_screenwidth() - width - 35)
        self.root.geometry(f"{width}x{height}+{x}+35")
        self.root.minsize(520, 600)
        self.root.attributes("-topmost", True)
        self.root.configure(bg="#f5f6f8")
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TFrame", background="#f5f6f8")
        style.configure("TLabel", background="#f5f6f8", font=("Microsoft YaHei UI", 9))
        style.configure("TButton", font=("Microsoft YaHei UI", 9), padding=(8, 5))
        style.configure("TCheckbutton", background="#f5f6f8", font=("Microsoft YaHei UI", 9))
        style.configure("TNotebook.Tab", font=("Microsoft YaHei UI", 10), padding=(16, 6))
        style.configure("Treeview", rowheight=27, font=("Microsoft YaHei UI", 9))
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9))
        style.map("Treeview", background=[("selected", "#f5d366")], foreground=[("selected", "#262626")])
        style.configure("Complete.TButton", background="#16845b", foreground="white", borderwidth=0,
                        padding=(14, 9), font=("Microsoft YaHei UI", 11, "bold"))
        style.map("Complete.TButton", background=[("disabled", "#dce6e0"), ("pressed", "#0e5b3c"), ("active", "#106e49")],
                  foreground=[("disabled", "#69786f"), ("!disabled", "white")])
        style.configure("Model.TButton", background="#315b9c", foreground="white", padding=(12, 7))
        style.map("Model.TButton", background=[("disabled", "#dce2ec"), ("active", "#234a86")],
                  foreground=[("disabled", "#69786f"), ("!disabled", "white")])
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True, padx=8, pady=8)
        self.manual_page = ttk.Frame(self.tabs, padding=10)
        self.automation_page = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(self.manual_page, text="人工处理")
        self.tabs.add(self.automation_page, text="自动化")
        self.model_page = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(self.model_page, text="模型辅助")
        self.model_panel = ModelPanel(self, self.model_page, self.model_client)
        ttk.Label(self.automation_page, textvariable=self.current_id, wraplength=440).pack(anchor="w", pady=(0, 8))
        self.automation_panel = AutomationPanel(self, self.automation_page, BASE / "runtime" / "wos-imports")
        auto = self.automation_panel.claim_page
        ttk.Label(auto, text="SA 提交 · 括号编号").pack(anchor="w")
        ttk.Entry(auto, textvariable=self.sa_number, state="readonly").pack(fill="x", pady=(5, 8))
        auto_tools = ttk.Frame(auto)
        auto_tools.pack(fill="x", pady=(0, 8))
        self.button(auto_tools, "定位网页", self.locate).pack(side="left")
        self.button(auto_tools, "复制编号", self.copy_sa_number).pack(side="left", padx=6)
        self.button(auto_tools, "查找认领人员", self.prepare_claim).pack(side="left")
        ttk.Label(auto, textvariable=self.claim_person, wraplength=440, font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=(0, 12))
        ttk.Label(auto, text="对应论文作者（确认署名后选择）").pack(anchor="w")
        self.claim_author_box = ttk.Combobox(auto, textvariable=self.claim_author, state="readonly")
        self.claim_author_box.pack(fill="x", pady=(5, 12))
        self.claim_author_box.bind("<<ComboboxSelected>>", lambda _e: self.refresh_approval())
        self.claim_button = self.button(auto, "确认并认领此作者", self.submit_claim, style="Complete.TButton")
        self.claim_button.pack(fill="x", pady=(0, 14))
        ttk.Label(auto, text="编号或署名不唯一时暂停；认领后仍需人工批准。", wraplength=420, foreground="#5b6572").pack(anchor="w")
        ttk.Label(self.automation_page, textvariable=self.status, wraplength=445).pack(anchor="w", pady=(7, 0))
        page = self.manual_page
        page.columnconfigure(0, weight=1)
        page.rowconfigure(2, weight=3)
        page.rowconfigure(4, weight=2)
        toolbar = ttk.Frame(page)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(toolbar, text="负责人").pack(side="left")
        self.owner_box = ttk.Combobox(toolbar, textvariable=self.owner, state="readonly", width=10)
        self.owner_box.pack(side="left", padx=(6, 8))
        self.owner_box.bind("<<ComboboxSelected>>", self.select_owner)
        self.button(toolbar, "重读 list.xlsx", self.reload_roster).pack(side="left")
        self.button(toolbar, "连接浏览器", self.pair, textvariable=self.connection).pack(side="left", padx=4)
        self.button(toolbar, "说明", self.help, width=4).pack(side="right")
        counts = ttk.Frame(page)
        counts.grid(row=1, column=0, sticky="ew", pady=(0, 7))
        self.view_buttons = []
        for value, caption, color in (("pending", self.pending_count, YELLOW), ("done", self.done_count, GREEN)):
            tab = tk.Radiobutton(counts, textvariable=caption, variable=self.task_view, value=value, indicatoron=False,
                                 bg="#f5f6f8", selectcolor=color, activebackground=color, relief="flat", borderwidth=1,
                                 padx=12, pady=5, command=self.switch_view, font=("Microsoft YaHei UI", 10))
            tab.pack(side="left", padx=(0, 5))
            self.view_buttons.append(tab)
        ttk.Label(counts, text="点击切换").pack(side="right")
        listing = ttk.Frame(page)
        listing.grid(row=2, column=0, sticky="nsew")
        self.tree = ttk.Treeview(listing, columns=("id", "title", "state"), show="headings", height=5, selectmode="browse")
        for name, caption, size in (("id", "名单 ID", 140), ("title", "题名", 260), ("state", "状态", 65)):
            self.tree.heading(name, text=caption)
            self.tree.column(name, width=size, minwidth=50, stretch=name == "title")
        self.tree.tag_configure("pending", background=YELLOW, foreground="#4b3d17")
        self.tree.tag_configure("done", background=GREEN, foreground="#19522c")
        scroll = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.select_record)
        self.tree.bind("<Button-1>", lambda _e: "break" if self.busy else None)
        self.tree.bind("<KeyPress>", lambda _e: "break" if self.busy else None)
        heading = ttk.Frame(page)
        heading.grid(row=3, column=0, sticky="ew", pady=(8, 5))
        ttk.Label(heading, textvariable=self.current_id).pack(side="left")
        self.badge = tk.Label(heading, textvariable=self.approval, bg=YELLOW, fg="#735000", padx=7, pady=3)
        self.badge.pack(side="right")
        detail_frame = ttk.Frame(page)
        detail_frame.grid(row=4, column=0, sticky="nsew")
        self.details = tk.Text(detail_frame, height=5, width=30, wrap="word", font=("Microsoft YaHei UI", 10),
                               bg="white", relief="flat", padx=8, pady=6)
        detail_scroll = ttk.Scrollbar(detail_frame, orient="vertical", command=self.details.yview)
        self.details.configure(yscrollcommand=detail_scroll.set, state="disabled")
        detail_scroll.pack(side="right", fill="y")
        self.details.pack(fill="both", expand=True)
        tools = ttk.Frame(page)
        tools.grid(row=5, column=0, sticky="ew", pady=5)
        self.button(tools, "定位网页", self.locate).pack(side="left")
        self.button(tools, "编辑", lambda: self.open_browser_panel("open_metadata")).pack(side="left", padx=3)
        self.button(tools, "认领", lambda: self.open_browser_panel("open_claim")).pack(side="left")
        self.button(tools, "ID", self.copy_id).pack(side="left", padx=3)
        self.button(tools, "SA 编号", self.copy_sa_number).pack(side="left")
        self.button(tools, "指引", self.show_guide).pack(side="left")
        self.button(tools, "下一条", self.next_record).pack(side="right")
        templates = ttk.Frame(page)
        templates.grid(row=6, column=0, sticky="ew", pady=(2, 4))
        self.preset_box = ttk.Combobox(templates, values=PRESETS, textvariable=self.preset, state="readonly", width=27)
        self.preset_box.pack(side="left", fill="x", expand=True)
        self.preset_box.bind("<<ComboboxSelected>>", lambda _e: self.use_remark(self.preset.get()))
        self.button(templates, "复制备注", self.copy_note).pack(side="right", padx=(6, 0))
        self.note = tk.Text(page, height=2, width=30, wrap="word", font=("Microsoft YaHei UI", 10), relief="solid", borderwidth=1)
        self.note.grid(row=7, column=0, sticky="ew")
        self.note.bind("<<Modified>>", self.note_changed)
        self.check = ttk.Checkbutton(page, variable=self.reviewed, text="我已核对当前记录，并完成网页处理")
        self.check.grid(row=8, column=0, sticky="w", pady=(7, 3))
        approvals = ttk.Frame(page)
        approvals.grid(row=9, column=0, sticky="ew", pady=(0, 6))
        approvals.columnconfigure((0, 1), weight=1)
        self.complete_button = self.button(approvals, "批准完成", self.confirm_manual_done, style="Complete.TButton")
        self.complete_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.claimed_complete_button = self.button(approvals, "网页认领结案", self.confirm_claim_done, style="Complete.TButton")
        self.claimed_complete_button.grid(row=0, column=1, sticky="ew")
        self.status_label = ttk.Label(page, textvariable=self.status, wraplength=485, foreground="#5b6572")
        self.status_label.grid(row=10, column=0, sticky="ew")
        page.bind("<Configure>", lambda event: self.status_label.configure(wraplength=max(250, event.width - 20)))
        self.refresh_approval()

    def refresh_approval(self):
        done = bool(self.current and self.current.done)
        enabled = bool(self.current and not done and self.reviewed.get() and not self.busy)
        self.complete_button.configure(state="normal" if enabled else "disabled", text="已批准完成" if done else "✓ 批准完成")
        can_close_claim = bool(enabled and self.current.owner == "谭勋策" and self.snapshot and self.comparison)
        self.claimed_complete_button.configure(state="normal" if can_close_claim else "disabled")
        can_claim = bool(self.current and not done and self.prepared_claim and not self.busy and self.claim_author_box.current() >= 0)
        self.claim_button.configure(state="normal" if can_claim else "disabled")
        self.model_panel.refresh()

    def set_busy(self, busy):
        self.busy = busy
        for widget in self.buttons:
            widget.configure(state="disabled" if busy else "normal")
        for widget in (self.owner_box, self.preset_box, self.claim_author_box):
            widget.configure(state="disabled" if busy else "readonly")
        self.check.configure(state="disabled" if busy else "normal")
        self.note.configure(state="disabled" if busy else "normal")
        for tab in self.view_buttons:
            tab.configure(state="disabled" if busy else "normal")
        self.model_panel.set_busy(busy)
        self.refresh_approval()

    def run(self, job, callback, status):
        if self.busy:
            return
        self.set_busy(True)
        self.status.set(status)
        def worker():
            try:
                self.events.put((True, callback, job()))
            except Exception as exc:
                self.events.put((False, callback, exc))
        threading.Thread(target=worker, daemon=True).start()

    def pump(self):
        self.connection.set("浏览器已连接" if self.bridge and self.bridge.online else "连接浏览器")
        try:
            while True:
                self.status.set(self.automation_panel.progress.get_nowait())
        except queue.Empty:
            pass
        try:
            while True:
                success, callback, value = self.events.get_nowait()
                self.set_busy(False)
                try:
                    if not success:
                        raise value
                    callback(value)
                except Exception as exc:
                    self.clear_browser_state()
                    self.reviewed.set(False)
                    self.status.set("已暂停，请按提示处理；不自动重试。")
                    messagebox.showwarning("等待人工处理", str(exc), parent=self.root)
        except queue.Empty:
            pass
        self.pump_id = self.root.after(120, self.pump)

    def show_text(self, content):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", content)
        self.details.configure(state="disabled")

    def clear_selection(self):
        self.current = None
        self.clear_browser_state()
        self.current_id.set("请选择一条记录")
        self.set_approval(False)
        self.note.delete("1.0", "end")
        self.preset.set("选择备注模板")
        self.show_text("")
        self.model_panel.clear(reset_evidence=True)
        self.automation_panel.clear()

    def clear_claim_preview(self):
        self.prepared_claim = None
        self.claim_options = []
        self.claim_person.set("尚未查找人员")
        self.claim_author.set("")
        self.claim_author_box["values"] = []
        self.refresh_approval()

    def clear_browser_state(self):
        self.snapshot = None
        self.comparison = None
        self.sa_number.set("先定位网页")
        self.clear_claim_preview()
        self.model_panel.clear()

    def set_approval(self, completed):
        self.approval.set("已完成" if completed else "未完成")
        self.badge.configure(bg=GREEN if completed else YELLOW, fg="#19522c" if completed else "#735000")
        self.reviewed.set(False)

    def reload_roster(self):
        if self.busy:
            return
        self.load_id = None
        self.roster = None
        self.records = []
        self.by_id = {}
        self.owner.set("")
        self.owner_box["values"] = []
        self.tree.delete(*self.tree.get_children())
        self.clear_selection()
        self.pending_count.set("未完成 —")
        self.done_count.set("已完成 —")
        self.run(lambda: read_roster(fixed_roster_path(BASE)), self.loaded, "读取 list.xlsx…")

    def loaded(self, roster):
        self.roster = roster
        self.owner_box["values"] = sorted({record.owner for record in roster.records})
        self.update_counts(roster.records)
        self.status.set("请选择负责人。黄色待办，绿色已完成。")

    def update_counts(self, records):
        done = sum(record.done for record in records)
        self.pending_count.set(f"未完成 {len(records) - done}")
        self.done_count.set(f"已完成 {done}")

    def populate(self):
        scope = [r for r in self.roster.records if r.owner == self.owner.get()]
        # Excel is authoritative. Legacy journal states must not hide pending rows.
        self.records = [r for r in scope if r.done == (self.task_view.get() == "done")]
        self.by_id = {r.sa_id: r for r in self.records}
        self.tree.delete(*self.tree.get_children())
        for record in self.records:
            self.tree.insert("", "end", iid=record.sa_id, values=(record.sa_id, record.title, "已完成" if record.done else "未完成"),
                             tags=("done" if record.done else "pending",))
        self.update_counts(scope)
        style = ttk.Style(self.root)
        style.map("Treeview", background=[("selected", "#a7dcbc" if self.task_view.get() == "done" else "#f5d366")],
                  foreground=[("selected", "#163d29" if self.task_view.get() == "done" else "#262626")])

    def switch_view(self):
        if self.busy:
            return
        self.clear_selection()
        if self.roster:
            self.populate()
        self.status.set("已完成记录可查看和定位网页，不可重复批准。" if self.task_view.get() == "done" else "选择待办进行处理。")

    def select_owner(self, _event=None):
        if self.busy or not self.roster:
            return
        self.clear_selection()
        self.populate()
        self.status.set("选择记录后可定位网页。" if self.records else "当前分类没有记录。")

    def select_record(self, _event=None):
        if self.busy or not self.tree.selection():
            return
        record = self.by_id.get(self.tree.selection()[0])
        if not record or record == self.current:
            return
        self.clear_selection()
        self.current = record
        self.set_approval(record.done)
        self.current_id.set(f"ID：{record.sa_id}")
        self.show_text(f"{record.title}\n\n工号 {record.staff_id or '—'}    匹配 {record.matches}\n{record.reason or '未提供差异原因'}")
        self.status.set("已完成记录，仅供查看。" if record.done else "可定位网页；处理完成后由你批准。")

    def pair(self):
        if self.busy:
            return
        try:
            if self.bridge is None:
                self.bridge = Bridge()
        except OSError as exc:
            messagebox.showwarning("连接服务未启动", f"请关闭旧助手后重试。\n{exc}", parent=self.root)
            return
        popup = tk.Toplevel(self.root)
        popup.title("连接浏览器")
        popup.transient(self.root)
        popup.attributes("-topmost", True)
        popup.geometry("510x290")
        frame = ttk.Frame(popup, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="在 Chrome / Edge 加载 code/extension 扩展。\n登录后台并进入 SA 数据比对页，点击扩展图标，\n粘贴下方配对码并连接当前标签页。", wraplength=465).pack(anchor="w", pady=(0, 10))
        token = tk.StringVar(value=self.bridge.token)
        ttk.Entry(frame, textvariable=token, state="readonly").pack(fill="x")
        ttk.Button(frame, text="复制配对码", command=lambda: self.copy(token.get(), "配对码已复制。请在扩展中连接。")).pack(anchor="w", pady=8)
        import webbrowser
        ttk.Button(frame, text="打开后台入口", command=lambda: webbrowser.open("http://admin.ir.lib.sjtu.edu.cn/#/dataCompare/list")).pack(anchor="w")
        def reset():
            try:
                self.bridge.re_pair()
                token.set(self.bridge.token)
                self.clear_browser_state()
                self.reviewed.set(False)
            except SafetyStop as exc:
                messagebox.showwarning("等待当前操作结束", str(exc), parent=popup)
        ttk.Button(frame, text="更换标签页 / 新配对码", command=reset).pack(anchor="w", pady=8)

    def locate(self):
        self.open_browser_panel("search")

    def open_browser_panel(self, action):
        if self.busy:
            return
        try:
            if action not in {"search", "open_metadata", "open_claim"}:
                raise SafetyStop("人工模式不支持网页自动写入。")
            if not self.current or not self.roster:
                raise SafetyStop("请先选择记录。")
            self.roster.assert_unchanged()
            if not self.bridge or not self.bridge.online:
                raise SafetyStop("请先点击“连接浏览器”完成配对。")
            if action != "search" and (self.current.done or not self.snapshot):
                raise SafetyStop("请先定位未完成记录，再打开编辑或认领窗口。")
            record = self.current
            payload = {"sa_id": record.sa_id}
            if action != "search":
                payload["expected"] = self.snapshot
            self.clear_browser_state()
            self.reviewed.set(False)
        except Exception as exc:
            messagebox.showwarning("暂未定位", str(exc), parent=self.root)
            return
        def opened(result):
            row = result.get("row", {})
            if row.get("saLzkId") != record.sa_id:
                raise SafetyStop("网页返回 ID 不一致，请人工检查。")
            self.snapshot = row if action == "search" else None
            self.comparison = result.get("comparison") if action == "search" else None
            if action == "search":
                try:
                    self.sa_number.set(sa_claim_source(self.comparison)[1])
                except SafetyStop:
                    self.sa_number.set("未识别唯一括号编号")
            self.status.set("已定位对应详情，请在浏览器核验。" if action == "search" else "已打开窗口，请手动修改并保存。")
            self.refresh_approval()
        self.run(lambda: self.bridge.call(action, payload), opened, "正在定位当前记录，请勿同时操作该网页…")

    def next_record(self):
        if self.busy or not self.records:
            return
        index = self.records.index(self.current) + 1 if self.current in self.records else 0
        if index >= len(self.records):
            index = 0
        target = self.records[index].sa_id
        self.tree.selection_set(target)
        self.tree.see(target)
        self.select_record()

    def copy(self, value, status):
        if self.busy or not value:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self.status.set(status)

    def copy_id(self):
        if self.current:
            self.copy(self.current.sa_id, "ID 已复制，请在网页手动搜索。")

    def copy_sa_number(self):
        if self.busy:
            return
        try:
            if not self.current or not self.snapshot:
                raise SafetyStop("请先选择记录并定位网页。")
            _, identifier = sa_claim_source(self.comparison)
            self.copy(identifier, "SA 括号编号已复制，前导 0 已保留。")
        except SafetyStop as exc:
            messagebox.showwarning("暂未复制", str(exc), parent=self.root)

    def claim_payload(self):
        if not self.current or not self.roster or self.current.done or not self.snapshot:
            raise SafetyStop("请先在人工页选择未完成记录，并定位网页。")
        self.roster.assert_unchanged()
        if not self.bridge or not self.bridge.online:
            raise SafetyStop("请先连接浏览器。")
        source, identifier = sa_claim_source(self.comparison)
        if self.current.staff_id and self.current.staff_id != identifier:
            raise SafetyStop("SA 括号编号与名单工号不一致，请人工核对。")
        return {"sa_id": self.current.sa_id, "expected": self.snapshot, "sa_text": source,
                "staff_id": identifier, "roster_staff_id": self.current.staff_id}

    def prepare_claim(self):
        if self.busy:
            return
        try:
            payload = self.claim_payload()
            record = self.current
            self.clear_claim_preview()
            self.reviewed.set(False)
        except SafetyStop as exc:
            messagebox.showwarning("暂未查找", str(exc), parent=self.root)
            return
        def ready(result):
            prepared = result.get("prepared", {})
            person = prepared.get("person", {})
            if (result.get("row", {}).get("saLzkId") != record.sa_id or prepared.get("staff_id") != payload["staff_id"]
                    or person.get("wno") != payload["staff_id"] or not person.get("id") or not person.get("name")):
                raise SafetyStop("人员查找结果不一致，请重新定位。")
            self.prepared_claim = prepared
            self.claim_person.set(f"{person['name']} · {person['wno']}\n请核对人员与论文作者署名。")
            self.claim_options = [a for a in prepared.get("authors", []) if a.get("eligible") and not a.get("scholarId")]
            self.claim_author_box["values"] = [f"{a['order']}. {a['fullname']}" for a in self.claim_options]
            suggested = result.get("suggested_index")
            choices = [i for i, a in enumerate(self.claim_options) if a["index"] == suggested]
            if len(choices) == 1:
                self.claim_author_box.current(choices[0])
            self.refresh_approval()
            self.status.set("已找到唯一人员，尚未认领。请核对并选择对应作者。")
        self.run(lambda: self.bridge.call("prepare_claim", payload), ready, "正在按 SA 编号查找人员，请勿操作网页…")

    def submit_claim(self):
        if self.busy:
            return
        try:
            payload = self.claim_payload()
            prepared = self.prepared_claim
            choice = self.claim_author_box.current()
            if not prepared or not 0 <= choice < len(self.claim_options):
                raise SafetyStop("请先查找人员，再选择对应作者署名。")
            author, person, record = self.claim_options[choice], prepared["person"], self.current
            question = (f"名单：{record.sa_id}\n{record.title[:100]}\n\n人员：{person['name']}（{person['wno']}）"
                        f"\n论文署名：第 {author['order']} 位 · {author['fullname']}\n\n确认该署名属于此人员，并向网站提交一次认领？"
                        "\n不会自动把 Excel 标记完成。")
            if not messagebox.askyesno("确认单条作者认领", question, parent=self.root):
                return
            payload.update(prepared=prepared, author_index=author["index"], confirmed=True)
            self.journal.save(record, "认领待提交", self.note.get("1.0", "end").strip(),
                              {"staff_id": person["wno"], "scholar_id": person["id"], "author": author["fullname"], "order": author["order"]})
            self.clear_browser_state()
            self.reviewed.set(False)
        except Exception as exc:
            messagebox.showwarning("暂未认领", str(exc), parent=self.root)
            return
        def claimed(result):
            if (result.get("row", {}).get("saLzkId") != record.sa_id or result.get("verified") is not True
                    or result.get("claimed") is not True or result.get("staff_id") != person["wno"]
                    or result.get("scholar_id") != person["id"] or result.get("author") != author["fullname"]
                    or result.get("order") != author["order"]):
                raise SafetyStop("已发出认领，但回读结果不一致。请核验网页，禁止直接重试。")
            self.use_remark("已认领")
            self.claim_person.set(f"已认领：{person['name']} · {author['fullname']}")
            self.status.set("认领已回读确认，备注已填“已认领”；核对后回人工页批准完成。")
            try:
                self.journal.save(record, "认领已核验", self.note.get("1.0", "end").strip(), result)
            except Exception:
                self.status.set("网页认领已成功，但本地日志失败；请人工核验，勿重复认领。")
        self.run(lambda: self.bridge.call("submit_claim", payload), claimed, "正在复核并提交单条认领，请勿操作网页…")

    def copy_note(self):
        self.copy(self.note.get("1.0", "end").strip(), "备注已复制，请在网页手动粘贴并保存。")

    def use_remark(self, value):
        if self.busy or not self.current or self.current.done:
            return
        updated = append_remark(self.note.get("1.0", "end"), value)
        self.note.delete("1.0", "end")
        self.note.insert("1.0", updated)
        self.reviewed.set(False)
        self.status.set("模板仅辅助填写，请人工确认适用条件。")

    def note_changed(self, _event=None):
        if self.note.edit_modified():
            self.reviewed.set(False)
            self.note.edit_modified(False)

    def confirm_manual_done(self):
        if self.busy:
            return
        try:
            record, roster = self.current, self.roster
            if not roster or not record or record not in self.records or record.done:
                raise SafetyStop("请先选择一条未完成记录。")
            if not self.reviewed.get():
                raise SafetyStop("请先完成人工审批，并勾选已核对。")
            roster.assert_unchanged()
            note = self.note.get("1.0", "end").strip()
            question = f"名单 ID：{record.sa_id}\n{record.title[:100]}\n\n确认这条记录已处理完成？\n仅将 list.xlsx 第 {record.row} 行完成备注写为数字 1。\n此批准按钮不会修改网页。"
            if record.remark:
                question += f"\n\n原完成备注：{record.remark[:200]}\n将替换为 1，原值保存在备份中。"
            if not messagebox.askyesno("人工确认完成", question, parent=self.root):
                return
            self.journal.save(record, "人工批准待回写", note, {"row": record.row, "previous": record.remark})
        except Exception as exc:
            messagebox.showwarning("暂未完成", str(exc), parent=self.root)
            return
        def saved(result):
            self.roster = result.roster
            self.current = next(r for r in self.roster.records if r.row == record.row and r.sa_id == record.sa_id)
            self.populate()
            self.set_approval(True)
            self.model_panel.clear()
            self.status.set("已完成：备注已写为数字 1，已从待办移除。")
            try:
                self.journal.save(record, "已完成", note, {"cell": result.cell, "backup": str(result.backup),
                                                          "previous": result.previous, "mode": "manual"})
            except Exception as exc:
                self.status.set("名单已写为 1，但日志保存失败。请检查磁盘，勿重复确认。")
                messagebox.showwarning("名单已完成，日志异常", str(exc), parent=self.root)
        self.run(lambda: mark_complete(roster, record), saved, "正在备份并回写完成标记…")

    def confirm_claim_done(self):
        """Explicitly reviewed, single-record backend closure plus local sync."""
        if self.busy:
            return
        try:
            record, roster = self.current, self.roster
            if (not roster or not record or record.owner != "谭勋策" or record.done or
                    record not in self.records or not self.reviewed.get()):
                raise SafetyStop("请选择谭勋策的一条待办，并勾选已核对。")
            if not self.bridge or not self.bridge.online or not self.snapshot or not self.comparison:
                raise SafetyStop("认领成功后请再次定位网页、核对结果，再进行结案。")
            if self.note.get("1.0", "end").strip() != "已认领":
                raise SafetyStop("此按钮仅保存“已认领”备注；其他问题请分别核验处理。")
            if record.matches != 1 or record.reason != "作者不一致":
                raise SafetyStop("此按钮仅用于单匹配、仅作者不一致的记录。")
            roster.assert_unchanged()
            snapshot, comparison = self.snapshot, self.comparison
            if not messagebox.askyesno("确认网页认领结案", f"名单：{record.sa_id}\n{record.title[:120]}\n\n"
                    "先重新核验已认领，再保存网页备注“已认领”并标为已处理。\n"
                    "后台回读成功后，备份 list.xlsx 并把本行完成备注写为数字 1。\n"
                    "如果后台已处理，只同步 Excel，不重复提交。是否继续？", parent=self.root):
                return
            self.journal.save(record, "认领结案待核验", "已认领", {"mode": "reviewed_claim_completion"})
            self.reviewed.set(False)
        except Exception as exc:
            messagebox.showwarning("暂未结案", str(exc), parent=self.root)
            return

        def saved(result):
            completion = result.completion
            self.roster = completion.roster
            self.current = next(r for r in self.roster.records if r.row == record.row and r.sa_id == record.sa_id)
            self.clear_browser_state()
            self.populate()
            self.set_approval(True)
            self.model_panel.clear()
            self.status.set("后台原已处理，仅同步名单。" if result.already_processed else
                            "已结案：网页备注/状态已回读，Excel 已备份并标为 1。")
            try:
                self.journal.save(record, "已完成", "已认领", {"mode": "reviewed_claim_completion",
                    "row": result.row, "cell": completion.cell, "backup": str(completion.backup),
                    "already_processed": result.already_processed})
            except Exception:
                self.status.set("网页与 Excel 已完成，但日志失败。请检查备份，不要重复提交。")

        self.run(lambda: complete_claim(roster, record, self.bridge, snapshot, comparison, reviewed=True),
                 saved, "正在核验认领、保存网页状态并同步名单；不自动重试…")

    def show_guide(self):
        if not self.current:
            return
        popup = tk.Toplevel(self.root)
        popup.title("本条信息与处理指引")
        popup.transient(self.root)
        popup.attributes("-topmost", True)
        popup.geometry("520x480")
        record = self.current
        content = (f"ID：{record.sa_id}\n题名：{record.title}\nDOI：{record.doi or '—'}\nWOS：{record.wos or '—'}\n"
                   f"平台号：{record.item_ids or '—'}\n查询方式：{record.query}\n原完成备注：{record.remark or '空'}\n\n{guide(record)}")
        view = tk.Text(popup, wrap="word", font=("Microsoft YaHei UI", 10), padx=12, pady=12)
        bar = ttk.Scrollbar(popup, command=view.yview)
        view.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        view.pack(fill="both", expand=True)
        view.insert("1.0", content)
        view.configure(state="disabled")

    def help(self):
        os.startfile(BASE / "README.md")

    def close(self):
        if self.busy:
            messagebox.showwarning("正在处理", "请等待当前操作结束再关闭。模型请求可先点击“停止等待”。", parent=self.root)
            return
        for timer in (self.pump_id, self.load_id):
            if timer:
                self.root.after_cancel(timer)
        if self.bridge:
            self.bridge.close()
        self.journal.close()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        App(root)
        root.mainloop()
    except Exception as exc:
        messagebox.showerror("助手无法启动", f"{exc}\n请检查名单、依赖及目录权限。", parent=root)
        root.destroy()


if __name__ == "__main__":
    main()
