"""Shared API settings. Secrets stay in the existing Windows DPAPI store."""
import json
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from core import SafetyStop
from model_review import API_BASE, DEFAULT_MODEL, MODELS
from notices import messages as messagebox
from paper_classify import atomic_json

DEFAULTS = {"review": DEFAULT_MODEL, "classification": "deepseek-chat", "submission": "deepseek-chat"}


def read_preferences(path):
    path = Path(path)
    if not path.exists():
        return dict(DEFAULTS)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != set(DEFAULTS) or any(v not in MODELS for v in data.values()):
            raise ValueError()
        return data
    except (OSError, ValueError, TypeError):
        raise SafetyStop("模型偏好配置无法读取，暂用默认值；可在设置页重新保存。") from None


class SettingsPanel:
    def __init__(self, app, page, runtime):
        self.app = app
        self.path = Path(runtime) / "model_settings.json"
        self.key = tk.StringVar()
        self.status = tk.StringVar()
        self.key_status = tk.StringVar()
        self.models = {name: tk.StringVar(value=value) for name, value in DEFAULTS.items()}
        self.controls = []
        from wos_browser import DEFAULTS as BROWSER_DEFAULTS, read_settings
        self.browser_path = Path(runtime) / 'wos_browser.json'
        try:
            browser = read_settings(runtime)
        except SafetyStop as exc:
            browser = dict(BROWSER_DEFAULTS)
            self.status.set(str(exc))
        self.browser_mode = tk.StringVar(value='浏览器技能（无需配对）' if browser['transport']=='browser-skill' else '原插件配对')
        self.browser_instance = tk.StringVar(value=browser['instance_id'])
        self.browser_origin = browser['origin']
        ttk.Label(page, text="设置", style="Title.TLabel").pack(anchor="w", pady=(4, 10))
        columns = ttk.Frame(page)
        columns.pack(fill="x")
        columns.columnconfigure((0, 1), weight=1, uniform="settings")
        api = ttk.LabelFrame(columns, text="模型服务 · 本机密钥", padding=12)
        api.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        preferences = ttk.Frame(columns)
        preferences.grid(row=0, column=1, sticky="nsew")
        ttk.Label(api, text="API 地址").pack(anchor="w", pady=(0, 4))
        address = ttk.Entry(api)
        address.insert(0, API_BASE)
        address.configure(state="readonly")
        address.pack(fill="x")
        ttk.Label(api, text="固定交大 HTTPS 地址，不向其他服务发送密钥。", style="Muted.TLabel",
                  wraplength=360).pack(anchor="w", pady=(6, 14))
        ttk.Label(api, textvariable=self.key_status, wraplength=360).pack(anchor="w")
        self.entry = ttk.Entry(api, textvariable=self.key, show="●")
        self.entry.pack(fill="x", pady=(5, 8))
        self.controls.append(self.entry)
        actions = ttk.Frame(api)
        actions.pack(fill="x")
        app.button(actions, "保存密钥", self.save_key, style="Primary.TButton").pack(side="left")
        app.button(actions, "测试连接", self.test_connection).pack(side="left", padx=8)
        ttk.Label(api, text="密钥由 Windows 账号加密，不回显、不进 Git 或日志。\n测试连接仅查询可用模型，不发送名单。",
                  wraplength=360, style="Muted.TLabel").pack(anchor="w", pady=(10, 6))
        models_frame = ttk.LabelFrame(preferences, text="默认模型", padding=12)
        models_frame.pack(fill="x", pady=(0, 10))
        for name, label in (("review", "单条核对模型"), ("classification", "批量分类模型"), ("submission", "材料准备模型")):
            row = ttk.Frame(models_frame)
            row.pack(fill="x", pady=3)
            ttk.Label(row, text=label, width=13).pack(side="left")
            combo = ttk.Combobox(row, textvariable=self.models[name], values=MODELS, state="readonly", width=18)
            combo.pack(side="left", fill="x", expand=True)
            self.controls.append(combo)
        app.button(models_frame, "保存模型偏好", self.save_models).pack(anchor="w", pady=(8, 0))
        browser_frame = ttk.LabelFrame(preferences, text='WOS 下载连接', padding=12)
        browser_frame.pack(fill='x')
        row = ttk.Frame(browser_frame)
        row.pack(fill='x')
        selector = ttk.Combobox(row, textvariable=self.browser_mode,
                               values=['浏览器技能（无需配对）','原插件配对'], state='readonly', width=23)
        selector.pack(fill='x')
        row = ttk.Frame(browser_frame)
        row.pack(fill='x', pady=(6,0))
        ttk.Label(row, text='浏览器编号').pack(side='left', padx=(0,8))
        instance = ttk.Entry(row, textvariable=self.browser_instance)
        instance.pack(side='left', fill='x', expand=True)
        self.controls.extend([selector,instance])
        browser_actions = ttk.Frame(browser_frame)
        browser_actions.pack(fill='x', pady=(8,0))
        save_browser = app.button(browser_actions, '保存下载连接', self.save_browser)
        check_browser = app.button(browser_actions, '检查下载连接', self.check_browser)
        save_browser.grid(row=0, column=0, sticky='w')
        check_browser.grid(row=0, column=1, sticky='w', padx=(8,0))
        def fit_browser_actions(event):
            # Keep the explicit labels legible in both compact and wide windows.
            stacked = event.width < save_browser.winfo_reqwidth()+check_browser.winfo_reqwidth()+8
            check_browser.grid_configure(row=1 if stacked else 0, column=0 if stacked else 1,
                                         padx=0 if stacked else (8,0), pady=(6,0) if stacked else 0)
        browser_actions.bind('<Configure>', fit_browser_actions)
        ttk.Label(browser_frame,text='技能通道：后台服务和指定浏览器均须在线。\n后台入库：仍需原插件配对。',
                  wraplength=360,style='Muted.TLabel').pack(anchor='w',pady=(8,0))
        ttk.Label(page, textvariable=self.status, wraplength=850).pack(anchor="w", pady=6)
        try:
            values = read_preferences(self.path)
            for name, value in values.items():
                self.models[name].set(value)
            self.apply_models(values)
        except SafetyStop as exc:
            self.status.set(str(exc))
        self.refresh()

    def refresh(self):
        configured = self.app.model_client.key_store.configured()
        self.key_status.set("密钥已配置 · 如需替换，请输入新密钥" if configured else "尚未配置 · 请输入校方 API Key")
        if self.app.classifier:
            self.app.classifier.key_status.set("密钥已配置" if configured else "请到设置配置密钥")

    def set_busy(self, busy):
        for control in self.controls:
            control.configure(state="disabled" if busy else "readonly" if isinstance(control, ttk.Combobox) else "normal")

    def save_key(self):
        if self.app.busy:
            return
        try:
            self.app.model_client.key_store.save(self.key.get().strip())
            self.key.set("")
            self.app.model_client.available = None
            self.app.model_panel.clear()
            self.status.set("密钥已加密保存，可测试连接。")
            self.refresh()
            self.app.note_operation("保存 API 密钥")
        except Exception:
            messagebox.showwarning("未保存", "密钥格式或加密保存失败，请检查后重试。", parent=self.app.root)

    def apply_models(self, values):
        self.app.model_panel.model.set(values["review"])
        self.app.model_panel.clear()
        if self.app.classifier:
            self.app.classifier.model.set(values["classification"])
            self.app.classifier.change_model()
        if self.app.submission_panel:
            self.app.submission_panel.model.set(values["submission"])

    def save_models(self):
        if self.app.busy:
            return
        values = {name: variable.get() for name, variable in self.models.items()}
        if any(value not in MODELS for value in values.values()):
            self.status.set("请选择支持的模型。")
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            atomic_json(self.path, values)
            self.apply_models(values)
            self.status.set("模型偏好已保存并应用；各工作页仍可临时选择模型。")
            self.app.note_operation("保存模型偏好")
        except (OSError, SafetyStop):
            self.status.set("模型偏好未保存，请检查目录权限。")

    def save_browser(self):
        if self.app.busy:
            return
        from wos_browser import validate_settings
        try:
            if self.browser_mode.get() not in {'浏览器技能（无需配对）','原插件配对'}:
                raise ValueError('请选择支持的下载连接。')
            values=validate_settings({'transport':'browser-skill' if self.browser_mode.get()=='浏览器技能（无需配对）' else 'extension',
                                      'instance_id':self.browser_instance.get().strip(),'origin':self.browser_origin})
            self.browser_path.parent.mkdir(parents=True,exist_ok=True)
            atomic_json(self.browser_path,values)
            self.status.set('WOS 下载连接已保存；下次下载生效，不影响认领或后台入库。')
            self.app.note_operation('保存 WOS 下载连接')
        except (OSError,ValueError) as exc:
            self.status.set('下载连接未保存：'+str(exc))

    def test_connection(self):
        if self.app.busy:
            return
        self.status.set("正在测试连接，不发送论文或名单…")
        def ready(models):
            self.status.set("连接成功 · " + "、".join(models) if models else "连接成功，但未返回支持的模型名。")
        self.app.run(self.app.model_client.models, ready, "测试模型服务连接…", log_action="测试模型连接")

    def check_browser(self):
        if self.app.busy:
            return
        from wos_browser import BrowserSkillWOS, validate_settings
        try:
            if self.browser_mode.get() == '原插件配对':
                online = bool(self.app.bridge and self.app.bridge.online)
                self.status.set('原插件已连接；仍需核对 WOS 页绑定。' if online else '原插件未连接，请先配对。')
                return
            if self.browser_mode.get() != '浏览器技能（无需配对）':
                raise ValueError('请选择支持的下载连接。')
            values = validate_settings({'transport':'browser-skill',
                'instance_id':self.browser_instance.get().strip(), 'origin':self.browser_origin})
        except ValueError as exc:
            self.status.set(str(exc))
            return
        def check():
            # No new Agent Window, WOS traffic, borrowing, daemon startup or secrets.
            client = BrowserSkillWOS(self.browser_path.parent.parent, values['instance_id'], values['origin'])
            return client.check_connection()
        def ready(result):
            self.status.set('Browser Skill '+result['browser_skill_version']+' 已连接；尚未检索或下载。')
        self.status.set('正在检查下载后台服务与指定浏览器，不检索论文…')
        self.app.run(check, ready, '检查 WOS 下载连接…', log_action='检查 WOS 下载连接')
