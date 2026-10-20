"""Two-task UI; all production browser actions use the paired extension."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import ttk

from bridge import Bridge
from core import SafetyStop
from notices import messages
from pairing_ui import copy_pairing_code
from ui_theme import P, install_theme, style_text
from workflow_service import WorkflowService

BASE = Path(__file__).resolve().parent
VERSION = "0.4.31"
SA_URL = "http://admin.ir.lib.sjtu.edu.cn/#/dataCompare/list"
SCOPES = {"待处理": "pending", "重试跳过项": "skipped", "已完成": "done"}


class App:
    def __init__(self, root, *, service=None, bridge=None, auto_load=True):
        self.root = root
        self.service = service or WorkflowService(BASE)
        self.bridge, self.roster = bridge, None
        self.busy, self.closed = False, False
        self.stop, self.events = threading.Event(), queue.Queue()
        self.last_results = {}
        self.owner = tk.StringVar()
        self.scope = tk.StringVar(value="待处理")
        self.limit = tk.StringVar(value="5")
        self.import_style = tk.StringVar(value="Crossref＋OpenAlex（Excel）")
        self.status = tk.StringVar(value="读取名单后，连接浏览器并开始。")
        self.connection = tk.StringVar(value="浏览器未连接")
        self.counts = tk.StringVar()
        install_theme(root)
        root.title(f"机构知识库 · 自动认领 / 自动导入 {VERSION}")
        root.geometry("760x540")
        root.minsize(660, 430)
        root.attributes("-topmost", True)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.build()
        self.timer = root.after(100, self.pump)
        if auto_load:
            self.reload_roster()

    def build(self):
        frame = ttk.Frame(self.root, padding=14)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)
        header = ttk.Frame(frame)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        ttk.Label(header, text="机构知识库", style="Title.TLabel").pack(side="left")
        self.pair_button = ttk.Button(header, text="连接浏览器", command=self.pair)
        self.pair_button.pack(side="right")
        ttk.Label(header, textvariable=self.connection, style="Muted.TLabel").pack(side="right", padx=10)
        controls = ttk.Frame(frame)
        controls.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(controls, text="负责人").pack(side="left")
        self.owner_box = ttk.Combobox(controls, textvariable=self.owner, state="readonly", width=11)
        self.owner_box.pack(side="left", padx=(6, 14))
        self.owner_box.bind("<<ComboboxSelected>>", lambda _: self.render())
        ttk.Label(controls, text="范围").pack(side="left")
        self.scope_box = ttk.Combobox(controls, textvariable=self.scope, values=list(SCOPES), state="readonly", width=11)
        self.scope_box.pack(side="left", padx=(6, 14))
        self.scope_box.bind("<<ComboboxSelected>>", lambda _: self.render())
        ttk.Label(controls, text="本轮条数").pack(side="left")
        self.limit_box = ttk.Spinbox(controls, textvariable=self.limit, from_=1, to=100, width=4)
        self.limit_box.pack(side="left", padx=6)
        self.reload_button = ttk.Button(controls, text="重读名单", command=self.reload_roster)
        self.reload_button.pack(side="right")
        self.notebook = ttk.Notebook(frame)
        self.notebook.grid(row=2, column=0, sticky="nsew")
        self.pages, self.trees, self.details = {}, {}, {}
        explanations = {
            "claim": ("自动认领", "单匹配任务：核对人员编号与署名 → 认领 → 网页批注 → 回写名单。"),
            "import": ("自动导入", "零匹配任务：获取元数据 → 核验交大署名及本库缺失 → 入库 → 批注并结案。"),
        }
        for mode, (title, explanation) in explanations.items():
            page = ttk.Frame(self.notebook, padding=(2, 4))
            page.columnconfigure(0, weight=1)
            page.rowconfigure(1, weight=1)
            self.pages[mode] = page
            self.notebook.add(page, text=title)
            intro = ttk.Frame(page)
            intro.grid(row=0, column=0, sticky="ew", pady=(0, 8))
            ttk.Label(intro, text=explanation, style="Muted.TLabel", wraplength=620).pack(anchor="w")
            if mode == "import":
                style_row = ttk.Frame(intro)
                style_row.pack(fill="x", pady=(6, 0))
                ttk.Label(style_row, text="导入方式").pack(side="left")
                self.import_style_box = ttk.Combobox(style_row, textvariable=self.import_style,
                    values=["Crossref＋OpenAlex（Excel）", "自动导出并入库", "手动导出（一次一篇）"], state="readonly", width=27)
                self.import_style_box.pack(side="left", padx=8)
                self.import_style_box.bind("<<ComboboxSelected>>", lambda _: self.render())
                ttk.Label(style_row, text="Excel 模式不依赖 WOS。", style="Muted.TLabel").pack(side="left")
            table = ttk.Frame(page)
            table.grid(row=1, column=0, sticky="nsew")
            tree = ttk.Treeview(table, columns=("id", "title", "status"), show="headings", height=6)
            for key, label, width in (("id", "名单 ID", 180), ("title", "论文题名", 360), ("status", "状态", 85)):
                tree.heading(key, text=label)
                tree.column(key, width=width, minwidth=70, stretch=key == "title")
            for tag, background, foreground in (("pending", P.amber_soft, P.amber),
                                                ("skipped", P.red_soft, P.red), ("done", P.green_soft, P.green)):
                tree.tag_configure(tag, background=background, foreground=foreground)
            tree.pack(side="left", fill="both", expand=True)
            scroll = ttk.Scrollbar(table, orient="vertical", command=tree.yview)
            scroll.pack(side="right", fill="y")
            tree.configure(yscrollcommand=scroll.set)
            detail = tk.Text(page, height=3, wrap="word")
            style_text(detail)
            detail.grid(row=2, column=0, sticky="ew", pady=(8, 0))
            detail.configure(state="disabled")
            self.trees[mode], self.details[mode] = tree, detail
            tree.bind("<<TreeviewSelect>>", lambda _, selected=mode: self.show_selected(selected))
        self.notebook.bind("<<NotebookTabChanged>>", lambda _: self.render())
        bottom = ttk.Frame(frame)
        bottom.grid(row=3, column=0, sticky="ew", pady=(10, 6))
        actions = ttk.Frame(bottom)
        actions.pack(fill="x")
        self.start_button = ttk.Button(actions, text="开始自动认领", style="Primary.TButton", command=self.start)
        self.start_button.pack(side="left")
        self.manual_import_button = ttk.Button(actions, text="② 接回 TXT 并入库", style="Primary.TButton",
            command=lambda: self.start(manual_intake=True))
        self.pause_button = ttk.Button(actions, text="暂停", command=self.pause, state="disabled")
        self.pause_button.pack(side="left", padx=8)
        self.skip_button = ttk.Button(actions, text="跳过所选数据", command=self.skip_selected, state="disabled")
        self.skip_button.pack(side="left")
        self.counts_label = ttk.Label(bottom, textvariable=self.counts, style="Muted.TLabel")
        self.counts_label.pack(anchor="e", pady=(4, 0))
        ttk.Label(frame, textvariable=self.status, wraplength=615).grid(row=4, column=0, sticky="ew")
        ttk.Label(frame, text="1 已完成 · 2 已跳过，写入末尾“是否识别”；前方“备注”保存处理说明。",
                  style="Muted.TLabel").grid(row=5, column=0, sticky="w", pady=(5, 0))

    def mode(self):
        return "import" if self.notebook.select() == str(self.pages["import"]) else "claim"

    def manual_mode(self):
        return self.mode() == "import" and self.import_style.get() == "手动导出（一次一篇）"

    def metadata_mode(self):
        return self.mode() == "import" and self.import_style.get() == "Crossref＋OpenAlex（Excel）"

    def configure_actions(self):
        manual = self.manual_mode()
        disabled = self.busy or self.scope.get() == "已完成"
        self.start_button.configure(text="① 定位 WOS 文献" if manual else
            "开始自动导入" if self.mode() == "import" else "开始自动认领",
            state="disabled" if disabled else "normal")
        self.limit_box.configure(state="disabled" if self.busy or manual else "normal")
        self.import_style_box.configure(state="disabled" if self.busy else "readonly")
        selected = self.trees[self.mode()].selection()
        can_skip = (not self.busy and self.scope.get() == "待处理" and
                    self.owner.get() == "谭勋策" and len(selected) == 1)
        self.skip_button.configure(state="normal" if can_skip else "disabled")
        if manual:
            self.counts_label.pack_forget()
            self.manual_import_button.pack(side="left", after=self.start_button, padx=(8, 0))
            self.manual_import_button.configure(state="disabled" if disabled else "normal")
        else:
            self.manual_import_button.pack_forget()
            self.counts_label.pack(anchor="e", pady=(4, 0))

    def reload_roster(self):
        if self.busy:
            return
        try:
            self.roster = self.service.load()
            owners = sorted({r.owner for r in self.roster.records if r.owner})
            self.owner_box["values"] = owners
            if self.owner.get() not in owners:
                self.owner.set("谭勋策" if "谭勋策" in owners else "")
            self.last_results.clear()
            self.status.set("名单已读取；当前仅自动处理谭勋策的任务。")
        except (SafetyStop, OSError) as exc:
            self.roster = None
            self.status.set(str(exc))
        self.render()

    def render(self):
        if self.closed:
            return
        scope = SCOPES.get(self.scope.get(), "pending")
        for mode, tree in self.trees.items():
            selection = tree.selection()
            tree.delete(*tree.get_children())
            for r in self.service.visible(self.roster, mode, self.owner.get(), scope):
                tag = "done" if r.done else "skipped" if r.skipped else "pending"
                tree.insert("", "end", iid=r.sa_id, values=(r.sa_id, r.title,
                    {"done": "已完成", "skipped": "已跳过", "pending": "待处理"}[tag]), tags=(tag,))
            if selection and tree.exists(selection[0]):
                tree.selection_set(selection[0])
            self.show_selected(mode)
        mode = self.mode()
        rows = self.service.visible(self.roster, mode, self.owner.get(), "all")
        self.counts.set(f"待处理 {sum(not r.done and not r.skipped for r in rows)}  ·  "
                        f"已跳过 {sum(r.skipped and not r.done for r in rows)}  ·  已完成 {sum(r.done for r in rows)}")
        self.configure_actions()

    def show_selected(self, mode):
        tree, detail = self.trees[mode], self.details[mode]
        selection = tree.selection()
        record = next((r for r in self.roster.records if selection and r.sa_id == selection[0]), None) if self.roster else None
        text = "选择记录可查看原因和批注。已完成任务不会再次执行。"
        if record:
            text = f"{record.title}\n待处理原因：{record.reason or '无'}  ·  数据来源：{record.source or '未记录'}\n备注：{record.remark or '空'}"
            if record.sa_id in self.last_results:
                text += "\n本轮：" + self.last_results[record.sa_id]
        detail.configure(state="normal")
        detail.delete("1.0", "end")
        detail.insert("1.0", text)
        detail.configure(state="disabled")
        self.configure_actions()

    def skip_selected(self):
        if self.busy:
            return
        try:
            mode, scope, owner = self.mode(), SCOPES[self.scope.get()], self.owner.get()
            selected = self.trees[mode].selection()
            if len(selected) != 1:
                raise SafetyStop("请先在列表中只选中一条待处理数据。")
            record = self.service.select_skip(self.roster, mode, owner, scope, selected[0])
            if not messages.askyesno("跳过所选数据", f"仅跳过这一条？\n\n{record.title}\n名单 ID：{record.sa_id}\n\n"
                "是否识别将写为 2。保留已有备注，不导入、不认领、不在网页结案。\n"
                "普通批次会跳过它；以后可在“重试跳过项”中处理。", parent=self.root):
                return
            self.set_busy(True)
            result = self.service.skip_selected(self.roster, mode, owner, scope, record.sa_id, self.bridge)
            self.roster = result.roster
            self.last_results.update(result.messages)
            self.status.set(result.summary)
        except (SafetyStop, OSError) as exc:
            self.status.set(str(exc))
            messages.showwarning("未跳过", str(exc), parent=self.root)
        finally:
            self.set_busy(False)
            self.render()

    def set_busy(self, value):
        self.busy = value
        mode = self.mode()
        for widget in (self.owner_box, self.scope_box):
            widget.configure(state="disabled" if value else "readonly")
        for widget in (self.limit_box, self.reload_button, self.pair_button):
            widget.configure(state="disabled" if value else "normal")
        for key, page in self.pages.items():
            self.notebook.tab(page, state="disabled" if value and key != mode else "normal")
        self.pause_button.configure(state="normal" if value else "disabled")
        self.configure_actions()

    def start(self, *, manual_intake=False):
        if self.busy:
            return
        try:
            mode, scope = self.mode(), SCOPES[self.scope.get()]
            manual = self.manual_mode()
            if manual_intake and not manual:
                raise SafetyStop("接回 TXT 仅用于手动导出的一篇模式。")
            if manual:
                selected = self.trees["import"].selection()
                if len(selected) != 1:
                    raise SafetyStop("请先在列表中选中一篇论文；手动模式不会处理其他记录。")
                records = [self.service.select_manual(self.roster, self.owner.get(), scope, selected[0])]
            else:
                if not self.limit.get().strip().isdigit():
                    raise SafetyStop("本轮条数须为 1–100 的整数。")
                records = self.service.select(self.roster, mode, self.owner.get(), int(self.limit.get()), scope)
            if not self.bridge or not self.bridge.online:
                raise SafetyStop("请连接 SA 比对页并绑定后台导入页；只有 WOS 模式还需绑定 WOS 页。")
        except (SafetyStop, KeyError) as exc:
            messages.showwarning("尚未开始", str(exc), parent=self.root)
            return
        self.stop.clear()
        self.set_busy(True)
        self.status.set("正在定位所选这一篇；定位完成后再手动 Export。" if manual and not manual_intake else
            f"开始{'导入' if mode == 'import' else '认领'} {len(records)} 条，运行中请勿手动操作工作页。")
        roster, bridge, owner = self.roster, self.bridge, self.owner.get()
        metadata = self.metadata_mode()
        def work():
            try:
                progress = lambda text: self.events.put(("progress", text))
                if manual and not manual_intake:
                    result = self.service.locate_import(roster, records[0], owner, scope,
                                                        bridge, self.stop, progress)
                else:
                    result = self.service.run(roster, records, mode, scope, bridge, self.stop, progress,
                                              **({"manual_export": True} if manual else
                                                 {"metadata_import": True} if metadata else {}))
                self.events.put(("result", result))
            except Exception as exc:
                text = str(exc) if isinstance(exc, SafetyStop) else f"操作异常（{type(exc).__name__}）；请核验网页，不重复提交。"
                self.events.put(("error", text))
        threading.Thread(target=work, daemon=True).start()

    def pause(self):
        if self.busy:
            self.stop.set()
            self.pause_button.configure(state="disabled")
            self.status.set("已请求暂停：等待当前操作回读，不强行中断或重复提交。")

    def pump(self):
        if self.closed:
            return
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                self.status.set(value)
            elif kind == "result":
                self.roster, self.last_results = value.roster, value.messages
                self.set_busy(False)
                self.render()
                self.status.set(value.summary)
            elif kind == "error":
                # Read only: recover fingerprints after a partially saved transaction.
                try:
                    self.roster = self.service.read_current()
                except (SafetyStop, OSError):
                    self.roster = None
                self.set_busy(False)
                self.render()
                self.status.set(value)
                messages.showwarning("任务已暂停", value, parent=self.root)
        self.connection.set("浏览器已连接" if self.bridge and self.bridge.online else "浏览器未连接")
        self.timer = self.root.after(100, self.pump)

    def pair(self):
        if self.busy:
            return
        window = getattr(self, "pair_window", None)
        if window is not None and window.winfo_exists():
            window.lift()
            return
        try:
            if self.bridge is None:
                self.bridge = Bridge()
        except OSError:
            messages.showwarning("连接服务未启动", "端口 8765 已被占用，请关闭旧助手后重试。", parent=self.root)
            return
        window = self.pair_window = tk.Toplevel(self.root)
        window.title("连接网页扩展")
        window.transient(self.root)
        window.attributes("-topmost", True)
        window.geometry("460x295")
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="1. 在 SA 比对结果页打开扩展，粘贴配对码并连接。\n"
                  "2. 自动导入另需绑定 WOS 文献页和后台导入页。\n"
                  "三个网页保持打开。认领只需要第一步。", wraplength=425).pack(anchor="w", pady=(0, 12))
        token = tk.StringVar(value=self.bridge.token)
        token_entry = ttk.Entry(frame, textvariable=token, state="readonly")
        token_entry.pack(fill="x")
        def select_token(event=None):
            token_entry.selection_range(0, tk.END)
            return "break"
        token_entry.bind("<Control-a>", select_token)
        token_entry.bind("<Control-A>", select_token)
        def copy():
            try:
                copy_pairing_code(window, token.get())
            except Exception:
                token_entry.focus_set()
                select_token()
                self.status.set("剪贴板暂不可用，配对码已全选；未假报复制成功。")
                return
            self.status.set("配对码已复制，请在 SA 比对页扩展中粘贴。")
        def reset():
            if self.busy:
                return
            try:
                self.bridge.re_pair()
                token.set(self.bridge.token)
                self.status.set("已更换配对码；请重新连接并绑定工作页。")
            except SafetyStop as exc:
                messages.showwarning("等待当前操作", str(exc), parent=window)
        ttk.Button(frame, text="复制配对码", command=copy, style="Primary.TButton").pack(fill="x", pady=8)
        ttk.Button(frame, text="打开 SA 比对页", command=lambda: webbrowser.open(SA_URL)).pack(fill="x")
        ttk.Button(frame, text="重新配对", command=reset).pack(fill="x", pady=8)

    def close(self):
        if self.busy:
            self.pause()
            messages.showwarning("正在安全暂停", "当前操作可能已发送，等待回读结束后再关闭助手。", parent=self.root)
            return
        self.closed = True
        self.root.after_cancel(self.timer)
        if self.bridge:
            self.bridge.close()
        self.root.destroy()


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
