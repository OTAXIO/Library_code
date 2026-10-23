"""Explicit small-batch live acceptance using ONLY the production extension.

Pair on existing SA/import tabs, return diagnostic tabs, then send RUN on stdin
or use --control-file with RUN/STATUS/PAUSE/QUIT.
No BrowserSkill, browser driver, credential store or WOS automation is imported.
"""
import argparse
import hashlib
import json
from pathlib import Path
import queue
import re
import sys
import threading
import tkinter as tk
from tkinter import ttk
from datetime import datetime

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
from bridge import Bridge
from core import SafetyStop, read_roster
from metadata_pipeline import run_metadata_batch
from operation_log import OperationLog
from pairing_ui import copy_pairing_code


def code_fingerprint(base):
    """A stopped acceptance must restart before executing changed source code."""
    paths = set(base.glob('*.py'))
    paths.update((base/'extension').glob('*.js'))
    paths.add(base/'extension/manifest.json')
    paths.add(base/'maintenance/metadata_acceptance.py')
    digest = hashlib.sha256()
    for path in sorted(paths):
        digest.update(path.relative_to(base).as_posix().encode('utf-8'))
        digest.update(b'\0')
        digest.update(path.read_bytes())
        digest.update(b'\0')
    return digest.hexdigest()


def control_stamp(path):
    return path.stat().st_mtime_ns if path and path.exists() else None


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sa-ids',nargs='+',required=True)
    parser.add_argument('--control-file',type=Path)
    args=parser.parse_args()
    loaded_code=code_fingerprint(BASE)
    if not 1<=len(args.sa_ids)<=5 or len(set(args.sa_ids))!=len(args.sa_ids):
        raise SafetyStop('只允许 1–5 条不同任务。')
    roster=read_roster(BASE/'list.xlsx')
    def select(current):
        result=[]
        for sa_id in args.sa_ids:
            rows=[r for r in current.records if r.sa_id==sa_id]
            if len(rows)!=1 or rows[0].owner!='谭勋策' or rows[0].matches!=0:
                raise SafetyStop('验收目标不是谭勋策未跳过的零匹配任务。')
            result.append(rows[0])
        return result
    select(roster)
    bridge=Bridge()
    # Keep this isolated acceptance's local loopback pairing across code-only
    # restarts. It is not a website credential and is never printed or pushed.
    pair_file=BASE/'runtime/metadata-acceptance-pair.json'
    if pair_file.exists():
        saved=json.loads(pair_file.read_text(encoding='utf-8'))
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}',saved.get('token','')):
            raise SafetyStop('本地验收配对记录异常。')
        bridge.token=saved['token']
    else:
        pair_file.parent.mkdir(parents=True,exist_ok=True)
        with pair_file.open('x',encoding='utf-8') as stream:json.dump({'token':bridge.token},stream)
    stop=threading.Event(); commands=queue.Queue(); events=queue.Queue()
    version=json.loads((BASE/'extension/manifest.json').read_text(encoding='utf-8'))['version']
    root=tk.Tk();root.title('公开元数据与官方论文集 · 导入验收 '+version);root.geometry('620x290')
    panel=ttk.Frame(root,padding=16);panel.pack(fill='both',expand=True)
    ttk.Label(panel,text='仅配对现有 SA 比对页，并绑定现有后台导入页。\n不需要 WOS；配对后由脚本执行上传、推送、批注与结案。').pack(anchor='w')
    code=tk.StringVar(value=bridge.token)
    entry=ttk.Entry(panel,textvariable=code,state='readonly');entry.pack(fill='x',pady=10)
    status=tk.StringVar(value='等待配对，尚未执行。')
    def copy():
        try:copy_pairing_code(root,bridge.token);status.set('配对码已复制。')
        except Exception:status.set('复制失败，请在码框 Ctrl+A/Ctrl+C。')
    ttk.Button(panel,text='复制配对码',command=copy).pack(anchor='w')
    ttk.Label(panel,textvariable=status,wraplength=585).pack(anchor='w',pady=12)
    def receive():
        for line in sys.stdin:commands.put(line.strip())
    threading.Thread(target=receive,daemon=True).start()
    busy=False
    # A RUN left by the previous process is not a new authorization to run.
    last_control_stamp=control_stamp(args.control_file)
    def run():
        try:
            current=read_roster(BASE/'list.xlsx'); selected=select(current)
            log=OperationLog(BASE/'log.txt')
            before=current.sha256
            result=run_metadata_batch(BASE,current,selected,bridge,stop,events.put,log.record)
            verified={}
            for item in result.outcomes:
                if item['status']=='done':
                    verified[item['sa_id']]=bridge.call('status',{'sa_id':item['sa_id']},timeout=75)
            report={'started_roster_sha256':before,'final_roster_sha256':result.roster.sha256,
                'outcomes':result.outcomes,'remaining':result.remaining,'halted':result.halted,'reason':result.reason,
                'website_readback':verified,'excel':[{'sa_id':r.sa_id,'done':r.done,'skipped':r.skipped,
                    'remark':r.remark,'source':r.source} for r in result.roster.records if r.sa_id in args.sa_ids]}
            folder=BASE/'runtime/live-acceptance';folder.mkdir(parents=True,exist_ok=True)
            path=folder/(datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'-metadata.json')
            with path.open('x',encoding='utf-8') as stream:json.dump(report,stream,ensure_ascii=False,indent=2)
            print(json.dumps({'report':str(path),'outcomes':result.outcomes,'halted':result.halted,'reason':result.reason,
                              'remaining':result.remaining},ensure_ascii=False),flush=True)
            events.put(('finish',result.reason or '本轮结束，回读证据已保存。'))
        except Exception as exc:
            message=str(exc) if isinstance(exc,SafetyStop) else f'验收异常 {type(exc).__name__}；未自动重复操作。'
            print(json.dumps({'error':message},ensure_ascii=False),flush=True);events.put(('finish',message))
    def pump():
        nonlocal busy,last_control_stamp
        if args.control_file and args.control_file.exists():
            stamp=args.control_file.stat().st_mtime_ns
            if stamp!=last_control_stamp:
                last_control_stamp=stamp
                command=args.control_file.read_text(encoding='utf-8').strip()
                if command in ('RUN','STATUS','PAUSE','QUIT'):commands.put(command)
        while not commands.empty():
            command=commands.get_nowait()
            if command=='RUN' and not busy:
                if code_fingerprint(BASE)!=loaded_code:
                    status.set('验收代码已更新，请重新启动本窗口；原配对码可复用，未执行任务。')
                    print('SOURCE_CHANGED_RESTART_REQUIRED',flush=True);continue
                if not bridge.online:print('NOT_CONNECTED',flush=True);continue
                busy=True;stop.clear();threading.Thread(target=run,daemon=True).start()
            elif command=='STATUS':print(json.dumps({'online':bridge.online,'busy':busy}),flush=True)
            elif command=='PAUSE':stop.set()
            elif command=='QUIT' and not busy:close();return
        while not events.empty():
            event=events.get_nowait()
            if isinstance(event,tuple):busy=False;status.set(event[1])
            else:status.set(event);print(event,flush=True)
        root.after(100,pump)
    def close():
        if busy:stop.set();status.set('等待当前操作回读后关闭。');return
        bridge.close();root.destroy()
    root.protocol('WM_DELETE_WINDOW',close);root.after(100,pump)
    print('PAIR_WINDOW_READY (token is shown only in local UI)',flush=True)
    root.mainloop()


if __name__=='__main__':main()
