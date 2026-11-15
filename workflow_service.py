"""UI-independent facade: two workflows, one production extension Bridge."""
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3

from automation import ImportStore
from batch_recovery import check_legacy_import_journals
from claim_batch import run_claim_batch
from core import SafetyStop, fixed_roster_path, read_roster, require_owner
from operation_log import OperationLog
from roster_write import mark_skipped_many, migrate_status_column
from skip_notes import brief_skip_note
from wos_batch import WOSDownload, default_store
from wos_policy import WOSPolicyStop, match_count, require_no_author_review, zero_result_note
from zero_match import SerialWOS, WorkflowStore, run_batch, verify_sa


@dataclass
class TaskResult:
    roster: object
    messages: dict
    summary: str
    halted: bool = False
    reason: str = ""
    cancelled: bool = False
    report_path: str = ""
    deferred_count: int = 0


def supported_claim(record):
    reason = str(record.reason or "")
    return record.matches == 1 and (reason == "作者不一致" or "DOI" in reason and "WOS" in reason)


class WorkflowService:
    def __init__(self, base, operation_log=None):
        self.base = Path(base)
        self.operation_log = operation_log or OperationLog(self.base / "log.txt")

    def read_current(self, kind="paper"):
        return read_roster(self.base / "list.xlsx" if kind == "patent" else fixed_roster_path(self.base), kind=kind)

    def load(self, kind="paper"):
        return migrate_status_column(self.read_current(kind))

    @staticmethod
    def visible(roster, mode, owner, scope):
        if not roster or mode not in ("paper", "patent", "claim", "import", "evidence"):
            return []
        if mode == "patent" and getattr(roster, "kind", "paper") != "patent":
            return []
        return [r for r in roster.records if r.owner == owner
                and (mode in ("paper", "patent", "evidence") or (r.matches == 0 if mode == "import" else supported_claim(r)))
                and (scope == "all" or scope == "done" and r.done
                     or scope == "pending" and not r.done and not r.skipped
                     or scope == "skipped" and not r.done and r.skipped)]

    def review_evidence(self, roster, owner, scope, limit, bridge, stop, progress):
        from evidence_review import inspect_record, save_report
        require_owner(owner)
        if not roster or type(limit) is not int or not 1 <= limit <= 100:
            raise SafetyStop("请读取名单并指定 1–100 条核验范围。")
        entries, messages = [], {}
        records = self.visible(roster, "evidence", owner, scope)[:limit]
        if not records:
            raise SafetyStop("当前范围没有可核验记录。")
        for record in records:
            if stop.is_set():
                break
            roster.assert_unchanged()
            progress("只读核验 " + record.sa_id)
            def call(action, payload):
                roster.assert_unchanged()
                if stop.is_set() or action not in {"status", "search", "front_search"}:
                    raise SafetyStop("只读核验已暂停。")
                return bridge.call(action, payload)
            try:
                entry = inspect_record(self.base, record, call)
            except (SafetyStop, OSError, ValueError, TypeError):
                entry = {"sa_id": record.sa_id, "判定": "待核", "待核原因": "网页、名单或原文证据未能核实"}
            entries.append(entry)
            messages[record.sa_id] = json.dumps(entry, ensure_ascii=False, indent=2)
        path = save_report(self.base, entries)
        return TaskResult(roster, messages, f"只读核验 {len(entries)} 条，报告 {path.name}",
                          cancelled=stop.is_set(), report_path=str(path))

    def select(self, roster, mode, owner, limit, scope):
        if not roster:
            raise SafetyStop("请先读取 code/list.xlsx。")
        if not owner:
            raise SafetyStop("请选择负责人。")
        require_owner(owner)
        if scope!='skipped' and (type(limit) is not int or not 1 <= limit <= 100):
            raise SafetyStop("本轮条数须为 1–100 的整数。")
        if mode not in ("paper", "patent", "claim", "import") or scope not in ("pending", "skipped"):
            raise SafetyStop("已完成仅供查看；请选择待处理或重试跳过项。")
        roster.assert_unchanged()
        visible=self.visible(roster,mode,owner,scope)
        records=visible if scope=='skipped' else visible[:limit]
        if not records:
            raise SafetyStop("当前范围没有可执行的论著认领任务。")
        return records

    def select_manual(self, roster, owner, scope, sa_id):
        self.select(roster, "import", owner, 1, scope)  # Same ownership/range guards.
        selected = [r for r in self.visible(roster, "import", owner, scope) if r.sa_id == sa_id]
        if len(selected) != 1:
            raise SafetyStop("请在当前范围中选择一篇所选负责人的未完成零匹配任务。")
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

    def skip_selected(self, roster, mode, owner, scope, sa_id, bridge=None):
        """Persist numeric 2 and a visible reason for one owned row. No browser commands."""
        record = self.select_skip(roster, mode, owner, scope, sa_id)
        if not roster.status_separate or roster.path.resolve() != (self.base / roster.path.name if roster.kind == "patent" else fixed_roster_path(self.base)).resolve() or roster.path.name not in ("list.xlsx", "patent.xlsx"):
            raise SafetyStop("请重读当前 code/list.xlsx，确认末尾独立的是否识别列后再跳过。")
        if bridge is not None and getattr(bridge, "pending", None):
            raise SafetyStop("浏览器命令仍在执行，未跳过；请等待当前操作结束。")
        check_legacy_import_journals(self.base, record)
        # A red row must always explain itself; an existing note is kept as is.
        saved = mark_skipped_many(roster, [record], reasons={record.sa_id: record.remark or "手动跳过"})
        text = ("已手动跳过这一条；是否识别写为 2" + ("，原备注保留" if record.remark else "，备注写“手动跳过”")
                + "，网页未修改。普通批次不再处理它。")
        try:
            self.operation_log.record("手动跳过所选数据（仅名单标记）", "已跳过", record.sa_id)
        except Exception:
            text += "跳过已保存，但 log.txt 未能保存，请检查日志。"
        return TaskResult(saved.roster, {record.sa_id: text}, text)

    def review_selected(self, roster, owner, sa_id, state, bridge=None):
        """Human override of a single local flag; keep all original intent journals."""
        from manual_review import STATES, select_review, state_of, write_review
        record = select_review(roster, owner, sa_id)
        if roster.path.resolve() != (self.base / roster.path.name if roster.kind == "patent" else fixed_roster_path(self.base)).resolve() or roster.path.name not in ("list.xlsx", "patent.xlsx"):
            raise SafetyStop('手动核验只允许保存当前 code/list.xlsx。')
        if bridge is not None and getattr(bridge, 'pending', None):
            raise SafetyStop('浏览器命令仍待回读，请等待结束后再手动核验。')
        saved = write_review(roster, record, state)
        label = STATES[state][0]
        note_text = '备注已保留。' if state != 'skipped' or record.remark else '空备注已补为“手动跳过”。'
        text = f'手动核验已保存：{label}（仅名单）。{note_text}'
        try:
            self.operation_log.record('手动核验名单状态', '已执行', record.sa_id,
                detail=f'{STATES[state_of(record)][0]} → {label}；{note_text}业务断点保留。')
        except Exception:
            text += 'log.txt 未能保存，请检查日志。'
        return TaskResult(saved.roster, {record.sa_id: text}, text)

    def locate_import(self, roster, record, owner, scope, bridge, stop, progress):
        return self._diagnosed("wos-locate", scope, [record], bridge, progress,
            lambda observed, updates: self._locate_import(roster, record, owner, scope, observed, stop, updates))

    def _locate_import(self, roster, record, owner, scope, bridge, stop, progress):
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
            return self._run(roster, [record], "import", scope, bridge, stop, progress, manual_export=True)
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
                return self._run(roster, [record], "import", scope, bridge, stop, progress, manual_export=True)
            if stop.is_set():
                raise SafetyStop("已请求暂停；未标记跳过或提交入库。") from exc
            short = brief_skip_note(note)
            saved = mark_skipped_many(roster, [record], reasons={record.sa_id: short})
            audit("手动定位后记录真实跳过原因", "已跳过", record.sa_id)
            return TaskResult(saved.roster, {record.sa_id: short}, f"已跳过所选一篇：{short}；未导入。")
        text = ("已有核验 TXT，请点“接回 TXT 并入库”。" if located["file_ready"] else
                "已定位这一篇。请在 WOS 手动导出制表符文件、Full Record，下载完成后点“接回 TXT 并入库”。")
        return TaskResult(roster, {record.sa_id: text}, text)

    def run(self, roster, records, mode, scope, bridge, stop, progress, *, manual_export=False,
            metadata_import=False, need_import=False, owner=None):
        records = list(records)
        workflow = ("need-import" if need_import else "metadata-import" if metadata_import else
                    "wos-manual-import" if manual_export else "wos-import" if mode == "import" else mode)
        return self._diagnosed(workflow, scope, records, bridge, progress,
            lambda observed, updates: self._run(roster, records, mode, scope, observed, stop, updates,
                manual_export=manual_export, metadata_import=metadata_import,
                need_import=need_import, owner=owner))

    def _diagnosed(self, workflow, scope, records, bridge, progress, execute):
        from run_diagnostics import RunDiagnostics, TracedBridge
        diagnostic = RunDiagnostics(self.base, workflow, scope, records, self.operation_log, progress, bridge)
        self.last_report_path = ""
        try:
            result = execute(TracedBridge(bridge, diagnostic) if bridge is not None else None, diagnostic.progress)
        except Exception as exc:
            diagnostic.failed(exc)
            if diagnostic.saved:
                self.last_report_path = str(diagnostic.path)
            raise
        diagnostic.finish(result)
        if diagnostic.saved:
            result.report_path = self.last_report_path = str(diagnostic.path)
            if result.halted:
                result.summary += "\n诊断：runtime/run-reports/" + diagnostic.path.name
        return result

    @staticmethod
    def needs_browser(records):
        return any(r.matches == 0 or supported_claim(r) for r in records)

    def _run_paper(self, roster, records, scope, bridge, stop, progress, owner):
        from need_import import run_need_import_batch
        from roster_annotations import annotate_roster, format_owned_status, review_reason

        owners = {r.owner for r in records}
        owner = owner if owner is not None else next(iter(owners)) if len(owners) == 1 else None
        if not owner or any(r.owner != owner for r in records):
            raise SafetyStop("运行队列须只包含所选负责人的记录，未开始。")
        if records != self.select(roster, "paper", owner, len(records), scope):
            raise SafetyStop("运行队列与当前名单范围不一致，未开始。")
        if self.needs_browser(records) and (not bridge or not bridge.online):
            raise SafetyStop("浏览器未连接，请在 SA 比对结果页扩展中配对。")
        if self.needs_browser(records):
            first = next(r for r in records if r.matches == 0 or supported_claim(r))
            status = bridge.call("status", {"sa_id": first.sa_id})
            if status.get("page_kind") != "paper":
                raise SafetyStop("绑定页类型不符：请切换到 SA 比对结果页重新配对。")
        local = [r for r in records if r.matches >= 2 or r.matches == 1 and not supported_claim(r)]
        messages, reasons = {}, {}
        multi = nonclaim = claimed = synced = imported = other = 0
        halted = cancelled = False
        reason = ""
        for r in local:
            if stop.is_set():
                break
            note = f"匹配到 {r.matches} 条，未结项" if r.matches >= 2 else "非认领任务：" + str(r.reason or "")[:40]
            messages[r.sa_id] = ("已是跳过状态，保持不动；" if r.skipped else "") + note
            if not r.skipped:
                reasons[r.sa_id] = r.remark or note
            if r.matches >= 2:
                multi += 1
            else:
                nonclaim += 1
        if reasons and not stop.is_set():
            roster.assert_unchanged()
            chosen = [r for r in local if r.sa_id in reasons]
            roster = mark_skipped_many(roster, chosen, reasons=reasons).roster
            for r in chosen:
                self.operation_log.record("论著分流（仅名单跳过）", "已跳过", r.sa_id)
        elif stop.is_set():
            messages.clear()
            multi = nonclaim = 0

        def audit(action, outcome, sa_id):
            self.operation_log.record(action, outcome, sa_id)

        # Keep original order within each sub-batch, refreshing rows after every write.
        groups = ([r.sa_id for r in records if supported_claim(r)],
                  [r.sa_id for r in records if r.matches == 0])
        for index, ids in enumerate(groups):
            if halted or cancelled or stop.is_set():
                break
            if not ids:
                continue
            roster.assert_unchanged()
            by_id = {r.sa_id: r for r in roster.records}
            selected = [by_id[sid] for sid in ids]
            if index == 0:
                result = run_claim_batch(roster, selected, bridge, cancel=stop.is_set,
                    progress=progress, audit=audit, limit=len(selected), retry_skipped=scope == "skipped", owner=owner)
                claimed += len(result.completed_ids)
                complete_text = "已认领并完成网页批注及名单回写"
            else:
                result = run_need_import_batch(roster, selected, bridge, owner, cancel=stop.is_set,
                    progress=progress, audit=audit, retry_skipped=scope == "skipped")
                imported += len(result.completed_ids)
                complete_text = "网页及名单已标注需导入"
            roster = result.roster
            batch_messages = {sid: complete_text for sid in result.completed_ids}
            batch_messages.update({sid: "后台原已处理，仅同步名单" for sid in result.synced_ids})
            batch_messages.update(result.skipped)
            messages.update(batch_messages)
            synced += len(result.synced_ids)
            other += len(result.skipped)
            halted = bool(result.halted)
            cancelled = bool(getattr(result, "cancelled", False))
            reason = getattr(result, "reason", "") or ""
            if scope == "skipped" and not halted and not cancelled:
                by_id = {r.sa_id: r for r in roster.records}
                unresolved = {sid: review_reason(msg) for sid, msg in batch_messages.items()
                              if sid in by_id and by_id[sid].skipped and not by_id[sid].done and review_reason(msg)}
                if unresolved:
                    roster = annotate_roster(roster, unresolved=unresolved).roster
                resolved = {sid for sid in batch_messages if sid in by_id and by_id[sid].done}
                if resolved:
                    roster = annotate_roster(roster, resolved_ids=resolved).roster
            roster = format_owned_status(roster, ids=batch_messages).roster
        cancelled = cancelled or stop.is_set()
        tail = reason or ("已暂停；后续子批未执行，请核验网页。" if halted else
                          "已按请求暂停；后续子批未执行。" if cancelled else "本轮结束。")
        if (halted or cancelled) and reason:
            tail += "；已暂停，后续子批未执行。"
        summary = (f"认领 {claimed} · 原已处理 {synced} · 需导入 {imported} · 多条匹配跳过 {multi} · "
                   f"非认领跳过 {nonclaim} · 其他跳过 {other}。" + tail)
        if scope == "skipped" and local and not cancelled:
            summary += "已跳过的多条匹配/非认领记录保持不动。"
        return TaskResult(roster, messages, summary, halted=halted, reason=reason, cancelled=cancelled)

    def _run(self, roster, records, mode, scope, bridge, stop, progress, *, manual_export=False,
             metadata_import=False, need_import=False, owner=None):
        if mode == "patent":
            from patent_claim import run_patent_batch
            if manual_export or metadata_import or need_import:
                raise SafetyStop("专利认领不能混用导入模式。")
            owners = {r.owner for r in records}
            owner = owner if owner is not None else next(iter(owners)) if len(owners) == 1 else None
            if records != self.select(roster, mode, owner, len(records), scope):
                raise SafetyStop("运行队列与当前名单范围不一致。")
            if any(r.matches < 3 for r in records) and (not bridge or not bridge.online):
                raise SafetyStop("请在 SA 专利比对页配对。")
            result = run_patent_batch(roster, records, bridge, owner, stop.is_set, progress,
                self.operation_log.record, scope == "skipped")
            return TaskResult(result.roster, result.skipped,
                f"专利：完成 {len(result.completed_ids)}，同步 {len(result.synced_ids)}，跳过 {len(result.skipped)}。" + result.reason,
                halted=result.halted, reason=result.reason, cancelled=result.cancelled)
        if mode == "paper":
            if manual_export or metadata_import or need_import:
                raise SafetyStop("论著认领不能混用导入模式。")
            return self._run_paper(roster, records, scope, bridge, stop, progress, owner)
        if type(metadata_import) is not bool or metadata_import and (mode != "import" or manual_export):
            raise SafetyStop("跨来源 Excel 模式仅用于零匹配导入，不能与 WOS 手动模式混用。")
        if type(manual_export) is not bool or manual_export and (mode != "import" or len(records) != 1):
            raise SafetyStop("手动导出模式仅允许一篇零匹配导入任务。")
        if type(need_import) is not bool or need_import and (mode != "import" or manual_export or metadata_import):
            raise SafetyStop("需导入标注仅用于零匹配任务，不能与导入模式混用。")
        if owner is None:
            owners = {record.owner for record in records}
            owner = owners.pop() if len(owners) == 1 else None
        if not owner or any(record.owner != owner for record in records):
            raise SafetyStop("运行队列须只包含所选负责人的记录，未开始。")
        expected = ([self.select_manual(roster, owner, scope, records[0].sa_id)] if manual_export else
                    self.select(roster, mode, owner, len(records), scope))
        if list(records) != expected:
            raise SafetyStop("运行队列与当前名单范围不一致，未开始。")
        if not bridge or not bridge.online:
            raise SafetyStop("浏览器未连接，请在 SA 比对结果页扩展中配对。")
        def audit(action, outcome, sa_id):
            self.operation_log.record(action, outcome, sa_id)
        deferred=0
        if mode == "claim":
            result = run_claim_batch(roster, records, bridge, cancel=stop.is_set,
                progress=progress, audit=audit, limit=len(records), retry_skipped=scope == "skipped",
                owner=owner)
            messages = {sa_id: "已认领并完成网页批注及名单回写" for sa_id in result.completed_ids}
            messages.update({sa_id: "后台原已处理，仅同步名单" for sa_id in result.synced_ids})
            messages.update(result.skipped)
            tail = "已暂停，请核验网页；不重复提交。" if result.halted else "已按请求暂停。" if result.cancelled else "本轮结束。"
            tail = getattr(result, "reason", "") or tail
            summary = f"完成 {len(result.completed_ids)} · 原已处理 {len(result.synced_ids)} · 跳过 {len(result.skipped)}。{tail}"
        elif need_import:
            from need_import import run_need_import_batch
            result = run_need_import_batch(roster, records, bridge, owner, cancel=stop.is_set,
                progress=progress, audit=audit, retry_skipped=scope == "skipped")
            messages = {sa_id: "网页及名单已标注需导入" for sa_id in result.completed_ids}
            messages.update({sa_id: "后台原已处理，仅同步名单" for sa_id in result.synced_ids})
            messages.update(result.skipped)
            tail = result.reason or ("已按请求暂停。" if result.cancelled else "本轮结束。")
            summary = (f"标注需导入 {len(result.completed_ids)} · 原已处理 {len(result.synced_ids)} · "
                       f"跳过 {len(result.skipped)}。{tail}")
        else:
            if metadata_import:
                from metadata_pipeline import run_metadata_batch
                result = run_metadata_batch(self.base, roster, records, bridge, stop, progress, audit,
                                            retry_skipped=scope == "skipped")
            else:
                # Keep the source guard per record, so an old Excel intent
                # cannot authorize WOS writes or block unrelated candidates.
                def source_check(record):
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
                    manual_export=manual_export,source_check=source_check)
            messages = {e["sa_id"]: e["message"] for e in result.outcomes}
            completed = sum(e["status"] == "done" for e in result.outcomes)
            synced = sum(e["status"] == "synced" for e in result.outcomes)
            skipped = sum(e["status"] == "skip" for e in result.outcomes)
            deferred=sum(e['status']=='deferred' for e in result.outcomes)
            summary = f"完成 {completed} · 原已处理 {synced} · 跳过 {skipped} · 待核验 {deferred} · 未执行 {result.remaining}。"
            if result.reason:
                summary += result.reason
            elif skipped or deferred:
                reasons = {}
                for item in result.outcomes:
                    if item["status"] in ('skip','deferred'):
                        note = str(item["message"])
                        reasons[note] = reasons.get(note, 0) + 1
                shown = "；".join(f"{note[:100]}（{count}条）" for note, count in list(reasons.items())[:3])
                summary += "本轮结束。"+('跳过/待核原因：' if deferred else '跳过原因：') + shown
                if len(reasons) > 3:
                    summary += "；其余见逐条结果和诊断。"
            else:
                summary += "本轮结束；完成记录已核验并回写名单。"
        if scope=='skipped' and not result.halted and not getattr(result,'cancelled',False):
            from roster_annotations import annotate_roster, review_reason
            by_id={r.sa_id:r for r in result.roster.records}
            unresolved={sid:review_reason(message) for sid,message in messages.items()
                        if sid in by_id and by_id[sid].skipped and not by_id[sid].done and review_reason(message)}
            if unresolved:
                result.roster=annotate_roster(result.roster,unresolved=unresolved).roster
            resolved_ids={sid for sid in messages if sid in by_id and by_id[sid].done}
            if resolved_ids:
                result.roster=annotate_roster(result.roster,resolved_ids=resolved_ids).roster
        from roster_annotations import format_owned_status
        result.roster=format_owned_status(result.roster,ids=messages).roster
        return TaskResult(result.roster, messages, summary, halted=bool(result.halted),
                          reason=getattr(result, "reason", ""), cancelled=bool(getattr(result, "cancelled", False)),
                          deferred_count=deferred)
