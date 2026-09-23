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

from core import Record, Roster, SafetyStop, file_hash, read_roster

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


@dataclass
class Completion:
    roster: Roster
    backup: Path
    cell: str
    previous: str


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


def patch_cell(data, reference, row_number):
    """Preserve XML namespaces, styles, extensions and all surrounding bytes."""
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
        replacement = start + ' t="n"><v>1</v></c>'
        segment = segment[:found[0].start()] + replacement + segment[found[0].end():]
    else:
        replacement = f'<c r="{reference}" t="n"><v>1</v></c>'
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


def mark_complete(roster, record, backup_dir=None):
    """Only the UI's explicit, confirmed human action may call this function."""
    path = roster.path
    if path.name.lower() != "list.xlsx" or not roster.completion_column:
        raise SafetyStop("只允许回写当前 list.xlsx 的完成备注列。")
    if record not in roster.records or record.done:
        raise SafetyStop("该任务已完成或不属于当前名单，请重新读取。")
    temporary = None
    try:
        with write_lock(path):
            if path.with_name("~$" + path.name).exists():
                raise SafetyStop("Excel 正在使用名单。请保存并关闭 list.xlsx，再重新读取后确认。")
            roster.assert_unchanged()
            reference = f"{column_name(roster.completion_column)}{record.row}"
            descriptor, name = tempfile.mkstemp(prefix=".list-write-", suffix=".xlsx", dir=path.parent)
            os.close(descriptor)
            temporary = Path(name)
            with zipfile.ZipFile(path) as source, zipfile.ZipFile(temporary, "w") as output:
                member = worksheet_member(source, roster.sheet_name)
                changed = patch_cell(source.read(member), reference, record.row)
                output.comment = source.comment
                for entry in source.infolist():
                    output.writestr(copy.copy(entry), changed if entry.filename == member else source.read(entry))
            verified = read_roster(temporary)
            expected = [r for r in verified.records if r.row == record.row and r.sa_id == record.sa_id]
            if len(expected) != 1 or not expected[0].done or expected[0].key != record.key:
                fields = "目标行或 ID" if len(expected) != 1 else ("完成标记类型" if not expected[0].done else "其他任务字段")
                raise SafetyStop(f"第 {record.row} 行回读失败（{fields}），原名单未修改。请重启最新版助手并重读名单；若仍失败，请反馈此行号。")
            from dataclasses import replace
            intended = [replace(r, done=True, remark="1") if r.row == record.row else r for r in roster.records]
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
            return Completion(verified, backup, reference, record.remark)
    except PermissionError as exc:
        raise SafetyStop("名单被占用或没有写权限。请保存并关闭 Excel，再重新读取；当前条目尚未确认完成。") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
