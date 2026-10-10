"""Repair ONLY a recorded 0.4.8 pre-click rejection, never an unknown Export.

This offline migration submits no browser commands and does not alter Excel.
The private acceptance reports and original journal are retained for auditing.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from core import SafetyStop, fixed_roster_path, read_roster
from maintenance.single_import import select_one
from operation_log import OperationLog
from wos_batch import default_store
from wos_files import record_ut

BASE = Path(__file__).resolve().parents[1]
REJECTED = "下载并核验 TXT：[扩展 0.4.8] [WOS 已暂停] 未处于 WOS 核心合集单篇完整记录页"
BLOCKED = "下载并核验 TXT：上一轮 Export 已提交或结果不明；尚未找到对应 TXT。请等待下载或人工导出后再继续，不重复导出。"


def validated_recovery(roster, record, state, reports):
    roster.assert_unchanged()
    if (not isinstance(state, dict) or set(state) != {"phase", "record_url"}
            or state.get("phase") != "export_intent" or not reports):
        raise SafetyStop("不是可恢复的旧版点击前拒绝断点；不更改未知意图。")
    record_ut(state["record_url"])
    for index, report in enumerate(reports):
        message = REJECTED if index == 0 else BLOCKED
        outcomes = report.get("outcomes", [])
        if (report.get("sa_id") != record.sa_id or report.get("roster_before_sha256") != roster.sha256
                or report.get("roster_after_sha256") != roster.sha256 or report.get("halted") is not True
                or report.get("reason") != message or report.get("import_phase_before") != ""
                or report.get("import_phase") != "" or report.get("batch") != {}
                or report.get("workflow_phase") != "" or report.get("excel_done") is not False
                or report.get("excel_skipped") is not False or report.get("imported_and_closed") is not False
                or report.get("new_import_and_closed") is not False or len(outcomes) != 1
                or outcomes[0].get("sa_id") != record.sa_id or outcomes[0].get("status") != "halted"
                or outcomes[0].get("message") != message):
            raise SafetyStop("验收证据不能证明最终 Export 未点击，断点保留。")
        if index and report["started_utc"] <= reports[index-1]["finished_utc"]:
            raise SafetyStop("验收证据次序不明，断点保留。")
    return {"phase": "export_preparing", "record_url": state["record_url"],
            "recovery": "recorded_0.4.8_pre_click_rejection"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sa-id", required=True)
    parser.add_argument("--report", action="append", required=True)
    args = parser.parse_args()
    roster = read_roster(fixed_roster_path(BASE))
    record = select_one(roster, args.sa_id)
    store = default_store(BASE)
    folder = (BASE / "runtime" / "live-acceptance").resolve()
    paths = [Path(value).resolve() for value in args.report]
    if any(p.parent != folder or p.suffix != ".json" for p in paths):
        raise SafetyStop("只读取本机验收目录中的实际报告。")
    reports = [json.loads(p.read_text(encoding="utf-8")) for p in paths]
    state = validated_recovery(roster, record, store.get(record), reports)
    # Include every later acceptance for this exact SA: an omitted unknown
    # action is not permission to rewind its Export intent.
    first = reports[0]["started_utc"]
    later = {p.resolve() for p in folder.glob("*.json") if p.name[:8].isdigit()
             and (data := json.loads(p.read_text(encoding="utf-8"))).get("sa_id") == record.sa_id
             and data.get("started_utc", "") >= first}
    if later != set(paths):
        raise SafetyStop("还有未审查的后续验收报告；不更改断点。")
    backup_dir = BASE / "runtime" / "checkpoints" / "rejected-export"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".sqlite3")
    with store.connect() as source, sqlite3.connect(backup) as target:
        source.backup(target)
    state["evidence_sha256"] = [hashlib.sha256(p.read_bytes()).hexdigest() for p in paths]
    roster.assert_unchanged()
    store.save(record, state)
    OperationLog(BASE / "log.txt").record("恢复明确点击前拒绝的 WOS 导出断点", "已执行", record.sa_id)
    print("Recovered pre-click rejection; original journal backed up. No browser action or Excel change.")


if __name__ == "__main__":
    main()
