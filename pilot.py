"""Fixed-scope pilot: read the backend before any automation or roster write.

The pilot deliberately does not approve, claim, merge or import pending items.
It reconciles only records the backend already marks as processed and reports
the remaining routes for individually verified work.
"""
from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from automation import classify
from core import Roster, SafetyStop
from roster_write import reconcile_processed

OWNER = "谭勋策"
LIMIT = 100


def pilot_scope(roster: Roster, path: Path, limit: int = LIMIT) -> list[str]:
    """Freeze the first pilot IDs so a restart cannot silently move to new rows."""
    if not 1 <= limit <= LIMIT:
        raise SafetyStop("试验范围必须为 1 到 100 条。")
    path = Path(path)
    by_id = {record.sa_id: record for record in roster.records}
    if path.exists():
        try:
            manifest = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeError) as exc:
            raise SafetyStop("试验名单文件无法读取，不能重新抽取下一批。") from exc
        ids = manifest.get("ids") if isinstance(manifest, dict) else None
        if (manifest.get("owner") != OWNER or not isinstance(ids, list) or
                not 1 <= len(ids) <= LIMIT or len(ids) != len(set(ids)) or
                any(not isinstance(i, str) or i not in by_id or by_id[i].owner != OWNER for i in ids)):
            raise SafetyStop("试验名单与当前 Excel 不一致，停止，避免处理他人或新一批记录。")
        return ids
    ids = [record.sa_id for record in roster.records if record.owner == OWNER and not record.done][:limit]
    if not ids:
        raise SafetyStop("谭勋策名下没有未完成记录。")
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps({"owner": OWNER, "ids": ids}, ensure_ascii=False, indent=2)
    try:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError:
        return pilot_scope(roster, path, limit)
    return ids


@dataclass
class PilotResult:
    roster: Roster
    checked: int
    synced: int
    synced_ids: list[str]
    local_done: int
    routes: dict[str, int]
    deferred: list[tuple[str, str]]
    cancelled: bool


def precheck_pilot(roster, ids, bridge, cancelled=lambda: False, progress=lambda _: None):
    """Read up to 100 exact SA rows; write Excel only for remote 已处理."""
    if not 1 <= len(ids) <= LIMIT or len(ids) != len(set(ids)):
        raise SafetyStop("试验名单 ID 数量或唯一性不合法。")
    by_id = {record.sa_id: record for record in roster.records}
    if any(i not in by_id or by_id[i].owner != OWNER for i in ids):
        raise SafetyStop("试验名单包含非谭勋策记录，未开始操作。")
    routes = Counter()
    deferred = []
    checked = synced = local_done = 0
    synced_ids = []
    for position, sa_id in enumerate(ids, 1):
        if cancelled():
            return PilotResult(roster, checked, synced, synced_ids, local_done, dict(routes), deferred, True)
        record = by_id[sa_id]
        if record.done:
            local_done += 1
            continue
        progress(f"试验预检 {position}/{len(ids)}：{sa_id}")
        try:
            result = bridge.call("search", {"sa_id": sa_id})
            checked += 1
            completion = reconcile_processed(roster, record, result.get("row"))
            if completion:
                roster = completion.roster
                by_id = {item.sa_id: item for item in roster.records}
                synced += 1
                synced_ids.append(sa_id)
                continue
            plan = classify(record, result)
            routes[plan.route] += 1
            if plan.route == "manual":
                deferred.append((sa_id, plan.reason))
        except SafetyStop as exc:
            deferred.append((sa_id, str(exc)))
    return PilotResult(roster, checked, synced, synced_ids, local_done, dict(routes), deferred, False)
