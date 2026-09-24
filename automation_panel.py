"""Compact workflow panel. Tk is only touched on its owning UI thread."""
import queue
import threading
import tkinter as tk
from tkinter import ttk
from notices import messages as messagebox

from automation import ImportStore, WOSFlow, classify
from core import SafetyStop


class AutomationPanel:
    def __init__(self, app, parent, runtime):
        self.app = app
        self.store = None  # Lazy: merely opening a tab creates no runtime state.
        self.runtime = runtime
        self.cancelled = threading.Event()
        self.progress = queue.Queue()
        self.route = tk.StringVar(value="自动识别当前条目的处理路径")
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill="x", pady=(0, 8))
        app.button(toolbar, "自动判断并执行", self.start, style="Model.TButton").pack(side="left")
        self.stop_button = ttk.Button(toolbar, text="暂停后续步骤", command=self.cancelled.set)
        self.stop_button.pack(side="right")
        ttk.Label(parent, textvariable=self.route, wraplength=445).pack(anchor="w", pady=(0, 8))
        self.subtabs = ttk.Notebook(parent)
        self.subtabs.pack(fill="both", expand=True)
        self.wos_page = ttk.Frame(self.subtabs, padding=8)
        self.claim_page = ttk.Frame(self.subtabs, padding=8)
        self.subtabs.add(self.wos_page, text="WOS 导入")
        self.subtabs.add(self.claim_page, text="作者认领")
        wos = self.wos_page
        ttk.Label(wos, text="检索 → 完整记录 → 核验 → 导入 → 推送", wraplength=425).pack(anchor="w", pady=(0, 5))
        box = ttk.Frame(wos)
        box.pack(fill="both", expand=True)
        self.output = tk.Text(box, wrap="word", height=6, width=25, state="disabled", relief="flat",
                              bg="white", padx=8, pady=8, font=("Microsoft YaHei UI", 9))
        scroll = ttk.Scrollbar(box, command=self.output.yview)
        self.output.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.output.pack(fill="both", expand=True)
        app.button(wos, "继续 / 核验导入结果", self.resume, style="Complete.TButton").pack(fill="x", pady=(8, 4))
        app.button(wos, "导出当前 WOS 文献", lambda: self.start(current_wos=True)).pack(fill="x", pady=3)
        ttk.Label(wos, text="扩展可打开导入页：数据管理 → 数据导入与批次管理。\n绑定 WOS 和该页；不上传 PDF，完成仍需人工批准。", wraplength=425,
                  foreground="#5b6572").pack(anchor="w", pady=(3, 0))

    def show(self, value):
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("1.0", value)
        self.output.configure(state="disabled")

    def clear(self):
        self.route.set("自动识别当前条目的处理路径")
        self.show("")

    def guard(self):
        app = self.app
        if not app.current or not app.roster or app.current.done:
            raise SafetyStop("请先选择一条未完成记录。")
        app.roster.assert_unchanged()
        if not app.bridge or not app.bridge.online:
            raise SafetyStop("请先连接浏览器，并在扩展绑定两个工作页。")
        return app.current

    def engine(self):
        if self.store is None:
            self.store = ImportStore(self.runtime)
        roster = self.app.roster
        def unchanged():
            if self.cancelled.is_set():
                raise SafetyStop("已暂停后续步骤。已发出的请求不能撤回，请核验网页。")
            roster.assert_unchanged()
        return WOSFlow(self.app.bridge, self.store, unchanged, self.progress.put)

    def render_state(self, state):
        c = state["candidate"]
        phase = {"exported": "已导出，等待身份确认" if not state["identity_confirmed"] else "已核验文献，待导入",
                 "upload_intent": "上传结果待人工核验", "import_intent": "导入结果待核验",
                 "imported": "已导入，待推送", "push_intent": "推送结果待核验", "pushed": "导入和推送已核验"}[state["phase"]]
        self.route.set(phase)
        self.show(f"{c['title']}\n{c['authors']}\n{c['journal']} · {c['year']}\n\nDOI：{c['doi'] or '—'}\n"
                  f"{c['wos']}\n\n署名机构：\n{c['affiliation']}\n\n说明：{state['instructions']}\n"
                  f"批次：{(state.get('batch') or {}).get('batchNumber', '尚未建立')}\n\n{phase}\n"
                  "推送规则：查重 / 组合规则 / 优先级合并 / 新增 / 本校成果。")
        if state["phase"] == "pushed":
            self.app.status.set("WOS 导入与推送已核验；请核对本库条目、平台号及其他差异后人工批准。")
        else:
            self.app.status.set("可查看文献证据后继续。遇到未知状态不会重复提交。")

    def start(self, current_wos=False):
        app = self.app
        if app.busy:
            return
        try:
            record = self.guard()
            self.cancelled.clear()
            app.reviewed.set(False)
        except SafetyStop as exc:
            messagebox.showwarning("暂未自动处理", str(exc), parent=app.root)
            return
        def planned(result):
            plan = classify(record, result)
            app.snapshot, app.comparison = result["row"], result.get("comparison")
            self.route.set(plan.reason)
            self.show("判断依据：" + plan.reason + "\n\n差异字段：" + "、".join(plan.issues))
            if plan.route == "wos":
                self.subtabs.select(self.wos_page)
                flow = self.engine()
                def job():
                    state = flow.prepare(record, current_wos=current_wos)
                    self.progress.put("已取得 WOS 文献证据。")
                    return flow.proceed(record) if state["identity_confirmed"] else state
                app.run(job, self.render_state, "开始单条 WOS 流程…")
            elif current_wos:
                raise SafetyStop("当前条目不是零匹配，不允许使用 WOS 补录。")
            elif plan.route == "claim":
                self.subtabs.select(self.claim_page)
                app.prepare_claim()
            elif plan.route == "metadata":
                app.open_browser_panel("open_metadata")
            else:
                app.status.set(plan.reason)
        app.run(lambda: app.bridge.call("search", {"sa_id": record.sa_id}), planned, "读取最新网页并判断处理路径…")

    def resume(self):
        app = self.app
        if app.busy:
            return
        try:
            record = self.guard()
            self.cancelled.clear()
            flow = self.engine()
            state = self.store.get(record)
            if not state:
                raise SafetyStop("请先自动判断，或在 WOS 选定文献后导出。")
            self.render_state(state)
            confirm = not state["identity_confirmed"]
            if confirm:
                c = state["candidate"]
                if not messagebox.askyesno("确认单篇文献身份",
                        f"名单：{record.title}\nWOS：{c['title']}\n作者：{c['authors']}\n{c['wos']}\n\n"
                        "缺少精确编号匹配或题名有变化。请核实这是该老师提交的同一篇交大成果。\n"
                        "确认后将上传此 WOS TXT，并按 PPT 规则导入、查重合并和推送一次；不上传 PDF。", parent=app.root):
                    return
            app.reviewed.set(False)
        except SafetyStop as exc:
            messagebox.showwarning("暂未继续", str(exc), parent=app.root)
            return
        app.run(lambda: flow.proceed(record, confirm_identity=confirm), self.render_state, "继续单条导入流程；不重试已发出的写入…")
