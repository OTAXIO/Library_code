"""UI-independent facade: two workflows, one production extension Bridge."""
from dataclasses import dataclass
from pathlib import Path

from automation import ImportStore
from claim_batch import run_claim_batch
from core import SafetyStop, fixed_roster_path, read_roster
from operation_log import OperationLog
from roster_write import mark_skipped_many, migrate_status_column
from skip_notes import brief_skip_note
from wos_batch import WOSDownload, default_store
from wos_policy import WOSPolicyStop, match_count, require_no_author_review, zero_result_note
from zero_match import OWNER, SerialWOS, WorkflowStore, run_batch, verify_sa


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

    def select_manual(self, roster, owner, scope, sa_id):
        self.select(roster, "import", owner, 1, scope)  # Same ownership/range guards.
        selected = [r for r in self.visible(roster, "import", owner, scope) if r.sa_id == sa_id]
        if len(selected) != 1:
            raise SafetyStop("请在当前范围中选择一篇谭勋策的未完成零匹配任务。")
        return selected[0]

    def locate_import(self, roster, record, owner, scope, bridge, stop, progress):
        if self.select_manual(roster, owner, scope, record.sa_id) != record:
            raise SafetyStop("所选论文与当前名单不一致，未定位。")
        if not bridge or not bridge.online or stop.is_set():
            raise SafetyStop("浏览器未连接或已暂停，未定位文献。")
        def audit(action, outcome, sa_id):
            self.operation_log.record(action, outcome, sa_id)
        fresh = bridge.call("status", {"sa_id": record.sa_id}, timeout=75)
        audit("手动模式核验后台状态", "已执行", record.sa_id)
        row = fresh.get("row")
        verify_sa(record, row)
        if row["markStatus"] != "待处理" or match_count(row) != 0:
            # Reuse exact existing-match/processed-row sync, never locate WOS.
            return self.run(roster, [record], "import", scope, bridge, stop, progress, manual_export=True)
        imports = ImportStore(self.base / "runtime" / "wos-imports")
        if imports.get(record):
            text = "此篇已有入库断点。请点“接回 TXT 并入库”只读续验；不重新导出或重复上传。"
            return TaskResult(roster, {record.sa_id: text}, text)
        download = SerialWOS(bridge, default_store(self.base), stop, progress)
        locator = WOSDownload(download, download.store, roster.assert_unchanged, stop, audit)
        try:
            require_no_author_review(record.reason, row.get("reason"))
            located = locator.locate(record, progress)
        except SafetyStop as exc:
            if stop.is_set():
                raise SafetyStop("已请求暂停；未标记跳过或提交入库。") from exc
            note = exc.note if isinstance(exc, WOSPolicyStop) else zero_result_note(str(exc))
            if not note:
                raise
            roster.assert_unchanged()
            current = bridge.call("status", {"sa_id": record.sa_id}, timeout=75).get("row")
            verify_sa(record, current)
            if current["markStatus"] != "待处理" or match_count(current) != 0:
                return self.run(roster, [record], "import", scope, bridge, stop, progress, manual_export=True)
            if stop.is_set():
                raise SafetyStop("已请求暂停；未标记跳过或提交入库。") from exc
            short = brief_skip_note(note)
            saved = mark_skipped_many(roster, [record], reasons={record.sa_id: short})
            audit("手动定位后记录真实跳过原因", "已跳过", record.sa_id)
            return TaskResult(saved.roster, {record.sa_id: short}, f"已跳过所选一篇：{short}；未导入。")
        text = ("已有核验 TXT，请点“接回 TXT 并入库”。" if located["file_ready"] else
                "已定位这一篇。请在 WOS 手动导出制表符文件、Full Record，下载完成后点“接回 TXT 并入库”。")
        return TaskResult(roster, {record.sa_id: text}, text)

    def run(self, roster, records, mode, scope, bridge, stop, progress, *, manual_export=False):
        if type(manual_export) is not bool or manual_export and (mode != "import" or len(records) != 1):
            raise SafetyStop("手动导出模式仅允许一篇零匹配导入任务。")
        expected = ([self.select_manual(roster, OWNER, scope, records[0].sa_id)] if manual_export else
                    self.select(roster, mode, OWNER, len(records), scope))
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
                WorkflowStore(imports.root), stop, progress, audit, retry_skipped=scope == "skipped",
                manual_export=manual_export)
            messages = {e["sa_id"]: e["message"] for e in result.outcomes}
            completed = sum(e["status"] == "done" for e in result.outcomes)
            synced = sum(e["status"] == "synced" for e in result.outcomes)
            skipped = sum(e["status"] == "skip" for e in result.outcomes)
            summary = f"完成 {completed} · 原已处理 {synced} · 跳过 {skipped} · 未执行/待续验 {result.remaining}。"
            summary += result.reason or "本轮结束，网页与名单已同步。"
        return TaskResult(result.roster, messages, summary)
