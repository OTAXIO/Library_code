"""Small desktop entry point for the independent batch classification workflow."""
import os
import json
import queue
import threading
import time
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from core import SafetyStop
from model_review import DEFAULT_MODEL, MODELS, KeyStore, limits
from paper_classify import BASE, ClassificationClient, list_owners, read_papers, run
from ui_theme import P, install_theme, style_text


class ClassifyApp:
    def __init__(self, root, parent=None, on_busy=None, on_review=None, on_export=None,
                 on_settings=None, on_export_skipped=None, on_import=None,on_export_selected=None):
        self.root = root
        self.embedded = parent is not None
        self.on_busy = on_busy
        self.on_review = on_review
        self.on_export = on_export
        self.on_export_skipped = on_export_skipped
        self.on_import = on_import
        self.on_export_selected = on_export_selected
        self.on_settings = on_settings
        self.external_busy = False
        self.queue = queue.Queue()
        self.stop = threading.Event()
        self.busy = False
        self.exporting = False
        self.export_generation = 0
        self._export_message = ''
        self._export_started = 0.0
        self._export_bar_value = 0.0
        self.close_pending = False
        self.store = KeyStore(BASE/'runtime')
        self.output = BASE/'runtime'/'classification'
        self.result_folder = None
        self.records = []
        self.roster_hash = None
        self.scope_count = 0
        self.owner = tk.StringVar()
        self.poll_id = None
        if not self.embedded:
            root.title('论文工作台 · AI 分类与导入渠道')
            root.geometry('1120x800')
            root.minsize(920,680)
            root.protocol('WM_DELETE_WINDOW',self.close)
        install_theme(root)
        page = ttk.Frame(parent if self.embedded else root,padding=16 if self.embedded else 22)
        page.pack(fill='both',expand=True)
        header=ttk.Frame(page)
        header.pack(fill='x')
        ttk.Label(header,text='论文分类工作台',style='Title.TLabel').pack(side='left')
        self.key_status=tk.StringVar(value='密钥已配置' if self.store.configured() else '请配置密钥')
        ttk.Label(header,textvariable=self.key_status,style='Muted.TLabel').pack(side='right')
        ttk.Label(page,text='AI 分类辅助选择数据库；WOS 下载可直接开始，无需先分类。分类进度自动保存。',style='Muted.TLabel').pack(anchor='w',pady=(4,14))
        self.summary = tk.StringVar()
        self.metrics={name:tk.StringVar(value='—') for name in ('名单记录','分类任务','已处理','渠道待判定','分类失败')}
        cards=ttk.Frame(page)
        cards.pack(fill='x',pady=(0,14))
        for column,(name,value) in enumerate(self.metrics.items()):
            cards.columnconfigure(column,weight=1,uniform='cards')
            card=ttk.Frame(cards,style='Card.TFrame',padding=(16,10))
            card.grid(row=0,column=column,sticky='ew',padx=(0,10 if column<len(self.metrics)-1 else 0))
            # Classification progress is not the backend's processed/completed flag.
            label={'已处理':'已分类','渠道待判定':'数据库待确认'}.get(name,name)
            ttk.Label(card,text=label,style='Card.TLabel').pack(anchor='w')
            ttk.Label(card,textvariable=value,style='Metric.TLabel').pack(anchor='w',pady=(3,0))
        settings = ttk.Frame(page)
        settings.pack(fill='x')
        ttk.Label(settings,text='负责人').pack(side='left')
        self.owner_box = ttk.Combobox(settings,textvariable=self.owner,state='readonly',width=10)
        self.owner_box.pack(side='left',padx=8)
        self.owner_box.bind('<<ComboboxSelected>>',self.change_owner)
        ttk.Label(settings,text='分类模型').pack(side='left',padx=(6,0))
        self.model = tk.StringVar(value='deepseek-chat')
        self.combo = ttk.Combobox(settings,textvariable=self.model,values=MODELS,state='readonly',width=22)
        self.combo.pack(side='left',padx=8)
        self.key_button = ttk.Button(settings,text='API 设置' if on_settings else '密钥设置',command=self.configure_key)
        self.key_button.pack(side='left')
        ttk.Label(settings,text='AI 每批').pack(side='left',padx=(14,0))
        self.batch = tk.StringVar(value=str(limits(self.model.get())['batch_size']))
        self.batch_combo = ttk.Combobox(settings,textvariable=self.batch,values=('1','2','3','5','8','10'),
                                        state='readonly',width=4)
        self.batch_combo.pack(side='left',padx=8)
        self.combo.bind('<<ComboboxSelected>>',self.change_model)
        actions = ttk.Frame(settings)
        actions.pack(side='right')
        self.start_button = ttk.Button(actions,text='开始 / 继续分类',command=self.start,style='Primary.TButton')
        self.start_button.pack(side='left')
        self.stop_button = ttk.Button(actions,text='当前步骤后暂停',command=self.request_stop,state='disabled')
        self.stop_button.pack(side='left',padx=8)
        ttk.Label(page,textvariable=self.summary,style='Muted.TLabel').pack(anchor='w',pady=(10,4))
        self.status = tk.StringVar(value=f'准备就绪；{self.model.get()} 默认每批 {limits(self.model.get())["batch_size"]} 篇。')
        self.status_label=ttk.Label(page,textvariable=self.status,wraplength=1000)
        self.status_label.pack(anchor='w',pady=(0,6))
        self.bar = ttk.Progressbar(page,mode='determinate',maximum=100)
        self.bar.pack(fill='x',pady=(0,14))
        filters=ttk.Frame(page)
        filters.pack(fill='x',pady=(0,8))
        ttk.Label(filters,text='搜索题名 / DOI').pack(side='left')
        self.search=tk.StringVar()
        ttk.Entry(filters,textvariable=self.search,width=26).pack(side='left',padx=(8,14),fill='x',expand=True)
        self.channel=tk.StringVar(value='全部渠道')
        from import_channels import CHANNELS
        ttk.Combobox(filters,textvariable=self.channel,values=['全部渠道',*CHANNELS,'待判定','未处理','分类失败'],state='readonly',width=14).pack(side='left')
        self.search.trace_add('write',lambda *_:self.filter_results())
        self.channel.trace_add('write',lambda *_:self.filter_results())
        self.count_text=tk.StringVar(value='暂无结果')
        ttk.Label(filters,textvariable=self.count_text,style='Muted.TLabel').pack(side='right',padx=(12,0))
        # The bottom rows are packed before the expanding pane below. Tk's packer hands
        # out space in packing order, so a pane packed first would claim the whole
        # cavity and push the footer buttons off the edge -- where they are not merely
        # hidden but unmapped, which is how the WOS download button disappeared.
        ttk.Label(page,text='分类会使用模型额度；AI 建议不代表数据库已收录。下载 TXT 不等于已导入，也不包含论文 PDF。',style='Muted.TLabel').pack(side='bottom',anchor='w',pady=(8,0))
        footer=ttk.Frame(page)
        footer.pack(side='bottom',fill='x',pady=(12,0))
        self.batch_scope=tk.StringVar()
        if self.on_export or self.on_export_skipped or self.on_import:
            self.scope_label=ttk.Label(footer,textvariable=self.batch_scope,style='Muted.TLabel',wraplength=900)
            self.scope_label.pack(fill='x',pady=(0,6))
            batch_actions=ttk.Frame(footer)
            batch_actions.pack(fill='x',pady=(0,8))
            self._batch_buttons=[]
            if self.on_export:
                self.export_button=ttk.Button(batch_actions,text='下载待补论文 TXT（WOS）',command=self.export_wos,style='Primary.TButton')
                self._batch_buttons.append(self.export_button)
            if self.on_export_skipped:
                self.skipped_export_button=ttk.Button(batch_actions,text='重试跳过论文（WOS）',command=self.export_skipped_wos)
                self._batch_buttons.append(self.skipped_export_button)
            if self.on_import:
                self.import_button=ttk.Button(batch_actions,text='检查 TXT 并导入',command=self.open_import)
                self._batch_buttons.append(self.import_button)
            self._action_row(batch_actions,self._batch_buttons)
        # Reports and single-paper review are separate from whole-owner batch actions.
        reports=ttk.Frame(footer)
        reports.pack(fill='x')
        self.report_button=ttk.Button(reports,text='分类结果报告',command=lambda:self.open_report('分类建议.md'))
        self.channel_button=ttk.Button(reports,text='按数据库分组',command=lambda:self.open_report('导入渠道分类.md'))
        self.output_button=ttk.Button(reports,text='打开分类文件夹',command=self.open_output)
        report_buttons=[self.report_button,self.channel_button,self.output_button]
        if self.on_review:
            self.review_button=ttk.Button(reports,text='人工核对所选论文',command=self.review_selected)
            report_buttons.append(self.review_button)
        if self.on_export_selected:
            self.selected_export_button=ttk.Button(reports,text='试下载所选论文 TXT',command=self.export_selected_wos)
            report_buttons.append(self.selected_export_button)
        self._action_row(reports,report_buttons)
        footer.bind('<Configure>',lambda event:self._wrap_scope(event.width))
        panes=ttk.Panedwindow(page,orient='vertical')
        panes.pack(fill='both',expand=True)
        table=ttk.Frame(panes)
        panes.add(table,weight=3)
        self.tree=ttk.Treeview(table,columns=('rows','title','type','channel','state'),show='headings',selectmode='browse',height=8)
        for key,title,width in [('rows','原表行号',85),('title','论文题名',430),('type','成果类型',100),('channel','推荐数据库',100),('state','分类进度',120)]:
            self.tree.heading(key,text=title)
            self.tree.column(key,width=width,minwidth=60,stretch=key=='title')
        scroll=ttk.Scrollbar(table,orient='vertical',command=self.tree.yview)
        horizontal=ttk.Scrollbar(table,orient='horizontal',command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll.set,xscrollcommand=horizontal.set)
        horizontal.pack(side='bottom',fill='x')
        scroll.pack(side='right',fill='y')
        self.tree.pack(fill='both',expand=True)
        self.tree.tag_configure('pending',foreground=P.amber)
        self.tree.bind('<<TreeviewSelect>>',self.show_detail)
        detail=ttk.Frame(panes,padding=(0,10,0,0))
        panes.add(detail,weight=1)
        ttk.Label(detail,text='论文详情 · 推荐依据',font=('Microsoft YaHei UI',10,'bold')).pack(anchor='w',pady=(0,4))
        self.detail=tk.Text(detail,height=5,wrap='word',font=('Microsoft YaHei UI',10),background='white',foreground='#334155',relief='flat',padx=12,pady=8,state='disabled')
        style_text(self.detail)
        ds=ttk.Scrollbar(detail,command=self.detail.yview)
        self.detail.configure(yscrollcommand=ds.set)
        ds.pack(side='right',fill='y')
        self.detail.pack(fill='both',expand=True)
        self.set_detail('选择一篇论文，查看完整题名、分类理由、推荐入口和待确认条件。')
        self.reload()
        self.load_results()
        self.poll_id=root.after(150,self.poll)
        root.bind('<Destroy>',lambda event:self.dispose() if event.widget is root else None,add='+')

    @staticmethod
    def _action_row(frame,buttons):
        """Keep both workflow and report controls visible in the compact window."""
        for column,button in enumerate(buttons):
            frame.columnconfigure(column,weight=1,uniform='actions')
            button.grid(row=0,column=column,sticky='ew',padx=(0,8 if column<len(buttons)-1 else 0))

    def _wrap_scope(self,width):
        if hasattr(self,'scope_label'):
            self.scope_label.configure(wraplength=max(200,width))
        self.status_label.configure(wraplength=max(200,width))

    def _sync_scope(self):
        owner=self.owner.get().strip()
        if not owner:
            self.batch_scope.set('请先选择负责人。下载和导入不会跨负责人处理。')
            return
        self.batch_scope.set(
            f'批量范围：{owner}的未完成、零匹配记录；不受表格筛选或选中行影响。\n'
            '待补下载：未跳过的论文（不限 AI 推荐数据库）；重试下载：“是否识别”为 2 的论文。')

    def reload(self):
        try:
            owners = list_owners(BASE/'list.xlsx')
            self.owner_box['values'] = owners
            if self.owner.get() not in owners:
                self.owner.set('')
            if not self.owner.get():
                self.roster_hash = None
                self.scope_count = 0
                self.metrics['名单记录'].set('—')
                self.metrics['分类任务'].set('—')
                self.summary.set('请选择负责人。分类与 WOS 下载都不会跨负责人处理。')
                self._sync_start_button()
                return
            data = read_papers(BASE/'list.xlsx', owner=self.owner.get())
            self.roster_hash=data.get('sha256')
            self.scope_count=sum(len(p['rows']) for p in data['papers'])
            self.metrics['名单记录'].set(str(self.scope_count))
            self.metrics['分类任务'].set(str(len(data['papers'])))
            self.summary.set(f'{self.owner.get()}：{self.scope_count} 条记录，'
                             f'合并为 {len(data["papers"])} 个题名/DOI 组合。')
        except (SafetyStop, OSError):
            self.roster_hash=None
            self.scope_count=0
            self.metrics['名单记录'].set('—')
            self.metrics['分类任务'].set('—')
            self.summary.set('名单未就绪：请将包含“题名”列的 list.xlsx 放在程序目录。')
        self._sync_start_button()

    def change_owner(self, _event=None):
        if self.busy or self.external_busy:
            return
        self.records=[]
        self.result_folder=None
        self.reload()
        self.load_results()

    def configure_key(self):
        if self.busy or self.external_busy:
            return
        if self.on_settings:
            self.on_settings()
            return
        key = simpledialog.askstring('交大模型密钥','输入密钥（仅使用 Windows 加密保存）',show='*',parent=self.root)
        if key:
            try:
                self.store.save(key)
                self.status.set('密钥已加密保存。')
                self.key_status.set('密钥已配置')
            except SafetyStop as exc:
                messagebox.showerror('无法保存密钥',str(exc),parent=self.root)

    def change_model(self,_event=None):
        # Reasoning models cannot answer a large batch inside the request timeout, so
        # the per-model default batch is restored whenever the model changes.
        self.batch.set(str(limits(self.model.get())['batch_size']))
        self.status.set(f'{self.model.get()}：默认每批 {limits(self.model.get())["batch_size"]} 篇，'
                        f'单次请求最长约 {limits(self.model.get())["request_timeout"]} 秒。')
        self.load_results()

    def _sync_start_button(self):
        self._sync_scope()
        if hasattr(self,'start_button'):
            ready=bool(self.owner.get() and self.scope_count and not self.busy and not self.external_busy)
            self.start_button.configure(state='normal' if ready else 'disabled')

    def _sync_export_button(self):
        ready=(bool(self.owner.get() and self.scope_count) and not self.busy and not self.external_busy
               and not self.exporting)
        if hasattr(self,'export_button'):
            self.export_button.configure(state='normal' if ready else 'disabled')
        if hasattr(self,'skipped_export_button'):
            self.skipped_export_button.configure(state='normal' if ready else 'disabled')
        if hasattr(self,'selected_export_button'):
            self.selected_export_button.configure(state='normal' if ready else 'disabled')
        if hasattr(self,'import_button'):
            # Previously downloaded files can be imported without a classification
            # result for the currently selected model.
            available=bool(self.owner.get().strip() and not self.busy and
                           not self.external_busy and not self.exporting)
            self.import_button.configure(state='normal' if available else 'disabled')

    def open_import(self):
        if not self.on_import or self.busy or self.external_busy or self.exporting:
            return
        if not self.owner.get().strip():
            self.status.set('请先选择负责人，再检查此负责人的 TXT 导入文件。')
            return
        self.on_import()

    def export_wos(self):
        if self.busy or self.external_busy or self.exporting or not self.on_export:
            return
        if not self.owner.get().strip():
            messagebox.showinfo('请选择负责人','请选择负责人。',parent=self.root)
            return
        # The batch reuses this same stop flag, so the pause also halts an export
        # run instead of leaving the window disabled with no way out.
        self.stop.clear()
        self.status.set('正在检查名单和浏览器连接…')
        self.on_export()
        if not self.exporting:
            self.status.set('下载未开始；请按弹窗提示检查负责人、名单范围和浏览器连接。')

    def export_skipped_wos(self):
        if self.busy or self.external_busy or self.exporting or not self.on_export_skipped:
            return
        if not self.owner.get().strip():
            messagebox.showinfo('请选择负责人','请选择负责人。',parent=self.root)
            return
        self.stop.clear()
        self.status.set('正在检查跳过项和浏览器连接…')
        self.on_export_skipped()
        if not self.exporting:
            self.status.set('下载未开始；请按弹窗提示检查负责人、跳过项和浏览器连接。')

    def export_selected_wos(self):
        if self.busy or self.external_busy or self.exporting or not self.on_export_selected:
            return
        selected=self.tree.selection()
        if not selected or not self.owner.get().strip():
            self.status.set('请先选择负责人，并在分类结果表格中选择一篇论文。')
            return
        self.stop.clear()
        self.status.set('正在检查所选论文及浏览器连接…')
        self.on_export_selected(self.records[int(selected[0])])
        if not self.exporting:
            self.status.set('下载未开始；请按提示检查所选论文及浏览器连接。')

    def set_exporting(self,value):
        """A batch export runs through the app's shared busy state, not set_busy()."""
        if value and not self.exporting:
            self.export_generation+=1
            self._export_bar_value=float(self.bar['value'])
            self._export_message='正在准备 WOS 下载队列'
            self._export_started=time.monotonic()
            self.bar.configure(mode='indeterminate',value=0)
            self.bar.start(80)
        elif not value and self.exporting:
            self.bar.stop()
            self.bar.configure(mode='determinate',value=self._export_bar_value)
            self._export_message=''
        self.exporting=bool(value)
        self._render_export_status()
        self.stop_button.configure(state='normal' if (self.exporting or self.busy) else 'disabled')
        self._sync_export_button()

    def _render_export_status(self):
        if self.exporting:
            elapsed=max(0,int(time.monotonic()-self._export_started))
            pause=' · 已请求暂停，当前步骤返回后停止' if self.stop.is_set() else ''
            self.status.set(f'{self._export_message} · 已等待 {elapsed} 秒'+pause)

    def set_busy(self,value):
        self.busy = value
        if not value:
            self.set_exporting(False)
        self.key_button.configure(state='disabled' if value else 'normal')
        self.owner_box.configure(state='disabled' if value else 'readonly')
        self.combo.configure(state='disabled' if value else 'readonly')
        self.batch_combo.configure(state='disabled' if value else 'readonly')
        self.stop_button.configure(state='normal' if (value or self.exporting) else 'disabled')
        self.start_button.configure(text='正在分类…' if value else '开始 / 继续分类')
        self._sync_start_button()
        self._sync_export_button()
        if self.on_busy:
            self.on_busy(value)

    def set_external_busy(self,value):
        self.external_busy=value and not self.busy
        if not value:
            self.set_exporting(False)
        if not self.busy:
            self.key_button.configure(state='disabled' if value else 'normal')
            self.owner_box.configure(state='disabled' if value else 'readonly')
            self.combo.configure(state='disabled' if value else 'readonly')
            self.batch_combo.configure(state='disabled' if value else 'readonly')
        self.stop_button.configure(state='normal' if (self.exporting or self.busy) else 'disabled')
        if hasattr(self,'review_button'):
            self.review_button.configure(state='disabled' if value else 'normal')
        self._sync_start_button()
        self._sync_export_button()

    def start(self):
        if self.busy or self.external_busy:
            return
        owner=self.owner.get().strip()
        if not owner:
            messagebox.showinfo('请选择负责人','请选择负责人。',parent=self.root)
            return
        if not self.store.configured():
            self.configure_key()
            if not self.store.configured():
                return
        self.reload()
        if not self.scope_count:
            self.status.set('当前负责人没有可分类记录。')
            return
        self.stop.clear()
        self.set_busy(True)
        self.status.set('正在准备名单及已保存进度…')
        model = self.model.get()
        try:
            batch_size=int(self.batch.get())
        except ValueError:
            batch_size=limits(model)['batch_size']
            self.batch.set(str(batch_size))
        def worker():
            try:
                folder = run(model=model,batch_size=batch_size,client=ClassificationClient(self.store),stop=self.stop,
                             progress=lambda value:self.queue.put(('progress',value)),resilient=True,
                             owner=owner)
                self.queue.put(('done',folder))
            except SafetyStop as exc:
                self.queue.put(('error',str(exc)))
            except Exception:
                self.queue.put(('error','读取名单或保存结果失败，请检查文件格式、文件占用和目录权限。已保存的批次可以继续。'))
        threading.Thread(target=worker,daemon=True).start()

    def poll(self):
        if self.poll_id:
            self.root.after_cancel(self.poll_id)
            self.poll_id=None
        try:
            while True:
                kind,value = self.queue.get_nowait()
                if kind=='progress':
                    self.status.set(value.split('；结果')[0])
                    self.load_results(update_status=False)
                elif kind=='download_progress':
                    generation,message,started=value
                    # A completed run can leave queued updates behind. They must
                    # not overwrite its summary, a later download, or AI progress.
                    if self.exporting and generation==self.export_generation:
                        self._export_message=message
                        self._export_started=started
                else:
                    self.set_busy(False)
                    if kind=='done':
                        self.result_folder = value
                        self.load_results(update_status=False,folder=value)
                    else:
                        self.status.set('已停止：'+value)
                    if self.close_pending:
                        self.root.destroy()
                        return
        except queue.Empty:
            pass
        self._render_export_status()
        self.poll_id=self.root.after(150,self.poll)

    def load_results(self,update_status=True,folder=None):
        candidates=[Path(folder)/'分类结果.json'] if folder else sorted(self.output.glob('*/分类结果.json'),key=lambda p:p.stat().st_mtime,reverse=True)
        found=None
        for path in candidates:
            try:
                data=json.loads(path.read_text(encoding='utf-8'))
                source=data.get('source',{})
                if (data.get('model')!=self.model.get() or not self.roster_hash or
                        source.get('sha256')!=self.roster_hash or source.get('owner')!=self.owner.get() or
                        source.get('pending_only') is not False):
                    continue
                if not isinstance(data.get('records'),list):
                    continue
                found=(path,data)
                break
            except (OSError,ValueError):
                continue
        if found:
            path,data=found
            self.result_folder=path.parent
            self.records=data['records']
        elif not self.busy:
            self.records=[]
            if folder is None:
                self.result_folder=None
        done=sum(bool(x.get('classification')) for x in self.records)
        pending=sum(bool(x.get('classification')) and not (x.get('import_route') or {}).get('recommended_channel') for x in self.records)
        failed=sum(bool(not x.get('classification') and x.get('failure')) for x in self.records)
        self.metrics['已处理'].set(str(done))
        self.metrics['渠道待判定'].set(str(pending))
        self.metrics['分类失败'].set(str(failed))
        value=100*done/len(self.records) if self.records else 0
        if self.exporting:
            self._export_bar_value=value
        else:
            self.bar['value']=value
        if update_status:
            if self.records:
                self.status.set(f'已加载保存结果 · {done}/{len(self.records)} 个任务'
                                + (f'，其中 {failed} 个分类失败' if failed else ''))
            else:
                self.status.set('请选择负责人。' if not self.owner.get() else
                                '暂无当前负责人及模型的结果，点击开始分类。')
        for button,filename in ((self.report_button,'分类建议.md'),(self.channel_button,'导入渠道分类.md')):
            button.configure(state='normal' if self.result_folder and (self.result_folder/filename).is_file() else 'disabled')
        self._sync_export_button()
        self._sync_start_button()
        self.filter_results()

    def filter_results(self):
        selected=self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        query=self.search.get().strip().casefold()
        category=self.channel.get()
        for index,item in enumerate(self.records):
            result=item.get('classification') or {}
            route=item.get('import_route') or {}
            if result:
                channel=route.get('recommended_channel') or '待判定'
            else:
                channel='分类失败' if item.get('failure') else '未处理'
            if query not in (item.get('title','')+' '+item.get('doi','')).casefold() or (category!='全部渠道' and category!=channel):
                continue
            self.tree.insert('', 'end',iid=str(index),values=('、'.join(map(str,item['rows'])),item['title'],result.get('type') or '待判定',channel,item.get('status','未处理')),tags=('pending',) if channel in ('待判定','未处理','分类失败') else ())
        self.count_text.set(f'{len(self.tree.get_children())} / {len(self.records)} 项')
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected[0])
            self.show_detail()
        else:
            self.set_detail('选择一篇论文查看详情。' if self.tree.get_children() else '没有匹配的结果。可调整搜索词或渠道筛选。')

    def set_detail(self,text):
        self.detail.configure(state='normal')
        self.detail.delete('1.0','end')
        self.detail.insert('1.0',text)
        self.detail.configure(state='disabled')

    def show_detail(self,_event=None):
        selected=self.tree.selection()
        if not selected:
            return
        item=self.records[int(selected[0])]
        result=item.get('classification') or {}
        route=item.get('import_route') or {}
        failure=item.get('failure') or {}
        lines=[item['title'],f"DOI：{item.get('doi') or '未提供'}"]
        if failure:
            lines.extend(['状态：分类失败（本轮未采纳，下次运行会自动重试）',
                          '失败原因：'+str(failure.get('error') or '未记录'),
                          '原表行号：'+'、'.join(map(str,item.get('rows',[])))])
        else:
            lines.extend([f"成果类型：{result.get('type') or '待判定'} · 推荐渠道：{route.get('recommended_channel') or '待判定'}",
                          '分类理由：'+result.get('reason','尚未处理'),
                          '渠道理由：'+route.get('reason','尚未提供'),
                          '可用入口：'+('；'.join(route.get('available_buttons',[])) or '待确认'),
                          '待补材料：'+('；'.join(result.get('missing_evidence',[])) or '未列出；仍需核实')])
        self.set_detail('\n'.join(lines))

    def open_report(self,filename):
        if self.result_folder and (self.result_folder/filename).is_file():
            try:
                os.startfile(self.result_folder/filename)
            except OSError:
                messagebox.showerror('无法打开','请点击“打开分类文件夹”查看文件。',parent=self.root)

    def request_stop(self):
        self.stop.set()
        self.stop_button.configure(state='disabled')
        if self.exporting:
            self._render_export_status()
        else:
            self.status.set('已请求停止；当前批次返回后保存结果，不再提交下一批。')

    def review_selected(self):
        if self.busy or self.external_busy:
            return
        selected=self.tree.selection()
        if not selected:
            self.status.set('请先在表格中选择一篇论文。')
            return
        self.on_review(self.records[int(selected[0])])

    def dispose(self):
        if self.poll_id:
            self.root.after_cancel(self.poll_id)
            self.poll_id=None

    def open_output(self):
        folder = self.result_folder or self.output
        folder.mkdir(parents=True,exist_ok=True)
        os.startfile(folder)

    def close(self):
        if self.busy:
            self.close_pending = True
            self.request_stop()
            self.status.set(f'正在等待当前批次保存，随后关闭窗口（模型请求最长约 {limits(self.model.get())["request_timeout"]} 秒）。')
        else:
            self.root.destroy()


if __name__=='__main__':
    from app import main
    main(initial_tab='classification')
