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
from claim_reassignment import run_reassignment, validate_authorization
from claim_batch import run_claim_batch


def code_fingerprint(base):
    """A stopped acceptance must restart before executing changed source code."""
    paths = set(base.glob('*.py'))
    paths.update(base.glob('*.mjs'))
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


def claim_outcomes(selected, claim):
    done=set(claim.completed_ids)|set(claim.synced_ids)
    attempted={r.sa_id for r in [r for r in selected if not r.done][:claim.checked]}
    return [{'sa_id':r.sa_id,
        'status':'done' if r.sa_id in done else 'review' if r.sa_id in attempted else 'unexecuted',
        'message':'网页与名单已核验' if r.sa_id in done else
            claim.skipped.get(r.sa_id,claim.reason or '待核验') if r.sa_id in attempted else '尚未执行'}
        for r in selected]


def verify_completed(bridge, records):
    verified, errors = {}, {}
    for record in records:
        if not record.done:
            continue
        try:
            found=bridge.call('search',{'sa_id':record.sa_id,'close_detail':True},timeout=75)
            row=found.get('row',{})
            if (row.get('saLzkId')!=record.sa_id or row.get('gh')!=record.staff_id or
                    row.get('markStatus')!='已处理' or row.get('remark')!=record.remark):
                raise SafetyStop('网页完成状态或备注与名单不一致。')
            verified[record.sa_id]=found
        except Exception as exc:
            errors[record.sa_id]=str(exc)
    return verified, errors


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sa-ids',nargs='+',required=True)
    parser.add_argument('--control-file',type=Path)
    parser.add_argument('--reassignment-authorization', type=Path,
        help='本地保存的人已确认的单条新旧工号授权，不上传此文件')
    parser.add_argument('--claim-only', action='store_true', help='只验收原认领流程，不上传或导入论文')
    parser.add_argument('--recover-unconfirmed', action='store_true',
        help='仅对明确要求核查的单条旧提交执行一次完整核查恢复，原意图另行归档')
    parser.add_argument('--retry-skipped', action='store_true', help='明确续验这几个指定的跳过项，不清空状态或断点')
    args=parser.parse_args()
    loaded_code=code_fingerprint(BASE)
    if not 1<=len(args.sa_ids)<=5 or len(set(args.sa_ids))!=len(args.sa_ids):
        raise SafetyStop('只允许 1–5 条不同任务。')
    roster=read_roster(BASE/'list.xlsx')
    authorization = (json.loads(args.reassignment_authorization.read_text(encoding='utf-8'))
                     if args.reassignment_authorization else None)
    if authorization is not None and len(args.sa_ids) != 1:
        raise SafetyStop('认领纠正验收一次只允许一条已确认任务。')
    if authorization and args.claim_only: raise SafetyStop('普通认领与授权纠正不能混用。')
    if args.recover_unconfirmed and (authorization or args.claim_only or args.retry_skipped or len(args.sa_ids)!=1):
        raise SafetyStop('旧提交恢复必须是单条导入，不能与认领模式混用。')
    def select(current):
        result=[]
        for sa_id in args.sa_ids:
            rows=[r for r in current.records if r.sa_id==sa_id]
            if len(rows)!=1 or rows[0].owner!='谭勋策' or authorization is None and rows[0].matches!=(1 if args.claim_only else 0):
                raise SafetyStop('验收目标不是谭勋策未跳过的零匹配任务。')
            if authorization is not None: validate_authorization(rows[0], authorization)
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
    root=tk.Tk();root.title(('按完整工号纠正认领 · 单条验收 ' if authorization else '自动认领流程验收 ' if args.claim_only else '公开元数据与出版社原文 · 导入验收 ')+version);root.geometry('620x290')
    panel=ttk.Frame(root,padding=16);panel.pack(fill='both',expand=True)
    ttk.Label(panel,text=('只连接现有 SA 比对页；限定原授权的两个工号和这一行作者。\n由脚本核验撤销、重新认领、网页与名单批注结案。'
        if authorization else '只连接现有 SA 页，按完整工号核验原认领入口与批注结案。\n不上传论文，不覆盖已有其他人员的认领。' if args.claim_only else '仅配对现有 SA 比对页，并绑定现有后台导入页。\n不需要 WOS；配对后由脚本执行上传、推送、批注与结案。')).pack(anchor='w')
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
            if authorization:
                from types import SimpleNamespace
                updated=run_reassignment(BASE,current,selected[0],bridge,authorization,log.record,stop.is_set)
                result=SimpleNamespace(roster=updated,outcomes=[{'sa_id':selected[0].sa_id,
                    'status':'done','message':'按完整工号核验认领，网页与名单已认领批注结案'}],remaining=0,halted=False,reason='')
            elif args.claim_only:
                from types import SimpleNamespace
                capability=bridge.call('search',{'sa_id':selected[0].sa_id})
                if (capability.get('native_claim_response_protocol')!=1 or
                        capability.get('claim_queue_protocol')!=4 or capability.get('non_institution_author_protocol')!=1):
                    raise SafetyStop('请加载扩展 0.4.53 后继续原生认领回执验收；未提交认领。')
                if len({r.skipped for r in selected if not r.done})>1:
                    raise SafetyStop('认领验收仅接受同一待处理或跳过范围。')
                claim=run_claim_batch(current,selected,bridge,cancel=stop.is_set,progress=events.put,
                    audit=log.record,limit=len(selected),retry_skipped=any(r.skipped for r in selected))
                outcomes=claim_outcomes(selected,claim)
                result=SimpleNamespace(roster=claim.roster,outcomes=outcomes,
                    remaining=max(0,len(selected)-claim.checked),halted=claim.halted,reason=claim.reason)
            else:
                if args.recover_unconfirmed:
                    from metadata_recovery import recover_missing_import
                    recover_missing_import(BASE,current,selected[0],bridge,explicitly_requested=True,
                        cancel=stop.is_set,audit=log.record)
                result=run_metadata_batch(BASE,current,selected,bridge,stop,events.put,log.record,
                    retry_skipped=args.retry_skipped)
            verified,readback_errors=verify_completed(bridge,[r for r in result.roster.records
                if r.sa_id in {item['sa_id'] for item in result.outcomes if item['status']=='done'}])
            report={'started_roster_sha256':before,'final_roster_sha256':result.roster.sha256,
                'outcomes':result.outcomes,'remaining':result.remaining,'halted':result.halted,'reason':result.reason,
                'website_readback':verified,'readback_errors':readback_errors,'excel':[{'sa_id':r.sa_id,'done':r.done,'skipped':r.skipped,
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
    def readback():
        try:
            current=read_roster(BASE/'list.xlsx');selected=select(current)
            verified,errors=verify_completed(bridge,selected)
            report={'readonly':True,'website_readback':verified,'readback_errors':errors,
                'excel':[{'sa_id':r.sa_id,'done':r.done,'skipped':r.skipped,'remark':r.remark} for r in selected]}
            folder=BASE/'runtime/live-acceptance';folder.mkdir(parents=True,exist_ok=True)
            path=folder/(datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'-readonly.json')
            path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
            print(json.dumps({'readonly_report':str(path),'verified':list(verified),'errors':errors},ensure_ascii=False),flush=True)
            events.put(('finish','已完成只读核验，未提交业务写入。'))
        except Exception as exc:
            print(json.dumps({'readonly_error':str(exc)},ensure_ascii=False),flush=True)
            events.put(('finish','只读核验结束，未提交业务写入。'))
    def probe_staff():
        """Read-only native preparation; diagnose snapshot drift without cancelling."""
        try:
            from claim import sa_claim_source
            current=read_roster(BASE/'list.xlsx'); target=select(current)[0]
            found=bridge.call('search',{'sa_id':target.sa_id,'authorization':authorization})
            source,staff=sa_claim_source(found.get('comparison'))
            result=bridge.call('prepare_reassignment',{'sa_id':target.sa_id,'expected':found['row'],
                'sa_text':source,'staff_id':staff,'roster_staff_id':target.staff_id,'authorization':authorization})
            folder=BASE/'runtime/claim-reassignments';folder.mkdir(parents=True,exist_ok=True)
            path=folder/('probe-'+datetime.now().strftime('%Y%m%d-%H%M%S-%f')+'.json')
            path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps({'readonly_probe':str(path)},ensure_ascii=False),flush=True)
            events.put(('finish','只读工号快照已保存，没有撤销或重新认领。'))
        except Exception as exc:
            print(json.dumps({'probe_error':str(exc)},ensure_ascii=False),flush=True)
            events.put(('finish','只读检查结束；未提交认领。'))
    def pump():
        nonlocal busy,last_control_stamp
        if args.control_file and args.control_file.exists():
            stamp=args.control_file.stat().st_mtime_ns
            if stamp!=last_control_stamp:
                last_control_stamp=stamp
                command=args.control_file.read_text(encoding='utf-8').strip()
                if command in ('RUN','STATUS','PAUSE','QUIT','PROBE_STAFF'):commands.put(command)
        while not commands.empty():
            command=commands.get_nowait()
            if command=='RUN' and not busy:
                if code_fingerprint(BASE)!=loaded_code:
                    status.set('验收代码已更新，请重新启动本窗口；原配对码可复用，未执行任务。')
                    print('SOURCE_CHANGED_RESTART_REQUIRED',flush=True);continue
                if not bridge.online:print('NOT_CONNECTED',flush=True);continue
                busy=True;stop.clear();threading.Thread(target=run,daemon=True).start()
            elif command=='STATUS':print(json.dumps({'online':bridge.online,'busy':busy}),flush=True)
            elif command=='PROBE_STAFF' and authorization and not busy:
                busy=True;threading.Thread(target=probe_staff,daemon=True).start()
            elif command=='READBACK' and not busy:
                busy=True;threading.Thread(target=readback,daemon=True).start()
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
