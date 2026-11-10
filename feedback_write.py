"""Locked, backed-up writes to the existing cross-review feedback workbook."""
import hashlib
import io
import os
import tempfile
from copy import copy
from pathlib import Path

from openpyxl import load_workbook

from core import SafetyStop, file_hash, is_assigned_owner
from roster_write import write_lock


HEADERS = ("Said", "问题备注", "负责人", "校对人")


def _snapshot(book):
    """Include formulas and cell types: cached values cannot verify a rewrite."""
    return [
        (sheet.title, tuple(tuple((cell.value, cell.data_type, cell.number_format,
                                  copy(cell.font), copy(cell.fill), copy(cell.border),
                                  copy(cell.alignment), copy(cell.protection),
                                  copy(cell.comment), copy(cell.hyperlink))
                                  for cell in row) for row in sheet.iter_rows()))
        for sheet in book.worksheets
    ]


def write_feedback(path, backup_dir, sid, note, owner, reviewer):
    if not is_assigned_owner(reviewer) or reviewer == owner:
        raise SafetyStop("请先在主界面负责人下拉框选好自己，校对人不能是被复核人。")
    path, backup_dir = Path(path), Path(backup_dir)
    temporary = None
    try:
        if not path.is_file():
            raise SafetyStop(f"复核反馈文件不存在：{path}。请先准备好四列表头的工作簿。")
        with write_lock(path):
            excel_lock = path.with_name("~$" + path.name)
            if excel_lock.exists():
                raise SafetyStop("Excel 正在使用复核反馈.xlsx，请先保存并关闭 Excel 后重试。")
            original = path.read_bytes()
            original_sha = hashlib.sha256(original).hexdigest()
            # Read from a closed byte snapshot so Windows never retains a file handle.
            book = load_workbook(io.BytesIO(original))
            try:
                sheet = book.worksheets[0]
                if tuple(cell.value for cell in sheet[1]) != HEADERS:
                    raise SafetyStop("复核反馈.xlsx 第一行表头必须依次为 Said | 问题备注 | 负责人 | 校对人。")
                seen, target = set(), None
                for row in sheet.iter_rows(min_row=2):
                    value = row[0].value
                    if value is None:
                        continue
                    key = str(value)
                    if key in seen:
                        raise SafetyStop(f"复核反馈.xlsx 中 Said {key} 出现多行，请先处理重复行。")
                    seen.add(key)
                    if key == sid:
                        if row[2].value != owner:
                            raise SafetyStop(f"复核反馈.xlsx 中 Said {sid} 的负责人不同，已停止写入。")
                        target = row[0].row
                target = target or sheet.max_row + 1
                for column, value in enumerate((sid, note, owner, reviewer), 1):
                    cell = sheet.cell(target, column, value)
                    cell.data_type = "s"
                    cell.number_format = "@"
                expected = _snapshot(book)
                descriptor, name = tempfile.mkstemp(prefix=".feedback-", suffix=".xlsx", dir=path.parent)
                os.close(descriptor)
                temporary = Path(name)
                book.save(temporary)
            finally:
                book.close()
            verified_bytes = temporary.read_bytes()
            verified = load_workbook(io.BytesIO(verified_bytes))
            try:
                if _snapshot(verified) != expected:
                    raise SafetyStop("复核反馈临时文件回读校验失败，原文件未修改。")
            finally:
                verified.close()
            backup_dir.mkdir(parents=True, exist_ok=True)
            backup = backup_dir / f"复核反馈-{original_sha}.xlsx"
            if not backup.exists():
                with backup.open("xb") as stream:
                    stream.write(original)
                    stream.flush()
                    os.fsync(stream.fileno())
            if file_hash(backup) != original_sha:
                raise SafetyStop("复核反馈备份校验失败，原文件未修改。")
            if file_hash(path) != original_sha:
                raise SafetyStop("复核反馈.xlsx 在写入期间发生变化，请重试。")
            if excel_lock.exists():
                raise SafetyStop("复核反馈.xlsx 又被 Excel 打开，请先关闭 Excel 后重试。")
            if temporary.read_bytes() != verified_bytes:
                raise SafetyStop("已校验的复核反馈临时文件发生变化，原文件未修改。")
            with temporary.open("rb+") as stream:
                os.fsync(stream.fileno())
            os.replace(temporary, path)
    except SafetyStop:
        raise
    except Exception as exc:
        raise SafetyStop(f"无法同步复核反馈.xlsx：{exc}") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
