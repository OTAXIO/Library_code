"""Native, human-in-the-loop SA comparison workbench."""
from __future__ import annotations

import json
import os
import queue
import threading
import tkinter as tk
from collections import Counter
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from bridge import Bridge
from core import Journal, SafetyStop, guide, latest_roster, read_roster

BASE = Path(__file__).resolve().parent


class App:
    def __init__(self, root, bridge=None, journal=None, auto_load=True):
        self.root = root
        self.bridge = bridge or Bridge()
        self.journal = journal or Journal(BASE / "runtime" / "progress.sqlite3")
        self.roster = None
        self.records = []
        self.current = None
        self.snapshot = None
        self.comparison = []
        self.busy = False
        self.events = queue.Queue()
        self.owner = tk.StringVar()
        self.status = tk.StringVar(value="先打开并登录网页，再连接浏览器。")
        self.connection = tk.StringVar(value="浏览器未连接")
        self.file_info = tk.StringVar(value="尚未读取名单")
        self.progress = tk.StringVar(value="请选择负责人")
        self.item_id = tk.StringVar()
        self.reviewed = tk.BooleanVar(value=False)
        self.auto_next = tk.BooleanVar(value=True)
        self.buttons = []
        self.build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.after(150, self.pump)
        if auto_load:
            self.root.after(350, self.load_latest)

    def button(self, parent, label, command, **pack):
        button = ttk.Button(parent, text=label, command=command)
        button.pack(side="left", padx=(0, 7), pady=4, **pack)
        self.buttons.append(button)
        return button

    def build(self):
        self.root.title("机构知识库 · SA 半自动比对助手")
        self.root.geometry("1240x900")
        self.root.minsize(1050, 760)
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f7f9")
        style.configure("TLabel", background="#f3f7f9", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", font=("Microsoft YaHei UI", 10), padding=(10, 7))
        style.configure("Header.TLabel", font=("Microsoft YaHei UI", 19, "bold"), foreground="#164e63")
        style.configure("State.TLabel", foreground="#176b75", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Treeview", rowheight=28, font=("Microsoft YaHei UI", 9))
        frame = ttk.Frame(self.root, padding=18)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(5, weight=1)
        ttk.Label(frame, text="SA 数据比对工作台", style="Header.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(frame, text="1 打开并登录网页    →    2 配对与读取名单    →    3 核验并执行    →    4 回读留痕").grid(row=1, column=0, sticky="w", pady=(5, 10))
        top = ttk.Frame(frame)
        top.grid(row=2, column=0, sticky="ew")
        self.button(top, "连接浏览器 / 配对码", self.pair)
        self.button(top, "读取最新名单", self.load_latest)
        self.button(top, "选择名单…", self.choose_roster)
        self.button(top, "使用说明", self.help)
        ttk.Label(top, textvariable=self.connection, style="State.TLabel").pack(side="right")
        ttk.Label(frame, textvariable=self.file_info, wraplength=1000).grid(row=3, column=0, sticky="w", pady=(2, 8))
        owner_bar = ttk.Frame(frame)
        owner_bar.grid(row=4, column=0, sticky="ew")
        ttk.Label(owner_bar, text="本次负责人：").pack(side="left")
        self.owner_box = ttk.Combobox(owner_bar, textvariable=self.owner, state="readonly", width=18)
        self.owner_box.pack(side="left", padx=(0, 12))
        self.owner_box.bind("<<ComboboxSelected>>", self.select_owner)
        self.button(owner_bar, "开始 / 下一条", self.next_record)
        ttk.Checkbutton(owner_bar, text="完成后搜索下一条", variable=self.auto_next).pack(side="left")
        ttk.Button(owner_bar, text="暂停后续", command=self.pause).pack(side="left", padx=12)
        ttk.Label(owner_bar, textvariable=self.progress).pack(side="right")

        panes = ttk.Panedwindow(frame, orient="horizontal")
        panes.grid(row=5, column=0, sticky="nsew", pady=10)
        left = ttk.Frame(panes)
        right = ttk.Frame(panes)
        panes.add(left, weight=2)
        panes.add(right, weight=5)
        self.tree = ttk.Treeview(left, columns=("id", "matches", "state"), show="headings", selectmode="browse", height=8)
        for key, title, width in [("id", "名单 ID", 190), ("matches", "匹配", 45), ("state", "本地进度", 110)]:
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=40)
        tree_scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=tree_scroll.set)
        tree_scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.select_record)
        self.tree.bind("<Button-1>", lambda _e: "break" if self.busy else None)
        self.tree.bind("<KeyPress>", lambda _e: "break" if self.busy else None)
        self.details = tk.Text(right, wrap="word", height=12, font=("Microsoft YaHei UI", 10), relief="flat", padx=14, pady=12, bg="white")
        detail_scroll = ttk.Scrollbar(right, orient="vertical", command=self.details.yview)
        self.details.configure(yscrollcommand=detail_scroll.set)
        detail_scroll.pack(side="right", fill="y")
        self.details.pack(fill="both", expand=True)
        self.details.configure(state="disabled")

        actions = ttk.Frame(frame)
        actions.grid(row=6, column=0, sticky="ew")
        self.button(actions, "搜索 / 人工处理后重查", self.search)
        self.button(actions, "打开元数据编辑", lambda: self.manual("open_metadata"))
        self.button(actions, "打开认领窗口", lambda: self.manual("open_claim"))
        self.button(actions, "跳过并记录", self.skip)
        edit = ttk.LabelFrame(frame, text="人工核验后，执行当前记录的修改", padding=10)
        edit.grid(row=7, column=0, sticky="ew", pady=(6, 8))
        id_row = ttk.Frame(edit)
        id_row.pack(fill="x")
        ttk.Label(id_row, text="正确平台唯一号：").pack(side="left")
        self.item_entry = ttk.Entry(id_row, textvariable=self.item_id, width=35)
        self.item_entry.pack(side="left", padx=6)
        self.button(id_row, "写入平台号并回读", self.link)
        ttk.Label(id_row, text="仅单个完整编号；不会自动合并").pack(side="left")
        ttk.Label(edit, text="核验依据与处理备注（必填；请写清原文/数据库出处、判断及已完成的修改）：").pack(anchor="w", pady=(6, 3))
        self.note = tk.Text(edit, height=3, wrap="word", font=("Microsoft YaHei UI", 10), relief="solid", borderwidth=1)
        self.note.pack(fill="x")
        last = ttk.Frame(edit)
        last.pack(fill="x", pady=(7, 0))
        self.check = ttk.Checkbutton(last, variable=self.reviewed, text="我已核对当前 ID、原文与差异，并处理了所有未知情况")
        self.check.pack(side="left")
        self.button(last, "设置已处理并核验", self.complete)
        self.button(last, "记录网页已完成", self.confirm_manual_done)
        ttk.Label(frame, textvariable=self.status, style="State.TLabel", wraplength=1000).grid(row=8, column=0, sticky="w")
        ttk.Label(frame, text="源 Excel 只读 · 每次写入前后核验 · 未知情况暂停 · 本地记录不上传 Git", foreground="#647782").grid(row=9, column=0, sticky="w", pady=(5, 0))

    def pump(self):
        self.connection.set("● 浏览器已连接" if self.bridge.online else "○ 浏览器未连接 / 等待页面响应")
        try:
            while True:
                success, callback, value = self.events.get_nowait()
                self.set_busy(False)
                if success:
                    try:
                        callback(value)
                    except Exception as exc:
                        success, value = False, SafetyStop(f"返回结果或本地记录异常：{exc}。请先核验网页，不要直接重试写入。")
                if not success:
                    self.snapshot = None
                    self.reviewed.set(False)
                    if self.current:
                        try:
                            self.journal.save(self.current, "暂停待核验", str(value))
                            self.update_tree()
                        except Exception:
                            self.status.set("本地留痕失败，请停止写入并检查磁盘。")
                    self.status.set("已暂停。请处理网页上的情况，再点击“搜索 / 人工处理后重查”。")
                    messagebox.showwarning("已暂停，等待你操作", str(value), parent=self.root)
        except queue.Empty:
            pass
        self.root.after(150, self.pump)

    def set_busy(self, busy):
        self.busy = busy
        for button in self.buttons:
            button.configure(state="disabled" if busy else "normal")
        self.owner_box.configure(state="disabled" if busy else "readonly")
        self.check.configure(state="disabled" if busy else "normal")
        self.item_entry.configure(state="disabled" if busy else "normal")
        self.note.configure(state="disabled" if busy else "normal")

    def run(self, job, callback, status):
        if self.busy:
            return
        self.set_busy(True)
        self.status.set(status)
        def work():
            try:
                value = job()
                self.events.put((True, callback, value))
            except Exception as exc:
                self.events.put((False, callback, exc))
        threading.Thread(target=work, daemon=True).start()

    def load_latest(self):
        if self.busy:
            return
        try:
            path = latest_roster(BASE.parent)
        except Exception as exc:
            messagebox.showwarning("读取名单", str(exc), parent=self.root)
            return
        self.load(path)

    def choose_roster(self):
        path = filedialog.askopenfilename(title="选择待处理名单", initialdir=BASE.parent, filetypes=[("Excel 名单", "*.xlsx")])
        if path:
            self.load(path)

    def load(self, path):
        self.current = self.snapshot = None
        self.run(lambda: read_roster(path), self.loaded, "正在检查表头、编号精度及重复 ID…")

    def loaded(self, roster):
        self.roster = roster
        modified = datetime.fromtimestamp(roster.path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        self.file_info.set(f"当前名单：{roster.path.name}  |  最后修改 {modified}  |  {len(roster.records):,} 条  |  SHA-256 {roster.sha256[:12]}")
        self.owner_box["values"] = sorted({r.owner for r in roster.records})
        self.owner.set("")
        self.records = []
        self.tree.delete(*self.tree.get_children())
        self.show_text("名单已读取。\n\n请先选择你负责的姓名。默认不处理其他人的记录。\n\n启动时只会读取名单，不会自动向网页写入。")
        self.progress.set("请选择负责人")
        self.status.set("请选择负责人，并通过浏览器扩展连接已登录的比对页。")

    def select_owner(self, _event=None):
        if self.busy or not self.roster:
            return
        self.records = [r for r in self.roster.records if r.owner == self.owner.get()]
        self.current = self.snapshot = None
        self.tree.delete(*self.tree.get_children())
        for record in self.records:
            self.tree.insert("", "end", iid=record.sa_id, values=(record.sa_id, record.matches, self.journal.state(record)))
        self.update_tree()
        self.status.set(f"已选 {self.owner.get()}，共 {len(self.records)} 条。点击“开始 / 下一条”。")

    def update_tree(self):
        counts = Counter()
        for record in self.records:
            state = self.journal.state(record)
            counts[state] += 1
            if self.tree.exists(record.sa_id):
                self.tree.item(record.sa_id, values=(record.sa_id, record.matches, state))
        self.progress.set(f"本地已确认 {counts['已完成']} / {len(self.records)} · 跳过 {counts['已跳过']}")

    def select_record(self, _event=None):
        if self.busy:
            return
        selection = self.tree.selection()
        if not selection:
            return
        record = next((r for r in self.records if r.sa_id == selection[0]), None)
        if record == self.current:
            return
        self.current = record
        self.snapshot = None
        self.comparison = []
        self.reviewed.set(False)
        self.item_id.set("")
        self.note.delete("1.0", "end")
        self.render()

    def next_record(self):
        if self.busy:
            return
        if not self.records:
            messagebox.showinfo("选择负责人", "请先读取名单并选择负责人。", parent=self.root)
            return
        if self.current and self.journal.state(self.current) not in {"已完成", "已跳过"}:
            messagebox.showinfo("当前记录未结束", "请完成或点击“跳过并记录”，也可重新搜索当前记录。", parent=self.root)
            return
        candidate = next((r for r in self.records if self.journal.state(r) not in {"已完成", "已跳过"}), None)
        if not candidate:
            self.status.set("本次队列已走完。跳过的记录仍需复核，可在左侧选中后继续。")
            return
        self.tree.selection_set(candidate.sa_id)
        self.tree.see(candidate.sa_id)
        self.select_record()
        self.search()

    def require_record(self, snapshot=False):
        if not self.current or not self.roster:
            raise SafetyStop("请先选择一条名单记录。")
        self.roster.assert_unchanged()
        if snapshot and not self.snapshot:
            raise SafetyStop("请先点击搜索并核验网页详情。")
        return self.current

    def search(self):
        try:
            record = self.require_record()
        except Exception as exc:
            messagebox.showwarning("无法搜索", str(exc), parent=self.root)
            return
        self.snapshot = None
        self.reviewed.set(False)
        self.run(lambda: self.bridge.call("search", {"sa_id": record.sa_id}), self.searched,
                 "正在精确查询并读取详情。若有网页编辑弹窗，请先人工关闭。")

    def searched(self, result):
        self.snapshot = result["row"]
        self.comparison = result.get("comparison", [])
        self.journal.save(self.current, "等待人工核验", detail=result)
        self.update_tree()
        self.render()
        self.status.set("已找到记录，等待你核验原文和差异；不会自动修改。")
        if self.current.query not in {"1", "2"}:
            messagebox.showwarning("未知查询方式", f"查询方式 {self.current.query} 未在 PPT 中定义。请先人工确认，再决定是否处理。", parent=self.root)

    def show_text(self, text):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    def render(self):
        r = self.current
        if not r:
            return
        lines = [f"名单第 {r.row} 行 · {r.owner}", f"sa_lzk表ID：{r.sa_id}", f"题名：{r.title}",
                 f"工号：{r.staff_id}  |  查询方式：{r.query}", f"Excel 匹配数：{r.matches}  |  状态：{r.mark}",
                 f"DOI：{r.doi or '—'}", f"WOS：{r.wos or '—'}", f"待处理原因：{r.reason or '未填写'}", "", "本条处理指引", guide(r)]
        if self.snapshot:
            s = self.snapshot
            lines += ["", "网页实时结果", f"匹配数：{s.get('matchCount')}  |  状态：{s.get('markStatus')}",
                      f"平台唯一号：{s.get('itemId') or '无'}", f"网页原因：{s.get('reason') or '无'}"]
            if str(s.get("matchCount")) != str(r.matches):
                lines += ["⚠ 网页匹配数与 Excel 不同。请以现场核验结果判断，不能沿用旧名单结论。"]
            for item in self.comparison:
                lines += ["", f"▸ {item['label']}", f"SA：{item['sa'] or '（空）'}", f"本库：{item['library'] or '（空）'}"]
            title = next((x["sa"] for x in self.comparison if x["label"] == "题名"), "")
            if "".join(title.split()).casefold() != "".join(r.title.split()).casefold():
                lines += ["", "⚠ SA 题名与 Excel 不同或为空：请人工核验确为当前任务后再处理。"]
        self.show_text("\n".join(lines))

    def manual(self, action):
        try:
            record = self.require_record(snapshot=True)
            payload = {"sa_id": record.sa_id, "expected": self.snapshot}
        except Exception as exc:
            messagebox.showwarning("需要重查", str(exc), parent=self.root)
            return
        def opened(_result):
            self.snapshot = None
            self.comparison = []
            self.reviewed.set(False)
            self.render()
            self.status.set("已交给你在原网页操作。保存并关闭所有编辑窗口后，点击“搜索 / 人工处理后重查”。")
        self.run(lambda: self.bridge.call(action, payload), opened, "正在核验目标并打开原网页操作窗口…")

    def reviewed_note(self):
        self.require_record(snapshot=True)
        note = self.note.get("1.0", "end").strip()
        if not self.reviewed.get() or len(note) < 6:
            raise SafetyStop("请填写核验依据和结论（至少 6 字），并勾选已核验。")
        if len(note) > 2000:
            raise SafetyStop("备注请控制在 2000 字以内；如后台另有更小限制，请精简。")
        return note

    def write(self, action):
        try:
            note = self.reviewed_note()
            if self.snapshot.get("markStatus") != "待处理":
                raise SafetyStop("网页已处理，不能再次切换状态。核验后可点击“记录网页已完成”。")
            target = self.item_id.get().strip()
            if action == "link" and (not target.isascii() or not target.isdigit() or len(target) > 40):
                raise SafetyStop("请输入一个完整的数字平台唯一号，不使用科学计数法或多个编号。")
            question = f"当前 ID：{self.current.sa_id}\n\n"
            question += (f"平台号：{self.snapshot.get('itemId') or '无'} → {target}" if action == "link" else "状态：待处理 → 已处理")
            question += f"\n\n备注：{note}\n\n确认只对当前这一条执行？"
            if not messagebox.askyesno("确认已核验的单条修改", question, parent=self.root):
                return
            self.require_record(snapshot=True)
            payload = {"sa_id": self.current.sa_id, "expected": self.snapshot, "reviewed": True, "note": note, "item_id": target}
            self.journal.save(self.current, "写入结果待核验", note, {"action": action, "before": self.snapshot, "target": target, "note": note})
            self.update_tree()
        except Exception as exc:
            messagebox.showwarning("暂不执行", str(exc), parent=self.root)
            return
        def saved(result):
            self.snapshot = result["row"]
            self.comparison = []
            self.reviewed.set(False)
            self.journal.save(self.current, "已完成" if action == "complete" else "平台号已核验", note, result)
            self.update_tree()
            self.render()
            self.status.set("状态及备注已回读确认，已留底。" if action == "complete" else "平台号已回读确认。请重新搜索详情，核验其他差异后再标记完成。")
            if action == "complete" and self.auto_next.get():
                self.root.after(500, lambda: self.next_record() if self.auto_next.get() else None)
            elif action == "link":
                self.snapshot = None
        self.run(lambda: self.bridge.call(action, payload), saved, "正在执行单条修改并回读。请勿同时操作该网页；请求发出后无法撤回。")

    def link(self):
        self.write("link")

    def complete(self):
        self.write("complete")

    def confirm_manual_done(self):
        try:
            note = self.reviewed_note()
            if self.snapshot.get("markStatus") != "已处理":
                raise SafetyStop("最近一次回读的网页状态不是已处理，请先完成网页操作并重查。")
            payload = {"sa_id": self.current.sa_id}
        except Exception as exc:
            messagebox.showwarning("不能记录完成", str(exc), parent=self.root)
            return
        def verified(result):
            self.snapshot = result["row"]
            if self.snapshot.get("markStatus") != "已处理":
                self.reviewed.set(False)
                self.status.set("网页状态已变化，不能记录完成。请重新核验。")
                return
            self.journal.save(self.current, "已完成", note, result)
            self.update_tree()
            self.reviewed.set(False)
            self.status.set("已再次回读网页的已处理状态，并记录人工完成。")
            if self.auto_next.get():
                self.root.after(500, lambda: self.next_record() if self.auto_next.get() else None)
        self.run(lambda: self.bridge.call("search", payload), verified, "正在再次回读网页完成状态，仅更新本地记录…")

    def skip(self):
        if not self.current:
            return
        note = self.note.get("1.0", "end").strip()
        if not note:
            messagebox.showinfo("填写跳过原因", "请先在备注框填写原因；跳过不会更改网页状态。", parent=self.root)
            return
        self.journal.save(self.current, "已跳过", note)
        self.snapshot = None
        self.update_tree()
        self.status.set("已记录跳过，不视为完成，也未修改网页。点击下一条继续。")

    def pause(self):
        self.auto_next.set(False)
        self.status.set("已暂停后续。正在执行的单条命令会等待结果，不会撤回已发出的请求。" if self.busy else "已暂停后续；当前停在人工核验阶段。")

    def pair(self):
        popup = tk.Toplevel(self.root)
        popup.title("连接浏览器")
        popup.geometry("650x420")
        frame = ttk.Frame(popup, padding=20)
        frame.pack(fill="both", expand=True)
        text = ("首次使用：\n1. 在 Chrome 或 Edge 打开扩展管理页，开启开发者模式。\n"
                "2. 选择“加载已解压的扩展”，选中 code 下的 extension 文件夹。\n"
                "3. 打开并登录后台，进入 SA数据比对 → 比对结果。\n"
                "4. 点击扩展图标，粘贴下面的配对码，连接当前标签页。\n\n"
                "每次重启助手都需重新配对；只连接一个标签页。配对码不是登录密码。")
        ttk.Label(frame, text=text, wraplength=600).pack(anchor="w", pady=(0, 12))
        token = tk.StringVar(value=self.bridge.token)
        ttk.Entry(frame, textvariable=token, state="readonly", width=60).pack(fill="x", pady=5)
        def copy():
            popup.clipboard_clear()
            popup.clipboard_append(token.get())
        ttk.Button(frame, text="复制配对码", command=copy).pack(anchor="w", pady=6)
        def reset():
            try:
                self.bridge.re_pair()
                token.set(self.bridge.token)
                self.snapshot = None
                self.reviewed.set(False)
            except SafetyStop as exc:
                messagebox.showwarning("暂不能重新配对", str(exc), parent=popup)
        ttk.Button(frame, text="换一个标签页：生成新配对码", command=reset).pack(anchor="w", pady=6)
        ttk.Label(frame, text=f"扩展文件夹：{BASE / 'extension'}", wraplength=600).pack(anchor="w")

    def help(self):
        os.startfile(BASE / "README.md")

    def close(self):
        if self.busy:
            messagebox.showwarning("命令仍在执行", "请等待结果或超时后再关闭。已发出的后台请求不能撤回。", parent=self.root)
            return
        self.bridge.close()
        self.journal.close()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        App(root)
        root.mainloop()
    except Exception as exc:
        messagebox.showerror("助手无法启动", f"{exc}\n\n请检查依赖或本机 8765 端口是否已被另一个助手占用。", parent=root)
        root.destroy()


if __name__ == "__main__":
    main()
