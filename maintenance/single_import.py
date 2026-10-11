"""One-record live acceptance using exactly the production extension workflow.

No browser driver is imported. Pair in the normal extension popup, then send
RUN on stdin. This permits ending the diagnostic browser session before the
acceptance starts. The private report never includes the pairing token.
"""
from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import tkinter as tk
from datetime import datetime, timezone, timedelta
from pathlib import Path
from tkinter import ttk

from automation import ImportStore
from bridge import Bridge
from core import SafetyStop, fixed_roster_path, read_roster
from operation_log import OperationLog
from pairing_ui import copy_pairing_code
from wos_batch import default_store
from zero_match import OWNER, SerialWOS, WorkflowStore, run_batch


BASE = Path(__file__).resolve().parents[1]


def select_one(roster, sa_id):
    roster.assert_unchanged()
    rows = [r for r in roster.records if r.sa_id == sa_id]
    if (len(rows) != 1 or rows[0].owner != OWNER or rows[0].matches != 0 or
            rows[0].done or rows[0].skipped):
        raise SafetyStop("单条验收只接受谭勋策未完成、未跳过的零匹配任务。")
    return rows[0]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sa-id", required=True)
    args = parser.parse_args()
    roster = read_roster(fixed_roster_path(BASE))
    record = select_one(roster, args.sa_id)
    bridge = Bridge()  # Exclusive listener; never attaches to or kills another app.
    stop = threading.Event()
    messages = queue.Queue()
    commands = queue.Queue()
    root = tk.Tk()
    root.title("单条导入验收 · 使用 SA 网页扩展")
    root.geometry("560x330")
    root.minsize(560, 330)
    panel = ttk.Frame(root, padding=16)
    panel.pack(fill="both", expand=True)
    ttk.Label(panel, text=f"只执行名单 ID：{record.sa_id}").pack(anchor="w")
    ttk.Label(panel, text=record.title, wraplength=525).pack(anchor="w", pady=8)
    ttk.Label(panel, text="请在 SA 页扩展配对，再绑定 WOS 页及后台导入页。\n"
              "配对完成后关闭浏览器排查会话，才开始纯脚本验收。").pack(anchor="w")
    # Keep the Tk variable alive for the window's lifetime; an anonymous
    # StringVar is garbage-collected and silently clears a readonly Entry.
    pair_code = tk.StringVar(value=bridge.token)
    code_entry = ttk.Entry(panel, textvariable=pair_code, state="readonly")
    code_entry.pack(fill="x", pady=8)
    def select_code(event=None):
        code_entry.selection_range(0, tk.END)
        return "break"
    code_entry.bind("<Control-a>", select_code)
    code_entry.bind("<Control-A>", select_code)
    def copy_token():
        try:
            copy_pairing_code(root, bridge.token)
        except Exception:
            code_entry.focus_set()
            select_code()
            status.set("剪贴板暂不可用，配对码已全选；未假报复制成功。")
            print("PAIR_CODE_COPY_FAILED (value not logged)", flush=True)
            return
        status.set("配对码已复制；请在 SA 页扩展连接，再绑定另外两个工作页。")
        print("PAIR_CODE_COPIED (value not logged)", flush=True)
    ttk.Button(panel, text="复制本次配对码", command=copy_token).pack(anchor="w")
    status = tk.StringVar(value="等待扩展配对；尚未提交任何操作。")
    ttk.Label(panel, textvariable=status, wraplength=525).pack(anchor="w", pady=8)
    busy = False
    finished = False
    passed = False
    attempt = 0

    def input_loop():
        for line in sys.stdin:
            commands.put(line.strip().upper())
    threading.Thread(target=input_loop, daemon=True).start()

    def execute():
        try:
            current = read_roster(fixed_roster_path(BASE))
            selected = select_one(current, args.sa_id)
            if selected.key != record.key:
                raise SafetyStop("验收前名单事实发生变化，未开始。")
            log = OperationLog(BASE / "log.txt")
            imports = ImportStore(BASE / "runtime" / "wos-imports")
            workflow = WorkflowStore(imports.root)
            download = SerialWOS(bridge, default_store(BASE), stop, messages.put)
            before_import = imports.get(selected) or {}
            started = datetime.now(timezone.utc).isoformat()
            stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S-%f")
            report = BASE / "runtime" / "live-acceptance" / (stamp + ".json")
            result = run_batch(current, [selected], bridge, download, imports, workflow, stop,
                               messages.put, log.record)
            state = imports.get(selected) or {}
            after = next(r for r in result.roster.records if r.sa_id == args.sa_id)
            evidence = {"sa_id": args.sa_id, "attempt": attempt, "started_utc": started,
                "finished_utc": datetime.now(timezone.utc).isoformat(),
                "roster_before_sha256": current.sha256, "roster_after_sha256": result.roster.sha256,
                "outcomes": result.outcomes, "halted": result.halted, "reason": result.reason,
                "import_phase_before": before_import.get("phase", ""),
                "import_phase": state.get("phase", ""), "batch": state.get("batch", {}),
                "workflow_phase": workflow.get(selected).get("phase", ""),
                "excel_done": after.done, "excel_skipped": after.skipped,
                "excel_remark": after.remark, "excel_source": after.source,
                "imported_and_closed": state.get("phase") == "pushed" and after.done
                    and any(o["status"] == "done" for o in result.outcomes)}
            evidence["new_import_and_closed"] = (evidence["imported_and_closed"]
                                                 and before_import.get("phase") != "pushed")
            report.parent.mkdir(parents=True, exist_ok=True)
            with report.open("x", encoding="utf-8") as stream:
                json.dump(evidence, stream, ensure_ascii=False, indent=2)
            print(json.dumps({"result": evidence, "report": str(report)}, ensure_ascii=False), flush=True)
            messages.put(("finish", evidence))
        except Exception as exc:
            message = str(exc) if isinstance(exc, SafetyStop) else f"验收异常（{type(exc).__name__}），未自动重试。"
            print(json.dumps({"error": message}, ensure_ascii=False), flush=True)
            messages.put(("finish", {"imported_and_closed": False, "reason": message}))

    def diagnose():
        """Only documented read-only commands, before a separate RUN attempt."""
        results = {}
        for action, payload in (
            ("status", {"sa_id": record.sa_id}),
            ("wos_diagnose", {}),
            ("import_capabilities", {"sa_id": record.sa_id}),
        ):
            try:
                results[action] = bridge.call(action, payload, timeout=25)
            except SafetyStop as exc:
                results[action] = {"error": str(exc)}
                break  # A lost result is not permission to send another command.
        stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S-%f")
        report = BASE / "runtime" / "live-acceptance" / (stamp + "-diagnostics.json")
        report.parent.mkdir(parents=True, exist_ok=True)
        with report.open("x", encoding="utf-8") as stream:
            json.dump(results, stream, ensure_ascii=False, indent=2)
        print(json.dumps({"diagnostics": results, "report": str(report)}, ensure_ascii=False), flush=True)
        messages.put(("diagnostic_finish", results))

    def pump():
        nonlocal busy, finished, passed, attempt
        while not commands.empty():
            command = commands.get_nowait()
            if command == "RUN" and not busy and not passed:
                if not bridge.online:
                    print("NOT_CONNECTED", flush=True)
                    continue
                # A RUN is an explicit, separate acceptance attempt, never an
                # automatic retry. Production journals still own every write's
                # recovery and the roster is revalidated before each attempt.
                stop.clear()
                busy, finished = True, False
                attempt += 1
                print("ACCEPTANCE_STARTED: production Bridge only; no manual browser actions", flush=True)
                threading.Thread(target=execute, daemon=True).start()
            elif command == "STOP":
                stop.set()
            elif command == "STATUS":
                print(json.dumps({"connected": bridge.online, "busy": busy, "finished": finished,
                                  "passed": passed, "attempt": attempt}), flush=True)
            elif command == "DIAG" and not busy:
                if not bridge.online:
                    print("NOT_CONNECTED", flush=True)
                    continue
                busy = True
                threading.Thread(target=diagnose, daemon=True).start()
            elif command == "QUIT" and not busy:
                root.destroy()
                return
        while not messages.empty():
            value = messages.get_nowait()
            if isinstance(value, tuple):
                if value[0] == "diagnostic_finish":
                    busy = False
                    status.set("只读连接诊断结束；尚未启动入库验收。")
                    continue
                busy, finished = False, True
                passed = value[1]["imported_and_closed"]
                status.set("已完成真实入库及批注回读。" if passed else
                           "本次尚未通过验收：" + value[1].get("reason", "查看私有报告中的逐步结果。"))
            else:
                status.set(value)
                print(value, flush=True)
        if not busy and not finished:
            status.set("扩展已连接；等待开始验收。" if bridge.online else "等待扩展配对；尚未提交任何操作。")
        root.after(150, pump)

    def close():
        if busy:
            stop.set()
            status.set("正在安全暂停，等待当前回读结束。")
        else:
            root.destroy()
    root.protocol("WM_DELETE_WINDOW", close)
    root.after(150, pump)
    print("READY_TO_PAIR: use the displayed local pairing code; send RUN only after diagnostics end", flush=True)
    try:
        root.mainloop()
    finally:
        bridge.close()


if __name__ == "__main__":
    main()
