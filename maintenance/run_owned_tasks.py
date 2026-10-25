"""Run the owned unfinished snapshot once through the existing production bridge.

The operator supplies fresh account-quota observations in a local JSON file.
No browser driver, model API, credentials or approval bypass is used here.
"""
import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re
import sys
import time
import traceback

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from bridge import Bridge
from batch_recovery import RecordReadback
from core import SafetyStop, read_roster
from claim_batch import run_claim_batch
from metadata_pipeline import run_metadata_batch
from metadata_sources import MetadataClient
from operation_log import OperationLog
from roster_annotations import annotate_roster, review_reason
from roster_write import mark_skipped_many, reconcile_processed
from workflow_service import supported_claim
from zero_match import verify_sa


class QuotaStop:
    def __init__(self, path, pause_path=None, *, now=time.time, max_age=180):
        self.path, self.pause_path = Path(path), pause_path
        self.now, self.max_age, self.reason = now, max_age, ''

    def is_set(self):
        if self.reason:
            return True
        try:
            value = json.loads(self.path.read_text(encoding='utf8'))
            remaining, observed = value['remaining_percent'], value['observed_at']
            if (type(remaining) not in (int, float) or not 0 <= remaining <= 100
                    or type(observed) not in (int, float) or not 0 <= self.now()-observed <= self.max_age):
                self.reason = '额度读数失效，停止新操作并保存断点。'
            elif remaining < 50:
                self.reason = '剩余额度低于 50%，停止新操作并保存断点。'
            elif self.pause_path and Path(self.pause_path).exists():
                self.reason = '已请求暂停，保存原断点。'
        except (OSError, ValueError, KeyError, TypeError):
            self.reason = '无法确认当前额度，停止新操作并保存断点。'
        return bool(self.reason)

    def wait(self, timeout=None):
        """Match threading.Event, checking quota/pause during native waits too."""
        deadline=None if timeout is None else time.monotonic()+max(0,timeout)
        while not self.is_set():
            left=None if deadline is None else deadline-time.monotonic()
            if left is not None and left<=0:return False
            time.sleep(.1 if left is None else min(.1,left))
        return True


def red_unresolved(roster, sa_id, message):
    record = next(r for r in roster.records if r.sa_id == sa_id)
    reason = review_reason(message)
    if record.done or not record.skipped or not reason:
        return roster
    return annotate_roster(roster, unresolved={sa_id: reason}).roster


def run_one(base, roster, record, bridge, stop, client, audit, progress):
    if stop.is_set():
        raise SafetyStop(stop.reason)
    if supported_claim(record):
        result = run_claim_batch(roster, [record], bridge, cancel=stop.is_set,
            progress=progress, audit=audit, limit=1, retry_skipped=record.skipped)
        finished = record.sa_id in result.completed_ids or record.sa_id in result.synced_ids
        message = (next(r for r in result.roster.records if r.sa_id==record.sa_id).remark or '后台原已处理') if finished else result.skipped.get(record.sa_id, result.reason)
        outcome = {'sa_id':record.sa_id, 'status':('synced' if record.sa_id in result.synced_ids else 'done') if finished else 'halted' if result.halted or result.cancelled else 'review', 'message':message}
        updated, halted = result.roster, result.halted or result.cancelled
    elif record.matches == 0:
        result = run_metadata_batch(base, roster, [record], bridge, stop, progress,
            audit, client=client, retry_skipped=record.skipped)
        outcome = result.outcomes[0] if result.outcomes else {'sa_id':record.sa_id,'status':'halted','message':result.reason or stop.reason}
        updated, halted = result.roster, result.halted or stop.is_set()
    else:
        # Existing role-only and multi-match tasks lack a source-backed safe
        # automatic decision. Verify the live row; never auto-merge candidates.
        row = bridge.call('status', {'sa_id':record.sa_id}, timeout=90).get('row')
        audit('复核未完成异常项', '已执行', record.sa_id)
        verify_sa(record, row, facts=False)
        if stop.is_set():
            raise SafetyStop(stop.reason)
        synced = reconcile_processed(roster, record, row)
        if synced:
            updated = synced.roster
            outcome = {'sa_id':record.sa_id, 'status':'synced', 'message':'后台原已处理，仅同步名单'}
        else:
            reason = ('匹配多条待合并' if record.matches > 1 else
                      '通讯作者待核验' if '通讯作者' in record.reason else
                      '第一作者待核验' if '第一作者' in record.reason else '处理方式待核验')
            updated = mark_skipped_many(roster, [record], reasons={record.sa_id:reason}).roster
            outcome = {'sa_id':record.sa_id,'status':'review','message':reason}
        halted = False
    if not halted and outcome['status'] not in ('done','synced','already_skipped'):
        updated = red_unresolved(updated, record.sa_id, outcome.get('detail') or outcome['message'])
        audit('重试后仍无法自动处理：'+outcome['message'], '已跳过', record.sa_id)
    elif not halted and outcome['status'] in ('done','synced'):
        updated=annotate_roster(updated,resolved_ids=[record.sa_id]).roster
    return updated, outcome, halted


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--quota-file', type=Path, required=True)
    parser.add_argument('--pause-file', type=Path)
    parser.add_argument('--resume', type=Path)
    args = parser.parse_args()
    stop = QuotaStop(args.quota_file, args.pause_file)
    if stop.is_set():
        raise SafetyStop(stop.reason)
    roster = read_roster(BASE/'list.xlsx')
    if args.resume:
        report = json.loads(args.resume.read_text(encoding='utf8'))
        if report.get('owner') != '谭勋策':
            raise SafetyStop('恢复报告不属于本负责人。')
    else:
        targets = [r for r in roster.records if r.owner=='谭勋策' and not r.done]
        targets.sort(key=lambda r:(0 if supported_claim(r) else 1 if r.matches==0 else 2,r.row))
        report = {'owner':'谭勋策','started':datetime.now().isoformat(),'initial_roster_sha256':roster.sha256,
                  'ids':[r.sa_id for r in targets], 'outcomes':[], 'halted':False,'reason':''}
    if report.get('reason'):
        report.setdefault('previous_stops',[]).append({'reason':report['reason'],
            **({'unexpected_error':report.pop('unexpected_error')} if report.get('unexpected_error') else {})})
    report['halted'], report['reason'] = False, ''
    root=BASE/'runtime/owned-task-runs';root.mkdir(exist_ok=True)
    path = args.resume or root/(datetime.now().strftime('%Y%m%d-%H%M%S')+'.json')
    log=OperationLog(BASE/'log.txt')
    def save():
        report['final_roster_sha256']=read_roster(BASE/'list.xlsx').sha256
        temporary=path.with_suffix('.tmp')
        temporary.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');temporary.replace(path)
    def progress(message):
        print(message,flush=True)
    def audit(action,outcome,sa_id):
        log.record(action,outcome,sa_id)
    bridge=Bridge()
    try:
        pair=json.loads((BASE/'runtime/metadata-acceptance-pair.json').read_text(encoding='utf8'))
        if not re.fullmatch(r'[A-Za-z0-9_-]{43}', pair.get('token','')):
            raise SafetyStop('本地配对记录不正确。')
        bridge.token=pair['token']
        deadline=time.monotonic()+65
        print('等待现有扩展连接；配对码不会输出。',flush=True)
        while not bridge.online and time.monotonic()<deadline:
            if stop.is_set():raise SafetyStop(stop.reason)
            time.sleep(.5)
        if not bridge.online:raise SafetyStop('现有 SA 扩展未恢复连接，尚未执行任务。')
        completed={r['sa_id'] for r in report['outcomes']}
        client=MetadataClient(BASE/'runtime/metadata-imports/papers',stop.is_set,progress)
        for number,sa_id in enumerate(report['ids'],1):
            if sa_id in completed:continue
            if stop.is_set():raise SafetyStop(stop.reason)
            roster=read_roster(BASE/'list.xlsx')
            matches=[r for r in roster.records if r.sa_id==sa_id]
            if len(matches)!=1 or matches[0].owner!='谭勋策':raise SafetyStop('原队列身份不唯一或负责人已变化。')
            record=matches[0]
            if not supported_claim(record):
                # Claim reads intentionally retain their exact detail. Release
                # only that known read-only drawer before changing workflows;
                # the adapter refuses editors, selections and unknown windows.
                previous=[x for x in report['outcomes'] if any(r.sa_id==x['sa_id'] and supported_claim(r) for r in roster.records)]
                previous_id=previous[-1]['sa_id'] if previous else None
                if previous_id and report.get('claim_boundary_released')!=previous_id:
                    answer=RecordReadback().call(bridge,'search',{'sa_id':previous_id,'close_detail':True},timeout=90)
                    previous_record=next(r for r in roster.records if r.sa_id==previous_id)
                    verify_sa(previous_record,answer.get('row'),facts=False)
                    if answer.get('detail_released') is not True:
                        raise SafetyStop('认领只读详情未确认收起，未执行后续任务。')
                    report['claim_boundary_released']=previous_id
                    audit('切换流程前收起原认领只读详情','已执行',previous_id);save()
            if record.done:
                outcome={'sa_id':sa_id,'status':'already_done','message':'已完成，未重复操作'}
                halted=False
            else:
                print(f'全量复核 {number}/{len(report["ids"])} · 名单行 {record.row}',flush=True)
                roster,outcome,halted=run_one(BASE,roster,record,bridge,stop,client,audit,progress)
            if halted:
                report['last_attempt']=outcome
                raise SafetyStop(outcome['message'] or stop.reason or '当前操作待核验，保留断点。')
            report['outcomes'].append(outcome);save()
            print(json.dumps({'processed':len(report['outcomes']),'total':len(report['ids']),'outcome':outcome},ensure_ascii=False),flush=True)
        report['finished']=datetime.now().isoformat()
    except SafetyStop as exc:
        report['halted'],report['reason']=True,str(exc)
        print(str(exc),flush=True)
    except Exception as exc:
        report['halted']=True
        report['reason']='运行异常 '+type(exc).__name__+'；保留断点，不自动重试写入。'
        report['unexpected_error']=traceback.format_exc()
        print(report['reason'],flush=True)
    finally:
        bridge.close();save()
        print(json.dumps({'report':str(path),'counts':dict(Counter(x['status'] for x in report['outcomes'])),
                          'processed':len(report['outcomes']),'total':len(report['ids']),
                          'halted':report['halted'],'reason':report['reason']},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
