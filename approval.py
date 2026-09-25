"""Narrow, reviewed claim closure: backend readback precedes Excel completion."""
from dataclasses import dataclass

from claim import sa_claim_source
from core import SafetyStop
from roster_write import reconcile_processed


@dataclass
class ClaimCompletion:
    completion: object
    row: dict
    already_processed: bool


def complete_claim(roster, record, bridge, expected, comparison, reviewed=False):
    if record.owner != "谭勋策" or record not in roster.records or record.done:
        raise SafetyStop("认领结案只允许谭勋策名单中的未完成记录。")
    if reviewed is not True:
        raise SafetyStop("请先核对当前记录、认领结果和处理备注。")
    if (record.matches != 1 or record.reason != "作者不一致" or
            not isinstance(expected, dict) or expected.get("saLzkId") != record.sa_id):
        raise SafetyStop("本按钮仅处理单匹配、仅作者不一致的认领结案，请重新定位。")
    roster.assert_unchanged()
    # Search reads the current status first and can safely reconcile the owned
    # read-only detail. Never switch an already-processed row back to pending.
    latest = bridge.call("search", {"sa_id": record.sa_id})
    row = latest.get("row")
    completion = reconcile_processed(roster, record, row)
    if completion:
        return ClaimCompletion(completion, row, True)
    if row != expected or latest.get("comparison") != comparison:
        raise SafetyStop("网页在核对后发生变化，请重新定位并核对，未提交结案。")
    if str(row.get("matchCount")) != "1" or row.get("reason") != "作者不一致":
        raise SafetyStop("后台不是单匹配、仅作者不一致，不能认领结案。")
    fields = [field for field in comparison or [] if field.get("label") == "认领状态"]
    if len(fields) != 1 or fields[0].get("library", "").strip() != "已认领":
        raise SafetyStop("尚未回读到已认领，不能保存完成状态。")
    _, staff_id = sa_claim_source(comparison)
    if not record.staff_id or staff_id != record.staff_id or row.get("gh") != staff_id:
        raise SafetyStop("名单、SA 和后台人员编号不一致，不能结案。")
    roster.assert_unchanged()
    result = bridge.call("complete", {"sa_id": record.sa_id, "expected": row,
        "expected_comparison": comparison, "reviewed": True, "note": "已认领"})
    verified = result.get("row", {})
    if (result.get("verified") is not True or verified.get("saLzkId") != record.sa_id or
            verified.get("markStatus") != "已处理" or verified.get("remark") != "已认领"):
        raise SafetyStop("网页结案结果未完整核验，Excel 未修改。禁止重复提交，请先核验后台。")
    try:
        completion = reconcile_processed(roster, record, verified)
    except Exception as exc:
        raise SafetyStop("网页已保存为已处理，但 Excel 同步失败。不要重复结案；重读名单并预检可同步。" + str(exc)) from exc
    return ClaimCompletion(completion, verified, False)
