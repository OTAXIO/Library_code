"""Manual review desk with optional, explicitly triggered browser navigation."""
from __future__ import annotations

import os
from copy import deepcopy
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from notices import messages as messagebox
from bridge import Bridge
from claim import sa_claim_source
from approval import auto_complete_claim, complete_claim, verify_claim_result
from model_review import KeyStore, ModelClient
from model_panel import ModelPanel
from automation_panel import AutomationPanel
from operation_log import OperationLog

from core import Journal, SafetyStop, fixed_roster_path, guide, read_roster
from remarks import PRESETS, append_remark
from roster_write import mark_complete

BASE = Path(__file__).resolve().parent
YELLOW = "#fff2bc"
GREEN = "#d9f2df"


class App:
    def __init__(self, root, journal=None, auto_load=True, bridge=None, model_client=None, operation_log=None, unified=False, initial_tab=None):
        self.root = root
        self.unified = unified
        self.classifier = None
        self.submission_panel = None
        self.closing = False
        self.journal = journal or Journal(BASE / "runtime" / "progress.sqlite3")
        self.operation_log = operation_log or OperationLog(BASE / "log.txt")
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
        self.auto_claim_completion = tk.BooleanVar(value=True)
        self.build()
        if unified:
            self.build_classification()
            if initial_tab == 'classification':
                self.tabs.select(self.classification_page)
        self.reviewed.trace_add("write", lambda *_: self.refresh_approval())
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.pump_id = self.root.after(120, self.pump)
        self.load_id = self.root.after(200, self.reload_roster) if auto_load else None

    def build_classification(self):
        from classify_app import ClassifyApp
        self.root.title('机构知识库工作台 · 处理 / 认领 / AI 分类')
        self.root.attributes('-topmost',False)
        height=min(900,self.root.winfo_screenheight()-80)
        width=min(1180,self.root.winfo_screenwidth()-80)
        self.root.geometry(f'{width}x{height}+30+25')
        self.root.minsize(960,740)
        self.classification_page=ttk.Frame(self.tabs)
        self.tabs.add(self.classification_page,text='批量分类 / 导入渠道')
        self.tabs.tab(self.automation_page,text='自动化 / 认领')
        self.classifier=ClassifyApp(self.root,parent=self.classification_page,
                                   on_busy=self.set_busy,on_review=self.review_classified,
                                   on_export=self.export_wos_metadata)
        from submission_panel import SubmissionPanel
        self.submission_page=ttk.Frame(self.tabs,padding=20)
        self.tabs.add(self.submission_page,text='零匹配提交准备')
        self.submission_panel=SubmissionPanel(self,self.submission_page)
        self.tabs.bind('<<NotebookTabChanged>>',self.refresh_workspace)

    def refresh_workspace(self,_event=None):
        if self.classifier and str(self.tabs.select())==str(self.classification_page):
            self.classifier.key_status.set('密钥已配置' if self.classifier.store.configured() else '请配置密钥')
            if not self.busy:
                self.classifier.reload()
                self.classifier.load_results()

    def review_classified(self,item):
        if self.busy:
            return
        if not self.roster:
            messagebox.showinfo('请先读取名单','请在人工处理页读取 list.xlsx。',parent=self.root)
            self.tabs.select(self.manual_page)
            return
        try:
            self.roster.assert_unchanged()
        except SafetyStop as exc:
            messagebox.showwarning('请重读名单',str(exc),parent=self.root)
            return
        matches=[r for r in self.roster.records if r.row in item['rows'] and r.title.strip()==item['title'] and r.doi.strip()==item.get('doi','')]
        if not matches:
            messagebox.showwarning('记录不匹配','分类结果与当前名单不一致，请重新读取。',parent=self.root)
            return
        def select(record):
            self.owner.set(record.owner)
            self.task_view.set('done' if record.done else 'pending')
            self.select_owner()
            self.tree.selection_set(record.sa_id)
            self.tree.see(record.sa_id)
            self.select_record()
            self.tabs.select(self.manual_page)
        if len(matches)==1:
            select(matches[0])
            return
        popup=tk.Toplevel(self.root)
        popup.title('选择要处理的原表记录')
        popup.transient(self.root)
        frame=ttk.Frame(popup,padding=18)
        frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='同一论文对应多条名单，请选择具体记录。').pack(anchor='w',pady=(0,10))
        choice=ttk.Combobox(frame,state='readonly',width=52,values=[f'第 {r.row} 行 · {r.owner} · {r.sa_id}' for r in matches])
        choice.pack(fill='x')
        choice.current(0)
        def confirm():
            if choice.current()<0:
                return
            record=matches[choice.current()]
            popup.destroy()
            select(record)
        ttk.Button(frame,text='打开所选记录',command=confirm).pack(anchor='e',pady=(12,0))
        popup.grab_set()

    def export_wos_metadata(self):
        # Batch download only. Upload, import and push write to the production library
        # and stay single-record with explicit confirmation in the automation page.
        if self.busy or not self.classifier:
            return
        if not self.roster:
            messagebox.showinfo('请先读取名单','请在人工处理页读取 list.xlsx。',parent=self.root)
            self.tabs.select(self.manual_page)
            return
        if not self.bridge or not self.bridge.online:
            messagebox.showinfo('需要连接浏览器',
                '批量导出会操作你在扩展里绑定的 WOS 标签页。\n'
                '请先在人工处理页点“连接浏览器”，并在扩展中绑定 WOS 工作页，然后再试。',
                parent=self.root)
            return
        from paper_classify import read_papers
        from wos_batch import default_inbox, export as export_batch, plan
        try:
            document=read_papers(BASE/'list.xlsx')
            classification,papers=plan(document,BASE/'runtime'/'classification')
        except SafetyStop as exc:
            messagebox.showwarning('无法开始批量导出',str(exc),parent=self.root)
            return
        roster=self.roster
        bridge=self.bridge
        store=self.automation_panel.store
        report=self.automation_panel.progress.put
        def job():
            return export_batch(roster,classification,papers,bridge,store,default_inbox(),
                                stop=self.classifier.stop,unchanged=roster.assert_unchanged,
                                progress=report)
        def done(result):
            detail=''
            if result.get('unconfirmed'):
                shown=result['unconfirmed'][:5]
                detail+=('\n\n身份未获强匹配（未放入待收目录，需人工核验）：\n'
                         +'\n'.join(f'· 原表第 {v["row"]} 行：{v["title"][:40]}' for v in shown))
                if len(result['unconfirmed'])>len(shown):
                    detail+=f'\n…共 {len(result["unconfirmed"])} 条。'
                detail+='\n请在“自动化 / 认领”页对这些条目逐条检索、核对身份后再导入。'
            if result['failed']:
                shown=list(result['failed'].values())[:5]
                detail+='\n\n失败条目：\n'+'\n'.join(f'· 原表第 {v["row"]} 行：{v["error"]}' for v in shown)
                if len(result['failed'])>len(shown):
                    detail+=f'\n…共 {len(result["failed"])} 条，其余见运行日志。'
            messagebox.showinfo('WOS 元数据导出结束',
                f'待导出 {result["total"]} 条，成功 {len(result["exported"])} 条，'
                f'身份待核验 {len(result.get("unconfirmed",[]))} 条，'
                f'失败 {len(result["failed"])} 条。\n\n'
                f'成功导出的 TXT 已放入待收目录：\n{result["inbox"]}\n\n'
                '下一步：在“零匹配提交准备”页点“开始 / 继续准备”，这些文件会被自动采纳；'
                '上传、导入与推送仍需在“自动化 / 认领”页逐条确认执行。'+detail,parent=self.root)
        self.classifier.set_exporting(True)
        self.run(job,done,'正在按分类结果导出 WOS 元数据…',log_action='WOS 批量导出')

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
        self.auto_claim_check = ttk.Checkbutton(auto, variable=self.auto_claim_completion,
            text="认领核验成功后，自动批注并结案")
        self.auto_claim_check.pack(anchor="w")
        ttk.Label(auto, text="仅限本人单匹配作者差异；其他情况保留人工审批。", wraplength=420, foreground="#5b6572").pack(anchor="w", pady=(4, 0))
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
        self.auto_claim_check.configure(state="disabled" if busy else "normal")
        self.note.configure(state="disabled" if busy else "normal")
        for tab in self.view_buttons:
            tab.configure(state="disabled" if busy else "normal")
        self.model_panel.set_busy(busy)
        if self.classifier:
            self.classifier.set_external_busy(busy)
        if self.submission_panel:
            self.submission_panel.set_external_busy(busy)
        if not busy and self.closing:
            self.root.after_idle(self.close)
        self.refresh_approval()

    def run(self, job, callback, status, log_action=None):
        if self.busy:
            return
        action = log_action or status.rstrip("…")
        sa_id = self.current.sa_id if self.current else ""
        self.set_busy(True)
        self.status.set(status)
        def worker():
            try:
                self.events.put((True, callback, job(), action, sa_id))
            except Exception as exc:
                self.events.put((False, callback, exc, action, sa_id))
        threading.Thread(target=worker, daemon=True).start()

    def note_operation(self, action, result="已执行", sa_id=None):
        try:
            self.operation_log.record(action, result,
                                      self.current.sa_id if sa_id is None and self.current else sa_id or "")
        except Exception:
            self.status.set("log.txt 未能保存本次操作；请检查文件权限和格式。")

    def pump(self):
        self.connection.set("浏览器已连接" if self.bridge and self.bridge.online else "连接浏览器")
        try:
            while True:
                self.status.set(self.automation_panel.progress.get_nowait())
        except queue.Empty:
            pass
        try:
            while True:
                success, callback, value, action, sa_id = self.events.get_nowait()
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
                    self.note_operation(action, "已暂停", sa_id)
                else:
                    self.note_operation(action, sa_id=sa_id)
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
        self.run(lambda: read_roster(fixed_roster_path(BASE)), self.loaded, "读取 list.xlsx…",
                 log_action="重读名单")

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
        self.note_operation("切换任务列表：" + ("已完成" if self.task_view.get() == "done" else "未完成"))

    def select_owner(self, _event=None):
        if self.busy or not self.roster:
            return
        self.clear_selection()
        self.populate()
        self.status.set("选择记录后可定位网页。" if self.records else "当前分类没有记录。")
        self.note_operation("选择负责人：" + self.owner.get(), sa_id="")

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
        self.note_operation("选择记录", sa_id=record.sa_id)

    def pair(self):
        if self.busy:
            return
        try:
            if self.bridge is None:
                self.bridge = Bridge()
        except OSError as exc:
            self.note_operation("打开浏览器配对窗口", "已暂停")
            messagebox.showwarning("连接服务未启动", f"请关闭旧助手后重试。\n{exc}", parent=self.root)
            return
        popup = tk.Toplevel(self.root)
        self.note_operation("打开浏览器配对窗口")
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
                self.note_operation("更新浏览器配对码")
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
            self.note_operation({"search": "定位网页", "open_metadata": "打开网页编辑",
                                 "open_claim": "打开网页认领"}.get(action, "打开网页"), "已暂停")
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
        self.run(lambda: self.bridge.call(action, payload), opened, "正在定位当前记录，请勿同时操作该网页…",
                 log_action={"search": "定位网页", "open_metadata": "打开网页编辑", "open_claim": "打开网页认领"}[action])

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
        self.note_operation(status)

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
            self.note_operation("复制 SA 编号", "已暂停")
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
            self.note_operation("查找认领人员", "已暂停")
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
        self.run(lambda: self.bridge.call("prepare_claim", payload), ready, "正在按 SA 编号查找人员，请勿操作网页…",
                 log_action="查找认领人员")

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
            roster, bridge = self.roster, self.bridge
            snapshot, comparison = deepcopy(self.snapshot), deepcopy(self.comparison)
            auto_close = bool(self.auto_claim_completion.get() and record.owner == "谭勋策" and
                              record.matches == 1 and record.reason == "作者不一致" and
                              self.note.get("1.0", "end").strip() in ("", "已认领") and
                              snapshot.get("remark", "") in ("", "已认领"))
            consequence = ("\n认领核验成功后，将自动保存网页批注“已认领”和已处理状态，\n"
                           "回读成功后备份并将 Excel 完成备注写为 1。\n"
                           "请同时确认本条没有其他待处理问题；信息变化时停止。" if auto_close else
                           "\n本条不自动结案，Excel 保持未完成；核验后需单独审批。")
            question = (f"名单：{record.sa_id}\n{record.title[:100]}\n\n人员：{person['name']}（{person['wno']}）"
                        f"\n论文署名：第 {author['order']} 位 · {author['fullname']}\n\n确认该署名属于此人员，并向网站提交一次认领？"
                        + consequence)
            if not messagebox.askyesno("确认单条作者认领", question, parent=self.root):
                return
            payload.update(prepared=prepared, author_index=author["index"], confirmed=True)
            self.journal.save(record, "认领待提交", self.note.get("1.0", "end").strip(),
                              {"staff_id": person["wno"], "scholar_id": person["id"], "author": author["fullname"],
                               "order": author["order"], "auto_complete": auto_close})
            self.clear_browser_state()
            self.reviewed.set(False)
        except Exception as exc:
            self.note_operation("提交作者认领", "已暂停")
            messagebox.showwarning("暂未认领", str(exc), parent=self.root)
            return
        def claimed(result):
            verify_claim_result(record, result, person, author)
            self.use_remark("已认领")
            self.claim_person.set(f"已认领：{person['name']} · {author['fullname']}")
            self.status.set("认领已回读确认，备注已填“已认领”；核对后回人工页批准完成。")
            try:
                self.journal.save(record, "认领已核验", self.note.get("1.0", "end").strip(), result)
            except Exception:
                self.status.set("网页认领已成功，但本地日志失败；请人工核验，勿重复认领。")
                return
            if auto_close:
                self.journal.save(record, "认领结案待核验", "已认领", {"mode": "automatic_claim_completion"})
                def finish():
                    try:
                        return auto_complete_claim(roster, record, bridge, snapshot, comparison,
                                                   result, person, author, confirmed=True)
                    except Exception as exc:
                        raise SafetyStop("认领已成功，但自动批注/结案未全部完成。不要重复认领；重新定位核验后台。\n" + str(exc)) from exc
                self.run(finish, lambda value: self.claim_completion_saved(record, value, "automatic_claim_completion"),
                         "认领已核验，正在自动保存批注、回读状态并同步 Excel…",
                         log_action="自动批注结案")
        self.run(lambda: bridge.call("submit_claim", payload), claimed, "正在复核并提交单条认领，请勿操作网页…",
                 log_action="提交作者认领")

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
        self.note_operation("选择备注模板：" + value)

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
            self.note_operation("人工批准完成", "已暂停")
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
        self.run(lambda: mark_complete(roster, record), saved, "正在备份并回写完成标记…",
                 log_action="人工批准完成")

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
            self.note_operation("网页认领结案", "已暂停")
            messagebox.showwarning("暂未结案", str(exc), parent=self.root)
            return

        self.run(lambda: complete_claim(roster, record, self.bridge, snapshot, comparison, reviewed=True),
                 lambda result: self.claim_completion_saved(record, result, "reviewed_claim_completion"),
                 "正在核验认领、保存网页状态并同步名单；不自动重试…",
                 log_action="网页认领结案")

    def claim_completion_saved(self, record, result, mode):
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
            self.journal.save(record, "已完成", result.row.get("remark", ""), {"mode": mode,
                "row": result.row, "cell": completion.cell, "backup": str(completion.backup),
                "already_processed": result.already_processed})
        except Exception:
            self.status.set("网页与 Excel 已完成，但日志失败。请检查备份，不要重复提交。")

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
        if self.submission_panel and self.submission_panel.busy:
            self.closing=True
            self.submission_panel.request_stop()
            self.tabs.select(self.submission_page)
            return
        if self.classifier and self.classifier.busy:
            self.closing=True
            self.classifier.request_stop()
            self.classifier.status.set('正在保存当前批次，完成后关闭工作台。')
            self.tabs.select(self.classification_page)
            return
        if self.busy:
            messagebox.showwarning("正在处理", "请等待当前操作结束再关闭。模型请求可先点击“停止等待”。", parent=self.root)
            return
        for timer in (self.pump_id, self.load_id):
            if timer:
                self.root.after_cancel(timer)
        if self.bridge:
            self.bridge.close()
        if self.classifier:
            self.classifier.dispose()
        if self.submission_panel:
            self.submission_panel.dispose()
        self.journal.close()
        self.root.destroy()


def main(initial_tab=None):
    root = tk.Tk()
    try:
        App(root,unified=True,initial_tab=initial_tab)
        root.mainloop()
    except Exception as exc:
        messagebox.showerror("助手无法启动", f"{exc}\n请检查名单、依赖及目录权限。", parent=root)
        root.destroy()


if __name__ == "__main__":
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--tab',choices=['manual','classification'],default='manual')
    main(initial_tab=parser.parse_args().tab)
