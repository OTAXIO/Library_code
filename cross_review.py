"""Independent read-only cross-review queue and synchronous local feedback."""
import hashlib
import json
import os
import re
from pathlib import Path
from uuid import uuid4

from automation import norm
from core import SafetyStop, is_assigned_owner
from evidence_review import _comparison, _redact, paper_identity, role_suggestion
from feedback_write import write_feedback


def in_scope(row, owner):
    return isinstance(row, dict) and row.get("updateUsername") == owner and row.get("markStatus") == "已处理"


def assess(detail, original=None):
    fields = {v["label"]: v for v in _comparison(detail)}
    def differs(label):
        value = fields.get(label, {})
        left, right = norm(value.get("sa")), norm(value.get("library"))
        return bool(left and right and left != right)
    if differs("题名") and any(differs(label) for label in ("期刊", "刊名")):
        return ["两边不是同一篇论文"], []
    if paper_identity(list(fields.values())) != "同篇已比对":
        return [], ["文献身份或版本待核"]
    for label in ("发表年月", "年份", "出版年"):
        value = fields.get(label, {})
        years = [re.search(r"\b(?:19|20)\d{2}\b", value.get(side, "")) for side in ("sa", "library")]
        if all(years) and years[0][0] != years[1][0]:
            return [], ["发表年份不一致，待核对版本"]
    issues, pending = [], []
    row = detail["row"]
    identity = detail.get("claim_identity") or {}
    left, right = identity.get("sa_staff_id"), identity.get("library_staff_id")
    digits = lambda value: isinstance(value, str) and bool(re.fullmatch(r"[0-9]{1,40}", value))
    if digits(left) and left == row.get("gh") and digits(right) and identity.get("state") in ("已认领", "认领不一致"):
        if left != right:
            return ["认领工号不一致"], ["作者角色待核"]
    elif (identity.get("state") == "未认领" and digits(left) and left == row.get("gh") and
          row.get("remark", "") == "已认领"):
        return ["未认领，但已处理"], ["作者角色待核"]
    else:
        pending.append("认领或结案依据待核")
    if not original or not digits(left) or original.get("staff_id") != left or left != right:
        return issues, pending + ["原文作者及角色待核"]
    count, target = original.get("author_count"), original.get("author_index")
    if type(count) is not int or type(target) is not int or not 1 <= target <= count:
        return issues, pending + ["原文作者映射待核"]
    text = fields.get("作者信息", {}).get("library", "")
    def flag(role):
        found = re.findall(r"是否" + role + r"\s*[：:]\s*([是否])(?=\s|$)", text)
        return found[0] == "是" if len(found) == 1 else None
    first = original.get("first_authors", [1] if count == 1 else None)
    first_valid = isinstance(first, list) and bool(first) and all(type(i) is int and 1 <= i <= count for i in first)
    if not first_valid or flag("第一作者") is None:
        pending.append("第一作者或共同一作待核")
    elif flag("第一作者") != (target in first):
        issues.append("共一未修改" if target in first and len(first) > 1 else
                      "第一作者未修改" if target in first else "误标第一作者")
    explicit = original.get("corresponding")
    _, roles = role_suggestion(count, explicit,
        original.get("email_authors") if original.get("email_mapping_verified") is True else None,
        complete="corresponding" in original and
                 (explicit is None or original.get("mark_source") in ("全文", "已核验知网标记")))
    if roles is None or flag("通讯作者") is None:
        pending.append("通讯作者待核")
    elif flag("通讯作者") != (target in roles):
        issues.append("是通讯未修改" if target in roles else "误标通讯作者")
    return issues, pending


class CrossReview:
    def __init__(self, base, owner, *, feedback_path=None):
        if not is_assigned_owner(owner) or len(owner) > 40:
            raise SafetyStop("请输入 1–40 字的复核对象。")
        self.owner = owner
        self.feedback_path = Path(feedback_path) if feedback_path is not None else Path(base) / "复核反馈.xlsx"
        self.backup_dir = Path(base) / "runtime/backups"
        self.root = Path(base) / "runtime/cross-review"
        self.path = self.root / (hashlib.sha256(owner.encode()).hexdigest() + ".json")
        self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {
            "owner": owner, "ready": False, "rows": {}, "scanned": 0, "total": None, "page_size": None,
        }
        if self.data.get("owner") != owner:
            raise SafetyStop("复核进度的处理人不一致。")

    def save(self):
        self.root.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix("." + uuid4().hex + ".tmp")
        try:
            temp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(temp, self.path)
        finally:
            temp.unlink(missing_ok=True)

    def scan(self, call, stop, progress):
        rows = self.data["rows"]
        page = 1
        seen = set()
        total, size = self.data["total"], self.data["page_size"]
        while not stop.is_set():
            progress(f"读取已处理列表 · 第 {page} 页")
            answer = call("cross_review_page", {"owner": self.owner, "page": page})
            current_total, current_size = answer.get("total"), answer.get("page_size")
            batch = answer.get("rows")
            if (answer.get("protocol") != 1 or answer.get("owner") != self.owner or answer.get("page") != page
                    or type(current_total) is not int or current_total < 0 or type(current_size) is not int
                    or current_size < 1 or not isinstance(batch, list)
                    or len(batch) != min(current_size, max(0, current_total - (page - 1) * current_size))
                    or total is not None and (current_total, current_size) != (total, size)):
                raise SafetyStop("分页总数或范围不稳定，复核队列尚未固定。")
            total, size = current_total, current_size
            for row in batch:
                sid = row.get("saLzkId") if isinstance(row, dict) else None
                if not in_scope(row, self.owner) or not isinstance(sid, str) or not sid or sid in seen:
                    raise SafetyStop("列表含重复 ID 或超出所选范围，停止核验。")
                seen.add(sid)
            page_keys = [row["saLzkId"] for row in batch]
            offset = (page - 1) * size
            if offset < self.data["scanned"] and page_keys != list(rows)[offset:offset + size]:
                raise SafetyStop("继续扫描时原分页已变化，需另建队列。")
            for row in batch:
                rows.setdefault(row["saLzkId"], {"title": row.get("titleValue", ""), "status": "未核验"})
            self.data.update(scanned=max(self.data["scanned"], len(seen)), total=total, page_size=size)
            self.data["ready"] = len(seen) == total
            self.save()
            if self.data["ready"]:
                return
            page += 1

    def original(self, row):
        path = self.original_path(row["saLzkId"])
        if not path.is_file() or path.is_symlink() or path.stat().st_size > 200_000:
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        if (isinstance(data, dict) and data.get("fulltext_verified") is True and data.get("source")
                and all(data.get(k) == row.get(k) for k in ("saLzkId", "itemId", "titleValue", "doiValue", "wosValue"))):
            return data
        return None

    def original_path(self, sid):
        return self.root / "original" / (hashlib.sha256(sid.encode()).hexdigest() + ".json")

    def run(self, bridge, stop, progress):
        def call(action, payload):
            if action not in ("cross_review_page", "status", "search") or stop.is_set():
                raise SafetyStop("只读复核已暂停。")
            return bridge.call(action, payload)
        if not self.data["ready"]:
            self.scan(call, stop, progress)
        if not self.data["ready"]:
            return
        for sid, entry in self.data["rows"].items():
            if stop.is_set():
                return
            if entry["status"] in ("通过", "有问题"):
                continue
            progress("只读复核 " + sid)
            row = call("status", {"sa_id": sid}).get("row")
            if not in_scope(row, self.owner) or row.get("saLzkId") != sid:
                entry.update(status="待核", pending=["网页处理人或状态已变化"])
                self.save()
                raise SafetyStop("复核范围已变化，进度已保存。")
            if str(row.get("matchCount")) == "1":
                detail = call("search", {"sa_id": sid, "close_detail": True})
                if detail.get("row") != row or detail.get("detail_released") is not True:
                    raise SafetyStop("网页详情变化或未关闭，进度已保存。")
                issues, pending = assess(detail, self.original(row))
                entry["comparison"] = _redact(_comparison(detail))
                entry["claim_identity"] = detail.get("claim_identity")
                entry["原文证据文件"] = str(self.original_path(sid))
                entry["原文证据定位"] = {key: row.get(key) for key in (
                    "saLzkId", "itemId", "titleValue", "doiValue", "wosValue", "gh")}
            else:
                issues, pending = [], ["当前匹配数非 1，需核验结案依据"]
            entry.update(status="有问题" if issues else "待核" if pending else "通过",
                         issues=issues, pending=pending)
            self.save()

    def confirm(self, sid, note, reviewer):
        if not is_assigned_owner(reviewer) or len(reviewer) > 40 or reviewer == self.owner:
            raise SafetyStop("请输入 1–40 字的校对人，校对人不能是被复核人。")
        note = note.strip()
        if not note or len(note) > 1000:
            raise SafetyStop("请填写 1–1000 字的已确认问题。")
        if self.data["rows"][sid]["status"] == "未核验":
            raise SafetyStop("请先读取本条复核证据。")
        self.data["rows"][sid].update(confirmed=note, status="有问题", reviewer=reviewer, feedback_synced=False)
        self.save()
        self.sync_pending()

    def sync_pending(self):
        errors = []
        for sid, entry in self.data["rows"].items():
            if not entry.get("confirmed") or entry.get("feedback_synced") is True:
                continue
            # Older queues have no reviewer; never attribute their notes to the loader.
            entry["feedback_synced"] = False
            self.save()
            try:
                if not entry.get("reviewer"):
                    raise SafetyStop("历史确认缺少校对人，请在交叉核验页输入校对人后重新确认该条。")
                write_feedback(self.feedback_path, self.backup_dir, sid, entry["confirmed"],
                               self.owner, entry["reviewer"])
            except SafetyStop as exc:
                errors.append(f"{sid}：{exc}")
                continue
            entry["feedback_synced"] = True
            try:
                self.save()
            except OSError:
                # The workbook may already be written; retry must remain idempotent.
                entry["feedback_synced"] = False
                raise
        if errors:
            raise SafetyStop("确认内容已保留在本地 JSON，以下问题未同步：\n" + "\n".join(errors))
