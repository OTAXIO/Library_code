"""Local review notes and proven-new-admission ID highlights.

Preserve the original package. Only selected note/style attributes and appended
fill/xf definitions change; no browser calls or workflow journal writes occur.
The yellow fill template is authored with the bundled spreadsheet tool.
"""
from dataclasses import dataclass, replace
from pathlib import Path
import copy
import os
import re
import shutil
import tempfile
import zipfile
from xml.etree import ElementTree as ET

from core import SafetyStop, file_hash, read_roster
from roster_write import NS, column_name, patch_text_cell, worksheet_member, write_lock, _commit_verified

OWNER='谭勋策'
FILL=Path(__file__).with_name('resources')/'intake-yellow-fill.xml'


def review_reason(message):
    """Channel failures need recovery, not a permanent red-paper diagnosis."""
    message=str(message or '')
    if message.startswith('匹配数非 0，已跳过；未修改备注'):
        return ''
    if not message or re.search(r'HTTP\s*\d|TLS|SSL|超时|未连接|断开|连接异常|连接失败|验证码|登录|端口|拒绝访问|仍在执行|回执未确认|命令回执|额度|操作窗口|未关闭的窗口',message,re.I):
        return ''
    from skip_notes import known_skip_note
    short=known_skip_note(message)
    if short:return short
    message=re.sub(r'^(?:\s*(?:\[[^\]]*\]|【[^】]*】))+\s*','',message)
    return re.split(r'，未自动|。|；|（',message,maxsplit=1)[0].strip()[:70]


@dataclass
class AnnotationUpdate:
    roster: object
    backup: Path | None
    note_cells: tuple
    yellow_cells: tuple
    red_cells: tuple = ()
    restored_cells: tuple = ()


def verified_new_admission(record, proof):
    """A completed task alone is insufficient: require a counted NEW batch."""
    if (record.owner!=OWNER or not record.done or record.skipped or record.remark!='已入库'
            or not isinstance(proof,dict) or proof.get('phase')!='done'
            or not isinstance(proof.get('item_id'),str) or not re.fullmatch(r'\d{1,40}',proof['item_id'])):
        return False
    batch=proof.get('batch')
    if (not isinstance(batch,dict) or not isinstance(batch.get('id'),str) or not batch['id']
            or not isinstance(batch.get('modelId'),str) or not batch['modelId']
            or batch.get('source') not in ('人工提交','WOS') or batch.get('instructions')!='SA补充-'+record.sa_id):
        return False
    expected={'total':1,'actual':1,'fail':0,'status':2,'increase':1,
              'duplicateSkip':0,'duplicateIncreaseUpdate':0,'duplicateOverallCoverage':0}
    return all(type(batch.get(k)) in (str,int) and str(batch[k])==str(v) for k,v in expected.items())


def _shape(node):
    return node.tag,tuple(sorted(node.attrib.items())),tuple(_shape(child) for child in node)


class YellowStyles:
    def __init__(self, raw, color='FFFFFF00'):
        self.raw=raw.decode('utf-8');self.root=ET.fromstring(raw)
        self.fill=ET.fromstring(FILL.read_bytes())
        pattern=self.fill.find(f'{{{NS}}}patternFill')
        if (self.fill.tag!=f'{{{NS}}}fill' or pattern is None or pattern.get('patternType')!='solid'
                or pattern.find(f'{{{NS}}}fgColor') is None
                or pattern.find(f'{{{NS}}}fgColor').get('rgb')!='FFFFFF00'):
            raise SafetyStop('黄色填充模板不正确，未修改名单。')
        if color not in ('FFFFFF00', 'FFFF0000'):
            raise SafetyStop('不支持的 ID 填充颜色。')
        pattern.find(f'{{{NS}}}fgColor').set('rgb', color)
        self.fills=self.root.find(f'{{{NS}}}fills');self.xfs=self.root.find(f'{{{NS}}}cellXfs')
        for group in (self.fills,self.xfs):
            if group is None or group.get('count')!=str(len(group)):
                raise SafetyStop('名单样式计数不明确，未修改。')

    def append(self, label, group, node):
        matches=list(re.finditer(r'<'+label+r'\b[^>]*>.*?</'+label+r'>',self.raw,re.S))
        if len(matches)!=1:raise SafetyStop('未知名单样式格式，未修改。')
        match=matches[0];segment=match.group();opening,rest=segment.split('>',1)
        opening,count=re.subn(r'\bcount="\d+"','count="'+str(len(group)+1)+'"',opening)
        if count!=1:raise SafetyStop('未知名单样式计数，未修改。')
        child=ET.tostring(node,encoding='unicode')
        segment=opening+'>'+rest[:rest.rfind('</')]+child+'</'+label+'>'
        self.raw=self.raw[:match.start()]+segment+self.raw[match.end():];group.append(node)

    def yellow(self, original):
        if not 0<=original<len(self.xfs):raise SafetyStop('ID 单元格原样式无效。')
        original_xf=self.xfs[original]
        fid=original_xf.get('fillId','0')
        if not fid.isdigit() or int(fid)>=len(self.fills):raise SafetyStop('ID 单元格填充无效。')
        if _shape(self.fills[int(fid)])==_shape(self.fill):return original
        found=[i for i,node in enumerate(self.fills) if _shape(node)==_shape(self.fill)]
        if found:fill_id=found[0]
        else:
            fill_id=len(self.fills);self.append('fills',self.fills,copy.deepcopy(self.fill))
        xf=copy.deepcopy(original_xf);xf.set('fillId',str(fill_id));xf.set('applyFill','1')
        found=[i for i,node in enumerate(self.xfs) if _shape(node)==_shape(xf)]
        if found:return found[0]
        index=len(self.xfs);self.append('cellXfs',self.xfs,xf);return index

    def clear_red(self, original):
        return self.clear_status_fill(original, colors=('FFFF0000',))

    def clear_status_fill(self, original, colors=('FFFF0000', 'FFFFFF00')):
        """Remove workflow colors while retaining font, borders and number format."""
        if not 0<=original<len(self.xfs):raise SafetyStop('ID 单元格原样式无效。')
        original_xf=self.xfs[original];fid=original_xf.get('fillId','0')
        if not fid.isdigit() or int(fid)>=len(self.fills):raise SafetyStop('ID 单元格填充无效。')
        pattern=self.fills[int(fid)].find(f'{{{NS}}}patternFill')
        color=pattern.find(f'{{{NS}}}fgColor') if pattern is not None else None
        if pattern is None or pattern.get('patternType')!='solid' or color is None or color.get('rgb') not in colors:
            return original
        xf=copy.deepcopy(original_xf);xf.set('fillId','0');xf.set('applyFill','1')
        found=[i for i,node in enumerate(self.xfs) if _shape(node)==_shape(xf)]
        if found:return found[0]
        index=len(self.xfs);self.append('cellXfs',self.xfs,xf);return index


def _yellow_id(data, reference, styles, restore=False, *, pending=False):
    raw=data.decode('utf-8')
    matches=list(re.finditer(r'<c\b(?=[^>]*\br="'+reference+r'")[^>]*?(?:/>|>.*?</c>)',raw,re.S))
    if len(matches)!=1:raise SafetyStop('ID 单元格不是唯一的原始文本单元格。')
    match=matches[0];node=ET.fromstring('<root xmlns="'+NS+'">'+match.group()+'</root>')[0]
    if node.find(f'{{{NS}}}f') is not None:raise SafetyStop('ID 单元格是公式，未修改。')
    original=node.get('s','0')
    if not original.isdigit():raise SafetyStop('ID 单元格样式不正确。')
    selected=(styles.clear_status_fill(int(original)) if pending else
              styles.clear_red(int(original)) if restore else styles.yellow(int(original)))
    if selected==int(original):return data,False
    cell=match.group();opening,rest=cell.split('>',1)
    if re.search(r'\ss="\d+"',opening):opening=re.sub(r'\ss="\d+"',' s="'+str(selected)+'"',opening)
    else:opening+=' s="'+str(selected)+'"'
    return (raw[:match.start()]+opening+'>'+rest+raw[match.end():]).encode('utf-8'),True


def annotate_roster(roster, *, notes=None, new_admissions=None, unresolved=None, resolved_ids=(), status_ids=(), backup_dir=None):
    """Explicit notes keep flags unchanged; ID yellow requires saved new proof."""
    notes=dict(notes or {});new_admissions=dict(new_admissions or {});unresolved=dict(unresolved or {})
    resolved_ids=set(resolved_ids)
    status_ids=set(status_ids)
    if unresolved and new_admissions:
        raise SafetyStop('失败项填红和新增项填黄请分别保存。')
    if set(notes)&set(unresolved):
        raise SafetyStop('同一行不能同时提交两份不同备注。')
    by_id={r.sa_id:r for r in roster.records}
    if roster.path.name.lower()!='list.xlsx' or not roster.status_separate or not roster.remark_column:
        raise SafetyStop('仅允许原名单的独立备注和 ID 填充。')
    if (set(notes)|set(new_admissions)|set(unresolved)|resolved_ids|status_ids)-set(by_id):raise SafetyStop('备注或填充目标不在当前名单。')
    for sid in set(notes)|set(new_admissions)|set(unresolved)|resolved_ids|status_ids:
        if by_id[sid].owner!=OWNER:raise SafetyStop('仅允许处理谭勋策的名单。')
    if any(not (by_id[sid].done or by_id[sid].skipped) for sid in status_ids):
        raise SafetyStop('未处理项不能通过填色变成已完成或已跳过。')
    if any(not by_id[sid].done or by_id[sid].skipped for sid in resolved_ids):
        raise SafetyStop('只能给已核验完成项清除失败红色。')
    intended={}
    for sid,reason in unresolved.items():
        record=by_id[sid]
        if (record.done or not record.skipped or not isinstance(reason,str) or not reason.strip()
                or len(reason)>80 or re.search(r'[\x00-\x1f]',reason)):
            raise SafetyStop('仅能给已复核且仍跳过的未完成项填红，并填写短原因。')
        if reason.strip()!=record.remark:intended[sid]=reason.strip()
    for sid,note in notes.items():
        record=by_id[sid]
        if record.done or record.matches!=0 or not isinstance(note,str) or not note.strip() or len(note)>80 or re.search(r'[\x00-\x1f]',note):
            raise SafetyStop('复核备注目标或内容不正确，未修改。')
        parts=[x.strip() for x in record.remark.split('；') if x.strip()]
        if note.strip() not in parts:parts.append(note.strip())
        value='；'.join(parts)
        if value!=record.remark:intended[sid]=value
    for sid,proof in new_admissions.items():
        if not verified_new_admission(by_id[sid],proof):raise SafetyStop('缺少已完成且新增一篇的批次证据，未填黄。')
    roster.assert_unchanged();path=roster.path
    if path.with_name('~$'+path.name).exists():raise SafetyStop('请先保存关闭 Excel，尚未修改名单。')
    temporary=None
    try:
        with write_lock(path):
            roster.assert_unchanged()
            with zipfile.ZipFile(path) as original:
                member=worksheet_member(original,roster.sheet_name);changed=original.read(member)
                styles=YellowStyles(original.read('xl/styles.xml'), 'FFFF0000' if unresolved else 'FFFFFF00') if new_admissions or unresolved or resolved_ids or status_ids else None
                # Resolve the actual ID header, never assume a fixed column.
                from openpyxl import load_workbook
                book=load_workbook(path,read_only=True,data_only=False)
                labels=[c.value for c in next(book[roster.sheet_name].iter_rows(min_row=1,max_row=1))];book.close()
                if labels.count('sa_lzk表ID')!=1:raise SafetyStop('ID 表头不唯一。')
                id_column=column_name(labels.index('sa_lzk表ID')+1)
                note_cells=[];yellow_cells=[];red_cells=[];restored_cells=[]
                for sid,value in intended.items():
                    record=by_id[sid];ref=column_name(roster.remark_column)+str(record.row)
                    changed=patch_text_cell(changed,ref,record.row,value);note_cells.append(ref)
                for sid in new_admissions:
                    ref=id_column+str(by_id[sid].row);changed,did_change=_yellow_id(changed,ref,styles)
                    if did_change:yellow_cells.append(ref)
                for sid in unresolved:
                    ref=id_column+str(by_id[sid].row);changed,did_change=_yellow_id(changed,ref,styles)
                    if did_change:red_cells.append(ref)
                for sid in resolved_ids:
                    ref=id_column+str(by_id[sid].row);changed,did_change=_yellow_id(changed,ref,styles,restore=True)
                    if did_change:restored_cells.append(ref)
                for sid in sorted(status_ids,key=lambda key:by_id[key].row):
                    completed=by_id[sid].done
                    styles.fill.find(f'{{{NS}}}patternFill/{{{NS}}}fgColor').set('rgb','FFFFFF00' if completed else 'FFFF0000')
                    ref=id_column+str(by_id[sid].row);changed,did_change=_yellow_id(changed,ref,styles)
                    if did_change:(yellow_cells if completed else red_cells).append(ref)
                if not note_cells and not yellow_cells and not red_cells and not restored_cells:return AnnotationUpdate(roster,None,(),())
                ET.fromstring(changed)
                if styles:ET.fromstring(styles.raw)
                fd,name=tempfile.mkstemp(prefix='.list-annotations-',suffix='.xlsx',dir=path.parent);os.close(fd);temporary=Path(name)
                with zipfile.ZipFile(temporary,'w') as output:
                    output.comment=original.comment
                    for entry in original.infolist():
                        data=(changed if entry.filename==member else styles.raw.encode('utf-8')
                              if styles and entry.filename=='xl/styles.xml' else original.read(entry))
                        output.writestr(copy.copy(entry),data)
            verified=read_roster(temporary)
            expected=[replace(r,remark=intended.get(r.sa_id,r.remark)) for r in roster.records]
            if verified.records!=expected:raise SafetyStop('备注以外的名单数据发生变化，未保存。')
            backup_root=Path(backup_dir) if backup_dir else path.parent/'runtime/backups';backup_root.mkdir(parents=True,exist_ok=True)
            backup=backup_root/('list-'+roster.sha256+'.xlsx')
            if not backup.exists():
                with path.open('rb') as source,backup.open('xb') as target:shutil.copyfileobj(source,target);target.flush();os.fsync(target.fileno())
            if file_hash(backup)!=roster.sha256:raise SafetyStop('名单备份校验失败。')
            with temporary.open('rb+') as stream:os.fsync(stream.fileno())
            _commit_verified(temporary,roster,verified.sha256);temporary=None
            verified.path=path;verified.mtime_ns=path.stat().st_mtime_ns;verified.assert_unchanged()
            return AnnotationUpdate(verified,backup,tuple(note_cells),tuple(yellow_cells),tuple(red_cells),tuple(restored_cells))
    finally:
        if temporary is not None:temporary.unlink(missing_ok=True)


def highlight_new_admission(roster, record, proof):
    current=next((r for r in roster.records if r.sa_id==record.sa_id),None)
    if current is None or not verified_new_admission(current,proof):
        return AnnotationUpdate(roster,None,(),())
    return annotate_roster(roster,new_admissions={current.sa_id:proof})


def format_owned_status(roster, ids=None):
    """User-requested unified display: 1 yellow, 2 red; no data/status changes."""
    selected=set(ids) if ids is not None else {r.sa_id for r in roster.records if r.owner==OWNER}
    targets={r.sa_id for r in roster.records if r.sa_id in selected and r.owner==OWNER and (r.done or r.skipped)}
    if not targets:return AnnotationUpdate(roster,None,(),())
    return annotate_roster(roster,status_ids=targets)
