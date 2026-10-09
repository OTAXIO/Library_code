"""Compact workflow panel. Tk is only touched on its owning UI thread."""
import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog
from notices import messages as messagebox

from automation import ImportStore, WOSFlow, classify
from claim_batch import run_claim_batch
from core import SafetyStop
from pilot import OWNER, pilot_scope, precheck_pilot
from roster_write import reconcile_processed
from ui_theme import style_text


class AutomationPanel:
    def __init__(self, app, parent, runtime):
        self.app = app
        self.store = None  # Lazy: merely opening a tab creates no runtime state.
        self.runtime = runtime
        self.cancelled = threading.Event()
        self.progress = queue.Queue()
        self.route = tk.StringVar(value="自动识别当前条目的处理路径")
        self.batch_limit = tk.StringVar(value="100")
        toolbar = ttk.Frame(parent)
        toolbar.pack(fill="x", pady=(0, 5))
        app.button(toolbar, "自动认领", self.claim_batch, style="Model.TButton").pack(side="left")
        app.button(toolbar, "处理跳过项", lambda: self.claim_batch(retry_skipped=True)).pack(side="left", padx=5)
        self.stop_button = ttk.Button(toolbar, text="暂停后续步骤", command=self.cancel)
        self.stop_button.pack(side="right")
        controls = ttk.Frame(parent)
        controls.pack(fill="x", pady=(0, 8))
        ttk.Label(controls, text="本轮").pack(side="left")
        self.limit_box = ttk.Spinbox(controls, from_=1, to=100, increment=1, width=5,
                                     textvariable=self.batch_limit)
        self.limit_box.pack(side="left", padx=(5, 2))
        ttk.Label(controls, text="条（1–100）").pack(side="left")
        app.button(controls, "预检前100条", self.pilot).pack(side="right")
        ttk.Label(parent, textvariable=self.route, wraplength=445).pack(anchor="w", pady=(0, 8))
        self.subtabs = ttk.Notebook(parent)
        self.subtabs.pack(fill="both", expand=True)
        self.wos_page = ttk.Frame(self.subtabs, padding=8)
        self.claim_page = ttk.Frame(self.subtabs, padding=8)
        self.subtabs.add(self.wos_page, text="WOS 导入")
        self.subtabs.add(self.claim_page, text="作者认领")
        self.subtabs.select(self.claim_page)
        wos = self.wos_page
        ttk.Label(wos, text="检索 → 完整记录 → 核验 → 导入 → 推送", wraplength=425).pack(anchor="w", pady=(0, 5))
        footer = ttk.Frame(wos)
        footer.pack(side="bottom", fill="x")
        box = ttk.Frame(wos)
        box.pack(fill="both", expand=True)
        self.output = tk.Text(box, wrap="word", height=6, width=25, state="disabled", relief="flat",
                              bg="white", padx=8, pady=8, font=("Microsoft YaHei UI", 9))
        style_text(self.output)
        scroll = ttk.Scrollbar(box, command=self.output.yview)
        self.output.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.output.pack(fill="both", expand=True)
        app.button(footer, "确认入库 / 继续核验批次", self.resume, style="Complete.TButton").pack(fill="x", pady=(8, 4))
        local = ttk.Frame(footer)
        local.pack(fill="x", pady=3)
        app.button(local, "选择已有 TXT", self.load_file).pack(side="left")
        app.button(local, "检索并下载此篇 TXT", self.start).pack(side="left", padx=5)
        app.button(footer, "只下载 WOS 当前打开的论文", lambda: self.start(current_wos=True)).pack(fill="x", pady=3)
        ttk.Label(footer, text="下载和选文件均不会入库。核实文献和本库缺失后，再点“确认入库”。\n入库会上传、导入并推送；不会自动完成 SA 任务。", wraplength=425,
                  style="Muted.TLabel").pack(anchor="w", pady=(3, 0))

    def show(self, value):
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("1.0", value)
        self.output.configure(state="disabled")

    def load_file(self):
        app = self.app
        if app.busy:
            return
        try:
            record = self.guard(require_connection=False)
            if record.matches != 0:
                raise SafetyStop("本地 TXT 仅用于零匹配补录。")
            selected = filedialog.askopenfilename(parent=app.root, filetypes=[("WOS 单篇完整记录", "*.txt")])
            if not selected:
                return
            self.cancelled.clear()
            flow = self.engine()
        except SafetyStop as exc:
            messagebox.showwarning("暂未读取", str(exc), parent=app.root)
            return
        app.run(lambda: flow.prepare_file(record, selected), self.render_state,
                "正在核验本地 WOS TXT；尚未上传或导入…", log_action="读取 WOS TXT")

    def clear(self):
        self.route.set("自动识别当前条目的处理路径")
        self.show("")

    def cancel(self):
        self.cancelled.set()
        self.app.note_operation("暂停自动认领列表")

    def claim_batch(self, retry_skipped=False):
        app = self.app
        if app.busy:
            return
        try:
            if not app.owner.get().strip():
                raise SafetyStop("请选择负责人。")
            if not app.roster or app.owner.get() != OWNER or app.task_view.get() != "pending":
                raise SafetyStop("请先在人工页选择谭勋策，并切换到未完成列表。")
            app.roster.assert_unchanged()
            if not app.bridge or not app.bridge.online:
                raise SafetyStop("请先连接 Edge 中的 SA 比对结果页。")
            raw_limit = self.batch_limit.get().strip()
            if not raw_limit.isdigit() or not 1 <= int(raw_limit) <= 100:
                raise SafetyStop("本轮条数必须是 1–100 的整数。")
            limit = int(raw_limit)
            candidates = [record for record in app.records if not record.done and
                          (record.skipped if retry_skipped else
                           not record.skipped and record.sa_id not in app.skipped)][:limit]
            if not candidates:
                raise SafetyStop("当前没有 Excel 数字 2 的跳过记录。" if retry_skipped else
                                 "当前没有可自动检查的未完成记录；红色跳过项请用专用按钮。")
            roster = app.roster
            originals = {record.sa_id: record for record in roster.records}
            self.cancelled.clear()
            app.reviewed.set(False)
            self.subtabs.select(self.claim_page)
        except SafetyStop as exc:
            app.note_operation("处理跳过项" if retry_skipped else "自动认领列表", "已暂停")
            messagebox.showwarning("暂未自动认领", str(exc), parent=app.root)
            return

        def audit(action, result, sa_id):
            app.operation_log.record(action, result, sa_id)

        def finished(result):
            app.roster = result.roster
            app.skipped = {record.sa_id: record.remark or "是否识别为 2，已标记为跳过。"
                           for record in result.roster.records if record.skipped and not record.done}
            app.skipped.update(result.skipped)
            app.clear_selection()
            app.populate()
            for sa_id in (*result.completed_ids, *result.synced_ids):
                original = originals.get(sa_id)
                if not original:
                    continue
                try:
                    app.journal.save(original, "已完成", "已认领" if sa_id in result.completed_ids else "", {
                        "mode": "automatic_claim_batch", "already_processed": sa_id in result.synced_ids})
                except Exception:
                    pass
            summary = (f"模式：{'只处理跳过项' if retry_skipped else '普通自动认领'}\n"
                       f"本轮检查：{result.checked} 条（设定上限 {limit}）\n"
                       f"自动认领并结案：{len(result.completed_ids)} 条\n"
                       f"后台原已处理并同步：{len(result.synced_ids)} 条\n"
                       f"红色跳过：{len(result.skipped)} 条")
            reasons = "\n".join(f"{sa_id}：{reason}" for sa_id, reason in list(result.skipped.items())[:12])
            label = "处理跳过项" if retry_skipped else "自动认领"
            self.route.set(label + "因网页状态不确定而停止" if result.halted else
                           label + "已暂停" if result.cancelled else label + "已完成")
            self.show(summary + ("\n\n红色记录：\n" + reasons if reasons else ""))
            app.status.set("已停止后续操作；红色记录保留未完成，请先核验网页。" if result.halted else
                           "自动认领已暂停；已完成结果已保留。" if result.cancelled else
                           "自动认领列表完成；红色记录已跳过并保持未完成。")

        action = "处理跳过项" if retry_skipped else "自动认领列表"
        app.run(lambda: run_claim_batch(roster, candidates, app.bridge, self.cancelled.is_set,
                                        self.progress.put, audit, limit=limit,
                                        retry_skipped=retry_skipped), finished,
                f"正在{action}，无法安全判断的记录将标红跳过…",
                log_action=action)

    def guard(self, require_connection=True):
        app = self.app
        if not app.owner.get().strip():
            raise SafetyStop("请选择负责人。")
        if not app.current or not app.roster or app.current.done:
            raise SafetyStop("请先选择一条未完成记录。")
        if app.current.owner != "谭勋策":
            raise SafetyStop("本次自动化试验只处理谭勋策负责的记录。")
        app.roster.assert_unchanged()
        if require_connection and (not app.bridge or not app.bridge.online):
            raise SafetyStop("请先连接浏览器，并在扩展绑定两个工作页。")
        return app.current

    def synced(self, record, completion):
        app = self.app
        app.roster = completion.roster
        app.clear_selection()
        app.populate()
        self.route.set("后台已处理，已同步名单；跳过所有自动修改。")
        self.show(f"{record.sa_id}\n后台标记：已处理\nExcel 完成标记：{completion.cell} = 1\n已跳过认领、编辑和导入。")
        app.status.set("后台已处理：名单已备份并标为数字 1，当前条目已从待办移除。")
        try:
            app.journal.save(record, "后台已处理并同步名单", "", {
                "cell": completion.cell, "backup": str(completion.backup),
                "previous": completion.previous, "mode": "remote_processed_sync"})
        except Exception:
            app.status.set("名单已标为 1，但本地日志失败；请检查备份，勿重复操作。")

    def pilot(self):
        app = self.app
        if app.busy:
            return
        try:
            if not app.owner.get().strip():
                raise SafetyStop("请选择负责人。")
            if not app.roster or app.owner.get() != OWNER:
                raise SafetyStop("请先在人工页选择负责人谭勋策。")
            app.roster.assert_unchanged()
            if not app.bridge or not app.bridge.online:
                raise SafetyStop("请先连接 Edge 中的 SA 比对结果页。")
            roster = app.roster
            ids = pilot_scope(roster, self.runtime.parent / "pilot-100.json")
            self.cancelled.clear()
            app.reviewed.set(False)
        except SafetyStop as exc:
            app.note_operation("预检前100条", "已暂停")
            messagebox.showwarning("暂未开始试验预检", str(exc), parent=app.root)
            return

        def finished(result):
            app.roster = result.roster
            app.clear_selection()
            app.populate()
            for sa_id in result.synced_ids:
                original = next(item for item in roster.records if item.sa_id == sa_id)
                try:
                    app.journal.save(original, "后台已处理并同步名单", "", {
                        "mode": "pilot_remote_processed_sync"})
                except Exception:
                    pass  # The backed-up Excel write remains authoritative.
            summary = (f"固定试验范围：{len(ids)} 条\n已读取后台：{result.checked} 条\n"
                       f"后台已处理并同步 Excel：{result.synced} 条\n"
                       f"原本本地已完成：{result.local_done} 条\n"
                       f"待继续：{sum(result.routes.values())} 条\n"
                       f"搁置待核验：{len(result.deferred)} 条")
            details = "\n".join(f"{sa_id}：{reason}" for sa_id, reason in result.deferred[:12])
            self.route.set("试验预检已暂停" if result.cancelled else "试验预检已完成；待办条目尚未自动批准。")
            self.show(summary + ("\n\n搁置示例：\n" + details if details else ""))
            app.status.set("试验预检已暂停，可对同一固定范围继续。" if result.cancelled else
                           "100 条试验预检完成。仅同步后台已处理项；其余待逐条核验。")

        app.run(lambda: precheck_pilot(roster, ids, app.bridge, self.cancelled.is_set,
                                       self.progress.put), finished,
                "正在只读核验固定的前 100 条；后台已处理的才同步 Excel…",
                log_action="预检前100条")

    def engine(self):
        if self.store is None:
            self.store = ImportStore(self.runtime)
        roster = self.app.roster
        def unchanged():
            if self.cancelled.is_set():
                raise SafetyStop("已暂停后续步骤。已发出的请求不能撤回，请核验网页。")
            roster.assert_unchanged()
        labels = {"wos_search": "WOS 检索", "wos_export": "WOS 导出 TXT", "search": "重查 SA 比对",
                  "import_scan": "核对导入批次", "import_upload": "上传 WOS TXT",
                  "import_submit": "提交 WOS 导入", "import_check": "回读导入批次",
                  "import_push": "推送 WOS 文献"}
        def audit(action, result, sa_id):
            try:
                self.app.operation_log.record(labels.get(action, "核验 WOS 工作页"), result, sa_id)
            except Exception:
                # A logging failure must not turn an already-submitted upload
                # into a retryable operation. Surface it through the UI queue.
                self.progress.put("log.txt 未能保存 WOS 操作，请检查文件权限和格式。")
        return WOSFlow(self.app.bridge, self.store, unchanged, self.progress.put, audit)

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
            app.note_operation("导出当前 WOS 文献" if current_wos else "自动判断并执行", "已暂停")
            messagebox.showwarning("暂未自动处理", str(exc), parent=app.root)
            return
        roster = app.roster
        def inspect():
            status = app.bridge.call("status", {"sa_id": record.sa_id})
            completion = reconcile_processed(roster, record, status.get("row"))
            if completion:
                return status, completion
            result = app.bridge.call("search", {"sa_id": record.sa_id})
            return result, reconcile_processed(roster, record, result.get("row"))
        def planned(outcome):
            result, completion = outcome
            if completion:
                self.synced(record, completion)
                return
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
                    # A download action must never silently upload to the library.
                    return state
                app.run(job, self.render_state, "下载并核验 WOS TXT；不会上传或导入…", log_action="WOS 单篇下载")
            elif current_wos:
                raise SafetyStop("当前条目不是零匹配，不允许使用 WOS 补录。")
            elif plan.route == "claim":
                self.subtabs.select(self.claim_page)
                app.prepare_claim()
            elif plan.route == "metadata":
                app.open_browser_panel("open_metadata")
            else:
                app.status.set(plan.reason)
        app.run(inspect, planned, "先核验后台是否已处理，再判断处理路径…",
                log_action="导出当前 WOS 文献" if current_wos else "自动判断并执行")

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
            app.reviewed.set(False)
        except SafetyStop as exc:
            app.note_operation("继续核验 WOS 导入", "已暂停")
            messagebox.showwarning("暂未继续", str(exc), parent=app.root)
            return
        roster = app.roster
        def inspect():
            status = app.bridge.call("status", {"sa_id": record.sa_id})
            completion = reconcile_processed(roster, record, status.get("row"))
            if completion:
                return completion
            result = app.bridge.call("search", {"sa_id": record.sa_id})
            return reconcile_processed(roster, record, result.get("row"))
        def checked(completion):
            if completion:
                self.synced(record, completion)
                return
            # Check remote completion before showing any import approval. A task
            # already completed elsewhere needs only the Excel reconciliation.
            if self.guard() != record:
                raise SafetyStop("所选论文已变化，请重新核验。")
            roster.assert_unchanged()
            confirm = not state["identity_confirmed"]
            if state["phase"] == "exported":
                c = state["candidate"]
                identity_notice = ("缺少精确编号匹配或题名有变化。请核实这是该老师提交的同一篇交大成果。\n"
                                   if confirm else "题名与编号匹配，完整记录含交大署名。\n")
                if not messagebox.askyesno("确认这篇论文入库",
                        f"名单：{record.title}\nWOS：{c['title']}\n作者：{c['authors']}\n{c['wos']}\n\n"
                        + identity_notice + "请确认已查过本库、确实缺少此篇。\n"
                        "确认后将上传此 WOS TXT，并按 PPT 规则导入、查重合并和推送一次；不上传 PDF。", parent=app.root):
                    return
            app.run(lambda: flow.proceed(record, confirm_identity=confirm), self.render_state,
                    "继续单条入库；不会重复提交已有批次…", log_action="继续核验 WOS 导入")
        app.run(inspect, checked, "先核验后台是否已处理…", log_action="入库前核验 SA 状态")
