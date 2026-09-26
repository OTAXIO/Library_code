"""Small desktop entry point for the independent batch classification workflow."""
import os
import json
import queue
import threading
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from core import SafetyStop
from model_review import DEFAULT_MODEL, MODELS, KeyStore, limits
from paper_classify import BASE, ClassificationClient, read_papers, run


class ClassifyApp:
    def __init__(self, root, parent=None, on_busy=None, on_review=None, on_export=None):
        self.root = root
        self.embedded = parent is not None
        self.on_busy = on_busy
        self.on_review = on_review
        self.on_export = on_export
        self.external_busy = False
        self.queue = queue.Queue()
        self.stop = threading.Event()
        self.busy = False
        self.exporting = False
        self.close_pending = False
        self.store = KeyStore(BASE/'runtime')
        self.output = BASE/'runtime'/'classification'
        self.result_folder = None
        self.records = []
        self.roster_hash = None
        self.poll_id = None
        if not self.embedded:
            root.title('论文工作台 · AI 分类与导入渠道')
            root.geometry('1120x800')
            root.minsize(920,680)
            root.configure(bg='#f3f5f9')
            root.protocol('WM_DELETE_WINDOW',self.close)
        style=ttk.Style(root)
        style.theme_use('clam')
        style.configure('TFrame',background='#f3f5f9')
        style.configure('TLabel',background='#f3f5f9',foreground='#22334a',font=('Microsoft YaHei UI',10))
        style.configure('TButton',font=('Microsoft YaHei UI',10),padding=(12,7))
        style.configure('TCombobox',padding=5,font=('Microsoft YaHei UI',10))
        style.configure('TEntry',padding=6)
        style.configure('Card.TFrame',background='white')
        style.configure('Card.TLabel',background='white')
        style.configure('Metric.TLabel',background='white',font=('Microsoft YaHei UI',23,'bold'),foreground='#163d71')
        style.configure('Muted.TLabel',foreground='#65758b')
        style.configure('Title.TLabel',font=('Microsoft YaHei UI',21,'bold'),foreground='#142c4a')
        style.configure('Primary.TButton',background='#2563eb',foreground='white',borderwidth=0,padding=(18,9))
        style.map('Primary.TButton',background=[('disabled','#dbe3ee'),('active','#1d4ed8')],foreground=[('disabled','#718096')])
        style.configure('Treeview',rowheight=33,font=('Microsoft YaHei UI',10),background='white',fieldbackground='white',borderwidth=0)
        style.configure('Treeview.Heading',font=('Microsoft YaHei UI',10,'bold'),padding=8,background='#eaf0f8')
        style.map('Treeview',background=[('selected','#dbeafe')],foreground=[('selected','#173f77')])
        style.configure('Horizontal.TProgressbar',background='#2563eb',troughcolor='#e2e8f0',borderwidth=0)
        page = ttk.Frame(parent if self.embedded else root,padding=16 if self.embedded else 22)
        page.pack(fill='both',expand=True)
        header=ttk.Frame(page)
        header.pack(fill='x')
        ttk.Label(header,text='论文分类工作台',style='Title.TLabel').pack(side='left')
        self.key_status=tk.StringVar(value='密钥已配置' if self.store.configured() else '请配置密钥')
        ttk.Label(header,textvariable=self.key_status,style='Muted.TLabel').pack(side='right')
        ttk.Label(page,text='成果类型与导入渠道，一处查看。每批自动保存，随时继续。',style='Muted.TLabel').pack(anchor='w',pady=(4,14))
        self.summary = tk.StringVar()
        self.metrics={name:tk.StringVar(value='—') for name in ('名单记录','分类任务','已处理','渠道待判定','分类失败')}
        cards=ttk.Frame(page)
        cards.pack(fill='x',pady=(0,14))
        for column,(name,value) in enumerate(self.metrics.items()):
            cards.columnconfigure(column,weight=1,uniform='cards')
            card=ttk.Frame(cards,style='Card.TFrame',padding=(16,10))
            card.grid(row=0,column=column,sticky='ew',padx=(0,10 if column<len(self.metrics)-1 else 0))
            ttk.Label(card,text=name,style='Card.TLabel').pack(anchor='w')
            ttk.Label(card,textvariable=value,style='Metric.TLabel').pack(anchor='w',pady=(3,0))
        self.reload()
        settings = ttk.Frame(page)
        settings.pack(fill='x')
        ttk.Label(settings,text='分类模型').pack(side='left')
        self.model = tk.StringVar(value='deepseek-chat')
        self.combo = ttk.Combobox(settings,textvariable=self.model,values=MODELS,state='readonly',width=22)
        self.combo.pack(side='left',padx=8)
        self.key_button = ttk.Button(settings,text='密钥设置',command=self.configure_key)
        self.key_button.pack(side='left')
        ttk.Label(settings,text='每批').pack(side='left',padx=(14,0))
        self.batch = tk.StringVar(value=str(limits(self.model.get())['batch_size']))
        self.batch_combo = ttk.Combobox(settings,textvariable=self.batch,values=('1','2','3','5','8','10'),
                                        state='readonly',width=4)
        self.batch_combo.pack(side='left',padx=8)
        self.combo.bind('<<ComboboxSelected>>',self.change_model)
        actions = ttk.Frame(settings)
        actions.pack(side='right')
        self.start_button = ttk.Button(actions,text='开始 / 继续分类',command=self.start,style='Primary.TButton')
        self.start_button.pack(side='left')
        self.stop_button = ttk.Button(actions,text='本批完成后停止',command=self.request_stop,state='disabled')
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
        panes=ttk.Panedwindow(page,orient='vertical')
        panes.pack(fill='both',expand=True)
        table=ttk.Frame(panes)
        panes.add(table,weight=3)
        self.tree=ttk.Treeview(table,columns=('rows','title','type','channel','state'),show='headings',selectmode='browse',height=8)
        for key,title,width in [('rows','原表行号',85),('title','论文题名',430),('type','成果类型',100),('channel','推荐渠道',100),('state','处理状态',120)]:
            self.tree.heading(key,text=title)
            self.tree.column(key,width=width,minwidth=60,stretch=key=='title')
        scroll=ttk.Scrollbar(table,orient='vertical',command=self.tree.yview)
        horizontal=ttk.Scrollbar(table,orient='horizontal',command=self.tree.xview)
        self.tree.configure(yscrollcommand=scroll.set,xscrollcommand=horizontal.set)
        horizontal.pack(side='bottom',fill='x')
        scroll.pack(side='right',fill='y')
        self.tree.pack(fill='both',expand=True)
        self.tree.tag_configure('pending',foreground='#946200')
        self.tree.bind('<<TreeviewSelect>>',self.show_detail)
        detail=ttk.Frame(panes,padding=(0,10,0,0))
        panes.add(detail,weight=1)
        ttk.Label(detail,text='论文详情 · 推荐依据',font=('Microsoft YaHei UI',10,'bold')).pack(anchor='w',pady=(0,4))
        self.detail=tk.Text(detail,height=5,wrap='word',font=('Microsoft YaHei UI',10),background='white',foreground='#334155',relief='flat',padx=12,pady=8,state='disabled')
        ds=ttk.Scrollbar(detail,command=self.detail.yview)
        self.detail.configure(yscrollcommand=ds.set)
        ds.pack(side='right',fill='y')
        self.detail.pack(fill='both',expand=True)
        self.set_detail('选择一篇论文，查看完整题名、分类理由、推荐入口和待确认条件。')
        footer=ttk.Frame(page)
        footer.pack(fill='x',pady=(12,0))
        self.report_button=ttk.Button(footer,text='查看分类报告',command=lambda:self.open_report('分类建议.md'))
        self.report_button.pack(side='left')
        self.channel_button=ttk.Button(footer,text='查看渠道分组',command=lambda:self.open_report('导入渠道分类.md'))
        self.channel_button.pack(side='left',padx=8)
        if self.on_review:
            self.review_button=ttk.Button(footer,text='转到人工处理',command=self.review_selected)
            self.review_button.pack(side='left')
        if self.on_export:
            self.export_button=ttk.Button(footer,text='按分类导出 WOS 元数据',command=self.export_wos)
            self.export_button.pack(side='left',padx=8)
        ttk.Button(footer,text='结果文件夹',command=self.open_output).pack(side='right')
        ttk.Label(page,text='AI 建议需核实收录及导出文件。开始会使用模型额度；原名单保持不变。',style='Muted.TLabel').pack(anchor='w',pady=(8,0))
        self.load_results()
        self.poll_id=root.after(150,self.poll)
        root.bind('<Destroy>',lambda event:self.dispose() if event.widget is root else None,add='+')

    def reload(self):
        try:
            data = read_papers(BASE/'list.xlsx')
            self.roster_hash=data.get('sha256')
            self.metrics['名单记录'].set(str(sum(len(p['rows']) for p in data['papers'])))
            self.metrics['分类任务'].set(str(len(data['papers'])))
            self.summary.set(f'名单：{sum(len(p["rows"]) for p in data["papers"])} 条记录，合并为 {len(data["papers"])} 个题名/DOI 组合。')
        except Exception:
            self.roster_hash=None
            self.metrics['名单记录'].set('—')
            self.metrics['分类任务'].set('—')
            self.summary.set('名单未就绪：请将包含“题名”列的 list.xlsx 放在程序目录。')

    def configure_key(self):
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

    def _sync_export_button(self):
        if hasattr(self,'export_button'):
            ready=(bool(self.records) and not self.busy and not self.external_busy
                   and not self.exporting)
            self.export_button.configure(state='normal' if ready else 'disabled')

    def export_wos(self):
        if self.busy or self.external_busy or self.exporting or not self.on_export:
            return
        # The batch reuses this same stop flag, so "本批完成后停止" also halts an export
        # run instead of leaving the window disabled with no way out.
        self.stop.clear()
        self.status.set('正在按分类结果导出 WOS 元数据；可点“本批完成后停止”。')
        self.on_export()

    def set_exporting(self,value):
        """A batch export runs through the app's shared busy state, not set_busy()."""
        self.exporting=bool(value)
        self.stop_button.configure(state='normal' if (self.exporting or self.busy) else 'disabled')
        self._sync_export_button()

    def set_busy(self,value):
        self.busy = value
        if not value:
            self.exporting=False
        self.start_button.configure(state='disabled' if value else 'normal')
        self.key_button.configure(state='disabled' if value else 'normal')
        self.combo.configure(state='disabled' if value else 'readonly')
        self.batch_combo.configure(state='disabled' if value else 'readonly')
        self.stop_button.configure(state='normal' if (value or self.exporting) else 'disabled')
        self.start_button.configure(text='正在分类…' if value else '开始 / 继续分类')
        self._sync_export_button()
        if self.on_busy:
            self.on_busy(value)

    def set_external_busy(self,value):
        self.external_busy=value and not self.busy
        if not value:
            self.exporting=False
        if not self.busy:
            self.start_button.configure(state='disabled' if value else 'normal')
            self.key_button.configure(state='disabled' if value else 'normal')
            self.combo.configure(state='disabled' if value else 'readonly')
            self.batch_combo.configure(state='disabled' if value else 'readonly')
        self.stop_button.configure(state='normal' if (self.exporting or self.busy) else 'disabled')
        if hasattr(self,'review_button'):
            self.review_button.configure(state='disabled' if value else 'normal')
        self._sync_export_button()

    def start(self):
        if self.busy or self.external_busy:
            return
        if not self.store.configured():
            self.configure_key()
            if not self.store.configured():
                return
        self.reload()
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
                             progress=lambda value:self.queue.put(('progress',value)),resilient=True)
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
        self.poll_id=self.root.after(150,self.poll)

    def load_results(self,update_status=True,folder=None):
        candidates=[Path(folder)/'分类结果.json'] if folder else sorted(self.output.glob('*/分类结果.json'),key=lambda p:p.stat().st_mtime,reverse=True)
        found=None
        for path in candidates:
            try:
                data=json.loads(path.read_text(encoding='utf-8'))
                if data.get('model')!=self.model.get() or not self.roster_hash or data.get('source',{}).get('sha256')!=self.roster_hash:
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
        self.bar['value']=100*done/len(self.records) if self.records else 0
        if update_status:
            if self.records:
                self.status.set(f'已加载保存结果 · {done}/{len(self.records)} 个任务'
                                + (f'，其中 {failed} 个分类失败' if failed else ''))
            else:
                self.status.set('暂无当前名单及模型的结果，点击开始分类。')
        for button,filename in ((self.report_button,'分类建议.md'),(self.channel_button,'导入渠道分类.md')):
            button.configure(state='normal' if self.result_folder and (self.result_folder/filename).is_file() else 'disabled')
        self._sync_export_button()
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
                messagebox.showerror('无法打开','请通过结果文件夹查看文件。',parent=self.root)

    def request_stop(self):
        self.stop.set()
        self.stop_button.configure(state='disabled')
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
