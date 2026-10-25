"""Read current exact-ID match reasons without changing the exported task facts."""
import json
from pathlib import Path
from datetime import datetime,timezone,timedelta
from dataclasses import replace

from core import SafetyStop,read_roster
from batch_recovery import RecordReadback
from zero_match import verify_sa
from approval import claim_completion_note
from remarks import detail_value


def execute_current_matches(base,ids,bridge,stop):
    """Reuse native staff claim/closure; never upload or rewrite exported facts."""
    from approval import current_claim_record,auto_complete_claim,complete_claim,verify_claim_result,verified_claim_rejection
    from claim_batch import automatic_claim_selection
    from claim import sa_claim_source,inspect_staff_claim
    from roster_write import reconcile_processed,mark_skipped_many
    from roster_annotations import format_owned_status,review_reason
    from zero_match import WorkflowStore
    from metadata_pipeline import check_cross_workflow
    from operation_log import OperationLog
    if not ids or len(ids)>100 or len(ids)!=len(set(ids)):raise SafetyStop('当前匹配任务列表无效。')
    base=Path(base);store=WorkflowStore(base/'runtime/current-claims');log=OperationLog(base/'log.txt')
    report={'outcomes':[],'halted':False,'reason':''}
    path=base/'runtime/owned-task-runs/20261025-current-match-execution.json'
    class Checked:
        def call(self,action,payload,**kwargs):
            roster.assert_unchanged()
            if stop.is_set():raise SafetyStop(stop.reason)
            result=RecordReadback().call(bridge,action,payload,timeout=kwargs.get('timeout',90))
            log.record(action,'已执行',record.sa_id);return result
    checked=Checked()
    try:
        for sid in ids:
            if stop.is_set():raise SafetyStop(stop.reason)
            roster=read_roster(base/'list.xlsx');found=[r for r in roster.records if r.sa_id==sid]
            if len(found)!=1 or found[0].owner!='谭勋策':raise SafetyStop('当前任务身份不唯一。')
            record=found[0]
            if record.done:continue
            state=store.get(record)
            fresh=checked.call('search',{'sa_id':sid,'close_detail':True});row=fresh['row'];verify_sa(record,row)
            if state.get('phase') in ('claim_intent','claimed','complete_intent') and row.get('markStatus')=='已处理':
                raise SafetyStop('原认领或结案意图待完整核验，未仅凭状态覆盖。')
            synced=reconcile_processed(roster,record,row)
            if synced:
                roster=synced.roster;outcome={'sa_id':sid,'status':'synced','message':'后台原已处理'}
            else:
                context=current_claim_record(record,row,True);comparison=fresh['comparison']
                try:
                    identity=fresh.get('claim_identity',{})
                    if identity.get('state')=='认领不一致':raise SafetyStop('认领工号不一致')
                    claimed=detail_value(comparison,'认领状态','library')=='已认领'
                    note=claim_completion_note(context,comparison,claimed=claimed)
                    check_cross_workflow(base,record)
                    other=WorkflowStore(base/'runtime/metadata-imports').get(record)
                    if other.get('phase') not in (None,'prepared'):raise SafetyStop('原入库流程待续验')
                except SafetyStop as error:
                    short=review_reason(str(error)) or '作者信息待核验'
                    roster=mark_skipped_many(roster,[record],reasons={sid:short}).roster
                    outcome={'sa_id':sid,'status':'review','message':short}
                else:
                    if not claimed:
                        if state.get('phase') in ('claim_intent','claimed','complete_intent'):
                            raise SafetyStop('原认领回执待核验，禁止重提。')
                        source,staff=sa_claim_source(comparison)
                        payload={'sa_id':sid,'expected':row,'sa_text':source,'staff_id':staff,'roster_staff_id':record.staff_id}
                        prepared=checked.call('prepare_claim',payload)
                        prepared,person,author,_,_=automatic_claim_selection(record,row,comparison,prepared)
                        state={'phase':'claim_intent','expected':row,'staff_id':staff,'item_id':row['itemId']};store.save(record,state)
                        proof=checked.call('submit_claim',{**payload,'prepared':prepared,'author_index':author['index'],'confirmed':True})
                        rejection=verified_claim_rejection(proof,row,person,author)
                        if rejection:
                            state.update(phase='rejected',rejection=rejection);store.save(record,state)
                            if rejection['code'] in (401,403,600):raise SafetyStop('认领会话或权限未通过，暂停后续。')
                            roster=mark_skipped_many(roster,[record],reasons={sid:'认领被拒'}).roster
                            outcome={'sa_id':sid,'status':'review','message':'认领被拒','rejection':rejection}
                            roster=format_owned_status(roster,[sid]).roster
                            report['outcomes'].append(outcome);path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8');continue
                        verify_claim_result(record,proof,person,author);state['phase']='claimed';store.save(record,state)
                        closed=auto_complete_claim(roster,record,checked,row,comparison,proof,person,author,confirmed=True,note=note,current_match=True)
                    else:
                        inspect_staff_claim(record,checked,row,comparison)
                        state.update(phase='complete_intent',item_id=row['itemId']);store.save(record,state)
                        closed=complete_claim(roster,record,checked,row,comparison,reviewed=True,note=note,current_match=True)
                    roster=closed.completion.roster;state['phase']='done';store.save(record,state)
                    outcome={'sa_id':sid,'status':'done','message':note}
            roster=format_owned_status(roster,[sid]).roster
            report['outcomes'].append(outcome)
            path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    except SafetyStop as exc:report.update(halted=True,reason=str(exc))
    except Exception as exc:
        import traceback
        report.update(halted=True,reason='当前认领运行异常 '+type(exc).__name__,error=traceback.format_exc())
    finally:path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    return report


def run_current_matches(base,ids,bridge,stop):
    base=Path(base);roster=read_roster(base/'list.xlsx');by_id={r.sa_id:r for r in roster.records}
    if not ids or len(set(ids))!=len(ids) or any(sid not in by_id or by_id[sid].owner!='谭勋策' for sid in ids):
        raise SafetyStop('仅复核本人唯一的名单 ID。')
    report={'readonly':True,'targets':[],'errors':{},'started':datetime.now(timezone(timedelta(hours=8))).isoformat()}
    path=base/'runtime/owned-task-runs/20261025-nonzero-inspection.json'
    try:
        for sid in ids:
            if stop.is_set():raise SafetyStop(stop.reason)
            roster.assert_unchanged();record=by_id[sid]
            answer=RecordReadback().call(bridge,'search',{'sa_id':sid,'close_detail':True},timeout=90)
            verify_sa(record,answer.get('row'),facts=False)
            row=answer['row'];assessment='需人工复核'
            if str(row.get('matchCount'))=='1':
                live=replace(record,matches=1,reason=row.get('reason',''),item_ids=row.get('itemId',''))
                claimed=detail_value(answer.get('comparison'),'认领状态','library')=='已认领'
                try:
                    note=claim_completion_note(live,answer.get('comparison'),claimed=claimed)
                    assessment='可按工号核验后认领/结案：'+note
                except SafetyStop as exc:assessment=str(exc)
            elif str(row.get('matchCount'))=='0':assessment='当前仍为零匹配'
            else:assessment='多条匹配，需查重合并'
            report['targets'].append({'sa_id':sid,'list_row':record.row,'assessment':assessment,**answer})
            path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
            print(json.dumps({'row':record.row,'matches':row.get('matchCount'),'reason':row.get('reason'),'assessment':assessment},ensure_ascii=False),flush=True)
    except SafetyStop as exc:report['errors']['read']=str(exc)
    finally:path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8')
    return {'report':str(path),'count':len(report['targets']),'errors':report['errors']}
