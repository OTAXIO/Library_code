"""Explicit one-time new attempt after complete, fresh negative effect checks.

This does NOT declare the old request failed or remove its history. Ordinary
batches never invoke this operator recovery. Every new write still goes through
the existing intent journal and native duplicate checks.
"""
import copy
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import uuid

from core import SafetyStop, digest, file_hash
from metadata_excel import build_excel
from metadata_sources import KIND, MetadataClient, merge_metadata
from zero_match import WorkflowStore, library_items, verify_sa, verify_import_match


def recover_missing_import(base, roster, record, bridge, *, explicitly_requested=False,
                           cancel=lambda: False, audit=lambda *args: None, builder=build_excel):
    """Keep old unknown intent, validate no effect twice, then stage ONE attempt."""
    if (explicitly_requested is not True or record not in roster.records or record.owner != '谭勋策'
            or record.matches != 0 or record.done or record.skipped):
        raise SafetyStop('恢复仅限明确要求重查的谭勋策未完成、未跳过零匹配条目。')
    root=Path(base).resolve()/'runtime/metadata-imports';store=WorkflowStore(root)
    original=store.get(record)
    # A repeat invocation only resumes the NEW attempt's existing journal.
    # It must never reset that attempt to prepared/uploaded again.
    recovery=original.get('recovery')
    if isinstance(recovery,dict) and recovery.get('kind')=='explicit-missing-effect-v1':
        return original
    if recovery is not None:raise SafetyStop('未知的旧恢复记录，未覆盖。')
    if (original.get('phase')!='import_intent' or original.get('batch') or original.get('item_id')
            or original.get('candidate',{}).get('kind')!=KIND
            or original.get('instructions')!='SA补充-'+record.sa_id
            or original.get('upload',{}).get('uploaded') is not True
            or original.get('upload',{}).get('validated') is not True):
        raise SafetyStop('不是无批次号的旧 Excel 提交意图，未恢复或重传。')
    old_path=Path(original['path'])
    if (old_path.is_symlink() or not old_path.resolve().is_relative_to(root/'papers')
            or not old_path.is_file() or file_hash(old_path)!=original.get('xlsx_sha256')):
        raise SafetyStop('原 Excel 路径或哈希不一致，未恢复。')
    archive=old_path.parent/'metadata.json'
    if archive.is_symlink():raise SafetyStop('原元数据不能是链接。')
    cached=json.loads(archive.read_text(encoding='utf-8'))
    previous=original['candidate']
    if (cached.get('record_key')!=record.key or cached.get('candidate')!=previous
            or digest({k:v for k,v in previous.items() if k!='sha256'})!=previous.get('sha256')):
        raise SafetyStop('原提交与原始元数据归档不一致。')
    candidate=merge_metadata(record,cached['crossref'],cached['openalex'])
    changed={k for k in set(previous)|set(candidate) if previous.get(k)!=candidate.get(k)}
    allowed={'addresses','affiliation','author_mapping','fields','sha256'}
    changed_fields={k for k in set(previous['fields'])|set(candidate['fields'])
                    if previous['fields'].get(k)!=candidate['fields'].get(k)}
    if changed-allowed or changed_fields-{'作者','作者单位'}:
        raise SafetyStop('原始来源重建后的文献身份或书目改变，未恢复。')
    evidence={'record_key':record.key,'old_state_sha256':digest(original),
        'old_excel_sha256':original['xlsx_sha256'],'candidate_sha256':candidate['sha256'],
        'old_request_outcome':'unknown','source_repair_fields':sorted(changed_fields)}

    def call(action,payload):
        roster.assert_unchanged()
        if cancel():raise SafetyStop('已暂停，未恢复或重传。')
        return bridge.call(action,{'sa_id':record.sa_id,**payload},timeout=90)

    def status():
        answer=call('status',{});row=answer.get('row')
        verify_sa(record,row);verify_import_match(row,{})
        if row.get('markStatus')!='待处理':raise SafetyStop('后台不再是待处理，未恢复。')
        return row

    payload={'candidate':candidate,'instructions':original['instructions']}
    def scan():
        answer=call('import_excel_scan',{**payload,'all_sources':True})
        if (type(answer.get('scan_protocol')) is not int or answer['scan_protocol']!=1
                or answer.get('all_sources') is not True or answer.get('instructions')!=payload['instructions']
                or type(answer.get('scanned_count')) is not int or not 0<=answer['scanned_count']<=5000
                or answer.get('batches')!=[]):
            raise SafetyStop('全部来源批次未完整确认不存在，旧意图保留。')
        return answer

    evidence['status_before']=status()
    evidence['scan_before']=scan()
    library=call('import_excel_resolve',payload)
    if library_items(library):raise SafetyStop('本库已有对应文献，未重传；需核验既有条目关联。')
    evidence['library']=library
    # Empty native model probe also refuses an operator's unfinished wizard.
    probe=call('import_excel_probe',{'candidate':candidate})
    if (type(probe.get('metadata_excel_protocol')) is not int or probe['metadata_excel_protocol']!=1
            or probe.get('source')!='人工提交' or probe.get('model_id')!=original.get('model_id')):
        raise SafetyStop('原生模型或空白向导未确认，未恢复。')
    evidence['probe']=probe
    evidence['scan_after']=scan()
    if evidence['scan_after']['scanned_count']!=evidence['scan_before']['scanned_count']:
        raise SafetyStop('核查期间批次总数改变，保留旧意图。')
    evidence['status_after']=status()
    if evidence['status_after']!=evidence['status_before']:
        raise SafetyStop('核查期间 SA 数据改变，保留旧意图。')
    if getattr(bridge,'pending',None) is not None or getattr(bridge,'online',True) is False:
        raise SafetyStop('仍有命令执行或连接已断开，保留旧意图。')
    client=MetadataClient(root/'papers')
    folder=root/'papers'/('recovered-'+candidate['sha256'])
    replacement=copy.deepcopy(cached);replacement['candidate']=candidate
    replacement['repair']={'kind':'discard-affiliation-footnote-v1',
        'original_archive_sha256':file_hash(archive),'original_candidate_sha256':previous['sha256']}
    client.publish_cache(folder,replacement)
    rebuilt,_=client.read_cache(folder,record)
    if rebuilt!=candidate:raise SafetyStop('修复来源重建不一致。')
    path,checksum=builder(folder,candidate)
    if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()) or file_hash(path)!=checksum:
        raise SafetyStop('修复 Excel 哈希或路径未确认。')
    roster.assert_unchanged()
    if cancel():raise SafetyStop('已暂停，未恢复或重传。')
    if store.get(record)!=original:raise SafetyStop('核查期间旧断点改变，未恢复。')
    ticket=uuid.uuid4().hex
    archive_root=Path(base).resolve()/'runtime/import-recovery'/ticket
    archive_root.mkdir(parents=True,exist_ok=False)
    evidence.update(created_utc=datetime.now(timezone.utc).isoformat(),new_excel_sha256=checksum,
                    decision='explicit_one_time_new_attempt_with_old_unknown_intent_archived')
    report=archive_root/'evidence.json'
    with report.open('x',encoding='utf-8') as stream:
        json.dump({'old_state':original,'evidence':evidence},stream,ensure_ascii=False,indent=2)
        stream.flush();os.fsync(stream.fileno())
    with closing(sqlite3.connect(store.path)) as source,closing(sqlite3.connect(archive_root/'original-journal.sqlite3')) as dest:
        source.backup(dest)
    state={'phase':'prepared','candidate':candidate,'path':str(path),'xlsx_sha256':checksum,
        'instructions':original['instructions'],'recovery':{'kind':'explicit-missing-effect-v1',
        'ticket':ticket,'old_state_sha256':digest(original),'evidence_path':str(report),
        'evidence_sha256':file_hash(report)}}
    # Compare-and-swap prevents a concurrent journal update from being lost.
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        row=db.execute('SELECT record_key,data FROM jobs WHERE sa_id=?',(record.sa_id,)).fetchone()
        if not row or row[0]!=record.key or json.loads(row[1])!=original:
            raise SafetyStop('原意图已变化，保留归档但未恢复。')
        roster.assert_unchanged()
        db.execute('UPDATE jobs SET data=? WHERE sa_id=?',
            (json.dumps(state,ensure_ascii=False),record.sa_id))
    audit('完整核查无批次与本库记录，归档旧未知提交并准备一次新尝试','已执行',record.sa_id)
    return state
