"""Small local-status review window, separate from automatic task controls."""
import tkinter as tk
from tkinter import ttk

from core import SafetyStop
from manual_review import OWNER, STATES, state_of
from notices import messages
from ui_theme import P, style_text


class ManualReviewDialog:
    def __init__(self, parent, roster, on_save, selected_id=''):
        self.roster, self.on_save = roster, on_save
        self.window = tk.Toplevel(parent)
        self.window.title('手动核验名单 · 谭勋策')
        self.window.transient(parent)
        self.window.attributes('-topmost', True)
        self.window.geometry('720x500')
        self.window.minsize(600, 430)
        self.window.protocol('WM_DELETE_WINDOW', self.close)
        self.query = tk.StringVar()
        self.scope = tk.StringVar(value='全部')
        self.choice = tk.StringVar()
        self.result = tk.StringVar(value='只修改所选条目的名单状态和 ID 颜色，保留原备注。')
        frame = ttk.Frame(self.window, padding=14)
        frame.pack(fill='both', expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)
        ttk.Label(frame, text='手动核验名单', style='Title.TLabel').grid(row=0, column=0, sticky='w')
        filters = ttk.Frame(frame)
        filters.grid(row=1, column=0, sticky='ew', pady=10)
        filters.columnconfigure(1, weight=1)
        ttk.Label(filters, text='ID / 题名').grid(row=0, column=0, padx=(0, 8))
        self.search = ttk.Entry(filters, textvariable=self.query)
        self.search.grid(row=0, column=1, sticky='ew')
        ttk.Combobox(filters, textvariable=self.scope, state='readonly', width=9,
            values=['全部', '已完成', '未完成', '已跳过']).grid(row=0, column=2, padx=(8, 0))
        table = ttk.Frame(frame)
        table.grid(row=2, column=0, sticky='nsew')
        self.tree = ttk.Treeview(table, columns=('id', 'title', 'status'), show='headings', selectmode='browse')
        for key, title, width in (('id', '名单 ID', 200), ('title', '论文题名', 320), ('status', '名单状态', 80)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=65, stretch=key == 'title')
        for tag, color in (('done', P.green_soft), ('skipped', P.red_soft), ('pending', P.amber_soft)):
            self.tree.tag_configure(tag, background=color)
        self.tree.pack(side='left', fill='both', expand=True)
        scroll = ttk.Scrollbar(table, orient='vertical', command=self.tree.yview)
        scroll.pack(side='right', fill='y')
        self.tree.configure(yscrollcommand=scroll.set)
        self.detail = tk.Text(frame, height=3, wrap='word')
        style_text(self.detail)
        self.detail.grid(row=3, column=0, sticky='ew', pady=8)
        self.detail.configure(state='disabled')
        actions = ttk.Frame(frame)
        actions.grid(row=4, column=0, sticky='ew')
        ttk.Label(actions, text='核验结果').pack(side='left')
        self.choices = ttk.Combobox(actions, textvariable=self.choice, values=[s[0] for s in STATES.values()],
                                    state='readonly', width=9)
        self.choices.pack(side='left', padx=8)
        self.save_button = ttk.Button(actions, text='保存核验结果', style='Primary.TButton',
                                      command=self.save, state='disabled')
        self.save_button.pack(side='left')
        ttk.Button(actions, text='关闭', command=self.close).pack(side='right')
        result_label = ttk.Label(frame, textvariable=self.result, wraplength=660)
        result_label.grid(row=5, column=0, sticky='ew', pady=(8, 4))
        explanation = ttk.Label(frame, text='已完成：1 / 黄色；已跳过：2 / 红色；未完成：清空状态及状态填色。\n'
            '此处不验证系统入库，不操作网页；原入库、认领断点仍保留。',
            style='Muted.TLabel', wraplength=660)
        explanation.grid(row=6, column=0, sticky='w')
        frame.bind('<Configure>', lambda event: [label.configure(wraplength=max(200, event.width - 28))
                                                  for label in (result_label, explanation)])
        self.tree.bind('<<TreeviewSelect>>', lambda _: self.show_selected())
        self.query.trace_add('write', lambda *_: self.render())
        self.scope.trace_add('write', lambda *_: self.render())
        self.render()
        selected = [r for r in roster.records if r.owner == OWNER and r.sa_id == selected_id]
        if len(selected) == 1 and self.tree.exists(str(selected[0].row)):
            self.tree.selection_set(str(selected[0].row))
            self.tree.see(str(selected[0].row))
            self.show_selected()
        self.window.grab_set()

    def selected(self):
        selected = self.tree.selection()
        return next((r for r in self.roster.records if r.owner == OWNER and selected
                     and str(r.row) == selected[0]), None)

    def render(self):
        selected = self.tree.selection()
        self.tree.delete(*self.tree.get_children())
        query = self.query.get().strip().casefold()
        for r in self.roster.records:
            state = state_of(r)
            if (r.owner != OWNER or self.scope.get() not in ('全部', STATES[state][0])
                    or query and query not in (r.sa_id + ' ' + r.title).casefold()):
                continue
            self.tree.insert('', 'end', iid=str(r.row), values=(r.sa_id, r.title, STATES[state][0]), tags=(state,))
        if selected and self.tree.exists(selected[0]):
            self.tree.selection_set(selected[0])
        self.show_selected()

    def show_selected(self):
        record = self.selected()
        text = '选择一条记录；支持所有匹配数及原因的名单项。'
        self.choice.set(STATES[state_of(record)][0] if record else '')
        if record:
            text = f'{record.title}\n原备注：{record.remark or "空"}\n数据来源：{record.source or "未记录"}；原匹配数：{record.matches}'
        self.detail.configure(state='normal')
        self.detail.delete('1.0', 'end')
        self.detail.insert('1.0', text)
        self.detail.configure(state='disabled')
        self.save_button.configure(state='normal' if record else 'disabled')

    def save(self):
        record = self.selected()
        state = next((key for key, (label, _) in STATES.items() if label == self.choice.get()), None)
        if record is None or state is None:
            return
        self.save_button.configure(state='disabled')
        try:
            result = self.on_save(record.sa_id, state)
            self.roster = result.roster
            self.result.set(result.summary)
            self.render()
        except (SafetyStop, OSError) as exc:
            self.result.set(str(exc))
            messages.showwarning('核验未保存', str(exc), parent=self.window)
            self.save_button.configure(state='normal')

    def close(self):
        self.window.grab_release()
        self.window.destroy()
