"""Narrow, backed-up XLSX completion writes; never resave unrelated workbook parts.

Openpyxl is used by core only for reading/verification. The writer replaces one
cell in the original worksheet XML and copies every other ZIP member unchanged.
No browser commands, credentials, formulas or automatic approvals are involved.
"""
from __future__ import annotations

import os
import copy
import posixpath
import re
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from core import Record, Roster, SOURCE_HEADER, STATUS_HEADER, SafetyStop, file_hash, read_roster

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


@dataclass
class Completion:
    roster: Roster
    backup: Path
    cell: str
    previous: str


@dataclass
class RosterUpdate:
    roster: Roster
    backup: Path
    cells: tuple
    previous: dict


@dataclass
class SourceUpdate:
    roster: Roster
    backup: Path | None
    cells: tuple
    rows: tuple
    source: str


def column_name(number):
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


@contextmanager
def write_lock(path):
    lock = path.with_name(f".{path.name}.assistant.lock")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise SafetyStop("另一个助手正在回写。若上次异常退出，请先关闭所有助手，再检查并移除 .list.xlsx.assistant.lock 后重开。") from exc
    try:
        os.close(descriptor)
        yield
    finally:
        lock.unlink(missing_ok=True)


def worksheet_member(archive, name):
    if len(archive.namelist()) != len(set(archive.namelist())):
        raise SafetyStop("工作簿包含重复内部文件，停止回写。")
    if any(part.startswith("_xmlsignatures/") for part in archive.namelist()):
        raise SafetyStop("工作簿有数字签名，不能直接修改。")
    book = ET.fromstring(archive.read("xl/workbook.xml"))
    protection = book.find(f"{{{NS}}}workbookProtection")
    if protection is not None and any(protection.get(key) in {"1", "true"} for key in ("lockStructure", "lockWindows", "lockRevision")):
        raise SafetyStop("工作簿受保护，请人工处理。")
    sheets = [sheet for sheet in book.findall(f"{{{NS}}}sheets/{{{NS}}}sheet") if sheet.get("name") == name]
    if len(sheets) != 1:
        raise SafetyStop("工作表定位不唯一。")
    relations = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    links = [link for link in relations if link.get("Id") == sheets[0].get(f"{{{REL}}}id")]
    if len(links) != 1 or links[0].get("TargetMode") == "External":
        raise SafetyStop("无法定位名单工作表。")
    target = links[0].get("Target", "")
    result = posixpath.normpath(target.lstrip("/") if target.startswith("/") else posixpath.join("xl", target))
    if not result.startswith("xl/worksheets/") or result not in archive.namelist():
        raise SafetyStop("工作表路径异常。")
    return result


def patch_cell(data, reference, row_number, value=1):
    """Preserve XML namespaces, styles, extensions and all surrounding bytes."""
    if value is not None and (type(value) is not int or value not in (1, 2)):
        raise SafetyStop("名单状态只能写入数字 1（完成）、2（跳过）或清空跳过标记。")
    document = ET.fromstring(data)
    protection = document.find(f"{{{NS}}}sheetProtection")
    if document.tag != f"{{{NS}}}worksheet" or (protection is not None and protection.get("sheet", "1") not in {"0", "false"}):
        raise SafetyStop("工作表格式不支持或已受保护。")
    for merged in document.findall(f"{{{NS}}}mergeCells/{{{NS}}}mergeCell"):
        from openpyxl.utils.cell import range_boundaries, coordinate_to_tuple
        left, top, right, bottom = range_boundaries(merged.get("ref"))
        row, col = coordinate_to_tuple(reference)
        if left <= col <= right and top <= row <= bottom:
            raise SafetyStop("完成备注位于合并单元格，停止回写。")
    rows = [row for row in document.findall(f"{{{NS}}}sheetData/{{{NS}}}row") if row.get("r") == str(row_number)]
    if len(rows) != 1:
        raise SafetyStop("名单行定位不唯一。")
    cells = [cell for cell in rows[0] if cell.get("r") == reference]
    if len(cells) > 1 or any(cell.find(f"{{{NS}}}f") is not None for cell in cells):
        raise SafetyStop("备注是公式或重复单元格，不能覆盖。")
    xml = data.decode("utf-8")
    # Only accept ordinary Excel OOXML serialization; unfamiliar layouts pause.
    row_pattern = re.compile(r'<row\b[^>]*\br="' + str(row_number) + r'"[^>]*>.*?</row>', re.S)
    matches = list(row_pattern.finditer(xml))
    if len(matches) != 1:
        raise SafetyStop("未知工作表行格式，请人工处理。")
    match = matches[0]
    segment = match.group()
    # A greedy opening-tag match can consume '/>' and swallow the next cell.
    cell_pattern = re.compile(r'<c\b(?=[^>]*\br="' + reference + r'")[^>]*?(?:/>|>.*?</c>)', re.S)
    found = list(cell_pattern.finditer(segment))
    if len(found) != len(cells):
        raise SafetyStop("未知单元格格式，请人工处理。")
    if cells:
        # Reject value metadata that could require edits in other package parts.
        if set(cells[0].attrib) - {"r", "s", "t"} or any(child.tag not in {f"{{{NS}}}v", f"{{{NS}}}is"} for child in cells[0]):
            raise SafetyStop("备注包含特殊元数据，请人工处理。")
        start = found[0].group().split(">", 1)[0].rstrip("/")
        start = re.sub(r'\s+t="[^"]*"', "", start)
        replacement = start + "/>" if value is None else start + f' t="n"><v>{value}</v></c>'
        segment = segment[:found[0].start()] + replacement + segment[found[0].end():]
    else:
        if value is None:
            raise SafetyStop("跳过标记单元格已不存在，请重新读取名单。")
        replacement = f'<c r="{reference}" t="n"><v>{value}</v></c>'
        from openpyxl.utils.cell import coordinate_to_tuple
        destination = coordinate_to_tuple(reference)[1]
        offset = segment.rfind("</row>")
        for cell in re.finditer(r'<c\b[^>]*\br="([A-Z]+[0-9]+)"', segment):
            if coordinate_to_tuple(cell.group(1))[1] > destination:
                offset = cell.start()
                break
        segment = segment[:offset] + replacement + segment[offset:]
    changed = (xml[:match.start()] + segment + xml[match.end():]).encode("utf-8")
    ET.fromstring(changed)
    return changed


def patch_text_cell(data, reference, row_number, value, expand_dimension=False):
    """Insert or replace one plain inline-string cell without resaving the workbook."""
    from html import escape
    from openpyxl.utils.cell import coordinate_to_tuple, get_column_letter, range_boundaries

    if not isinstance(value, str) or not value or len(value) > 2000 or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", value):
        raise SafetyStop("备注或数据来源文本无效，停止回写。")
    document = ET.fromstring(data)
    protection = document.find(f"{{{NS}}}sheetProtection")
    if document.tag != f"{{{NS}}}worksheet" or (protection is not None and protection.get("sheet", "1") not in {"0", "false"}):
        raise SafetyStop("工作表格式不支持或已受保护。")
    target_row, target_column = coordinate_to_tuple(reference)
    for merged in document.findall(f"{{{NS}}}mergeCells/{{{NS}}}mergeCell"):
        left, top, right, bottom = range_boundaries(merged.get("ref"))
        if left <= target_column <= right and top <= target_row <= bottom:
            raise SafetyStop("数据来源位于合并单元格，停止回写。")
    rows = [row for row in document.findall(f"{{{NS}}}sheetData/{{{NS}}}row") if row.get("r") == str(row_number)]
    if len(rows) != 1:
        raise SafetyStop("名单行定位不唯一。")
    cells = [cell for cell in rows[0] if cell.get("r") == reference]
    if len(cells) > 1 or any(cell.find(f"{{{NS}}}f") is not None for cell in cells):
        raise SafetyStop("数据来源是公式或重复单元格，不能覆盖。")
    xml = data.decode("utf-8")
    row_pattern = re.compile(r'<row\b[^>]*\br="' + str(row_number) + r'"[^>]*>.*?</row>', re.S)
    matches = list(row_pattern.finditer(xml))
    if len(matches) != 1:
        raise SafetyStop("未知工作表行格式，请人工处理。")
    match = matches[0]
    segment = match.group()
    cell_pattern = re.compile(r'<c\b(?=[^>]*\br="' + reference + r'")[^>]*?(?:/>|>.*?</c>)', re.S)
    found = list(cell_pattern.finditer(segment))
    if len(found) != len(cells):
        raise SafetyStop("未知单元格格式，请人工处理。")
    content = f'<is><t>{escape(value, quote=False)}</t></is>'
    if cells:
        if set(cells[0].attrib) - {"r", "s", "t"} or any(
                child.tag not in {f"{{{NS}}}v", f"{{{NS}}}is"} for child in cells[0]):
            raise SafetyStop("数据来源包含特殊元数据，请人工处理。")
        start = found[0].group().split(">", 1)[0].rstrip("/")
        start = re.sub(r'\s+t="[^"]*"', "", start)
        replacement = start + f' t="inlineStr">{content}</c>'
        segment = segment[:found[0].start()] + replacement + segment[found[0].end():]
    else:
        style = ""
        previous = []
        for cell in rows[0]:
            cell_reference = cell.get("r", "")
            try:
                column = coordinate_to_tuple(cell_reference)[1]
            except ValueError:
                continue
            if column < target_column:
                previous.append((column, cell.get("s")))
        if previous and max(previous)[1] is not None:
            style = f' s="{max(previous)[1]}"'
        replacement = f'<c r="{reference}"{style} t="inlineStr">{content}</c>'
        offset = segment.rfind("</row>")
        for cell in re.finditer(r'<c\b[^>]*\br="([A-Z]+[0-9]+)"', segment):
            if coordinate_to_tuple(cell.group(1))[1] > target_column:
                offset = cell.start()
                break
        segment = segment[:offset] + replacement + segment[offset:]
    # Some producers emit an advisory row span. Keep it consistent with the new cell.
    opening_end = segment.find(">")
    opening = segment[:opening_end + 1]
    span = re.search(r'\bspans="(\d+):(\d+)"', opening)
    if span and int(span.group(2)) < target_column:
        opening = opening[:span.start()] + f'spans="{span.group(1)}:{target_column}"' + opening[span.end():]
        segment = opening + segment[opening_end + 1:]
    changed_text = xml[:match.start()] + segment + xml[match.end():]
    if expand_dimension:
        dimensions = list(re.finditer(r'(<dimension\b[^>]*\bref=")([^"]+)(")', changed_text))
        if len(dimensions) > 1:
            raise SafetyStop("工作表范围信息不唯一，停止回写。")
        if dimensions:
            dim = dimensions[0]
            left, top, right, bottom = range_boundaries(dim.group(2))
            if target_column > right or target_row > bottom:
                new_ref = f"{get_column_letter(left)}{top}:{get_column_letter(max(right, target_column))}{max(bottom, target_row)}"
                changed_text = changed_text[:dim.start(2)] + new_ref + changed_text[dim.end(2):]
    changed = changed_text.encode("utf-8")
    ET.fromstring(changed)
    return changed


def _validated_source(source):
    value = str(source or "").strip()
    if (not value or len(value) > 40 or re.search(r"[\x00-\x1f；;]", value)
            or value[0] in "=+-@"):
        raise SafetyStop("数据来源名称无效；请使用不含分隔符的简短名称。")
    return value


def _append_source(existing, source):
    values = [part.strip() for part in re.split(r"[；;]", existing or "") if part.strip()]
    if source.casefold() not in {value.casefold() for value in values}:
        values.append(source)
    return "；".join(values)


def record_data_sources(roster, records=(), source="WOS", backup_dir=None):
    """Append one verified provenance label to selected rows and ensure the last column.

    The method is deliberately independent from the numeric workflow flag. A skipped
    row can have a successfully exported source, while a failed search remains blank.
    """
    source = _validated_source(source)
    records = list(records)
    if roster.path.name.lower() != "list.xlsx" or not roster.sheet_name:
        raise SafetyStop("只允许回写当前 code/list.xlsx。")
    if len({record.row for record in records}) != len(records) or any(record not in roster.records for record in records):
        raise SafetyStop("数据来源目标行重复或不属于当前名单，请重新读取。")
    destination = roster.source_column or roster.header_column_count + 1
    if destination <= 0 or destination > 16384:
        raise SafetyStop("无法确定数据来源列。")
    if not roster.source_column and destination != roster.header_column_count + 1:
        raise SafetyStop("数据来源必须追加在名单最后一列。")
    intended_values = {record.row: _append_source(record.source, source) for record in records}
    changed_rows = [record for record in records if intended_values[record.row] != record.source]
    header_needed = not roster.source_column
    if not header_needed and not changed_rows:
        return SourceUpdate(roster, None, (), (), source)
    path = roster.path
    temporary = None
    try:
        with write_lock(path):
            if path.with_name("~$" + path.name).exists():
                raise SafetyStop("Excel 正在使用名单。请保存并关闭 list.xlsx，再重新读取后继续。")
            roster.assert_unchanged()
            descriptor, name = tempfile.mkstemp(prefix=".list-source-", suffix=".xlsx", dir=path.parent)
            os.close(descriptor)
            temporary = Path(name)
            references = []
            with zipfile.ZipFile(path) as original, zipfile.ZipFile(temporary, "w") as output:
                member = worksheet_member(original, roster.sheet_name)
                changed = original.read(member)
                if header_needed:
                    reference = f"{column_name(destination)}1"
                    changed = patch_text_cell(changed, reference, 1, SOURCE_HEADER, expand_dimension=True)
                    references.append(reference)
                for record in changed_rows:
                    reference = f"{column_name(destination)}{record.row}"
                    changed = patch_text_cell(changed, reference, record.row, intended_values[record.row],
                                              expand_dimension=header_needed)
                    references.append(reference)
                output.comment = original.comment
                for entry in original.infolist():
                    output.writestr(copy.copy(entry), changed if entry.filename == member else original.read(entry))
            verified = read_roster(temporary)
            if verified.source_column != destination:
                raise SafetyStop("数据来源列回读失败，原名单未修改。")
            from dataclasses import replace
            intended = [replace(record, source=intended_values.get(record.row, record.source))
                        for record in roster.records]
            if verified.records != intended:
                raise SafetyStop("回读发现数据来源以外的内容发生变化，原名单未修改。")
            backups = Path(backup_dir) if backup_dir else path.parent / "runtime" / "backups"
            backups.mkdir(parents=True, exist_ok=True)
            backup = backups / f"list-{roster.sha256}.xlsx"
            if not backup.exists():
                with path.open("rb") as source_stream, backup.open("xb") as target:
                    import shutil
                    shutil.copyfileobj(source_stream, target)
                    target.flush()
                    os.fsync(target.fileno())
            if file_hash(backup) != roster.sha256:
                raise SafetyStop("备份校验失败，未修改名单。请检查备份目录。")
            roster.assert_unchanged()
            if path.with_name("~$" + path.name).exists():
                raise SafetyStop("名单又被 Excel 打开，暂停回写。")
            with temporary.open("rb+") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
            verified.path = path
            verified.mtime_ns = path.stat().st_mtime_ns
            verified.assert_unchanged()
            return SourceUpdate(verified, backup, tuple(references),
                                tuple(record.row for record in changed_rows), source)
    except PermissionError as exc:
        raise SafetyStop("名单被占用或没有写权限。请保存并关闭 Excel，再重新读取；数据来源尚未写入。") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _mark_values(roster, updates, backup_dir=None, notes=None):
    """Atomically write one or more workflow states after full-file verification."""
    path = roster.path
    if path.name.lower() != "list.xlsx" or not roster.completion_column:
        raise SafetyStop("只允许回写当前 list.xlsx 的完成备注列。")
    updates = list(updates)
    notes = dict(notes or {})
    if not updates:
        raise SafetyStop("没有需要写入的名单状态。")
    rows = set()
    for record, value in updates:
        valid_value = value is None or (type(value) is int and value in (1, 2))
        if record not in roster.records or record.done or not valid_value:
            raise SafetyStop("任务已完成、状态无效或不属于当前名单，请重新读取。")
        if record.row in rows or (value == 2 and record.skipped and record.sa_id not in notes) or (value is None and not record.skipped):
            raise SafetyStop("跳过任务已标记或目标行重复，请重新读取。")
        if roster.status_separate and value in (1, 2):
            note = notes.get(record.sa_id)
            if not isinstance(note, str) or not note.strip() or len(note) > 2000:
                raise SafetyStop("请填写本次完成备注或跳过原因，再写入是否识别。")
        rows.add(record.row)
    if set(notes) - {record.sa_id for record, _ in updates}:
        raise SafetyStop("备注不属于本次目标行，未回写。")
    temporary = None
    try:
        with write_lock(path):
            if path.with_name("~$" + path.name).exists():
                raise SafetyStop("Excel 正在使用名单。请保存并关闭 list.xlsx，再重新读取后确认。")
            roster.assert_unchanged()
            descriptor, name = tempfile.mkstemp(prefix=".list-write-", suffix=".xlsx", dir=path.parent)
            os.close(descriptor)
            temporary = Path(name)
            with zipfile.ZipFile(path) as source, zipfile.ZipFile(temporary, "w") as output:
                member = worksheet_member(source, roster.sheet_name)
                changed = source.read(member)
                references = []
                for record, value in updates:
                    reference = f"{column_name(roster.completion_column)}{record.row}"
                    references.append(reference)
                    changed = patch_cell(changed, reference, record.row, value)
                    if roster.status_separate and record.sa_id in notes:
                        note_ref = f"{column_name(roster.remark_column)}{record.row}"
                        note = notes[record.sa_id].strip()
                        changed = (patch_cell(changed, note_ref, record.row, None)
                                   if value is None and not note else
                                   patch_text_cell(changed, note_ref, record.row, note))
                        references.append(note_ref)
                output.comment = source.comment
                for entry in source.infolist():
                    output.writestr(copy.copy(entry), changed if entry.filename == member else source.read(entry))
            verified = read_roster(temporary)
            target_values = {record.row: (record, value) for record, value in updates}
            for row_number, (record, value) in target_values.items():
                expected = [r for r in verified.records if r.row == row_number and r.sa_id == record.sa_id]
                state_ok = (value == 1 and expected and expected[0].done and not expected[0].skipped) or \
                           (value == 2 and expected and expected[0].skipped and not expected[0].done) or \
                           (value is None and expected and not expected[0].done and
                            not expected[0].skipped and (roster.status_separate or expected[0].remark == ""))
                if len(expected) != 1 or not state_ok or expected[0].key != record.key:
                    fields = "目标行或 ID" if len(expected) != 1 else ("状态标记类型" if not state_ok else "其他任务字段")
                    raise SafetyStop(f"第 {row_number} 行回读失败（{fields}），原名单未修改。请重启最新版助手并重读名单；若仍失败，请反馈此行号。")
            from dataclasses import replace
            intended = [replace(r, done=target_values[r.row][1] == 1,
                                skipped=target_values[r.row][1] == 2,
                                remark=(notes.get(r.sa_id, r.remark).strip() if roster.status_separate else
                                        "" if target_values[r.row][1] is None else str(target_values[r.row][1])))
                        if r.row in target_values else r for r in roster.records]
            if verified.records != intended:
                raise SafetyStop("回读发现非目标行发生变化，原名单未修改。")
            # A content-addressed complete backup also preserves overwritten notes.
            backups = Path(backup_dir) if backup_dir else path.parent / "runtime" / "backups"
            backups.mkdir(parents=True, exist_ok=True)
            backup = backups / f"list-{roster.sha256}.xlsx"
            if not backup.exists():
                with path.open("rb") as source, backup.open("xb") as target:
                    import shutil
                    shutil.copyfileobj(source, target)
                    target.flush()
                    os.fsync(target.fileno())
            if file_hash(backup) != roster.sha256:
                raise SafetyStop("备份校验失败，未修改名单。请检查备份目录。")
            roster.assert_unchanged()
            if path.with_name("~$" + path.name).exists():
                raise SafetyStop("名单又被 Excel 打开，暂停回写。")
            with temporary.open("rb+") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
            verified.path = path
            verified.mtime_ns = path.stat().st_mtime_ns
            verified.assert_unchanged()
            return RosterUpdate(verified, backup, tuple(references),
                                {record.sa_id: record.remark for record, _ in updates})
    except PermissionError as exc:
        raise SafetyStop("名单被占用或没有写权限。请保存并关闭 Excel，再重新读取；当前状态尚未写入。") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def mark_complete(roster, record, backup_dir=None, note=None):
    """Back up and mark one verified record complete in the local roster."""
    result = _mark_values(roster, [(record, 1)], backup_dir,
                          {record.sa_id: note} if note is not None else None)
    return Completion(result.roster, result.backup, result.cells[0], result.previous[record.sa_id])


def migrate_status_column(roster):
    """One-time schema migration, not a business action on any owner's task.

    Preserve numeric 1/2 history; retain nonnumeric local notes and the separate
    backend remark. No claim/import/completion conclusion is invented here.
    All ZIP members except the roster sheet are kept byte-for-byte unchanged.
    """
    if roster.status_separate:
        return roster
    path=roster.path
    if path.name.lower()!='list.xlsx' or not roster.sheet_name:
        raise SafetyStop('只允许迁移 code/list.xlsx 的状态列。')
    destination=roster.header_column_count+1
    temporary=None
    try:
        with write_lock(path):
            if path.with_name('~$'+path.name).exists():
                raise SafetyStop('请保存并关闭 Excel，再迁移是否识别列。')
            roster.assert_unchanged()
            descriptor,name=tempfile.mkstemp(prefix='.list-status-',suffix='.xlsx',dir=path.parent)
            os.close(descriptor)
            temporary=Path(name)
            with zipfile.ZipFile(path) as original, zipfile.ZipFile(temporary,'w') as output:
                member=worksheet_member(original,roster.sheet_name)
                changed=patch_text_cell(original.read(member),f'{column_name(destination)}1',1,
                                        STATUS_HEADER,expand_dimension=True)
                for record in roster.records:
                    if record.done or record.skipped:
                        changed=patch_cell(changed,f'{column_name(destination)}{record.row}',record.row,
                                           1 if record.done else 2)
                        changed=patch_cell(changed,f'{column_name(roster.completion_column)}{record.row}',record.row,None)
                output.comment=original.comment
                for entry in original.infolist():
                    output.writestr(copy.copy(entry),changed if entry.filename==member else original.read(entry))
            verified=read_roster(temporary)
            from dataclasses import replace
            intended=[replace(record,remark='') if record.done or record.skipped else record
                      for record in roster.records]
            if verified.records!=intended or not verified.status_separate or verified.completion_column!=destination:
                raise SafetyStop('状态迁移回读不一致，原名单未修改。')
            backups=path.parent/'runtime'/'backups'
            backups.mkdir(parents=True,exist_ok=True)
            backup=backups/f'list-{roster.sha256}.xlsx'
            if not backup.exists():
                import shutil
                with path.open('rb') as source,backup.open('xb') as target:
                    shutil.copyfileobj(source,target)
                    target.flush()
                    os.fsync(target.fileno())
            if file_hash(backup)!=roster.sha256:
                raise SafetyStop('迁移备份校验失败，原名单未修改。')
            roster.assert_unchanged()
            if path.with_name('~$'+path.name).exists():
                raise SafetyStop('名单已被 Excel 打开，迁移未提交。')
            with temporary.open('rb+') as stream:
                os.fsync(stream.fileno())
            os.replace(temporary,path)
            temporary=None
            return read_roster(path)
    except PermissionError as exc:
        raise SafetyStop('名单被占用或无写入权限，是否识别列尚未迁移。') from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def mark_skipped_many(roster, records, backup_dir=None, reasons=None):
    """Back up and mark several safely skipped records with numeric 2."""
    return _mark_values(roster, [(record, 2) for record in records], backup_dir, reasons)


def clear_skipped_many(roster, records, backup_dir=None, *, clear_notes=False):
    """Clear numeric-2 flags; clearing their notes additionally requires opt-in.

    Never accepts completed records. Ordinary retry callers retain their original
    notes; an explicitly requested cleanup can clear only the same target cells.
    """
    if type(clear_notes) is not bool or clear_notes and not roster.status_separate:
        raise SafetyStop("清理备注须有独立是否识别列且明确指定清理，未回写。")
    records = list(records)
    return _mark_values(roster, [(record, None) for record in records], backup_dir,
                        {record.sa_id: "" for record in records} if clear_notes else None)


def cleanup_skipped_notes(roster, clear_records, short_notes, backup_dir=None):
    """Explicit, atomic local cleanup; never clears a completion or other owner.

    ``clear_records`` clears both the remark and 2. ``short_notes`` changes only
    the explanation of another skipped row, retaining its numeric-2 protection.
    No source, browser state, TXT, or operation journal is removed.
    """
    clear_records = list(clear_records)
    short_notes = dict(short_notes)
    clear_ids = {record.sa_id for record in clear_records}
    if len(clear_ids) != len(clear_records) or clear_ids & set(short_notes):
        raise SafetyStop("清理目标重复或交叉，未回写。")
    by_id = {record.sa_id: record for record in roster.records}
    if set(short_notes) - set(by_id) or not roster.status_separate or roster.remark_column != 1:
        raise SafetyStop("无法确认第一列备注、独立状态或清理目标，未回写。")
    targets = clear_records + [by_id[sa_id] for sa_id in short_notes]
    if any(record not in roster.records or record.owner != '谭勋策' or
           record.done or not record.skipped for record in targets):
        raise SafetyStop("只允许清理谭勋策已跳过记录，完成项和其他负责人不变。")
    return _mark_values(roster, [(record, None) for record in clear_records] +
                        [(by_id[sa_id], 2) for sa_id in short_notes], backup_dir,
                        {**short_notes, **{sa_id: '' for sa_id in clear_ids}})


def reconcile_processed(roster, record, remote_row, owner="谭勋策"):
    """Mirror an already-processed backend row; never change backend state here.

    Returns ``None`` only when the exact remote row is still pending. All
    unfamiliar or conflicting states stop before touching the workbook.
    """
    if record.owner != owner or record not in roster.records:
        raise SafetyStop("自动核验只允许处理谭勋策本人名单中的记录。")
    if record.done:
        raise SafetyStop("本地名单已经完成，请重新读取后跳过。")
    if not isinstance(remote_row, dict) or remote_row.get("saLzkId") != record.sa_id:
        raise SafetyStop("后台结果与名单 ID 不完全一致，禁止同步完成标记。")
    status = remote_row.get("markStatus")
    if status not in ("待处理", "已处理"):
        raise SafetyStop("后台标记状态未知，禁止同步完成标记。")
    roster.assert_unchanged()
    if status == "待处理":
        return None
    # Existing backend notes are evidence, not a guessed preset. If they are
    # blank, record only the verified processed state, without inventing a claim.
    remote_note = remote_row.get("remark", "")
    if not isinstance(remote_note, str):
        raise SafetyStop("后台备注格式未知，禁止同步。")
    if getattr(roster,'status_separate',False):
        return mark_complete(roster, record, note=remote_note.strip() or "后台已处理（只读核验同步）")
    return mark_complete(roster, record)
