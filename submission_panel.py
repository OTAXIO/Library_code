"""Integrated, explicitly started zero-match material preparation page."""
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, filedialog, messagebox
from model_review import MODELS, KeyStore
from paper_classify import BASE
from core import SafetyStop
from submission_prepare import prepare, templates, zero_roster


class SubmissionPanel:
    def __init__(self,app,page):
        self.app=app
        self.busy=False
        self.stop=threading.Event()
        self.events=queue.Queue()
        self.folder=BASE/'runtime'/'submission'
        self.template_dir=tk.StringVar(value=str(BASE/'templates'))
        self.manifest=tk.StringVar()
        self.model=tk.StringVar(value='deepseek-chat')
        self.retry=tk.BooleanVar(value=False)
        self.status=tk.StringVar(value='只准备匹配数量为 0 的记录。不会自动上传或标记完成。')
        self.controls=[]
        ttk.Label(page,text='零匹配论文 · 提交材料准备',style='Title.TLabel').pack(anchor='w',pady=(4,12))
        ttk.Label(page,text='筛选零匹配 → 优先下载数据库原始文件 → 无法下载时 API 分类填模板 → 导出材料与来源总表',wraplength=900).pack(anchor='w',pady=(0,16))
        self.path_row(page,'模板目录（可扩展）',self.template_dir,True)
        self.path_row(page,'补充来源配置（可选）',self.manifest,False)
        options=ttk.Frame(page)
        options.pack(fill='x',pady=12)
        ttk.Label(options,text='填写模型').pack(side='left')
        self.model_box=ttk.Combobox(options,textvariable=self.model,values=MODELS,state='readonly',width=22)
        self.model_box.pack(side='left',padx=8)
        retry=ttk.Checkbutton(options,text='重新处理待补资料的条目',variable=self.retry)
        retry.pack(side='left',padx=8)
        self.controls.append(retry)
        actions=ttk.Frame(page)
        actions.pack(fill='x',pady=10)
        for text,command in [('检查名单与模板',self.preview),('开始 / 继续准备',self.start),('密钥设置',app.model_panel.configure_key)]:
            button=ttk.Button(actions,text=text,command=command)
            button.pack(side='left',padx=(0,8))
            self.controls.append(button)
        self.stop_button=ttk.Button(actions,text='当前条目后停止',command=self.request_stop,state='disabled')
        self.stop_button.pack(side='left')
        ttk.Button(actions,text='打开材料目录',command=self.open_folder).pack(side='right')
        ttk.Label(page,textvariable=self.status,wraplength=920).pack(anchor='w',pady=12)
        ttk.Label(page,text='字段齐备：保存到“字段齐备”，仍需核验本校归属和本库查重。\n缺少作者、单位、日期、出处等关键资料：保存到“待补草稿”，不混入齐备文件。\n模型字段必须引用来源；无依据不补造。原始 WOS/CNKI 等导出文件只从配置中的真实地址下载。\n运行会调用交大模型 API 和公开元数据服务；名单负责人、工号和内部平台编号不发送给 AI。',wraplength=920,style='Muted.TLabel').pack(anchor='w',pady=12)
        self.timer=app.root.after(150,self.poll)

    def path_row(self,page,label,variable,directory):
        row=ttk.Frame(page)
        row.pack(fill='x',pady=5)
        ttk.Label(row,text=label,width=24).pack(side='left')
        entry=ttk.Entry(row,textvariable=variable)
        entry.pack(side='left',fill='x',expand=True,padx=8)
        def choose():
            value=filedialog.askdirectory(parent=self.app.root) if directory else filedialog.askopenfilename(parent=self.app.root,filetypes=[('JSON','*.json')])
            if value:
                variable.set(value)
        button=ttk.Button(row,text='选择',command=choose)
        button.pack(side='right')
        self.controls.extend([entry,button])

    def set_external_busy(self,value):
        for control in self.controls:
            control.configure(state='disabled' if value else 'normal')
        self.model_box.configure(state='disabled' if value else 'readonly')
        self.stop_button.configure(state='normal' if self.busy and not self.stop.is_set() else 'disabled')

    def preview(self):
        if self.app.busy:
            return
        try:
            roster=zero_roster(BASE/'list.xlsx')
            spec=templates(self.template_dir.get())
            self.status.set(f'匹配数为0：{sum(len(p["rows"]) for p in roster["papers"])} 行 / {len(roster["papers"])} 个任务；发现 {len(spec)} 类模板，结构检查通过。未联网或调用 AI。')
        except Exception as exc:
            self.status.set(str(exc) if isinstance(exc,SafetyStop) else '无法读取名单或模板，请检查路径。')

    def start(self):
        if self.app.busy:
            return
        options={'template_dir':Path(self.template_dir.get()),'model':self.model.get(),
                 'manifest':Path(self.manifest.get()) if self.manifest.get().strip() else None,
                 'retry_incomplete':self.retry.get()}
        self.stop.clear()
        self.busy=True
        self.app.set_busy(True)
        self.status.set('正在读取零匹配队列并准备材料…')
        def worker():
            try:
                folder=prepare(**options,stop=self.stop,progress=lambda text:self.events.put(('progress',text)))
                self.events.put(('done',folder))
            except Exception as exc:
                self.events.put(('error',str(exc) if isinstance(exc,SafetyStop) else '本地读写或配置错误；已保存的材料可继续。'))
        threading.Thread(target=worker,daemon=True).start()

    def request_stop(self):
        self.stop.set()
        self.stop_button.configure(state='disabled')
        self.status.set('已请求停止，将保存当前条目后结束。')

    def poll(self):
        try:
            while True:
                kind,value=self.events.get_nowait()
                if kind=='progress':
                    self.status.set(value)
                else:
                    self.busy=False
                    self.app.set_busy(False)
                    if kind=='done':
                        self.folder=value
                    else:
                        self.status.set(value)
        except queue.Empty:
            pass
        self.timer=self.app.root.after(150,self.poll)

    def open_folder(self):
        self.folder.mkdir(parents=True,exist_ok=True)
        os.startfile(self.folder)

    def dispose(self):
        self.app.root.after_cancel(self.timer)
