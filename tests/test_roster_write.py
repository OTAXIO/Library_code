"""Synthetic XLSX fixtures only; never change the user's list.xlsx."""
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook, load_workbook
from openpyxl.styles import PatternFill

from core import HEADERS, QUERY_HEADER, SafetyStop, file_hash, read_roster
from roster_write import (clear_skipped_many, mark_complete, mark_skipped_many,
                          mark_skipped_without_note, reconcile_processed, record_data_sources)


def make_roster(path, flags=(None, 1, "1", 0, True)):
    book = Workbook()
    sheet = book.active
    sheet.title = "名单"
    sheet.append(["备注"] + list(HEADERS.values()) + [QUERY_HEADER, "备注"])
    for i, flag in enumerate(flags, 1):
        values = dict(owner="测试员", sa_id=f"demo-{i:03}", title=f"Synthetic {i}", doi="", wos="",
                      staff_id="00001", matches=1, item_ids="1234567890123456789",
                      mark="待处理", reason="作者不一致")
        sheet.append([flag] + [values[k] for k in HEADERS] + ["1", "网页备注保留"])
    sheet['A2'].fill = PatternFill('solid', fgColor='FFF4C2')
    sheet.freeze_panes = "C2"
    sheet.column_dimensions['A'].width = 12
    extra = book.create_sheet("保留页")
    extra['A1'] = "=1+1"
    extra['B1'] = "不要修改"
    book.save(path)
    book.close()


class RosterWriteTests(unittest.TestCase):
    def tan_roster(self):
        book = load_workbook(self.path)
        book.active['B2'] = '谭勋策'
        book.save(self.path)
        book.close()
        return read_roster(self.path)

    def test_remote_processed_sync_marks_only_exact_tan_row(self):
        roster = self.tan_roster()
        record = roster.records[0]
        before = self.path.read_bytes()
        completion = reconcile_processed(roster, record, {
            'saLzkId': record.sa_id, 'markStatus': '已处理'})
        self.assertEqual(completion.cell, 'A2')
        self.assertEqual(completion.backup.read_bytes(), before)
        self.assertTrue(completion.roster.records[0].done)
        self.assertEqual([r.done for r in completion.roster.records[1:]],
                         [r.done for r in roster.records[1:]])

    def test_remote_pending_does_not_touch_roster(self):
        roster = self.tan_roster()
        before = file_hash(self.path)
        self.assertIsNone(reconcile_processed(roster, roster.records[0], {
            'saLzkId': 'demo-001', 'markStatus': '待处理'}))
        self.assertEqual(file_hash(self.path), before)

    def test_remote_sync_rejects_wrong_owner_id_and_unknown_state(self):
        roster = read_roster(self.path)
        before = file_hash(self.path)
        with self.assertRaises(SafetyStop):
            reconcile_processed(roster, roster.records[0], {
                'saLzkId': 'demo-001', 'markStatus': '已处理'})
        roster = self.tan_roster()
        tan_hash = file_hash(self.path)
        for row in ({'saLzkId': 'different', 'markStatus': '已处理'},
                    {'saLzkId': 'demo-001', 'markStatus': '未知'}):
            with self.assertRaises(SafetyStop):
                reconcile_processed(roster, roster.records[0], row)
        self.assertNotEqual(tan_hash, before)
        self.assertEqual(file_hash(self.path), tan_hash)
        roster.assert_unchanged()
        self.assertFalse(roster.records[0].done)

    def test_self_closing_flag_does_not_swallow_next_cell(self):
        import copy
        import io
        import re
        source = self.path.read_bytes()
        with zipfile.ZipFile(io.BytesIO(source)) as original, zipfile.ZipFile(self.path, 'w') as target:
            for entry in original.infolist():
                data = original.read(entry)
                if entry.filename == 'xl/worksheets/sheet1.xml':
                    data = re.sub(rb'<c r="A2"[^>]*(?:/>|></c>)', b'<c r="A2" s="1"/>', data)
                    self.assertIn(b'<c r="A2" s="1"/>', data)
                target.writestr(copy.copy(entry), data)
        roster = read_roster(self.path)
        result = mark_complete(roster, roster.records[0])
        self.assertTrue(result.roster.records[0].done)
        self.assertEqual(result.roster.records[0].owner, roster.records[0].owner)
        self.assertEqual([r.key for r in result.roster.records], [r.key for r in roster.records])

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "list.xlsx"
        make_roster(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_numeric_one_counts(self):
        roster = read_roster(self.path)
        self.assertEqual([r.done for r in roster.records], [False, True, False, False, False])
        self.assertEqual([r.skipped for r in roster.records], [False] * 5)
        self.assertEqual(roster.completion_column, 1)

    def test_only_numeric_two_counts_as_persistent_skip(self):
        make_roster(self.path, [2, "2", 2.0, True, None])
        roster = read_roster(self.path)
        self.assertEqual([r.done for r in roster.records], [False] * 5)
        self.assertEqual([r.skipped for r in roster.records],
                         [True, False, True, False, False])

    def test_batch_skip_is_atomic_and_skipped_row_can_later_complete(self):
        make_roster(self.path, [None, None, None])
        roster = read_roster(self.path)
        before = self.path.read_bytes()
        with zipfile.ZipFile(self.path) as archive:
            parts = {name: archive.read(name) for name in archive.namelist()}
        update = mark_skipped_many(roster, [roster.records[0], roster.records[2]])
        self.assertEqual(update.cells, ("A2", "A4"))
        self.assertEqual(update.backup.read_bytes(), before)
        self.assertEqual([r.skipped for r in update.roster.records], [True, False, True])
        self.assertEqual([r.done for r in update.roster.records], [False, False, False])
        book = load_workbook(self.path)
        try:
            self.assertEqual([book["名单"][cell].value for cell in ("A2", "A3", "A4")],
                             [2, None, 2])
            self.assertEqual(book["名单"]["M2"].value, "网页备注保留")
        finally:
            book.close()
        with zipfile.ZipFile(self.path) as archive:
            changed = [name for name in archive.namelist() if archive.read(name) != parts[name]]
        self.assertEqual(changed, ["xl/worksheets/sheet1.xml"])
        completed = mark_complete(update.roster, update.roster.records[0])
        self.assertTrue(completed.roster.records[0].done)
        self.assertFalse(completed.roster.records[0].skipped)
        self.assertEqual(completed.roster.records[0].remark, "1")
        self.assertTrue(completed.roster.records[2].skipped)

    def test_note_free_skip_only_changes_separate_flag_and_preserves_existing_notes(self):
        from xml.etree import ElementTree as ET
        make_roster(self.path, [None, None, None])
        book = load_workbook(self.path)
        sheet = book["名单"]
        sheet.cell(1, 14, "数据来源")
        sheet.cell(1, 15, "是否识别")
        sheet["A2"] = "已有备注，不覆盖"
        sheet["N2"] = "已有来源"
        book.save(self.path)
        book.close()
        roster = read_roster(self.path)
        before = self.path.read_bytes()
        with zipfile.ZipFile(self.path) as archive:
            parts = {name: archive.read(name) for name in archive.namelist()}
        saved = mark_skipped_without_note(roster, roster.records[:2])
        self.assertEqual(saved.cells, ("O2", "O3"))
        self.assertEqual(saved.backup.read_bytes(), before)
        self.assertEqual([r.remark for r in saved.roster.records], ["已有备注，不覆盖", "", ""])
        self.assertEqual([r.skipped for r in saved.roster.records], [True, True, False])
        self.assertEqual(saved.roster.records[0].source, "已有来源")
        with zipfile.ZipFile(self.path) as archive:
            self.assertEqual([name for name in archive.namelist() if archive.read(name) != parts[name]],
                             ["xl/worksheets/sheet1.xml"])
            ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
            cells = lambda raw: {c.attrib["r"]: ET.tostring(c) for c in ET.fromstring(raw).iter(ns + "c")}
            old, new = cells(parts["xl/worksheets/sheet1.xml"]), cells(archive.read("xl/worksheets/sheet1.xml"))
            self.assertEqual({k: v for k, v in old.items() if k not in ("O2", "O3")},
                             {k: v for k, v in new.items() if k not in ("O2", "O3")})
        # Existing normal skip calls still require a meaningful reason.
        with self.assertRaises(SafetyStop):
            mark_skipped_many(saved.roster, [saved.roster.records[2]])

    def test_note_free_skip_refuses_legacy_first_column_flag(self):
        roster = read_roster(self.path)
        before = file_hash(self.path)
        with self.assertRaises(SafetyStop):
            mark_skipped_without_note(roster, [roster.records[0]])
        self.assertEqual(file_hash(self.path), before)

    def test_clear_skipped_many_only_clears_numeric_two(self):
        make_roster(self.path, [2, "2", 2.0, 1, None])
        roster = read_roster(self.path)
        before = self.path.read_bytes()
        with zipfile.ZipFile(self.path) as archive:
            parts = {name: archive.read(name) for name in archive.namelist()}
        targets = [record for record in roster.records if record.skipped]

        update = clear_skipped_many(roster, targets)

        self.assertEqual(update.cells, ("A2", "A4"))
        self.assertEqual(update.backup.read_bytes(), before)
        self.assertFalse(any(record.skipped for record in update.roster.records))
        self.assertEqual([record.done for record in update.roster.records],
                         [False, False, False, True, False])
        self.assertEqual([record.remark for record in update.roster.records],
                         ["", "2", "", "1", ""])
        book = load_workbook(self.path)
        try:
            self.assertEqual([book["名单"][cell].value for cell in ("A2", "A3", "A4", "A5", "A6")],
                             [None, "2", None, 1, None])
            self.assertEqual(book["名单"]["A2"].fill.fgColor.rgb, "00FFF4C2")
            self.assertEqual(book["名单"]["M2"].value, "网页备注保留")
        finally:
            book.close()
        with zipfile.ZipFile(self.path) as archive:
            changed = [name for name in archive.namelist() if archive.read(name) != parts[name]]
        self.assertEqual(changed, ["xl/worksheets/sheet1.xml"])

    def test_clear_requires_existing_numeric_two(self):
        roster = read_roster(self.path)
        with self.assertRaises(SafetyStop):
            clear_skipped_many(roster, [roster.records[0]])

    def test_write_preserves_other_zip_parts_and_notes(self):
        roster = read_roster(self.path)
        before = self.path.read_bytes()
        with zipfile.ZipFile(self.path) as z:
            parts = {name: z.read(name) for name in z.namelist()}
        result = mark_complete(roster, roster.records[0])
        self.assertEqual(result.backup.read_bytes(), before)
        self.assertEqual(result.cell, "A2")
        self.assertTrue(result.roster.records[0].done)
        self.assertEqual(result.roster.records[0].key, roster.records[0].key)
        with zipfile.ZipFile(self.path) as z:
            changed = [name for name in z.namelist() if z.read(name) != parts[name]]
        self.assertEqual(changed, ["xl/worksheets/sheet1.xml"])
        book = load_workbook(self.path)
        try:
            self.assertEqual(book['名单']['A2'].value, 1)
            self.assertEqual(book['名单']['A2'].data_type, 'n')
            self.assertEqual(book['名单']['M2'].value, "网页备注保留")
            self.assertEqual(book['名单']['A2'].fill.fgColor.rgb, '00FFF4C2')
            self.assertEqual(book['名单'].freeze_panes, 'C2')
            self.assertEqual(book['保留页']['A1'].value, '=1+1')
        finally:
            book.close()

    def test_source_column_is_appended_and_only_success_rows_are_recorded(self):
        roster = read_roster(self.path)
        before = self.path.read_bytes()
        with zipfile.ZipFile(self.path) as archive:
            parts = {name: archive.read(name) for name in archive.namelist()}

        update = record_data_sources(roster, [roster.records[0], roster.records[2]], "WOS")

        self.assertEqual(update.roster.source_column, roster.header_column_count + 1)
        self.assertEqual(update.cells, ("N1", "N2", "N4"))
        self.assertEqual(update.rows, (2, 4))
        self.assertEqual(update.backup.read_bytes(), before)
        self.assertEqual([record.source for record in update.roster.records],
                         ["WOS", "", "WOS", "", ""])
        self.assertEqual([record.remark for record in update.roster.records],
                         ["", "1", "1", "0", "True"])
        with zipfile.ZipFile(self.path) as archive:
            changed = [name for name in archive.namelist() if archive.read(name) != parts[name]]
        self.assertEqual(changed, ["xl/worksheets/sheet1.xml"])
        book = load_workbook(self.path)
        try:
            sheet = book["名单"]
            self.assertEqual(sheet["N1"].value, "数据来源")
            self.assertEqual([sheet[cell].value for cell in ("N2", "N3", "N4")],
                             ["WOS", None, "WOS"])
            self.assertEqual(sheet["A2"].fill.fgColor.rgb, "00FFF4C2")
            self.assertEqual(book["保留页"]["A1"].value, "=1+1")
        finally:
            book.close()

    def test_source_values_append_uniquely_and_support_future_providers(self):
        roster = read_roster(self.path)
        first = record_data_sources(roster, [roster.records[0]], "WOS")
        same = record_data_sources(first.roster, [first.roster.records[0]], "WOS")
        self.assertIsNone(same.backup)
        self.assertEqual(same.cells, ())
        second = record_data_sources(same.roster, [same.roster.records[0]], "CNKI")
        self.assertEqual(second.roster.records[0].source, "WOS；CNKI")
        self.assertEqual(second.cells, ("N2",))

    def test_source_header_can_be_added_before_any_success(self):
        roster = read_roster(self.path)
        update = record_data_sources(roster, [], "WOS")
        self.assertEqual(update.cells, ("N1",))
        self.assertTrue(update.roster.source_column)
        self.assertTrue(all(not record.source for record in update.roster.records))

    def test_source_formula_stops_without_overwrite(self):
        book = load_workbook(self.path)
        book.active["N1"] = "数据来源"
        book.active["N2"] = "=1+1"
        book.save(self.path)
        book.close()
        with self.assertRaisesRegex(SafetyStop, "数据来源是公式"):
            read_roster(self.path)

    def test_sequential_completion_updates_fingerprint(self):
        roster = read_roster(self.path)
        first = mark_complete(roster, roster.records[0])
        second = mark_complete(first.roster, first.roster.records[2])
        self.assertTrue(second.roster.records[0].done)
        self.assertTrue(second.roster.records[2].done)
        second.roster.assert_unchanged()

    def test_duplicate_completion_stops(self):
        roster = read_roster(self.path)
        with self.assertRaises(SafetyStop):
            mark_complete(roster, roster.records[1])

    def test_changed_file_stops_without_overwrite(self):
        roster = read_roster(self.path)
        make_roster(self.path, [0])
        current = file_hash(self.path)
        with self.assertRaises(SafetyStop):
            mark_complete(roster, roster.records[0])
        self.assertEqual(file_hash(self.path), current)

    def test_changed_during_prepare_stops(self):
        roster = read_roster(self.path)
        def changed(path):
            verified = read_roster(path)
            make_roster(self.path, [0])
            return verified
        with patch('roster_write.read_roster', side_effect=changed):
            with self.assertRaises(SafetyStop):
                mark_complete(roster, roster.records[0])
        self.assertEqual(len(read_roster(self.path).records), 1)

    def test_excel_lock_stops(self):
        roster = read_roster(self.path)
        self.path.with_name('~$list.xlsx').touch()
        with self.assertRaisesRegex(SafetyStop, "Excel"):
            mark_complete(roster, roster.records[0])
        roster.assert_unchanged()

    def test_assistant_lock_stops(self):
        roster = read_roster(self.path)
        self.path.with_name('.list.xlsx.assistant.lock').touch()
        with self.assertRaises(SafetyStop):
            mark_complete(roster, roster.records[0])
        roster.assert_unchanged()

    def test_replace_failure_leaves_original_and_cleans_temp(self):
        roster = read_roster(self.path)
        with patch('roster_write.os.replace', side_effect=PermissionError):
            with self.assertRaises(SafetyStop):
                mark_complete(roster, roster.records[0])
        roster.assert_unchanged()
        self.assertEqual(list(self.path.parent.glob('.list-write-*')), [])
        self.assertFalse(self.path.with_name('.list.xlsx.assistant.lock').exists())

    def test_backup_failure_leaves_original(self):
        roster = read_roster(self.path)
        blocked = self.path.parent / 'blocked'
        blocked.write_text('fixture', encoding='utf-8')
        with self.assertRaises(OSError):
            mark_complete(roster, roster.records[0], blocked)
        roster.assert_unchanged()

    def test_missing_flag_cell_is_inserted_in_order(self):
        make_roster(self.path, [None, 1, "1", None])
        roster = read_roster(self.path)
        result = mark_complete(roster, roster.records[3])
        self.assertTrue(result.roster.records[3].done)

    def test_existing_note_is_backed_up(self):
        make_roster(self.path, ["未核对原文"])
        roster = read_roster(self.path)
        result = mark_complete(roster, roster.records[0])
        self.assertEqual(result.previous, "未核对原文")
        self.assertEqual(read_roster(result.backup).records[0].remark, "未核对原文")

    def test_missing_or_ambiguous_header_stops(self):
        for header in ('其他', '备注'):
            make_roster(self.path)
            book = load_workbook(self.path)
            book.active['A1'] = header
            book.active['B1'] = '负责人' if header == '其他' else '备注'
            if header == '其他':
                book.active['M1'] = '其他备注'
            book.save(self.path)
            book.close()
            with self.assertRaises(SafetyStop):
                read_roster(self.path)

    def test_formula_flag_stops(self):
        make_roster(self.path, ['=1'])
        with self.assertRaises(SafetyStop):
            read_roster(self.path)

    def test_protected_sheet_stops(self):
        book = load_workbook(self.path)
        book.active.protection.sheet = True
        book.save(self.path)
        book.close()
        roster = read_roster(self.path)
        with self.assertRaises(SafetyStop):
            mark_complete(roster, roster.records[0])
        roster.assert_unchanged()
