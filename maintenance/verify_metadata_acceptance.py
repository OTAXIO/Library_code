"""Independent read-only acceptance audit: report, two-source archive, list ZIP.

No server writes or browser operations are performed. Real evidence stays local.
"""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import xml.etree.ElementTree as ET
import zipfile

BASE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(BASE))
from core import SafetyStop, digest, file_hash, read_roster
NS={'x':'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}


def verify(before, after, targets, report):
    old,new=read_roster(before),read_roster(after)
    if len(old.records)!=len(new.records):raise SafetyStop('名单行数改变。')
    old_rows={r.sa_id:r for r in old.records};new_rows={r.sa_id:r for r in new.records}
    if set(old_rows)!=set(new_rows):raise SafetyStop('名单编号集合改变。')
    for key,record in new_rows.items():
        origin=old_rows[key]
        if key not in targets and record!=origin:raise SafetyStop('非目标记录改变。')
        if key in targets and (record.owner!='谭勋策' or record.key!=origin.key):raise SafetyStop('目标负责人或任务事实改变。')
    allowed=set()
    from openpyxl.utils import get_column_letter
    for key in targets:
        r=new_rows[key]
        allowed.update(get_column_letter(col)+str(r.row) for col in (new.remark_column,new.source_column,new.completion_column))
    changed=[]
    with zipfile.ZipFile(before) as b,zipfile.ZipFile(after) as a:
        if set(b.namelist())!=set(a.namelist()):raise SafetyStop('名单 ZIP 成员集合改变。')
        for name in a.namelist():
            first,last=b.read(name),a.read(name)
            if first==last:continue
            if not name.startswith('xl/worksheets/sheet') or not name.endswith('.xml'):
                raise SafetyStop('目标单元格外的工作簿部件改变。')
            left={c.get('r'):ET.tostring(c) for c in ET.fromstring(first).findall('.//x:sheetData/x:row/x:c',NS)}
            right={c.get('r'):ET.tostring(c) for c in ET.fromstring(last).findall('.//x:sheetData/x:row/x:c',NS)}
            differences={key for key in set(left)|set(right) if left.get(key)!=right.get(key)}
            if differences-allowed:raise SafetyStop('名单目标范围外的单元格改变。')
            changed.extend(sorted(differences))
    done=[]
    for item in report['outcomes']:
        if item['status']!='done':continue
        key=item['sa_id'];r=new_rows[key];proof=report['website_readback'][key]['row']
        if (key not in targets or not r.done or r.skipped or r.remark!='已入库' or 'Crossref+OpenAlex' not in r.source
          or proof.get('saLzkId')!=key or proof.get('markStatus')!='已处理' or proof.get('remark')!='已入库'
          or str(proof.get('matchCount'))!='1' or str(proof.get('itemId','')).lstrip(',')!=item['item_id']):
            raise SafetyStop('网页、结案或 list 未完全对应。')
        # Independent checks of the private durable evidence, not just the
        # success message emitted by the orchestration code.
        dbpath=BASE/'runtime/metadata-imports/zero-match.sqlite3'
        with sqlite3.connect(dbpath.resolve().as_uri()+'?mode=ro',uri=True) as db:
            saved=db.execute('SELECT record_key,data FROM jobs WHERE sa_id=?',(key,)).fetchone()
        if not saved or saved[0]!=r.key:raise SafetyStop('入库断点与名单事实不符。')
        state=json.loads(saved[1]);candidate=state.get('candidate',{})
        from metadata_pipeline import validate_batch
        from metadata_sources import merge_metadata
        batch=validate_batch(state.get('batch'),r,pushed=True)
        path=Path(state.get('path','')).resolve()
        if (state.get('phase')!='done' or state.get('item_id')!=item['item_id'] or batch['id']!=item['batch_id']
          or not path.is_relative_to(BASE/'runtime/metadata-imports/papers') or not path.is_file()
          or file_hash(path)!=state.get('xlsx_sha256')
          or digest({k:v for k,v in candidate.items() if k!='sha256'})!=candidate.get('sha256')):
            raise SafetyStop('新增批次、平台号或归档 Excel 未对应。')
        cache=json.loads((path.parent/'metadata.json').read_text(encoding='utf-8'))
        if (cache.get('record_key')!=r.key or merge_metadata(r,cache['crossref'],cache['openalex'])!=candidate):
            raise SafetyStop('两源原始记录未通过独立复核。')
        done.append(key)
    return {'verified':True,'newly_imported_and_closed':len(done),'completed_ids':done,
            'changed_cells':changed,'other_records_unchanged':True,'before_sha256':file_hash(before),'after_sha256':file_hash(after)}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--before',type=Path,required=True)
    p.add_argument('--report',type=Path,required=True);p.add_argument('--targets',nargs='+',required=True)
    args=p.parse_args();report=json.loads(args.report.read_text(encoding='utf-8'))
    print(json.dumps(verify(args.before,BASE/'list.xlsx',set(args.targets),report),ensure_ascii=False))
