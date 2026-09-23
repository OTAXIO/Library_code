"""Extract the SA-submitted identifier, never substitute a roster/person ID."""
import re

from core import SafetyStop


def sa_claim_source(comparison):
    if not isinstance(comparison, list):
        raise SafetyStop("请先定位网页，读取认领状态。")
    fields = [row for row in comparison if isinstance(row, dict) and row.get("label") == "认领状态"]
    if len(fields) != 1 or not isinstance(fields[0].get("sa"), str):
        raise SafetyStop("未找到唯一的 SA 提交认领状态，请在网页核对。")
    source = fields[0]["sa"].strip()
    # Full/half-width parentheses are accepted; leading zeroes stay intact.
    groups = re.findall(r"\(([^()]*)\)|（([^（）]*)）", source)
    if len(groups) != 1 or sum(source.count(char) for char in "()（）") != 2:
        raise SafetyStop("SA 提交中的括号编号为空或不唯一，请人工处理。")
    identifier = (groups[0][0] or groups[0][1]).strip()
    if not re.fullmatch(r"[0-9]{1,40}", identifier):
        raise SafetyStop("SA 括号编号不是完整数字文本，请人工核对。")
    return source, identifier
