"""UI-independent facade: two workflows, one production extension Bridge."""
from dataclasses import dataclass
from pathlib import Path

from automation import ImportStore
from claim_batch import run_claim_batch
from core import SafetyStop, fixed_roster_path, read_roster
from operation_log import OperationLog
from roster_write import migrate_status_column
from wos_batch import default_store
from zero_match import OWNER, SerialWOS, WorkflowStore, run_batch


@dataclass
class TaskResult:
    roster: object
    messages: dict
    summary: str


def supported_claim(record):
    reason = str(record.reason or "")
    return record.matches == 1 and (reason == "作者不一致" or "DOI" in reason and "WOS" in reason)


class WorkflowService:
    def __init__(self, base, operation_log=None):
        self.base = Path(base)
        self.operation_log = operation_log or OperationLog(self.base / "log.txt")

    def read_current(self):
        return read_roster(fixed_roster_path(self.base))

    def load(self):
        return migrate_status_column(self.read_current())

    @staticmethod
    def visible(roster, mode, owner, scope):
        if not roster or mode not in ("claim", "import"):
            return []
        return [r for r in roster.records if r.owner == owner
                and (r.matches == 0 if mode == "import" else supported_claim(r))
                and (scope == "all" or scope == "done" and r.done
                     or scope == "pending" and not r.done and not r.skipped
                     or scope == "skipped" and not r.done and r.skipped)]

    def select(self, roster, mode, owner, limit, scope):
        if not roster:
            raise SafetyStop("请先读取 code/list.xlsx。")
        if not owner:
            raise SafetyStop("请选择负责人。")
        if owner != OWNER:
            raise SafetyStop("本次自动化仅处理谭勋策，其他负责人的记录不会修改。")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise SafetyStop("本轮条数须为 1–100 的整数。")
        if mode not in ("claim", "import") or scope not in ("pending", "skipped"):
            raise SafetyStop("已完成仅供查看；请选择待处理或重试跳过项。")
        roster.assert_unchanged()
        records = self.visible(roster, mode, owner, scope)[:limit]
        if not records:
            raise SafetyStop("当前范围没有可执行的" + ("零匹配导入" if mode == "import" else "单匹配认领") + "任务。")
        return records

    def run(self, roster, records, mode, scope, bridge, stop, progress):
        expected = self.select(roster, mode, OWNER, len(records), scope)
        if list(records) != expected:
            raise SafetyStop("运行队列与当前名单范围不一致，未开始。")
        if not bridge or not bridge.online:
            raise SafetyStop("浏览器未连接，请在 SA 比对结果页扩展中配对。")
        def audit(action, outcome, sa_id):
            self.operation_log.record(action, outcome, sa_id)
        if mode == "claim":
            result = run_claim_batch(roster, records, bridge, cancel=stop.is_set,
                progress=progress, audit=audit, limit=len(records), retry_skipped=scope == "skipped")
            messages = {sa_id: "已认领并完成网页批注及名单回写" for sa_id in result.completed_ids}
            messages.update({sa_id: "后台原已处理，仅同步名单" for sa_id in result.synced_ids})
            messages.update(result.skipped)
            tail = "已暂停，请核验网页；不重复提交。" if result.halted else "已按请求暂停。" if result.cancelled else "本轮结束。"
            summary = f"完成 {len(result.completed_ids)} · 原已处理 {len(result.synced_ids)} · 跳过 {len(result.skipped)}。{tail}"
        else:
            imports = ImportStore(self.base / "runtime" / "wos-imports")
            download = SerialWOS(bridge, default_store(self.base), stop, progress)
            result = run_batch(roster, records, bridge, download, imports,
                WorkflowStore(imports.root), stop, progress, audit, retry_skipped=scope == "skipped")
            messages = {e["sa_id"]: e["message"] for e in result.outcomes}
            completed = sum(e["status"] == "done" for e in result.outcomes)
            synced = sum(e["status"] == "synced" for e in result.outcomes)
            skipped = sum(e["status"] == "skip" for e in result.outcomes)
            summary = f"完成 {completed} · 原已处理 {synced} · 跳过 {skipped} · 未执行/待续验 {result.remaining}。"
            summary += result.reason if result.halted else "本轮结束，网页与名单已同步。"
        return TaskResult(result.roster, messages, summary)
