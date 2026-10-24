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


def inspect_staff_claim(record, bridge, before, comparison):
    """Resolve wno to one scholar, then verify that scholar's native author row.

    Names are display data. Even equal Chinese names never establish identity.
    """
    source, staff = sa_claim_source(comparison)
    if (not record.staff_id or record.staff_id != staff or before.get('gh') != staff
            or before.get('saLzkId') != record.sa_id):
        raise SafetyStop('名单、SA 与网页完整工号不一致。')
    result = bridge.call('inspect_claim', {'sa_id': record.sa_id, 'expected': before,
        'sa_text': source, 'staff_id': staff, 'roster_staff_id': record.staff_id})
    if (not isinstance(result, dict) or result.get('row') != before
            or result.get('comparison') != comparison or result.get('verified') is not True
            or result.get('claimed') is not True or result.get('staff_id') != staff
            or not isinstance(result.get('scholar_id'), str) or not result['scholar_id']
            or type(result.get('order')) is not int or result['order'] < 1):
        raise SafetyStop('已认领状态未对应 SA 工号的唯一学者及作者行，未结案。')
    return result
