"""Compact model-advice tab; final approval stays in the manual workflow."""
import json
import tkinter as tk
from tkinter import messagebox, ttk

from core import SafetyStop
from model_review import DEFAULT_MODEL, MODELS, context_stamp, make_context, render_advice


class ModelPanel:
    def __init__(self, app, page, client):
        self.app, self.page, self.client = app, page, client
        self.model = tk.StringVar(value=DEFAULT_MODEL)
        self.consent = tk.BooleanVar(value=False)
        self.info = tk.StringVar(value="仅提供建议，不代替人工批准。")
        self.result_text = ""
        self.result_stamp = None
        self.running = False
        self.cancelled = False
        self.clearing = False
        page.columnconfigure(0, weight=1)
        page.rowconfigure(6, weight=1)
        ttk.Label(page, textvariable=app.current_id, wraplength=450).grid(row=0, column=0, sticky="w", pady=(0, 8))
        selection = ttk.Frame(page)
        selection.grid(row=1, column=0, sticky="ew")
        ttk.Label(selection, text="模型").pack(side="left")
        self.model_box = ttk.Combobox(selection, textvariable=self.model, values=MODELS, state="readonly", width=20)
        self.model_box.pack(side="left", fill="x", expand=True, padx=6)
        self.model_box.bind("<<ComboboxSelected>>", lambda _e: self.clear())
        app.button(selection, "密钥设置", self.configure_key).pack(side="right")
        network = ttk.Frame(page)
        network.grid(row=2, column=0, sticky="ew", pady=(6, 8))
        app.button(network, "测试连接", self.test_connection).pack(side="left")
        app.button(network, "查看发送内容", self.preview).pack(side="left", padx=6)
        ttk.Label(network, text="仅交大服务 · 校园网/VPN", foreground="#5b6572").pack(side="right")
        evidence_frame = ttk.Frame(page)
        evidence_frame.grid(row=3, column=0, sticky="ew")
        ttk.Label(evidence_frame, text="补充证据（选填：原文、人员信息摘录及出处）").pack(anchor="w")
        self.evidence = tk.Text(evidence_frame, height=4, width=30, wrap="word", font=("Microsoft YaHei UI", 10),
                                relief="solid", borderwidth=1, padx=5, pady=4)
        self.evidence.pack(fill="x", pady=(4, 6))
        self.evidence.bind("<<Modified>>", self.evidence_changed)
        self.allow = ttk.Checkbutton(page, variable=self.consent, text="允许发送本条比对与补充证据到交大模型服务")
        self.allow.grid(row=4, column=0, sticky="w", pady=(0, 6))
        actions = ttk.Frame(page)
        actions.grid(row=5, column=0, sticky="ew", pady=(0, 8))
        self.send = app.button(actions, "辅助核对", self.review, style="Model.TButton")
        self.send.pack(side="left")
        self.cancel_button = ttk.Button(actions, text="停止等待", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="left", padx=6)
        self.copy_button = app.button(actions, "复制意见", self.copy_result)
        self.copy_button.pack(side="right")
        output_frame = ttk.Frame(page)
        output_frame.grid(row=6, column=0, sticky="nsew")
        self.output = tk.Text(output_frame, height=10, width=30, wrap="word", font=("Microsoft YaHei UI", 10),
                              bg="white", relief="flat", padx=8, pady=7, state="disabled")
        scroll = ttk.Scrollbar(output_frame, command=self.output.yview)
        self.output.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.output.pack(fill="both", expand=True)
        self.info_label = ttk.Label(page, textvariable=self.info, foreground="#5b6572", wraplength=450)
        self.info_label.grid(row=7, column=0, sticky="ew", pady=(7, 0))
        page.bind("<Configure>", lambda event: self.info_label.configure(wraplength=max(250, event.width - 24)))
        self.consent.trace_add("write", lambda *_: self.refresh())
        self.refresh()

    def refresh(self):
        app = self.app
        self.send.configure(state="normal" if app.current and self.consent.get() and not app.busy else "disabled")
        self.copy_button.configure(state="normal" if self.result_text and not app.busy else "disabled")

    def set_busy(self, busy):
        if not busy:
            self.running = False
        self.model_box.configure(state="disabled" if busy else "readonly")
        self.evidence.configure(state="disabled" if busy else "normal")
        self.allow.configure(state="disabled" if busy else "normal")
        self.cancel_button.configure(state="normal" if busy and self.running else "disabled")
        self.refresh()

    def show(self, text):
        self.output.configure(state="normal")
        self.output.delete("1.0", "end")
        self.output.insert("1.0", text)
        self.output.configure(state="disabled")

    def clear(self, reset_evidence=False):
        self.result_text, self.result_stamp = "", None
        self.show("")
        self.consent.set(False)
        self.info.set("仅提供建议，不联网检索、不操作网页、不批准完成。")
        if reset_evidence:
            self.clearing = True
            self.evidence.delete("1.0", "end")
            self.evidence.edit_modified(False)
            self.clearing = False
        self.refresh()

    def evidence_changed(self, _event=None):
        if self.evidence.edit_modified():
            if not self.clearing:
                self.clear()
            self.evidence.edit_modified(False)

    def context(self):
        app = self.app
        if not app.current or not app.roster:
            raise SafetyStop("请先在人工页选择一条记录。")
        app.roster.assert_unchanged()
        evidence = self.evidence.get("1.0", "end").strip()
        return make_context(app.current, app.snapshot, app.comparison, evidence), context_stamp(
            app.current, app.snapshot, app.comparison, evidence, self.model.get())

    def configure_key(self):
        if self.app.busy:
            return
        popup = tk.Toplevel(self.app.root)
        popup.title("模型密钥 · 仅本机保存")
        popup.transient(self.app.root)
        popup.attributes("-topmost", True)
        popup.geometry("460x220")
        popup.resizable(False, False)
        frame = ttk.Frame(popup, padding=15)
        frame.pack(fill="both", expand=True)
        configured = self.client.key_store.configured()
        ttk.Label(frame, text="已配置加密密钥；粘贴新密钥可替换。" if configured else "粘贴校方提供的 API key。", wraplength=420).pack(anchor="w")
        key = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=key, show="●")
        entry.pack(fill="x", pady=10)
        ttk.Label(frame, text="Windows 账号绑定加密，不显示已保存密钥。\n不会进入代码、Git 或日志。", wraplength=420).pack(anchor="w")
        def save():
            if self.app.busy:
                return
            try:
                self.client.key_store.save(key.get())
                self.client.available = None
                key.set("")
                popup.destroy()
                self.clear()
                self.info.set("密钥已加密保存，可测试连接。")
            except Exception:
                messagebox.showwarning("未保存", "密钥格式或本机加密保存失败，请检查后重试。", parent=popup)
        ttk.Button(frame, text="加密保存", command=save).pack(anchor="e", pady=10)
        entry.focus_set()

    def preview(self):
        if self.app.busy:
            return
        try:
            context, _ = self.context()
        except SafetyStop as exc:
            messagebox.showwarning("暂无资料", str(exc), parent=self.app.root)
            return
        popup = tk.Toplevel(self.app.root)
        popup.title("发送内容预览 · 当前单条")
        popup.geometry("520x480")
        popup.transient(self.app.root)
        popup.attributes("-topmost", True)
        text = tk.Text(popup, wrap="word", width=30, padx=10, pady=10, font=("Microsoft YaHei UI", 10))
        scroll = ttk.Scrollbar(popup, command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        text.pack(fill="both", expand=True)
        text.insert("1.0", "只发送以下单条资料和程序核对规则，不发送整份名单、负责人、登录信息或密钥。\n修改资料后请重新查看。\n\n" +
                    json.dumps(context, ensure_ascii=False, indent=2))
        text.configure(state="disabled")

    def test_connection(self):
        if self.app.busy:
            return
        self.cancelled = False
        self.running = True
        def ready(models):
            if self.cancelled:
                self.info.set("已停止等待。")
                return
            self.info.set("连接成功，可用：" + "、".join(models) if models else "连接成功，但未返回已配置的调用名，请核对授权。")
            self.model_box["values"] = models or MODELS
        self.app.run(self.client.models, ready, "测试交大模型连接（不发送论文或名单）…",
                     log_action="测试模型连接")

    def review(self):
        if self.app.busy:
            return
        try:
            if not self.consent.get():
                raise SafetyStop("请先查看发送内容并勾选本条发送许可。")
            context, stamp = self.context()
            model = self.model.get()
            if model not in MODELS:
                raise SafetyStop("请重新选择模型。")
        except SafetyStop as exc:
            messagebox.showwarning("未发送", str(exc), parent=self.app.root)
            return
        self.result_text, self.result_stamp = "", None
        self.show("正在核对当前资料…\n你可以停止等待；请求可能已产生用量。")
        self.info.set("模型只读分析，结束后仍需人工批准。")
        self.app.reviewed.set(False)
        self.cancelled = False
        self.running = True
        def ready(result):
            if self.cancelled:
                self.show("已停止等待，未采纳模型结果。")
                return
            _, current_stamp = self.context()
            if current_stamp != stamp:
                raise SafetyStop("当前任务或证据已变化，旧模型意见已丢弃。")
            self.result_text = render_advice(result["advice"], result["model"], result["usage"])
            self.result_stamp = stamp
            self.show(self.result_text)
            self.info.set("意见仅供参考。网页有修改后请重新定位并重新核对。")
            self.app.status.set("模型意见已生成，尚未批准或修改任何数据。")
            self.refresh()
        self.app.run(lambda: self.client.review(context, model), ready, "模型正在辅助核对；不提交网页、不改 Excel…",
                     log_action="模型辅助核对")

    def cancel(self):
        if self.running:
            self.cancelled = True
            self.client.cancel()
            self.info.set("已请求停止等待；服务端可能已计入用量，不自动重试。")
            self.cancel_button.configure(state="disabled")

    def copy_result(self):
        if self.app.busy or not self.result_text:
            return
        try:
            _, stamp = self.context()
            if stamp != self.result_stamp:
                self.clear()
                raise SafetyStop("资料已改变，请重新核对。")
            self.app.copy(self.result_text, "AI 意见已复制；不是完成备注或批准结果。")
        except SafetyStop as exc:
            messagebox.showwarning("未复制", str(exc), parent=self.app.root)
