"""Manual review desk with optional, explicitly triggered browser navigation."""
from __future__ import annotations

import os
from copy import deepcopy
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk
from notices import messages as messagebox
from bridge import Bridge
from claim import sa_claim_source
from approval import auto_complete_claim, complete_claim, verify_claim_result
from model_review import KeyStore, ModelClient
from model_panel import ModelPanel
from automation_panel import AutomationPanel
from operation_log import OperationLog
from ui_theme import FONT, P, install_theme, style_text

from core import Journal, SafetyStop, fixed_roster_path, guide, read_roster
from remarks import PRESETS, append_remark
from roster_write import mark_complete, migrate_status_column

BASE = Path(__file__).resolve().parent
YELLOW = P.amber_soft
GREEN = P.green_soft


def load_workflow_roster():
    roster = migrate_status_column(read_roster(fixed_roster_path(BASE)))
    from paper_classify import rebind_current_classification_sources
    rebind_current_classification_sources(roster.path)
    roster.assert_unchanged()
    return roster


class App:
    def __init__(self, root, journal=None, auto_load=True, bridge=None, model_client=None, operation_log=None, unified=False, initial_tab=None):
        self.root = root
        self.unified = unified
        self.classifier = None
        self.submission_panel = None
        self.settings_panel = None
        self.wos_import_panel = None
        self.last_wos_scope = "pending"
        self.last_wos_owner = ""
        self.closing = False
        self.journal = journal or Journal(BASE / "runtime" / "progress.sqlite3")
        self.operation_log = operation_log or OperationLog(BASE / "log.txt")
        self.roster = None
        self.records = []
        self.by_id = {}
        self.current = None
        self.bridge = bridge
        self.model_client = model_client or ModelClient(KeyStore(BASE / "runtime"))
        self.snapshot = None
        self.comparison = None
        self.prepared_claim = None
        self.claim_options = []
        self.skipped = {}
        self.busy = False
        self.events = queue.Queue()
        self.buttons = []
        self.owner = tk.StringVar()
        self.status = tk.StringVar(value="选择负责人后，开始人工处理。")
        self.pending_count = tk.StringVar(value="未完成 —")
        self.done_count = tk.StringVar(value="已完成 —")
        self.current_id = tk.StringVar(value="请选择一条记录")
        self.approval = tk.StringVar(value="未完成")
        self.reviewed = tk.BooleanVar(value=False)
        self.preset = tk.StringVar(value="选择备注模板")
        self.task_view = tk.StringVar(value="pending")
        self.connection = tk.StringVar(value="连接浏览器")
        self.sa_number = tk.StringVar(value="先定位网页")
        self.claim_person = tk.StringVar(value="尚未查找人员")
        self.claim_author = tk.StringVar()
        self.auto_claim_completion = tk.BooleanVar(value=True)
        self.build()
        if unified:
            self.build_classification()
            if initial_tab == 'classification':
                self.tabs.select(self.classification_page)
        from wos_import_panel import WOSImportPanel
        self.wos_import_page = ttk.Frame(self.tabs, padding=14)
        self.tabs.add(self.wos_import_page, text="WOS 导入")
        self.wos_import_panel = WOSImportPanel(self, self.wos_import_page)
        from settings_panel import SettingsPanel
        self.settings_page = ttk.Frame(self.tabs, padding=18)
        self.tabs.add(self.settings_page, text="设置")
        self.settings_panel = SettingsPanel(self, self.settings_page, BASE / "runtime")
        self.reviewed.trace_add("write", lambda *_: self.refresh_approval())
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.pump_id = self.root.after(120, self.pump)
        self.load_id = self.root.after(200, self.reload_roster) if auto_load else None

    def build_classification(self):
        from classify_app import ClassifyApp
        self.root.title('机构知识库工作台 · 处理 / 认领 / AI 分类')
        self.root.attributes('-topmost',False)
        height=min(900,self.root.winfo_screenheight()-80)
        width=min(1180,self.root.winfo_screenwidth()-80)
        self.root.geometry(f'{width}x{height}+30+25')
        self.root.minsize(960,740)
        self.classification_page=self.analysis_page
        self.tabs.tab(self.classification_page,text='批量分类 / 核对')
        self.batch_classification_page=ttk.Frame(self.analysis_tabs)
        self.analysis_tabs.insert(0,self.batch_classification_page,text='批量分类 / 导入渠道')
        self.analysis_tabs.select(self.batch_classification_page)
        self.tabs.tab(self.automation_page,text='自动化 / 认领')
        self.classifier=ClassifyApp(self.root,parent=self.batch_classification_page,
                                   on_busy=self.set_busy,on_review=self.review_classified,
                                   on_export=self.export_wos_metadata,on_settings=self.open_settings,
                                   on_export_skipped=self.export_skipped_wos_metadata,
                                   on_export_selected=self.export_selected_wos_metadata,
                                   on_import=self.open_wos_import)
        from submission_panel import SubmissionPanel
        self.submission_page=ttk.Frame(self.tabs,padding=20)
        self.tabs.add(self.submission_page,text='零匹配提交准备')
        self.submission_panel=SubmissionPanel(self,self.submission_page)
        self.tabs.bind('<<NotebookTabChanged>>',self.refresh_workspace)

    def open_settings(self):
        if self.busy or not self.settings_panel:
            return
        self.settings_panel.refresh()
        self.tabs.select(self.settings_page)
        self.settings_panel.entry.focus_set()

    def open_wos_import(self):
        if self.busy or not self.wos_import_panel:
            return
        owner = self.classifier.owner.get().strip() if self.classifier else self.owner.get().strip()
        if not owner:
            messagebox.showinfo("请选择负责人", "请选择负责人。", parent=self.root)
            return
        scope = self.last_wos_scope if self.last_wos_owner == owner else "pending"
        self.wos_import_panel.receive_downloads(owner, scope)
        self.tabs.select(self.wos_import_page)

    def refresh_workspace(self,_event=None):
        if self.classifier and str(self.tabs.select())==str(self.classification_page):
            self.classifier.key_status.set('密钥已配置' if self.classifier.store.configured() else '请配置密钥')
            if not self.busy:
                self.classifier.reload()
                self.classifier.load_results()

    def review_classified(self,item):
        if self.busy:
            return
        if not self.roster:
            messagebox.showinfo('请先读取名单','请在人工处理页读取 list.xlsx。',parent=self.root)
            self.tabs.select(self.manual_page)
            return
        try:
            self.roster.assert_unchanged()
        except SafetyStop as exc:
            messagebox.showwarning('请重读名单',str(exc),parent=self.root)
            return
        matches=[r for r in self.roster.records if r.row in item['rows'] and r.title.strip()==item['title'] and r.doi.strip()==item.get('doi','')]
        if not matches:
            messagebox.showwarning('记录不匹配','分类结果与当前名单不一致，请重新读取。',parent=self.root)
            return
        def select(record):
            self.owner.set(record.owner)
            self.task_view.set('done' if record.done else 'pending')
            self.select_owner()
            self.tree.selection_set(record.sa_id)
            self.tree.see(record.sa_id)
            self.select_record()
            self.tabs.select(self.manual_page)
        if len(matches)==1:
            select(matches[0])
            return
        popup=tk.Toplevel(self.root)
        popup.title('选择要处理的原表记录')
        popup.transient(self.root)
        frame=ttk.Frame(popup,padding=18)
        frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='同一论文对应多条名单，请选择具体记录。').pack(anchor='w',pady=(0,10))
        choice=ttk.Combobox(frame,state='readonly',width=52,values=[f'第 {r.row} 行 · {r.owner} · {r.sa_id}' for r in matches])
        choice.pack(fill='x')
        choice.current(0)
        def confirm():
            if choice.current()<0:
                return
            record=matches[choice.current()]
            popup.destroy()
            select(record)
        ttk.Button(frame,text='打开所选记录',command=confirm).pack(anchor='e',pady=(12,0))
        popup.grab_set()

    def _wos_export_ready(self, owner):
        """Shared guard: returns the roster document, or None after telling the user."""
        if self.busy:
            return None
        if not str(owner).strip():
            messagebox.showinfo('请选择负责人','请选择负责人。',parent=self.root)
            return None
        if not self.roster:
            messagebox.showinfo('请先读取名单','请在人工处理页读取 list.xlsx。',parent=self.root)
            self.tabs.select(self.manual_page)
            return None
        from wos_browser import read_settings
        try:
            connection_settings = read_settings(BASE / 'runtime')
        except SafetyStop as exc:
            messagebox.showwarning('下载连接配置无效',str(exc),parent=self.root)
            return None
        if connection_settings['transport']=='extension' and (not self.bridge or not self.bridge.online):
            messagebox.showinfo('需要连接浏览器',
                '批量导出会操作你在扩展里绑定的 WOS 标签页。\n'
                '请点“连接浏览器”取得配对码，在已登录的 WOS 页打开扩展并连接即可。无需 SA 或导入页。',
                parent=self.root)
            return None
        from paper_classify import read_papers
        try:
            self.roster.assert_unchanged()
            if owner not in {record.owner for record in self.roster.records}:
                raise SafetyStop('所选负责人不在当前名单中，请重新读取名单。')
            return read_papers(BASE/'list.xlsx', owner=owner)
        except SafetyStop as exc:
            messagebox.showwarning('无法读取名单',str(exc),parent=self.root)
            return None

    def export_wos_metadata(self):
        """Search every zero-match record in the roster and export its WOS Full Record.

        Every WOS export is download-only. Upload, import and push write to the
        production library through the separately reviewed import queue.
        """
        owner=self.classifier.owner.get().strip() if self.classifier else ''
        if self._wos_export_ready(owner) is None:
            return
        self.last_wos_owner, self.last_wos_scope = owner, "pending"
        from wos_batch import all_targets, roster_rows
        rows=len(roster_rows(self.roster,owner))
        self._start_wos_export(all_targets(self.roster,owner),f'下载待补论文 TXT（WOS）·{owner}',rows=rows)

    def export_skipped_wos_metadata(self):
        """Search only numeric-2 rows without changing their persistent skip marker."""
        owner=self.classifier.owner.get().strip() if self.classifier else ''
        if self._wos_export_ready(owner) is None:
            return
        self.last_wos_owner, self.last_wos_scope = owner, "skipped"
        from wos_batch import skipped_roster_rows, skipped_targets
        rows=len(skipped_roster_rows(self.roster,owner))
        self._start_wos_export(skipped_targets(self.roster,owner),f'重试跳过论文（WOS）·{owner}',rows=rows)

    def export_selected_wos_metadata(self,item):
        """Download one exact roster paper, independent of the batch scope."""
        owner=self.classifier.owner.get().strip()
        if self._wos_export_ready(owner) is None:
            return
        title=str(item.get('title') or '').strip()
        doi=str(item.get('doi') or '').strip()
        rows=[record for record in self.roster.records if record.owner==owner and record.row in item.get('rows',[])
              and record.title.strip()==title and record.doi.strip()==doi]
        if not rows:
            messagebox.showinfo('所选论文与名单不一致','所选题名、DOI、原表行号与当前负责人名单不对应；请重读名单。',parent=self.root)
            return
        record=next((record for record in rows if not record.done and record.matches==0), rows[0])
        # Explicit one-paper trial is download-only, even for a matched/done row.
        # It must not alter the batch scope, roster, provenance or import queue.
        self._start_wos_export([record],f'试下载所选论文 TXT·{owner}',rows=len(rows),trial=True)

    def _start_wos_export(self, targets, label, rows=None, trial=False):
        if not targets:
            messagebox.showinfo('没有待导出的记录',
                '当前负责人在所选范围内没有未完成的零匹配论文。\n'
                '“是否识别”为 2 的论文请点“重试跳过论文（WOS）”。已有 TXT 可点“检查 TXT 并导入”。',parent=self.root)
            return
        from wos_batch import default_inbox, export as export_batch
        roster=self.roster
        self.classifier.set_exporting(True)
        generation=self.classifier.export_generation
        def report(message):
            # Workers only enqueue; Tk variables are updated by the UI's polls.
            # Keep the manual page informed, and also update the page that owns
            # the download button instead of leaving it on a static AI percentage.
            self.automation_panel.progress.put(message)
            self.classifier.queue.put(('download_progress',(generation,message,time.monotonic())))
        total=len(targets)
        scope=(f'名单 {rows} 条记录，同一篇论文合并为 {total} 份文件' if rows and rows!=total
               else f'名单范围 {total} 条')
        def audit(action, outcome, sa_id):
            try:
                self.operation_log.record(action, outcome, sa_id)
            except Exception:
                report('log.txt 保存失败；本轮完整结果仍将单独保存，请检查文件权限。')
        def job():
            from wos_batch import default_store, preflight
            from wos_browser import BrowserSkillWOS, select_transport
            report('正在检查 WOS 下载连接和字段检索页面（尚未点击检索）')
            bridge=select_transport(self.bridge,BASE,stop=self.classifier.stop,progress=report)
            try:
                connection=preflight(bridge)
                channel='浏览器技能专用窗口' if connection.get('transport')=='browser-skill' else '插件 '+connection['extension_version']
                report(f'已连接{channel}；准备下载 {total} 篇')
                if trial:
                    from automation import ImportStore
                    store=ImportStore(BASE/'runtime'/'wos-trials'/'archives')
                    inbox=BASE/'runtime'/'wos-trials'/'files'
                else:
                    store,inbox=default_store(),default_inbox()
                result=export_batch(targets,bridge,store,inbox,
                                    stop=self.classifier.stop,unchanged=roster.assert_unchanged,
                                    progress=report, audit=audit, eligible_only=not trial)
            finally:
                if isinstance(bridge,BrowserSkillWOS):
                    report('正在保存浏览器证据并关闭专用下载窗口…')
                    bridge.close()
            result.update(connection)
            result['trial']=trial
            if trial:
                result['skipped_saved']=0
            else:
                try:
                    from wos_batch import persist_download_outcomes
                    update=persist_download_outcomes(roster,targets,result)
                    result['_roster_update']=update
                    result['skipped_saved']=update.skipped_count
                    if update.source_error:
                        result['source_error']=update.source_error
                    if update.workflow_error:
                        result['workflow_error']=update.workflow_error
                    if update.roster.sha256!=roster.sha256:
                        audit(f'WOS 下载回写（来源 {update.source_count} 行；跳过 {update.skipped_count} 行）','已执行', '')
                        try:
                            from paper_classify import rebind_current_classification_sources
                            result['classification_rebound']=rebind_current_classification_sources(update.roster.path)
                        except Exception as exc:
                            result['classification_rebind_error']=str(exc)
                except Exception as exc:
                    result['workflow_error']=str(exc)
            try:
                from wos_reports import save_download_report
                result['report_path'] = str(save_download_report(
                    result, targets[0].owner, 'trial' if trial else 'skipped' if targets[0].skipped else 'pending',
                    BASE / 'runtime' / 'wos-reports'))
            except Exception:
                result['report_error'] = '完整结果报告保存失败，请检查 runtime 目录的写入权限。'
            return result
        def done(result):
            detail=''
            roster_update=result.pop('_roster_update',None)
            if roster_update is not None:
                self.roster=roster_update.roster
                self.skipped={record.sa_id:record.remark or "是否识别为 2，已标记为跳过。"
                              for record in self.roster.records if record.skipped and not record.done}
                self.owner_box['values']=sorted({record.owner for record in self.roster.records})
                self.clear_selection()
                if self.owner.get():
                    self.populate()
                if self.classifier:
                    self.classifier.reload()
                    self.classifier.load_results()
                if roster_update.source_count:
                    detail+=(f'\n\n已在 list.xlsx 的“数据来源”列为 '
                             f'{roster_update.source_count} 行记录 WOS。')
                if roster_update.skipped_count:
                    detail+=(f'\n\n已为实际尝试但未能采纳的 {roster_update.skipped_count} 行写入“是否识别”=2及真实原因；'
                             '未执行项保持原样，重试请使用“重试跳过论文（WOS）”。')
            if result.get('source_error'):
                detail+=('\n\nTXT 已成功导出，但“数据来源”尚未写入：'
                         +result['source_error'])
            if result.get('classification_rebind_error'):
                detail+=('\n\n名单已回写；旧分类结果索引刷新失败，请重新打开分类页：'
                         +result['classification_rebind_error'])
            if result.get('workflow_error'):
                detail+=('\n\n跳过状态/原因未能回写（未假定写入成功）：'+result['workflow_error'])
            if result.get('report_path'):
                self.wos_import_panel.download_report = result['report_path']
                detail += '\n\n每篇的结果和文件位置已完整保存：\n' + result['report_path']
            if result.get('report_error'):
                detail += '\n\n' + result['report_error']
            if result.get('disconnected'):
                detail+=(f'\n\n浏览器会话已不可继续（断连、回传超时或旧命令仍占用），整批已经停止；剩余 '
                         f'{result.get("remaining",0)} 条尚未执行。检查当前网页，重新连接并绑定 WOS 页后再继续。')
            if result.get('page_blocked'):
                detail+='\n\n专用浏览器已暂停，未把后续论文批量标成跳过：'+result.get('halt_reason','请检查 WOS 页面。')
            if trial:
                paths=[entry.get('file') or entry.get('archive') for entry in result.get('exported',[])+result.get('unconfirmed',[])]
                detail+='\n\n纯试下载：名单、备注、是否识别、数据来源和入库队列均未修改。'
                if paths:
                    detail+='\n本次 TXT：\n'+'\n'.join(paths)
            outcome='已暂停' if result.get('stopped') else '已结束'
            self.classifier.status.set(
                f'WOS 下载{outcome} · 已核验 TXT {len(result["exported"])} 篇 · '
                f'待核验 {len(result.get("unconfirmed",[]))} 篇 · '
                f'无可用记录 {result.get("not_exported",0)} 篇 · '
                f'页面/会话问题 {result.get("session_failures",0)} 篇 · '
                f'未执行 {result.get("remaining",0)} 篇')
            messagebox.showinfo(f'{label}结束',
                f'{scope}\n\nTXT 已核验 {len(result["exported"])} 篇 · 身份待核验 {len(result.get("unconfirmed",[]))} 篇\n'
                f'WOS 无可用记录 {result.get("not_exported",0)} 篇 · 页面/会话问题 {result.get("session_failures",0)} 篇\n'
                f'尚未执行 {result.get("remaining",0)} 篇\n\n'+
                ('试下载只验证 WOS 检索与文件保存，不表示可入库。' if trial else
                 '下一步：在“WOS 导入”检查 TXT，核实本库缺失后上传入库。\n'
                 '身份待核验项也会列出，可点“核验所选论文”。\n'
                 '下载不会入库，也不会把 Excel 标为完成。')+detail,parent=self.root)
            from pilot import OWNER
            if not trial and targets[0].owner == OWNER and (result.get('exported') or result.get('unconfirmed')):
                # A weak download is now persistently skipped; offer its archive
                # in the skipped scope instead of silently hiding it in pending.
                import_scope='skipped' if targets[0].skipped or (
                    not result.get('exported') and result.get('skipped_saved')) else 'pending'
                self.wos_import_panel.receive_downloads(targets[0].owner,import_scope)
                self.tabs.select(self.wos_import_page)
                self.wos_import_panel.preview()
        self.run(job,done,f'正在{label}…共 {total} 条',log_action=label)

    def button(self, parent, text, command, **kwargs):
        # Use text-sized buttons; ttk's default nine-character minimum clips the
        # six compact navigation actions at the smallest supported window size.
        kwargs.setdefault("width", 0)
        widget = ttk.Button(parent, text=text, command=command, **kwargs)
        self.buttons.append(widget)
        return widget

    def build(self):
        self.root.title("机构知识库 · 比对助手")
        width, height = 560, min(700, self.root.winfo_screenheight() - 90)
        x = max(0, self.root.winfo_screenwidth() - width - 35)
        self.root.geometry(f"{width}x{height}+{x}+35")
        self.root.minsize(520, 600)
        self.root.attributes("-topmost", True)
        install_theme(self.root)
        header = ttk.Frame(self.root, padding=(18, 12, 18, 8))
        header.pack(fill="x")
        ttk.Label(header, text="机构知识库", font=(FONT, 14, "bold")).pack(side="left")
        ttk.Label(header, text="LIBRARY  /  WORKSPACE", style="Eyebrow.TLabel").pack(side="right")
        self.tabs = ttk.Notebook(self.root)
        self.tabs.pack(fill="both", expand=True, padx=12, pady=(0, 10))
        self.manual_page = ttk.Frame(self.tabs, padding=10)
        self.automation_page = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(self.manual_page, text="人工处理")
        self.tabs.add(self.automation_page, text="自动化")
        self.analysis_page = ttk.Frame(self.tabs)
        self.tabs.add(self.analysis_page, text="资料核对")
        self.analysis_tabs = ttk.Notebook(self.analysis_page)
        self.analysis_tabs.pack(fill="both", expand=True)
        self.model_page = ttk.Frame(self.analysis_tabs, padding=12)
        self.analysis_tabs.add(self.model_page, text="单条核对")
        self.model_panel = ModelPanel(self, self.model_page, self.model_client)
        ttk.Label(self.automation_page, textvariable=self.current_id, wraplength=440).pack(anchor="w", pady=(0, 8))
        self.automation_panel = AutomationPanel(self, self.automation_page, BASE / "runtime" / "wos-imports")
        auto = self.automation_panel.claim_page
        ttk.Label(auto, text="SA 提交 · 括号编号").pack(anchor="w")
        ttk.Entry(auto, textvariable=self.sa_number, state="readonly").pack(fill="x", pady=(5, 8))
        auto_tools = ttk.Frame(auto)
        auto_tools.pack(fill="x", pady=(0, 8))
        self.button(auto_tools, "定位网页", self.locate).pack(side="left")
        self.button(auto_tools, "复制编号", self.copy_sa_number).pack(side="left", padx=6)
        self.button(auto_tools, "查找认领人员", self.prepare_claim).pack(side="left")
        ttk.Label(auto, textvariable=self.claim_person, wraplength=440, font=("Microsoft YaHei UI", 10)).pack(anchor="w", pady=(0, 12))
        ttk.Label(auto, text="对应论文作者（确认署名后选择）").pack(anchor="w")
        self.claim_author_box = ttk.Combobox(auto, textvariable=self.claim_author, state="readonly")
        self.claim_author_box.pack(fill="x", pady=(5, 12))
        self.claim_author_box.bind("<<ComboboxSelected>>", lambda _e: self.refresh_approval())
        self.claim_button = self.button(auto, "确认并认领此作者", self.submit_claim, style="Complete.TButton")
        self.claim_button.pack(fill="x", pady=(0, 14))
        self.auto_claim_check = ttk.Checkbutton(auto, variable=self.auto_claim_completion,
            text="认领核验成功后，自动批注并结案")
        self.auto_claim_check.pack(anchor="w")
        ttk.Label(auto, text="仅限本人单匹配作者差异；其他情况保留人工审批。", wraplength=420, style="Muted.TLabel").pack(anchor="w", pady=(4, 0))
        ttk.Label(self.automation_page, textvariable=self.status, wraplength=445).pack(anchor="w", pady=(7, 0))
        page = self.manual_page
        page.columnconfigure(0, weight=1)
        page.rowconfigure(2, weight=3)
        page.rowconfigure(4, weight=2)
        toolbar = ttk.Frame(page)
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        ttk.Label(toolbar, text="负责人").pack(side="left")
        self.owner_box = ttk.Combobox(toolbar, textvariable=self.owner, state="readonly", width=10)
        self.owner_box.pack(side="left", padx=(6, 8))
        self.owner_box.bind("<<ComboboxSelected>>", self.select_owner)
        self.button(toolbar, "重读 list.xlsx", self.reload_roster).pack(side="left")
        self.button(toolbar, "连接浏览器", self.pair, textvariable=self.connection).pack(side="left", padx=4)
        self.button(toolbar, "说明", self.help, width=4).pack(side="right")
        counts = ttk.Frame(page)
        counts.grid(row=1, column=0, sticky="ew", pady=(0, 7))
        self.view_buttons = []
        for value, caption, color in (("pending", self.pending_count, YELLOW), ("done", self.done_count, GREEN)):
            tab = tk.Radiobutton(counts, textvariable=caption, variable=self.task_view, value=value, indicatoron=False,
                                 bg=P.canvas, fg=P.muted, selectcolor=color, activebackground=color,
                                 activeforeground=P.ink, relief="flat", borderwidth=0, highlightthickness=0,
                                 padx=12, pady=5, command=self.switch_view, font=(FONT, 9))
            tab.pack(side="left", padx=(0, 5))
            self.view_buttons.append(tab)
        ttk.Label(counts, text="点击切换", style="Muted.TLabel").pack(side="right")
        listing = ttk.Frame(page)
        listing.grid(row=2, column=0, sticky="nsew")
        self.tree = ttk.Treeview(listing, columns=("id", "title", "state"), show="headings", height=5, selectmode="browse",
                                style="Task.Treeview")
        for name, caption, size in (("id", "名单 ID", 140), ("title", "题名", 260), ("state", "状态", 65)):
            self.tree.heading(name, text=caption)
            self.tree.column(name, width=size, minwidth=50, stretch=name == "title")
        self.tree.tag_configure("pending", background=YELLOW, foreground=P.amber)
        self.tree.tag_configure("done", background=GREEN, foreground=P.green)
        self.tree.tag_configure("skipped", background=P.red_soft, foreground=P.red)
        scroll = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.select_record)
        self.tree.bind("<Button-1>", lambda _e: "break" if self.busy else None)
        self.tree.bind("<KeyPress>", lambda _e: "break" if self.busy else None)
        heading = ttk.Frame(page)
        heading.grid(row=3, column=0, sticky="ew", pady=(8, 5))
        ttk.Label(heading, textvariable=self.current_id).pack(side="left")
        self.badge = tk.Label(heading, textvariable=self.approval, bg=YELLOW, fg=P.amber,
                              font=(FONT, 8), padx=9, pady=3)
        self.badge.pack(side="right")
        detail_frame = ttk.Frame(page)
        detail_frame.grid(row=4, column=0, sticky="nsew")
        self.details = tk.Text(detail_frame, height=5, width=30, wrap="word", font=("Microsoft YaHei UI", 10),
                               bg="white", relief="flat", padx=8, pady=6)
        style_text(self.details)
        detail_scroll = ttk.Scrollbar(detail_frame, orient="vertical", command=self.details.yview)
        self.details.configure(yscrollcommand=detail_scroll.set, state="disabled")
        detail_scroll.pack(side="right", fill="y")
        self.details.pack(fill="both", expand=True)
        tools = ttk.Frame(page)
        tools.grid(row=5, column=0, sticky="ew", pady=5)
        self.button(tools, "定位网页", self.locate).pack(side="left")
        self.button(tools, "编辑", lambda: self.open_browser_panel("open_metadata")).pack(side="left", padx=3)
        self.button(tools, "认领", lambda: self.open_browser_panel("open_claim")).pack(side="left")
        self.button(tools, "ID", self.copy_id).pack(side="left", padx=3)
        self.button(tools, "SA 编号", self.copy_sa_number).pack(side="left")
        self.button(tools, "指引", self.show_guide).pack(side="left")
        self.button(tools, "下一条", self.next_record).pack(side="right")
        templates = ttk.Frame(page)
        templates.grid(row=6, column=0, sticky="ew", pady=(2, 4))
        self.preset_box = ttk.Combobox(templates, values=PRESETS, textvariable=self.preset, state="readonly", width=27)
        self.preset_box.pack(side="left", fill="x", expand=True)
        self.preset_box.bind("<<ComboboxSelected>>", lambda _e: self.use_remark(self.preset.get()))
        self.button(templates, "复制备注", self.copy_note).pack(side="right", padx=(6, 0))
        self.note = tk.Text(page, height=2, width=30, wrap="word", font=("Microsoft YaHei UI", 10), relief="solid", borderwidth=1)
        style_text(self.note, inset=True)
        self.note.grid(row=7, column=0, sticky="ew")
        self.note.bind("<<Modified>>", self.note_changed)
        self.check = ttk.Checkbutton(page, variable=self.reviewed, text="我已核对当前记录，并完成网页处理")
        self.check.grid(row=8, column=0, sticky="w", pady=(7, 3))
        approvals = ttk.Frame(page)
        approvals.grid(row=9, column=0, sticky="ew", pady=(0, 6))
        approvals.columnconfigure((0, 1), weight=1)
        self.complete_button = self.button(approvals, "批准完成", self.confirm_manual_done, style="Complete.TButton")
        self.complete_button.grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.claimed_complete_button = self.button(approvals, "网页认领结案", self.confirm_claim_done, style="Complete.TButton")
        self.claimed_complete_button.grid(row=0, column=1, sticky="ew")
        self.status_label = ttk.Label(page, textvariable=self.status, wraplength=485, style="Muted.TLabel")
        self.status_label.grid(row=10, column=0, sticky="ew")
        page.bind("<Configure>", lambda event: self.status_label.configure(wraplength=max(250, event.width - 20)))
        self.refresh_approval()

    def refresh_approval(self):
        done = bool(self.current and self.current.done)
        enabled = bool(self.current and not done and self.reviewed.get() and not self.busy)
        self.complete_button.configure(state="normal" if enabled else "disabled", text="已批准完成" if done else "✓ 批准完成")
        can_close_claim = bool(enabled and self.current.owner == "谭勋策" and self.snapshot and self.comparison)
        self.claimed_complete_button.configure(state="normal" if can_close_claim else "disabled")
        can_claim = bool(self.current and not done and self.prepared_claim and not self.busy and self.claim_author_box.current() >= 0)
        self.claim_button.configure(state="normal" if can_claim else "disabled")
        self.model_panel.refresh()

    def set_busy(self, busy):
        self.busy = busy
        for widget in self.buttons:
            widget.configure(state="disabled" if busy else "normal")
        for widget in (self.owner_box, self.preset_box, self.claim_author_box):
            widget.configure(state="disabled" if busy else "readonly")
        self.check.configure(state="disabled" if busy else "normal")
        self.auto_claim_check.configure(state="disabled" if busy else "normal")
        self.note.configure(state="disabled" if busy else "normal")
        for tab in self.view_buttons:
            tab.configure(state="disabled" if busy else "normal")
        self.automation_panel.limit_box.configure(state="disabled" if busy else "normal")
        self.model_panel.set_busy(busy)
        if self.classifier:
            self.classifier.set_external_busy(busy)
        if self.submission_panel:
            self.submission_panel.set_external_busy(busy)
        if self.settings_panel:
            self.settings_panel.set_busy(busy)
        if self.wos_import_panel:
            self.wos_import_panel.set_busy(busy)
        if not busy and self.closing:
            self.root.after_idle(self.close)
        self.refresh_approval()

    def run(self, job, callback, status, log_action=None):
        if self.busy:
            return
        action = log_action or status.rstrip("…")
        sa_id = self.current.sa_id if self.current else ""
        self.set_busy(True)
        self.status.set(status)
        def worker():
            try:
                self.events.put((True, callback, job(), action, sa_id))
            except Exception as exc:
                self.events.put((False, callback, exc, action, sa_id))
        threading.Thread(target=worker, daemon=True).start()

    def note_operation(self, action, result="已执行", sa_id=None):
        try:
            self.operation_log.record(action, result,
                                      self.current.sa_id if sa_id is None and self.current else sa_id or "")
        except Exception:
            self.status.set("log.txt 未能保存本次操作；请检查文件权限和格式。")

    def pump(self):
        self.connection.set("浏览器已连接" if self.bridge and self.bridge.online else "连接浏览器")
        try:
            while True:
                self.status.set(self.automation_panel.progress.get_nowait())
        except queue.Empty:
            pass
        try:
            while True:
                success, callback, value, action, sa_id = self.events.get_nowait()
                downloading=bool(self.classifier and self.classifier.exporting)
                self.set_busy(False)
                try:
                    if not success:
                        raise value
                    callback(value)
                except Exception as exc:
                    self.clear_browser_state()
                    self.reviewed.set(False)
                    self.status.set("已暂停，请按提示处理；不自动重试。")
                    if downloading:
                        self.classifier.status.set('WOS 下载已暂停：'+str(exc)+'；未自动重试。')
                    messagebox.showwarning("等待人工处理", str(exc), parent=self.root)
                    self.note_operation(action, "已暂停", sa_id)
                else:
                    self.note_operation(action, sa_id=sa_id)
        except queue.Empty:
            pass
        self.pump_id = self.root.after(120, self.pump)

    def show_text(self, content):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", content)
        self.details.configure(state="disabled")

    def clear_selection(self):
        self.current = None
        self.clear_browser_state()
        self.current_id.set("请选择一条记录")
        self.set_approval(False)
        self.note.delete("1.0", "end")
        self.preset.set("选择备注模板")
        self.show_text("")
        self.model_panel.clear(reset_evidence=True)
        self.automation_panel.clear()

    def clear_claim_preview(self):
        self.prepared_claim = None
        self.claim_options = []
        self.claim_person.set("尚未查找人员")
        self.claim_author.set("")
        self.claim_author_box["values"] = []
        self.refresh_approval()

    def clear_browser_state(self):
        self.snapshot = None
        self.comparison = None
        self.sa_number.set("先定位网页")
        self.clear_claim_preview()
        self.model_panel.clear()

    def set_approval(self, completed):
        self.approval.set("已完成" if completed else "未完成")
        self.badge.configure(bg=GREEN if completed else YELLOW, fg=P.green if completed else P.amber)
        self.reviewed.set(False)

    def reload_roster(self):
        if self.busy:
            return
        self.load_id = None
        self.roster = None
        self.records = []
        self.by_id = {}
        self.skipped.clear()
        self.owner.set("")
        self.owner_box["values"] = []
        self.tree.delete(*self.tree.get_children())
        self.clear_selection()
        self.pending_count.set("未完成 —")
        self.done_count.set("已完成 —")
        self.run(load_workflow_roster, self.loaded, "读取 list.xlsx…",
                 log_action="重读名单")

    def loaded(self, roster):
        self.roster = roster
        self.skipped = {record.sa_id: record.remark or "是否识别为 2，已标记为跳过。"
                        for record in roster.records if record.skipped and not record.done}
        self.owner_box["values"] = sorted({record.owner for record in roster.records})
        self.update_counts(roster.records)
        self.status.set("请选择负责人。黄色待办，红色跳过，绿色已完成。")

    def update_counts(self, records):
        done = sum(record.done for record in records)
        skipped = sum(record.skipped and not record.done for record in records)
        suffix = f" · 跳过 {skipped}" if skipped else ""
        self.pending_count.set(f"未完成 {len(records) - done}{suffix}")
        self.done_count.set(f"已完成 {done}")

    def populate(self):
        scope = [r for r in self.roster.records if r.owner == self.owner.get()]
        # Excel is authoritative. Legacy journal states must not hide pending rows.
        self.records = [r for r in scope if r.done == (self.task_view.get() == "done")]
        self.by_id = {r.sa_id: r for r in self.records}
        self.tree.delete(*self.tree.get_children())
        for record in self.records:
            tag = "done" if record.done else "skipped" if record.skipped or record.sa_id in self.skipped else "pending"
            state = "已完成" if record.done else "跳过" if tag == "skipped" else "未完成"
            self.tree.insert("", "end", iid=record.sa_id, values=(record.sa_id, record.title, state), tags=(tag,))
        self.update_counts(scope)
        style = ttk.Style(self.root)
        style.map("Task.Treeview", background=[("selected", "#CFE5D5" if self.task_view.get() == "done" else P.accent_soft)],
                  foreground=[("selected", P.green if self.task_view.get() == "done" else P.accent)])

    def switch_view(self):
        if self.busy:
            return
        self.clear_selection()
        if self.roster:
            self.populate()
        self.status.set("已完成记录可查看和定位网页，不可重复批准。" if self.task_view.get() == "done" else "选择待办进行处理。")
        self.note_operation("切换任务列表：" + ("已完成" if self.task_view.get() == "done" else "未完成"))

    def select_owner(self, _event=None):
        if self.busy or not self.roster:
            return
        self.clear_selection()
        self.populate()
        self.status.set("选择记录后可定位网页。" if self.records else "当前分类没有记录。")
        self.note_operation("选择负责人：" + self.owner.get(), sa_id="")

    def select_record(self, _event=None):
        if self.busy or not self.tree.selection():
            return
        record = self.by_id.get(self.tree.selection()[0])
        if not record or record == self.current:
            return
        self.clear_selection()
        self.current = record
        self.set_approval(record.done)
        self.current_id.set(f"ID：{record.sa_id}")
        skipped = self.skipped.get(record.sa_id)
        if record.skipped and not skipped:
            skipped = record.remark or "是否识别为 2，已标记为跳过。"
        self.show_text(f"{record.title}\n\n工号 {record.staff_id or '—'}    匹配 {record.matches}\n"
                       f"{record.reason or '未提供差异原因'}" +
                       (f"\n\n自动认领已跳过：{skipped}" if skipped else ""))
        self.status.set("已完成记录，仅供查看。" if record.done else "可定位网页；处理完成后由你批准。")
        self.note_operation("选择记录", sa_id=record.sa_id)

    def pair(self):
        if self.busy:
            return
        try:
            if self.bridge is None:
                self.bridge = Bridge()
        except OSError as exc:
            self.note_operation("打开浏览器配对窗口", "已暂停")
            messagebox.showwarning("连接服务未启动", f"请关闭旧助手后重试。\n{exc}", parent=self.root)
            return
        popup = tk.Toplevel(self.root)
        self.note_operation("打开浏览器配对窗口")
        popup.title("连接浏览器")
        popup.transient(self.root)
        popup.attributes("-topmost", True)
        popup.geometry("510x290")
        frame = ttk.Frame(popup, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="在 Chrome / Edge 加载 extension 扩展。\n下载：在已登录的 WOS 页粘贴配对码并连接。\n后台处理：在 SA 比对结果页配对。两种用途独立。", wraplength=465).pack(anchor="w", pady=(0, 10))
        token = tk.StringVar(value=self.bridge.token)
        ttk.Entry(frame, textvariable=token, state="readonly").pack(fill="x")
        ttk.Button(frame, text="复制配对码", command=lambda: self.copy(token.get(), "配对码已复制。请在扩展中连接。")).pack(anchor="w", pady=8)
        import webbrowser
        ttk.Button(frame, text="打开后台入口", command=lambda: webbrowser.open("http://admin.ir.lib.sjtu.edu.cn/#/dataCompare/list")).pack(anchor="w")
        def reset():
            try:
                self.bridge.re_pair()
                token.set(self.bridge.token)
                self.clear_browser_state()
                self.reviewed.set(False)
                self.note_operation("更新浏览器配对码")
            except SafetyStop as exc:
                messagebox.showwarning("等待当前操作结束", str(exc), parent=popup)
        ttk.Button(frame, text="更换标签页 / 新配对码", command=reset).pack(anchor="w", pady=8)

    def locate(self):
        self.open_browser_panel("search")

    def open_browser_panel(self, action):
        if self.busy:
            return
        try:
            if action not in {"search", "open_metadata", "open_claim"}:
                raise SafetyStop("人工模式不支持网页自动写入。")
            if not self.current or not self.roster:
                raise SafetyStop("请先选择记录。")
            self.roster.assert_unchanged()
            if not self.bridge or not self.bridge.online:
                raise SafetyStop("请先点击“连接浏览器”完成配对。")
            if action != "search" and (self.current.done or not self.snapshot):
                raise SafetyStop("请先定位未完成记录，再打开编辑或认领窗口。")
            record = self.current
            payload = {"sa_id": record.sa_id}
            if action != "search":
                payload["expected"] = self.snapshot
            self.clear_browser_state()
            self.reviewed.set(False)
        except Exception as exc:
            self.note_operation({"search": "定位网页", "open_metadata": "打开网页编辑",
                                 "open_claim": "打开网页认领"}.get(action, "打开网页"), "已暂停")
            messagebox.showwarning("暂未定位", str(exc), parent=self.root)
            return
        def opened(result):
            row = result.get("row", {})
            if row.get("saLzkId") != record.sa_id:
                raise SafetyStop("网页返回 ID 不一致，请人工检查。")
            self.snapshot = row if action == "search" else None
            self.comparison = result.get("comparison") if action == "search" else None
            if action == "search":
                try:
                    self.sa_number.set(sa_claim_source(self.comparison)[1])
                except SafetyStop:
                    self.sa_number.set("未识别唯一括号编号")
            self.status.set("已定位对应详情，请在浏览器核验。" if action == "search" else "已打开窗口，请手动修改并保存。")
            self.refresh_approval()
        self.run(lambda: self.bridge.call(action, payload), opened, "正在定位当前记录，请勿同时操作该网页…",
                 log_action={"search": "定位网页", "open_metadata": "打开网页编辑", "open_claim": "打开网页认领"}[action])

    def next_record(self):
        if self.busy or not self.records:
            return
        index = self.records.index(self.current) + 1 if self.current in self.records else 0
        if index >= len(self.records):
            index = 0
        target = self.records[index].sa_id
        self.tree.selection_set(target)
        self.tree.see(target)
        self.select_record()

    def copy(self, value, status):
        if self.busy or not value:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(value)
        self.status.set(status)
        self.note_operation(status)

    def copy_id(self):
        if self.current:
            self.copy(self.current.sa_id, "ID 已复制，请在网页手动搜索。")

    def copy_sa_number(self):
        if self.busy:
            return
        try:
            if not self.current or not self.snapshot:
                raise SafetyStop("请先选择记录并定位网页。")
            _, identifier = sa_claim_source(self.comparison)
            self.copy(identifier, "SA 括号编号已复制，前导 0 已保留。")
        except SafetyStop as exc:
            self.note_operation("复制 SA 编号", "已暂停")
            messagebox.showwarning("暂未复制", str(exc), parent=self.root)

    def claim_payload(self):
        if not self.current or not self.roster or self.current.done or not self.snapshot:
            raise SafetyStop("请先在人工页选择未完成记录，并定位网页。")
        self.roster.assert_unchanged()
        if not self.bridge or not self.bridge.online:
            raise SafetyStop("请先连接浏览器。")
        source, identifier = sa_claim_source(self.comparison)
        if self.current.staff_id and self.current.staff_id != identifier:
            raise SafetyStop("SA 括号编号与名单工号不一致，请人工核对。")
        return {"sa_id": self.current.sa_id, "expected": self.snapshot, "sa_text": source,
                "staff_id": identifier, "roster_staff_id": self.current.staff_id}

    def prepare_claim(self):
        if self.busy:
            return
        try:
            payload = self.claim_payload()
            record = self.current
            self.clear_claim_preview()
            self.reviewed.set(False)
        except SafetyStop as exc:
            self.note_operation("查找认领人员", "已暂停")
            messagebox.showwarning("暂未查找", str(exc), parent=self.root)
            return
        def ready(result):
            prepared = result.get("prepared", {})
            person = prepared.get("person", {})
            if (result.get("row", {}).get("saLzkId") != record.sa_id or prepared.get("staff_id") != payload["staff_id"]
                    or person.get("wno") != payload["staff_id"] or not person.get("id") or not person.get("name")):
                raise SafetyStop("人员查找结果不一致，请重新定位。")
            self.prepared_claim = prepared
            self.claim_person.set(f"{person['name']} · {person['wno']}\n请核对人员与论文作者署名。")
            self.claim_options = [a for a in prepared.get("authors", []) if a.get("eligible") and not a.get("scholarId")]
            self.claim_author_box["values"] = [f"{a['order']}. {a['fullname']}" for a in self.claim_options]
            suggested = result.get("suggested_index")
            choices = [i for i, a in enumerate(self.claim_options) if a["index"] == suggested]
            if len(choices) == 1:
                self.claim_author_box.current(choices[0])
            self.refresh_approval()
            self.status.set("已找到唯一人员，尚未认领。请核对并选择对应作者。")
        self.run(lambda: self.bridge.call("prepare_claim", payload), ready, "正在按 SA 编号查找人员，请勿操作网页…",
                 log_action="查找认领人员")

    def submit_claim(self):
        if self.busy:
            return
        try:
            payload = self.claim_payload()
            prepared = self.prepared_claim
            choice = self.claim_author_box.current()
            if not prepared or not 0 <= choice < len(self.claim_options):
                raise SafetyStop("请先查找人员，再选择对应作者署名。")
            author, person, record = self.claim_options[choice], prepared["person"], self.current
            roster, bridge = self.roster, self.bridge
            snapshot, comparison = deepcopy(self.snapshot), deepcopy(self.comparison)
            auto_close = bool(self.auto_claim_completion.get() and record.owner == "谭勋策" and
                              record.matches == 1 and record.reason == "作者不一致" and
                              self.note.get("1.0", "end").strip() in ("", "已认领") and
                              snapshot.get("remark", "") in ("", "已认领"))
            consequence = ("\n认领核验成功后，将自动保存网页批注“已认领”和已处理状态，\n"
                           "回读成功后备份，并写入完成备注及是否识别=1。\n"
                           "请同时确认本条没有其他待处理问题；信息变化时停止。" if auto_close else
                           "\n本条不自动结案，Excel 保持未完成；核验后需单独审批。")
            question = (f"名单：{record.sa_id}\n{record.title[:100]}\n\n人员：{person['name']}（{person['wno']}）"
                        f"\n论文署名：第 {author['order']} 位 · {author['fullname']}\n\n确认该署名属于此人员，并向网站提交一次认领？"
                        + consequence)
            if not messagebox.askyesno("确认单条作者认领", question, parent=self.root):
                return
            payload.update(prepared=prepared, author_index=author["index"], confirmed=True)
            self.journal.save(record, "认领待提交", self.note.get("1.0", "end").strip(),
                              {"staff_id": person["wno"], "scholar_id": person["id"], "author": author["fullname"],
                               "order": author["order"], "auto_complete": auto_close})
            self.clear_browser_state()
            self.reviewed.set(False)
        except Exception as exc:
            self.note_operation("提交作者认领", "已暂停")
            messagebox.showwarning("暂未认领", str(exc), parent=self.root)
            return
        def claimed(result):
            verify_claim_result(record, result, person, author)
            self.use_remark("已认领")
            self.claim_person.set(f"已认领：{person['name']} · {author['fullname']}")
            self.status.set("认领已回读确认，备注已填“已认领”；核对后回人工页批准完成。")
            try:
                self.journal.save(record, "认领已核验", self.note.get("1.0", "end").strip(), result)
            except Exception:
                self.status.set("网页认领已成功，但本地日志失败；请人工核验，勿重复认领。")
                return
            if auto_close:
                self.journal.save(record, "认领结案待核验", "已认领", {"mode": "automatic_claim_completion"})
                def finish():
                    try:
                        return auto_complete_claim(roster, record, bridge, snapshot, comparison,
                                                   result, person, author, confirmed=True)
                    except Exception as exc:
                        raise SafetyStop("认领已成功，但自动批注/结案未全部完成。不要重复认领；重新定位核验后台。\n" + str(exc)) from exc
                self.run(finish, lambda value: self.claim_completion_saved(record, value, "automatic_claim_completion"),
                         "认领已核验，正在自动保存批注、回读状态并同步 Excel…",
                         log_action="自动批注结案")
        self.run(lambda: bridge.call("submit_claim", payload), claimed, "正在复核并提交单条认领，请勿操作网页…",
                 log_action="提交作者认领")

    def copy_note(self):
        self.copy(self.note.get("1.0", "end").strip(), "备注已复制，请在网页手动粘贴并保存。")

    def use_remark(self, value):
        if self.busy or not self.current or self.current.done:
            return
        updated = append_remark(self.note.get("1.0", "end"), value)
        self.note.delete("1.0", "end")
        self.note.insert("1.0", updated)
        self.reviewed.set(False)
        self.status.set("模板仅辅助填写，请人工确认适用条件。")
        self.note_operation("选择备注模板：" + value)

    def note_changed(self, _event=None):
        if self.note.edit_modified():
            self.reviewed.set(False)
            self.note.edit_modified(False)

    def confirm_manual_done(self):
        if self.busy:
            return
        try:
            record, roster = self.current, self.roster
            if not roster or not record or record not in self.records or record.done:
                raise SafetyStop("请先选择一条未完成记录。")
            if not self.reviewed.get():
                raise SafetyStop("请先完成人工审批，并勾选已核对。")
            roster.assert_unchanged()
            note = self.note.get("1.0", "end").strip()
            if not note:
                raise SafetyStop("请选择已核验的完成备注，或填写本次完成说明。")
            question = f"名单 ID：{record.sa_id}\n{record.title[:100]}\n\n确认这条记录已处理完成？\n将是否识别写为 1，前面备注填写：{note}\n此批准按钮不会修改网页。"
            if record.remark:
                question += f"\n\n原备注：{record.remark[:200]}\n旧值保存在备份中。"
            if not messagebox.askyesno("人工确认完成", question, parent=self.root):
                return
            self.journal.save(record, "人工批准待回写", note, {"row": record.row, "previous": record.remark})
        except Exception as exc:
            self.note_operation("人工批准完成", "已暂停")
            messagebox.showwarning("暂未完成", str(exc), parent=self.root)
            return
        def saved(result):
            self.roster = result.roster
            self.current = next(r for r in self.roster.records if r.row == record.row and r.sa_id == record.sa_id)
            self.populate()
            self.set_approval(True)
            self.model_panel.clear()
            self.status.set("已完成：是否识别=1，完成备注已保存。")
            try:
                self.journal.save(record, "已完成", note, {"cell": result.cell, "backup": str(result.backup),
                                                          "previous": result.previous, "mode": "manual"})
            except Exception as exc:
                self.status.set("名单已写为 1，但日志保存失败。请检查磁盘，勿重复确认。")
                messagebox.showwarning("名单已完成，日志异常", str(exc), parent=self.root)
        self.run(lambda: mark_complete(roster, record, note=note), saved, "正在备份并回写完成标记…",
                 log_action="人工批准完成")

    def confirm_claim_done(self):
        """Explicitly reviewed, single-record backend closure plus local sync."""
        if self.busy:
            return
        try:
            if not self.owner.get().strip():
                raise SafetyStop("请选择负责人。")
            record, roster = self.current, self.roster
            if (not roster or not record or record.owner != "谭勋策" or record.done or
                    record not in self.records or not self.reviewed.get()):
                raise SafetyStop("请选择谭勋策的一条待办，并勾选已核对。")
            if not self.bridge or not self.bridge.online or not self.snapshot or not self.comparison:
                raise SafetyStop("认领成功后请再次定位网页、核对结果，再进行结案。")
            if self.note.get("1.0", "end").strip() != "已认领":
                raise SafetyStop("此按钮仅保存“已认领”备注；其他问题请分别核验处理。")
            if record.matches != 1 or record.reason != "作者不一致":
                raise SafetyStop("此按钮仅用于单匹配、仅作者不一致的记录。")
            roster.assert_unchanged()
            snapshot, comparison = self.snapshot, self.comparison
            if not messagebox.askyesno("确认网页认领结案", f"名单：{record.sa_id}\n{record.title[:120]}\n\n"
                    "先重新核验已认领，再保存网页备注“已认领”并标为已处理。\n"
                    "后台回读成功后，备份 list.xlsx，填写完成备注及是否识别=1。\n"
                    "如果后台已处理，只同步 Excel，不重复提交。是否继续？", parent=self.root):
                return
            self.journal.save(record, "认领结案待核验", "已认领", {"mode": "reviewed_claim_completion"})
            self.reviewed.set(False)
        except Exception as exc:
            self.note_operation("网页认领结案", "已暂停")
            messagebox.showwarning("暂未结案", str(exc), parent=self.root)
            return

        self.run(lambda: complete_claim(roster, record, self.bridge, snapshot, comparison, reviewed=True),
                 lambda result: self.claim_completion_saved(record, result, "reviewed_claim_completion"),
                 "正在核验认领、保存网页状态并同步名单；不自动重试…",
                 log_action="网页认领结案")

    def claim_completion_saved(self, record, result, mode):
        completion = result.completion
        self.roster = completion.roster
        self.current = next(r for r in self.roster.records if r.row == record.row and r.sa_id == record.sa_id)
        self.clear_browser_state()
        self.populate()
        self.set_approval(True)
        self.model_panel.clear()
        self.status.set("后台原已处理，仅同步名单。" if result.already_processed else
                        "已结案：网页备注/状态已回读，Excel 已备份并标为 1。")
        try:
            self.journal.save(record, "已完成", result.row.get("remark", ""), {"mode": mode,
                "row": result.row, "cell": completion.cell, "backup": str(completion.backup),
                "already_processed": result.already_processed})
        except Exception:
            self.status.set("网页与 Excel 已完成，但日志失败。请检查备份，不要重复提交。")

    def show_guide(self):
        if not self.current:
            return
        popup = tk.Toplevel(self.root)
        popup.title("本条信息与处理指引")
        popup.transient(self.root)
        popup.attributes("-topmost", True)
        popup.geometry("520x480")
        record = self.current
        content = (f"ID：{record.sa_id}\n题名：{record.title}\nDOI：{record.doi or '—'}\nWOS：{record.wos or '—'}\n"
                   f"平台号：{record.item_ids or '—'}\n查询方式：{record.query}\n原完成备注：{record.remark or '空'}\n\n{guide(record)}")
        view = tk.Text(popup, wrap="word", font=("Microsoft YaHei UI", 10), padx=12, pady=12)
        bar = ttk.Scrollbar(popup, command=view.yview)
        view.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        view.pack(fill="both", expand=True)
        view.insert("1.0", content)
        view.configure(state="disabled")

    def help(self):
        os.startfile(BASE / "README.md")

    def close(self):
        if self.submission_panel and self.submission_panel.busy:
            self.closing=True
            self.submission_panel.request_stop()
            self.tabs.select(self.submission_page)
            return
        if self.classifier and self.classifier.busy:
            self.closing=True
            self.classifier.request_stop()
            self.classifier.status.set('正在保存当前批次，完成后关闭工作台。')
            self.tabs.select(self.classification_page)
            return
        if self.busy:
            messagebox.showwarning("正在处理", "请等待当前操作结束再关闭。模型请求可先点击“停止等待”。", parent=self.root)
            return
        for timer in (self.pump_id, self.load_id):
            if timer:
                self.root.after_cancel(timer)
        if self.bridge:
            self.bridge.close()
        if self.classifier:
            self.classifier.dispose()
        if self.submission_panel:
            self.submission_panel.dispose()
        self.journal.close()
        self.root.destroy()


def main(initial_tab=None):
    root = tk.Tk()
    try:
        App(root,unified=True,initial_tab=initial_tab)
        root.mainloop()
    except Exception as exc:
        messagebox.showerror("助手无法启动", f"{exc}\n请检查名单、依赖及目录权限。", parent=root)
        root.destroy()


if __name__ == "__main__":
    import argparse
    parser=argparse.ArgumentParser()
    parser.add_argument('--tab',choices=['manual','classification'],default='manual')
    main(initial_tab=parser.parse_args().tab)
