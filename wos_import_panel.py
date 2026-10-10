"""Explicit local TXT -> backend import UI, independent from the download queue."""
import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog

from automation import ImportStore
from core import SafetyStop
from notices import messages as messagebox
from ui_theme import P, style_text
from wos_import import build_plan, require_owner, run_import_plan

LABELS = {"ready": "本轮可入库", "resume": "本轮续验", "deferred": "需人工核验", "pushed": "已入库·待关联",
          "synced": "原已处理", "halted": "已暂停", "queued": "下轮再核验", "skip": "本轮只记跳过"}
SCOPES = {"待补论文（不含跳过项）": "pending", "已跳过论文（是否识别为 2）": "skipped"}


class WOSImportPanel:
    def __init__(self, app, page):
        self.app = app
        self.plan = None
        self.running = False
        self.stop = threading.Event()
        self.entries = {}
        self.file_errors = ()
        self.download_report = None
        self.limit = tk.StringVar(value="5")
        self.scope = tk.StringVar(value=next(iter(SCOPES)))
        self.inbox = tk.StringVar(value=str(app.automation_panel.runtime.parent / "submission" / "待收导出"))
        self.reviewed = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="第 1 步：选择负责人和范围，检查已下载的 TXT。")
        ttk.Label(page, text="论文入库 · WOS TXT", style="Title.TLabel").pack(anchor="w", pady=(2, 8))
        owner_row = ttk.Frame(page)
        owner_row.pack(fill="x", pady=(0, 6))
        ttk.Label(owner_row, text="负责人").pack(side="left")
        self.owner_box = ttk.Combobox(owner_row, textvariable=app.owner, state="readonly", width=10,
            postcommand=lambda: self.owner_box.configure(values=sorted({r.owner for r in app.roster.records}) if app.roster else []))
        self.owner_box.pack(side="left", padx=6)
        self.owner_box.bind("<<ComboboxSelected>>", app.select_owner)
        app.button(owner_row, "连接浏览器", app.pair).pack(side="right")
        source = ttk.Frame(page)
        source.pack(fill="x", pady=(0, 8))
        ttk.Label(source, text="文件目录").pack(side="left")
        self.folder_entry = ttk.Entry(source, textvariable=self.inbox, width=10)
        self.folder_entry.pack(side="left", fill="x", expand=True, padx=6)
        app.button(source, "更换目录", self.choose_folder).pack(side="right")
        row = ttk.Frame(page)
        row.pack(fill="x", pady=(0, 8))
        self.scope_box = ttk.Combobox(row, textvariable=self.scope, values=list(SCOPES), state="readonly", width=23)
        self.scope_box.pack(side="left", padx=(0, 8))
        ttk.Label(row, text="本轮最多").pack(side="left")
        self.limit_box = ttk.Spinbox(row, from_=1, to=100, textvariable=self.limit, width=5)
        self.limit_box.pack(side="left", padx=6)
        ttk.Label(row, text="篇").pack(side="left")
        actions = ttk.Frame(page)
        actions.pack(fill="x", pady=(0, 8))
        app.button(actions, "1. 检查 TXT 文件", self.preview).pack(side="left")
        self.start_button = app.button(actions, "2. 上传并入库 / 续验", self.start, style="Primary.TButton")
        self.start_button.pack(side="left", padx=5)
        self.stop_button = ttk.Button(actions, text="暂停", command=self.cancel, state="disabled")
        self.stop_button.pack(side="right")
        utilities = ttk.Frame(page)
        utilities.pack(fill="x", pady=(0, 6))
        app.button(utilities, "核验所选论文", self.open_single).pack(side="left")
        app.button(utilities, "打开 TXT 文件夹", self.open_folder).pack(side="left", padx=5)
        app.button(utilities, "查看下载结果", self.open_download_report).pack(side="left")
        self.intro_label = ttk.Label(page, text="入库需连接 SA 比对页，并绑定“数据导入与批次管理”页。\n检查文件不操作网页。入库会上传、导入、推送，但不代表 SA 已完成。",
                  wraplength=430, style="Muted.TLabel")
        self.intro_label.pack(anchor="w", pady=(0, 8))
        # Reserve the consent/status footer before allocating the flexible list area.
        self.status_label = ttk.Label(page, textvariable=self.status, wraplength=850)
        self.status_label.pack(side="bottom", fill="x", pady=(8, 0))
        self.consent = ttk.Checkbutton(page, variable=self.reviewed,
                                      text="已核验本轮文献；新入库项已确认本库缺失")
        self.consent.pack(side="bottom", anchor="w", pady=(6, 0))
        panes = ttk.Panedwindow(page, orient="vertical")
        panes.pack(fill="both", expand=True)
        listing = ttk.Frame(panes)
        panes.add(listing, weight=2)
        self.tree = ttk.Treeview(listing, columns=("title", "state"), show="headings", height=5, selectmode="browse")
        self.tree.heading("title", text="论文 / 名单")
        self.tree.heading("state", text="导入状态")
        self.tree.column("title", width=280, minwidth=140)
        self.tree.column("state", width=140, minwidth=125, stretch=False)
        for tag, color in (("ready", P.amber), ("resume", P.amber), ("deferred", P.red), ("skip", P.red),
                           ("halted", P.red), ("pushed", P.green), ("synced", P.green)):
            self.tree.tag_configure(tag, foreground=color)
        scrollbar = ttk.Scrollbar(listing, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        detail = ttk.Frame(panes)
        panes.add(detail, weight=1)
        self.details = tk.Text(detail, height=4, width=20, wrap="word", state="disabled")
        style_text(self.details)
        bar = ttk.Scrollbar(detail, command=self.details.yview)
        self.details.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.details.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.show_selected)
        for variable in (self.limit, self.inbox, self.scope, app.owner):
            variable.trace_add("write", lambda *_: self.invalidate(clear=True))
        def fit(event):
            for label in (self.intro_label, self.status_label):
                label.configure(wraplength=max(250, event.width - 30))
        page.bind("<Configure>", fit)

    def store(self):
        panel = self.app.automation_panel
        if panel.store is None:
            panel.store = ImportStore(panel.runtime)
        return panel.store

    def invalidate(self, clear=False):
        self.plan = None
        self.reviewed.set(False)
        if clear:
            self.entries = {}
            self.file_errors = ()
            self.render()
            self.status.set("范围或目录已改变，请重新预检。")

    def choose_folder(self):
        if self.app.busy:
            return
        selected = filedialog.askdirectory(parent=self.app.root)
        if selected:
            self.inbox.set(selected)

    def open_folder(self):
        if not self.app.busy:
            folder = Path(self.inbox.get())
            if folder.is_dir():
                os.startfile(folder)
            else:
                messagebox.showinfo("还没有 TXT 文件", "请先在分类页下载 WOS TXT，或更换到已有文件目录。", parent=self.app.root)

    def open_download_report(self):
        if self.app.busy:
            return
        if self.download_report and Path(self.download_report).is_file():
            os.startfile(self.download_report)
        else:
            messagebox.showinfo("下载结果报告", "本次打开助手后尚未生成下载报告。历史报告保存在 runtime/wos-reports。", parent=self.app.root)

    def receive_downloads(self, owner, scope="pending"):
        """Handoff preserves the exact owner/skip scope and does not submit writes."""
        from wos_batch import default_inbox
        self.app.owner.set(owner)
        self.app.select_owner()
        self.scope.set(next(label for label, key in SCOPES.items() if key == scope))
        self.inbox.set(str(default_inbox()))
        self.status.set("TXT 已准备。点“1. 检查 TXT 文件”核对本轮清单。")

    def set_busy(self, busy):
        if not busy:
            self.running = False
        for widget in (self.folder_entry, self.limit_box, self.consent):
            widget.configure(state="disabled" if busy else "normal")
        for widget in (self.owner_box, self.scope_box):
            widget.configure(state="disabled" if busy else "readonly")
        self.stop_button.configure(state="normal" if self.running and busy and not self.stop.is_set() else "disabled")

    def preview(self):
        if self.app.busy:
            return
        try:
            require_owner(self.app.owner.get())
            if not self.app.roster:
                raise SafetyStop("请先读取 list.xlsx。")
            text = self.limit.get().strip()
            if not text.isdigit() or not 1 <= int(text) <= 100:
                raise SafetyStop("本轮条数须为 1–100 的整数。")
            inbox = Path(self.inbox.get())
            if not inbox.is_dir():
                raise SafetyStop("TXT 目录不存在，请先下载元数据或选择已有导出目录。")
            roster, owner, limit = self.app.roster, self.app.owner.get(), int(text)
            scope = SCOPES[self.scope.get()]
            self.invalidate()
        except SafetyStop as exc:
            messagebox.showwarning("暂未预检", str(exc), parent=self.app.root)
            return
        def ready(plan):
            self.plan = plan
            self.file_errors = plan.file_errors
            self.entries = {i.record.sa_id: {"sa_id": i.record.sa_id, "title": i.record.title,
                "status": i.status, "message": i.message, "candidate": i.candidate, "path": i.path, "sha256": i.sha256}
                for i in (*plan.items, *plan.excluded)}
            self.render()
            counts = {s: sum(i.status == s for i in plan.items) for s in LABELS}
            self.status.set(f"可入库 {counts['ready']} 篇 · 续验 {counts['resume']} 篇 · 只记跳过 {counts['skip']} 条 · 另列 {len(plan.excluded)} 条"
                            + (f" · {len(plan.file_errors)} 个文件格式不适用。" if plan.file_errors else ""))
            if not plan.items:
                self.status.set("本轮没有可直接入库的文件。可选中下表论文查看原因，再点“核验所选论文”。" if plan.excluded else
                                "当前负责人在所选范围内没有未完成的零匹配记录。")
        from wos_batch import default_store
        self.app.run(lambda: build_plan(roster, owner, limit, inbox, self.store(), scope=scope, download_store=default_store()), ready,
                     "正在预检本地 WOS TXT（不操作网页、不调用模型）…", log_action="预检 WOS 导入文件")

    def start(self):
        app = self.app
        if app.busy:
            return
        try:
            require_owner(app.owner.get())
            if not self.plan or not self.plan.items:
                raise SafetyStop("请先预检文件。")
            if self.plan.owner != app.owner.get() or self.plan.sha256 != app.roster.sha256 or self.plan.scope != SCOPES[self.scope.get()]:
                raise SafetyStop("负责人或名单已变化，请重新预检。")
            app.roster.assert_unchanged()
            if not app.bridge or not app.bridge.online:
                raise SafetyStop("请连接 SA 比对页，并在扩展绑定后台“数据导入与批次管理”页。")
            if not self.reviewed.get():
                raise SafetyStop("请逐条核验预检列表的文献身份及本库缺失，再勾选确认。")
            count = sum(i.status in ("ready", "resume") for i in self.plan.items)
            skipped = sum(i.status == 'skip' for i in self.plan.items)
            if not messagebox.askyesno("确认本轮论文入库", f"负责人：{app.owner.get()}\n范围：{self.scope.get()}\n本轮 {count} 篇可入库 / 待续验。\n\n"
                f"另有 {skipped} 条只重查并记录跳过，不执行入库。\n"
                "每条先重查 SA；原已处理的只同步 Excel。\n所属机构：上海交通大学；说明：SA补充-名单ID。\n"
                "按 PPT 查重、优先级合并、新增并推送，可能合并已有文献元数据。\n"
                "不上传 PDF；新导入不会直接写完成标记。结果不明时停止，不重复提交。\n\n是否继续？", parent=app.root):
                return
            plan, roster = self.plan, app.roster
        except SafetyStop as exc:
            messagebox.showwarning("暂未导入", str(exc), parent=app.root)
            return
        self.stop.clear()
        self.running = True
        progress = app.automation_panel.progress.put
        def audit(action, outcome, sa_id):
            try:
                app.operation_log.record(action, outcome, sa_id)
            except Exception:
                progress("log.txt 保存失败；导入状态仍保存在独立日志中，请检查目录。")
        def done(result):
            self.running = False
            app.roster = result.roster
            app.skipped = {record.sa_id: record.remark or '是否识别为 2，已标记为跳过。'
                           for record in app.roster.records if record.skipped and not record.done}
            app.clear_selection()
            app.populate()
            for outcome in result.outcomes:
                self.entries[outcome["sa_id"]].update(outcome)
            self.render()
            self.invalidate()
            self.status.set(("已暂停，请核验网页后重新预检。" if result.halted else "已暂停后续步骤。" if result.cancelled else "本轮结束。")
                            + "已推送仍需关联平台号及 SA 结案；新导入未写 Excel 完成标记。")
            app.status.set(self.status.get())
            self.set_busy(False)
        app.run(lambda: run_import_plan(plan, roster, app.bridge, self.store(), reviewed=True,
            stop=self.stop.is_set, progress=progress, audit=audit), done, "开始逐篇核验并导入 WOS…", log_action="WOS 导入队列")

    def cancel(self):
        self.stop.set()
        self.stop_button.configure(state="disabled")
        self.status.set("已请求暂停后续步骤；已发出的提交不能撤回，请等待回读。")

    def render(self):
        self.tree.delete(*self.tree.get_children())
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.configure(state="disabled")
        for sa_id, entry in self.entries.items():
            self.tree.insert("", "end", iid=sa_id, values=(entry["title"], LABELS[entry["status"]]), tags=(entry["status"],))
        if self.entries:
            self.tree.selection_set(next(iter(self.entries)))
            self.show_selected()

    def show_selected(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            return
        entry = self.entries[selection[0]]
        candidate = entry.get("candidate", {})
        text = f"{entry['sa_id']}\n{entry['title']}\n{entry['message']}"
        if candidate:
            text += (f"\n\nWOS：{candidate['wos']}\nDOI：{candidate['doi'] or '—'}\n"
                     f"作者：{candidate['authors']}\n{candidate['journal']} · {candidate['year']}\n"
                     f"单位：{candidate['affiliation']}")
        if entry.get("path"):
            text += "\n文件：" + entry["path"]
        if self.file_errors:
            text += "\n\n目录内未采纳的文件（最多显示 5 项）：\n" + "\n".join(self.file_errors[:5])
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def open_single(self):
        if self.app.busy:
            return
        selection = self.tree.selection()
        entry = self.entries.get(selection[0]) if selection else None
        if not entry or not self.app.roster:
            messagebox.showinfo("请选择论文", "请先在导入列表中选中需要核验的一篇论文。", parent=self.app.root)
            return
        if selection and self.app.roster:
            record = next((r for r in self.app.roster.records if r.sa_id == selection[0]), None)
            if record:
                self.app.owner.set(record.owner)
                self.app.task_view.set("done" if record.done else "pending")
                self.app.select_owner()
                self.app.tree.selection_set(record.sa_id)
                self.app.select_record()
                path = entry.get("path") if entry else None
                if path:
                    panel = self.app.automation_panel
                    panel.cancelled.clear()
                    flow = panel.engine()
                    self.app.run(lambda: flow.prepare_file(record, path, entry.get("sha256")), panel.render_state,
                                 "载入所选 TXT 供核验；不会上传或入库…", log_action="载入待核验 WOS TXT")
        self.app.tabs.select(self.app.automation_page)
        self.app.automation_panel.subtabs.select(self.app.automation_panel.wos_page)
