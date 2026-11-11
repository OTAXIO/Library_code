"""Independent read-only cross-review queue and synchronous local feedback."""
import hashlib
import json
import os
import re
import unicodedata
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

from automation import norm
from bridge import BrowserRejected
from core import SafetyStop, is_assigned_owner
from evidence_review import _comparison, _redact, role_suggestion
from feedback_write import FeedbackModified, remove_feedback, write_feedback


SCAN_PASSES = 8
RULE_VERSION = 3
TRANSIENT_WAIT_SECONDS = 15
TRANSIENT_RETRIES = 4
MAX_CONSECUTIVE_REJECTIONS = 5
SJTU_NAMES = ("shanghai jiao tong", "shanghai jiaotong", "sjtu", "上海交通大学", "上海交大")


def in_scope(row, owner):
    return isinstance(row, dict) and row.get("updateUsername") == owner and row.get("markStatus") == "已处理"


def title_key(text):
    return "".join(char for char in unicodedata.normalize("NFKC", text or "").casefold() if char.isalnum())


def doi_key(text):
    text = "".join(unicodedata.normalize("NFKC", text or "").casefold().split())
    match = re.search(r"10\.\d{4,9}/\S+", text)
    return match[0].rstrip(".,;") if match else ""


def accession_key(text):
    text = "".join((text or "").split()).upper()
    if text in ("", "—", "-", "无", "NULL"):
        return ""
    return "WOS:" + text if re.fullmatch(r"[0-9]{15}", text) else text


def assess(detail, original=None):
    fields = {v["label"]: v for v in _comparison(detail)}
    def differs(label):
        value = fields.get(label, {})
        left, right = norm(value.get("sa")), norm(value.get("library"))
        return bool(left and right and left != right)
    issues, pending = [], []
    def pair(label, normalize):
        value = fields.get(label, {})
        return tuple(normalize(value.get(side, "")) for side in ("sa", "library"))
    def equal(values):
        return bool(all(values) and values[0] == values[1])
    def different(values):
        return bool(all(values) and values[0] != values[1])
    titles = pair("题名", title_key)
    dois = pair("DOI", doi_key)
    accessions = pair("WOS记录号", accession_key)
    if equal(titles) or equal(dois) or equal(accessions):
        if equal(titles) and not equal(dois) and not equal(accessions) and different(dois):
            pending.append("题名相同但 DOI 不同，待核版本")
    elif different(titles) and (different(dois) or any(
            different(pair(label, title_key)) for label in ("期刊", "刊名"))):
        return ["两边不是同一篇论文"], []
    else:
        pending.append("题名不同且无共同 DOI/WOS，待核是否同一篇")
    for label in ("发表年月", "年份", "出版年"):
        value = fields.get(label, {})
        years = [re.search(r"\b(?:19|20)\d{2}\b", value.get(side, "")) for side in ("sa", "library")]
        if all(years) and (gap := abs(int(years[0][0]) - int(years[1][0]))) >= 2:
            pending.append(f"发表年份相差 {gap} 年")
            break
    row = detail.get("row")
    row = row if isinstance(row, dict) else {}
    identity = detail.get("claim_identity")
    identity = identity if isinstance(identity, dict) else {}
    left, right = identity.get("sa_staff_id"), identity.get("library_staff_id")
    state = identity.get("state")
    remark = row.get("remark", "")
    valid_remark = isinstance(remark, str)
    remark = remark.strip() if valid_remark else ""
    digits = lambda value: isinstance(value, str) and bool(re.fullmatch(r"[0-9]{1,40}", value))
    if not digits(left) or left != row.get("gh"):
        pending.append("SA 工号无法确认")
    if state in ("已认领", "认领不一致") and digits(right):
        if left != right:
            issues.append("认领工号不一致")
    elif state == "未认领" and valid_remark:
        if "非交大" in remark:
            pass
        elif not remark or "已认领" in remark:
            issues.append("未认领，但已处理")
        else:
            pending.append(f"未认领，按备注“{remark[:40]}”结案，待核")
    else:
        pending.append("认领或结案依据待核")

    unit = fields.get("作者单位", {}).get("library", "").strip().casefold()
    has_sjtu = any(name in unit for name in SJTU_NAMES)
    if unit and not has_sjtu and state == "已认领":
        pending.append("本库作者单位未见交大，待核是否交大文献")
    if "非交大" in remark and has_sjtu:
        pending.append("备注非交大，但本库作者单位含交大")
    if differs("交大是否第一单位"):
        pending.append("交大是否第一单位两边不一致")

    def flag(role, side="library"):
        text = fields.get("作者信息", {}).get(side, "")
        found = re.findall(r"是否" + role + r"\s*[：:]\s*([是否])(?=\s|$)", text)
        return found[0] == "是" if len(found) == 1 else None

    if not original:
        for role in ("第一作者", "通讯作者"):
            sa, library = flag(role, "sa"), flag(role)
            if sa is not None and library is not None and sa != library:
                pending.append(role + "标记两边不一致")
        return issues, pending
    if not digits(left) or original.get("staff_id") != left or left != right:
        return issues, pending + ["原文作者及角色待核"]
    count, target = original.get("author_count"), original.get("author_index")
    if type(count) is not int or type(target) is not int or not 1 <= target <= count:
        return issues, pending + ["原文作者映射待核"]
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
        original_ids = set(rows)
        seen = set()
        total = None
        self.data["ready"] = False
        self.save()

        def read_page(page, requested_size=None, *, allow_cap=False):
            nonlocal total
            payload = {"owner": self.owner, "page": page}
            if requested_size is not None:
                payload["page_size"] = requested_size
            answer = call("cross_review_page", payload)
            current_total, current_size = answer.get("total"), answer.get("page_size")
            batch = answer.get("rows")
            if allow_cap and (type(current_size) is not int or current_size != requested_size):
                raise SafetyStop("扩展未更新到 0.4.63，请在扩展管理页重载扩展后重新配对")
            if (answer.get("protocol") != 1 or answer.get("owner") != self.owner or answer.get("page") != page
                    or type(current_total) is not int or current_total < 0 or type(current_size) is not int
                    or current_size < 1 or not isinstance(batch, list)
                    or total is not None and current_total != total
                    or requested_size is not None and current_size != requested_size):
                raise SafetyStop("分页总数或范围不稳定，复核队列尚未固定。")
            expected = min(current_size, max(0, current_total - (page - 1) * current_size))
            if (allow_cap and not 0 < len(batch) <= expected
                    or not allow_cap and len(batch) != expected):
                raise SafetyStop("分页总数或范围不稳定，复核队列尚未固定。")
            page_ids = set()
            for row in batch:
                sid = row.get("saLzkId") if isinstance(row, dict) else None
                if not in_scope(row, self.owner) or not isinstance(sid, str) or not sid or sid in page_ids:
                    raise SafetyStop("列表含重复 ID 或超出所选范围，停止核验。")
                page_ids.add(sid)
            total = current_total
            seen.update(page_ids)
            for row in batch:
                rows.setdefault(row["saLzkId"], {"title": row.get("titleValue", ""), "status": "未核验"})
            self.data.update(scanned=len(seen), total=total, page_size=current_size)
            self.save()
            return len(batch)

        def finish():
            if len(seen) > total:
                raise SafetyStop("收集的 ID 数超过分页总数，复核范围已变化，进度已保存。")
            if len(seen) != total:
                return False
            missing = len(original_ids - seen)
            if missing:
                raise SafetyStop(f"已入队的 {missing} 条不在当前已处理列表，复核范围已变化。")
            self.data["ready"] = True
            self.save()
            return True

        if stop.is_set():
            return
        read_page(1)
        progress(f"读取已处理列表 · 探测第 1 页 · 已收齐 {len(seen)}/{total}")
        if stop.is_set():
            return
        if total == 0:
            finish()
            return
        cap = 1000
        if total <= 1000:
            cap = read_page(1, total, allow_cap=True)
            progress(f"读取已处理列表 · 一次读取 {cap} 条 · 已收齐 {len(seen)}/{total}")
            if stop.is_set():
                return
            if cap == total:
                finish()
                return
        pages = (total + cap - 1) // cap
        for scan_pass in range(1, SCAN_PASSES + 1):
            for page in range(1, pages + 1):
                if stop.is_set():
                    self.save()
                    return
                read_page(page, cap)
                progress(f"读取已处理列表 · 第 {scan_pass} 遍 第 {page}/{pages} 页 · 已收齐 {len(seen)}/{total}")
                if stop.is_set():
                    return
            if finish():
                return
        raise SafetyStop(f"网站分页排序不稳定，读 {SCAN_PASSES} 遍后仍只收齐 {len(seen)}/{total} 条，"
                         "未开始核验，进度已保存。")

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

    def _inspect(self, call, sid, entry):
        row = call("status", {"sa_id": sid}).get("row")
        if not in_scope(row, self.owner) or row.get("saLzkId") != sid:
            return [], ["网页处理人或状态已变化"]
        entry["remark"] = row.get("remark", "")
        if str(row.get("matchCount")) != "1":
            return [], ["当前匹配数非 1，需核验结案依据"]
        detail = call("search", {"sa_id": sid, "close_detail": True})
        if detail.get("detail_released") is not True:
            raise SafetyStop("网页详情未关闭，进度已保存。")
        if detail.get("row") != row:
            return [], ["网页处理人或状态已变化"]
        try:
            issues, pending = assess(detail, self.original(row))
        except SafetyStop as exc:
            return [], [str(exc)]
        entry["comparison"] = _redact(_comparison(detail))
        entry["claim_identity"] = detail.get("claim_identity")
        entry["原文证据文件"] = str(self.original_path(sid))
        entry["原文证据定位"] = {key: row.get(key) for key in (
            "saLzkId", "itemId", "titleValue", "doiValue", "wosValue", "gh")}
        return issues, pending

    def validate_reviewer(self, reviewer):
        if not is_assigned_owner(reviewer) or len(reviewer) > 40 or reviewer == self.owner:
            raise SafetyStop("请输入 1–40 字的校对人，校对人不能是被复核人。")

    def _offline_detail(self, sid, entry):
        locator = entry.get("原文证据定位")
        keys = ("saLzkId", "itemId", "titleValue", "doiValue", "wosValue", "gh")
        if (entry.get("rule") == RULE_VERSION or entry.get("rejected")
                or not isinstance(entry.get("comparison"), list)
                or not isinstance(entry.get("claim_identity"), dict) or "remark" not in entry
                or not isinstance(locator, dict) or not all(key in locator for key in keys)
                or locator.get("saLzkId") != sid or not locator.get("gh")):
            return None
        detail = {"comparison": entry["comparison"], "claim_identity": entry["claim_identity"],
                  "row": {**locator, "remark": entry["remark"]}}
        try:
            _comparison(detail)
        except SafetyStop:
            return None
        return detail

    def _read_with_retry(self, call, sid, entry, stop, progress):
        for attempt in range(TRANSIENT_RETRIES + 1):
            # A timeout may follow a successful status read. Commit evidence only
            # once the entire read succeeds, leaving interrupted entries untouched.
            evidence = deepcopy(entry)
            try:
                result = self._inspect(call, sid, evidence)
            except BrowserRejected as exc:
                if "已发出写入" in str(exc) or not any(
                        word in str(exc) for word in ("仍在加载", "查询记录超时")):
                    raise
                if attempt == TRANSIENT_RETRIES:
                    raise SafetyStop("网页持续卡在加载中，请刷新 SA 比对页后重新开始，进度已保存") from exc
                progress(f"网页仍在加载，等待 {TRANSIENT_WAIT_SECONDS} 秒后重试 {sid}（{attempt+1}/{TRANSIENT_RETRIES}）")
                if stop.wait(TRANSIENT_WAIT_SECONDS):
                    return None
            else:
                entry.update(evidence)
                return result

    def run(self, bridge, stop, progress, reviewer=None):
        if reviewer is not None:
            self.validate_reviewer(reviewer)
        def call(action, payload):
            if action not in ("cross_review_page", "status", "search") or stop.is_set():
                raise SafetyStop("只读复核已暂停。")
            return bridge.call(action, payload)
        if not self.data["ready"]:
            self.scan(call, stop, progress)
        if not self.data["ready"]:
            return
        n = len(self.data["rows"])
        consecutive_rejections = 0
        sync_errors = {}
        needs_sync = reviewer is not None or any(
            entry.get("auto_confirmed") for entry in self.data["rows"].values())
        for i, (sid, entry) in enumerate(self.data["rows"].items(), 1):
            if stop.is_set():
                break
            if "confirmed" in entry and not entry.get("auto_confirmed"):
                continue
            if (not entry.get("rejected") and entry["status"] in ("通过", "有问题", "待核")
                    and entry.get("rule") == RULE_VERSION):
                continue
            offline = self._offline_detail(sid, entry)
            progress(f"按新规则重判 {i}/{n} {sid}" if offline else f"只读复核 {i}/{n} {sid}")
            try:
                result = (assess(offline, self.original(offline["row"])) if offline else
                          self._read_with_retry(call, sid, entry, stop, progress))
                if result is None:
                    return
                issues, pending = result
            except BrowserRejected as exc:
                if "已发出写入" in str(exc):
                    raise
                issues, pending = [], ["网页读取被拒绝：" + str(exc)[-200:]]
                entry["rejected"] = True
                consecutive_rejections += 1
            else:
                entry.pop("rejected", None)
                consecutive_rejections = 0
            if entry.get("auto_confirmed") and not entry.get("rejected"):
                needs_sync = True
                if issues:
                    entry.update(confirmed="；".join(issues), feedback_synced=False)
                    entry.pop("feedback_remove_pending", None)
                else:
                    # Keep the exact old four-column values until removal succeeds.
                    entry["feedback_remove_pending"] = True
            entry.update(status="有问题" if issues else "待核" if pending else "通过",
                         issues=issues, pending=pending, rule=RULE_VERSION)
            if reviewer is not None and issues:
                entry.update(confirmed="；".join(issues), reviewer=reviewer,
                             auto_confirmed=True, feedback_synced=False)
            self.save()
            if consecutive_rejections >= MAX_CONSECUTIVE_REJECTIONS:
                raise SafetyStop("连续 5 条被网页拒绝，可能页面或登录异常，已暂停")
            if entry.get("auto_confirmed") and not entry.get("rejected"):
                try:
                    self._sync_entry(sid, entry)
                except SafetyStop as exc:
                    sync_errors[sid] = str(exc)
        if needs_sync:
            self.sync_pending(errors=sync_errors)

    def confirm(self, sid, note, reviewer):
        self.validate_reviewer(reviewer)
        note = note.strip()
        if not note or len(note) > 1000:
            raise SafetyStop("请填写 1–1000 字的已确认问题。")
        if self.data["rows"][sid]["status"] == "未核验":
            raise SafetyStop("请先读取本条复核证据。")
        self.data["rows"][sid].update(confirmed=note, status="有问题", reviewer=reviewer, feedback_synced=False)
        self.data["rows"][sid].pop("auto_confirmed", None)
        self.data["rows"][sid].pop("feedback_remove_pending", None)
        self.save()
        self.sync_pending()

    def _sync_entry(self, sid, entry):
        if entry.get("feedback_remove_pending") and entry.get("auto_confirmed"):
            self._remove_entry_feedback(sid, entry)
            return
        # Older queues have no reviewer; never attribute their notes to the loader.
        entry["feedback_synced"] = False
        self.save()
        if not entry.get("reviewer"):
            raise SafetyStop("历史确认缺少校对人，请在交叉核验页输入校对人后重新确认该条。")
        write_feedback(self.feedback_path, self.backup_dir, sid, entry["confirmed"],
                       self.owner, entry["reviewer"])
        entry["feedback_synced"] = True
        try:
            self.save()
        except OSError:
            # The workbook may already be written; retry must remain idempotent.
            entry["feedback_synced"] = False
            raise

    def _remove_entry_feedback(self, sid, entry):
        self.save()
        try:
            remove_feedback(self.feedback_path, self.backup_dir, sid, entry["confirmed"],
                            self.owner, entry.get("reviewer"))
        except FeedbackModified as exc:
            pending = entry.setdefault("pending", [])
            if str(exc) not in pending:
                pending.append(str(exc))
            entry["status"] = "待核"
        else:
            entry["status"] = "待核" if entry.get("pending") else "通过"
        # Human edits are terminal: stop automatic deletion/upsert for this row.
        # Failures such as an Excel lock escape above, retaining retry metadata.
        for key in ("confirmed", "auto_confirmed", "reviewer", "feedback_synced", "feedback_remove_pending"):
            entry.pop(key, None)
        self.save()

    def sync_pending(self, *, errors=None):
        # A run supplies failures already attempted so each entry is tried once.
        errors = {} if errors is None else errors.copy()
        for sid, entry in self.data["rows"].items():
            if (sid in errors or not entry.get("confirmed")
                    or entry.get("feedback_synced") is True and not entry.get("feedback_remove_pending")):
                continue
            try:
                self._sync_entry(sid, entry)
            except SafetyStop as exc:
                errors[sid] = str(exc)
        if errors:
            raise SafetyStop("确认内容已保留在本地 JSON，以下问题未同步：\n" +
                             "\n".join(f"{sid}：{error}" for sid, error in errors.items()))
