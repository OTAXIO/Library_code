"""UI-independent facade: two workflows, one production extension Bridge."""
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3

from automation import ImportStore
from claim_batch import run_claim_batch
from core import SafetyStop, fixed_roster_path, read_roster
from operation_log import OperationLog
from roster_write import mark_skipped_many, mark_skipped_without_note, migrate_status_column
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

    def select_skip(self, roster, mode, owner, scope, sa_id):
        """An explicit local skip is never a batch or a completion override."""
        if scope != "pending":
            raise SafetyStop("只可手动跳过待处理项；已完成或已跳过的记录保持原样。")
        self.select(roster, mode, owner, 1, scope)
        selected = [r for r in self.visible(roster, mode, owner, scope) if r.sa_id == sa_id]
        if len(selected) != 1 or sum(r.sa_id == sa_id for r in roster.records) != 1:
            raise SafetyStop("请只选择当前列表中的一条待处理数据，不能跳过其他记录。")
        return selected[0]

    def _check_skip_journals(self, record):
        # Read existing journals without initializing tables, resetting progress,
        # or depending on the very SA query the operator is trying to set aside.
        for relative, table, safe_phase in (("wos-imports/imports.sqlite3", "imports", "exported"),
                                        ("wos-imports/zero-match.sqlite3", "jobs", "downloaded"),
                                        ("metadata-imports/zero-match.sqlite3", "jobs", "prepared")):
            path = self.base / "runtime" / relative
            if not path.exists():
                continue
            if path.is_symlink() or not path.resolve().is_relative_to(self.base.resolve()):
                raise SafetyStop("断点文件不在当前 code 内，未跳过。")
            try:
                db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1)
                try:
                    saved = db.execute(f"SELECT record_key,data FROM {table} WHERE sa_id=?",
                                       (record.sa_id,)).fetchone()
                finally:
                    db.close()
                if not saved:
                    continue
                state = json.loads(saved[1])
                if (saved[0] != record.key or not isinstance(state, dict) or
                        state.get("phase") != safe_phase or state.get("item_id")):
                    raise SafetyStop("所选数据已有入库、关联或结案断点待核验，不能手动跳过；请先续验原操作。")
            except (sqlite3.Error, ValueError, OSError) as exc:
                raise SafetyStop("本地断点无法核验，未跳过；请检查原操作记录。") from exc

    def skip_selected(self, roster, mode, owner, scope, sa_id, bridge=None):
        """Persist only numeric 2 for one owned row. No browser commands."""
        record = self.select_skip(roster, mode, owner, scope, sa_id)
        if not roster.status_separate or roster.path.resolve() != fixed_roster_path(self.base).resolve():
            raise SafetyStop("请重读当前 code/list.xlsx，确认末尾独立的是否识别列后再跳过。")
        if bridge is not None and getattr(bridge, "pending", None):
            raise SafetyStop("浏览器命令仍在执行，未跳过；请等待当前操作结束。")
        self._check_skip_journals(record)
        saved = mark_skipped_without_note(roster, [record])
        text = "已手动跳过这一条；仅将是否识别写为 2，备注和网页未修改。普通批次不再处理它。"
        try:
            self.operation_log.record("手动跳过所选数据（仅名单标记）", "已跳过", record.sa_id)
        except Exception:
            text += "跳过已保存，但 log.txt 未能保存，请检查日志。"
        return TaskResult(saved.roster, {record.sa_id: text}, text)

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

    def run(self, roster, records, mode, scope, bridge, stop, progress, *, manual_export=False, metadata_import=False):
        if type(metadata_import) is not bool or metadata_import and (mode != "import" or manual_export):
            raise SafetyStop("跨来源 Excel 模式仅用于零匹配导入，不能与 WOS 手动模式混用。")
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
            tail = getattr(result, "reason", "") or tail
            summary = f"完成 {len(result.completed_ids)} · 原已处理 {len(result.synced_ids)} · 跳过 {len(result.skipped)}。{tail}"
        else:
            if metadata_import:
                from metadata_pipeline import run_metadata_batch
                result = run_metadata_batch(self.base, roster, records, bridge, stop, progress, audit,
                                            retry_skipped=scope == "skipped")
            else:
                # Switching modes must not bypass an unresolved Excel write.
                for record in records:
                    path = self.base / "runtime/metadata-imports/zero-match.sqlite3"
                    if path.exists():
                        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
                        try:
                            saved = db.execute("SELECT data FROM jobs WHERE sa_id=?", (record.sa_id,)).fetchone()
                        finally:
                            db.close()
                        if saved and json.loads(saved[0]).get("phase") != "prepared":
                            raise SafetyStop("此条存在 Excel 入库写入断点，请用 Crossref＋OpenAlex 模式续验，未重复导入。")
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
