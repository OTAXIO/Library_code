"""User-defined completion remarks, validated against freshly read details."""
import re

from core import SafetyStop

CLAIMED = "已认领"
SA_MISSING_IDS = "DOI和WOSID SA未提交"
CORRESPONDENT_FIXED = "通讯作者修正"
PRESETS = (CLAIMED, SA_MISSING_IDS, CORRESPONDENT_FIXED)


def selected_rules(note):
    return tuple(rule for rule in PRESETS if rule in note)


def valid_length(note):
    parts = [part.strip() for part in re.split(r"[；;]", note) if part.strip()]
    return bool(note) and (len(note) >= 6 or bool(parts) and all(part in PRESETS for part in parts))


def detail_value(comparison, label, side):
    rows = [row for row in comparison if row.get("label") == label]
    if len(rows) != 1 or not isinstance(rows[0].get(side), str):
        raise SafetyStop(f"未读到唯一、完整的“{label}”字段，请先重新查询详情。")
    return rows[0][side].strip()


def validate_note(note, comparison, reason, match_count):
    if not valid_length(note) or len(note) > 2000:
        raise SafetyStop("请选择标准备注，或填写 6–2000 字的核验依据与结论。")
    for rule in selected_rules(note):
        if rule == CLAIMED:
            if "作者不一致" not in reason:
                raise SafetyStop("“已认领”用于作者不一致的认领处理，请核对当前任务原因。")
            if str(match_count) != "1" or detail_value(comparison, "认领状态", "library") != "已认领":
                raise SafetyStop("尚未回读到当前唯一条目“已认领”。请先在网页完成认领、保存，再重新查询。")
        elif rule == SA_MISSING_IDS:
            if detail_value(comparison, "DOI", "sa") or detail_value(comparison, "WOS记录号", "sa"):
                raise SafetyStop("只有 SA 的 DOI 和 WOS ID 都为空，才使用“DOI和WOSID SA未提交”；只缺一个或字段未知时请人工备注。")
        elif rule == CORRESPONDENT_FIXED:
            if "通讯作者" not in reason:
                raise SafetyStop("当前原因未包含通讯作者问题，请核对后再选择“通讯作者修正”。")
            info = detail_value(comparison, "作者信息", "library")
            if not re.search(r"是否通讯作者\s*[：:]\s*[是否](?:\s|$)", info):
                raise SafetyStop("没有读到明确的本库通讯作者标记。请先完成修改并重新查询，再按原文确认。")


def append_remark(existing, rule):
    if rule not in PRESETS:
        raise SafetyStop("未知备注模板。")
    existing = existing.strip()
    if rule in selected_rules(existing):
        return existing
    return existing.rstrip("；;") + "；" + rule if existing else rule
