"""Read-only evidence review. Recommendations never authorize a browser write."""
import json
import re
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

from automation import doi, norm, wos
from core import SafetyStop
from front_lookup import lookup
from wos_policy import match_count
from zero_match import verify_sa

SJTU = re.compile(r"上海交通大学|shanghai\s+jiao\s*tong|\bsjtu\b", re.I)
UNKNOWN_UNITS = {"", "unknown", "待核", "未提供", "不详", "-"}


def paper_identity(comparison):
    fields = {row["label"]: (norm(row["sa"]), norm(row["library"])) for row in comparison}
    try:
        for label, normalize in (("DOI", doi), ("WOS记录号", wos)):
            if label in fields:
                fields[label] = tuple(normalize(value) for value in fields[label])
    except SafetyStop:
        return "文献标识待核"
    for label in ("题名", "DOI", "WOS记录号", "期刊"):
        left, right = fields.get(label, ("", ""))
        if left and right and left != right:
            return "错配待核"
    title = fields.get("题名", ("", ""))
    if title[0] and title[0] == title[1] and any(
        left and left == right for left, right in
        (fields.get(label, ("", "")) for label in ("DOI", "WOS记录号"))
    ):
        return "同篇已比对"
    return "文献身份待核"


def role_suggestion(count, explicit, emails, *, complete=True):
    """Author indices are one-based. None means missing or ambiguous evidence."""
    if not complete or type(count) is not int or count < 1:
        return "全文作者表待核", None
    def valid(indices):
        return (isinstance(indices, list) and all(type(i) is int and 1 <= i <= count for i in indices)
                and len(indices) == len(set(indices)))
    if explicit is not None:
        return ("全文明确标记", sorted(explicit)) if valid(explicit) and explicit else ("通讯标记待核", None)
    if count == 1:
        return "单作者规则", [1]
    if not valid(emails) or not emails:
        return "作者邮箱对应待核", None
    return "作者邮箱规则", [] if len(emails) == count else sorted(emails)


def author_suggestion(original, library, aliases, affiliations):
    """Use only operator-verified full author lists and an exhaustive alias mapping."""
    original, library, aliases = map(lambda names: [norm(name) for name in names],
                                     (original, library, aliases))
    if not original or not library or not aliases or any(not name for name in original + library + aliases):
        return "待核"
    if len(set(original)) != len(original) or len(set(library)) != len(library):
        return "待核"
    matches = [i for i, name in enumerate(original) if name in aliases]
    if len(matches) > 1:
        return "待核"
    if matches and (original != library or not any(name in aliases for name in library)):
        return "库内作者待修正"
    if original == library and not matches:
        return "SA数据有误"
    if not matches or len(affiliations) != len(original):
        return "待核"
    units = affiliations[matches[0]]
    if not units or any(norm(unit) in UNKNOWN_UNITS for unit in units):
        return "待核"
    if any(SJTU.search(norm(unit)) for unit in units):
        return "待核"
    return "作者署名非交大"


def _comparison(answer):
    rows = answer.get("comparison")
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or any(not isinstance(row.get(key), str) for key in ("label", "sa", "library"))
        for row in rows
    ) or len({row["label"] for row in rows}) != len(rows):
        raise SafetyStop("网页对比详情不完整。")
    return [{key: row[key] for key in ("label", "sa", "library")} for row in rows]


def _original(base, record):
    # Key includes record identity; a file for a different workbook row cannot be reused.
    path = base / "runtime/evidence-input" / (record.key + ".json")
    if not path.is_file():
        return {}
    if path.is_symlink() or path.stat().st_size > 200_000:
        raise SafetyStop("原文证据文件路径或大小异常。")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("record_key") != record.key or data.get("fulltext_verified") is not True:
        raise SafetyStop("原文证据与本条名单不对应或未经核验。")
    for key in ("authors", "library_authors", "verified_aliases"):
        values = data.get(key, [])
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise SafetyStop("原文作者证据格式异常。")
    units = data.get("affiliations", [])
    if not isinstance(units, list) or any(not isinstance(group, list) or
        any(not isinstance(unit, str) for unit in group) for group in units):
        raise SafetyStop("逐作者单位证据格式异常。")
    if not isinstance(data.get("source"), str) or not data["source"].strip():
        raise SafetyStop("原文来源未记录。")
    return data


def _redact(value):
    if isinstance(value, str):
        return re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[邮箱省略]", value)
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    return value


def inspect_record(base, record, call):
    base = Path(base)
    status = call("status", {"sa_id": record.sa_id})
    row = status.get("row")
    verify_sa(record, row)
    count = match_count(row)
    comparison = []
    reason = ""
    if count == 1:
        detail = call("search", {"sa_id": record.sa_id, "close_detail": True})
        if detail.get("row") != row or detail.get("detail_released") is not True:
            raise SafetyStop("网页记录变化或详情未释放，请重读。")
        comparison = _comparison(detail)
    elif count > 1:
        reason = "多个平台号，逐条身份待核"
    identity = paper_identity(comparison)
    outcome = {
        "sa_id": record.sa_id, "record_key": record.key, "工号": record.staff_id,
        "当前平台号": row.get("itemId"), "匹配数": count, "网页状态": row.get("markStatus"),
        "SA": {"题名": record.title, "DOI": record.doi, "WOS": record.wos},
        "对比": comparison, "判定": identity, "待核原因": reason, "建议动作": [],
        "原文证据文件": "runtime/evidence-input/" + record.key + ".json",
    }
    absent = False
    if count != 1 or identity != "同篇已比对":
        candidates = []
        def collect(items):
            for item in items:
                candidate = {key: item[key] for key in ("id", "title", "doi", "wos", "deleted")}
                if candidate not in candidates:
                    candidates.append(candidate)
        try:
            found = lookup(call, record, collect=collect)
        except SafetyStop:
            outcome["待核原因"] = "前台检索未确认，请检查绑定、登录及网页状态"
            return _redact(outcome)
        outcome["馆藏候选"] = candidates
        outcome["重复管理检索题名"] = list(dict.fromkeys(item["title"] for item in candidates if item["title"]))
        if found.item:
            outcome["关联建议"] = {"原平台号": row.get("itemId"), "候选平台号": found.item["id"]}
            outcome["建议动作"].append("核对候选平台号 " + found.item["id"] + " 后决定是否改关联")
        elif found.review:
            outcome["待核原因"] = found.review
        elif found.absent:
            absent = True
            title = quote(record.title, safe="")
            outcome["外库检索"] = {
                "WOS": "https://webofscience.clarivate.cn/wos/woscc/basic-search",
                "Scopus": "https://www.scopus.com/search/form.uri",
                "知网": "https://kns.cnki.net/kns8s/defaultresult/index?kw=" + title,
            }
            outcome["建议动作"].append("按 SA 题名和标识核验外库原文及署名单位")
    original = _original(base, record)
    if original:
        names = original.get("authors", [])
        units = original.get("affiliations", [])
        mapping = original.get("email_authors") if original.get("email_mapping_verified") is True else None
        explicit = original.get("corresponding")
        role_verified = ("corresponding" in original and
                         (explicit is None or original.get("mark_source") in {"全文", "已核验知网标记"}))
        basis, roles = role_suggestion(len(names), explicit, mapping, complete=role_verified)
        from run_diagnostics import safe_text
        outcome["原文来源"] = safe_text(original["source"])
        outcome["角色建议"] = {"通讯作者序号": roles, "依据": basis,
                               "第一作者序号": [1] if len(names) == 1 else None}
        current_roles = original.get("library_corresponding")
        if roles is not None and isinstance(current_roles, list) and all(
            type(i) is int and 1 <= i <= len(names) for i in current_roles
        ) and identity == "同篇已比对" and original.get("library_authors") == names:
            outcome["角色建议"].update({"建议设为通讯": sorted(set(roles) - set(current_roles)),
                                       "建议取消通讯": sorted(set(current_roles) - set(roles))})
        if absent and original.get("affiliations_verified") is True and names and len(units) == len(names) and all(
            group and all(norm(unit) not in UNKNOWN_UNITS for unit in group) for group in units
        ):
            note = "需导入" if any(SJTU.search(norm(unit)) for group in units for unit in group) else "非交大"
            outcome["建议备注"] = {"网页": note, "名单": note}
        if (identity == "同篇已比对" and re.fullmatch(r"[0-9]{1,40}", record.staff_id)
                and original.get("staff_id") == record.staff_id and original.get("identity_verified") is True):
            suggestion = author_suggestion(names, original.get("library_authors", []),
                                           original.get("verified_aliases", []),
                                           units if original.get("affiliations_verified") is True else [])
            outcome["建议动作"].append(suggestion)
            if suggestion == "作者署名非交大":
                outcome["建议备注"] = {"网页": suggestion, "名单": "已处理"}
            elif suggestion == "SA数据有误":
                outcome["建议备注"] = {"网页": suggestion, "名单": suggestion}
    else:
        outcome["建议动作"].append("核对完整原文作者、单位及角色")
    from batch_recovery import check_legacy_import_journals
    try:
        check_legacy_import_journals(base, record)
    except SafetyStop as exc:
        outcome["写入阻塞"] = str(exc)
    if "需导入结案待核" in record.remark:
        outcome["写入阻塞"] = "原结案回执不明，需只读续验"
    return _redact(outcome)


def save_report(base, entries):
    folder = Path(base) / "runtime/evidence-reviews"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (uuid4().hex + ".json")
    with path.open("x", encoding="utf-8") as stream:
        json.dump(entries, stream, ensure_ascii=False, indent=2)
    return path
