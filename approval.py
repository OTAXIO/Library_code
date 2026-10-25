"""Narrow, reviewed claim closure: backend readback precedes Excel completion."""
from dataclasses import dataclass, replace
import unicodedata
import re

from claim import sa_claim_source
from core import SafetyStop
from remarks import CLAIMED, SA_MISSING_IDS, detail_value, validate_note
from roster_write import reconcile_processed

COMBINED_MISSING_IDS_CLAIM = f"{SA_MISSING_IDS}；{CLAIMED}"
_AUTHOR_FIELDS = {"认领状态", "作者信息"}
_UNCLAIMED = {"未认领", "无人认领", "未被认领"}


@dataclass
class ClaimCompletion:
    completion: object
    row: dict
    already_processed: bool


def current_claim_record(record, row, enabled=False):
    """Use verified live routing facts; keep the original roster/key for writes."""
    if type(enabled) is not bool:raise SafetyStop('当前匹配核验开关无效。')
    if not enabled:return record
    from zero_match import verify_sa
    verify_sa(record,row)
    item=row.get('itemId','').strip(',') if isinstance(row.get('itemId'),str) else ''
    if (record.owner!='谭勋策' or record.matches!=0 or str(row.get('matchCount'))!='1'
            or not re.fullmatch(r'\d{1,40}',item) or not isinstance(row.get('reason'),str)):
        raise SafetyStop('原零匹配记录未确认变成唯一单匹配，未切换认领。')
    return replace(record,matches=1,item_ids=row['itemId'],reason=row['reason'])


def complete_non_institution_author(roster, record, bridge, before, comparison, prepared, author):
    """Close the user's author task from positive native foreign-affiliation evidence."""
    if (record.owner!='谭勋策' or record not in roster.records or record.done or record.matches!=1
            or before.get('saLzkId')!=record.sa_id or before.get('gh')!=record.staff_id
            or prepared.get('staff_id')!=record.staff_id or prepared.get('person',{}).get('wno')!=record.staff_id
            or author.get('non_institution') is not True or author.get('eligible') is not False
            or author.get('scholarId')!='' or author not in prepared.get('authors',[])):
        raise SafetyStop('非交大作者身份或机构证据未确认。')
    roster.assert_unchanged()
    result=bridge.call('complete', {'sa_id':record.sa_id,'expected':before,
        'expected_comparison':comparison,'reviewed':True,'owner':record.owner,'note':'非交大作者',
        'non_institution_author_evidence':{'prepared':prepared,'author':author}})
    row=result.get('row',{})
    if (result.get('verified') is not True or row.get('saLzkId')!=record.sa_id or row.get('gh')!=record.staff_id or
            row.get('itemId')!=before.get('itemId') or
            row.get('markStatus')!='已处理' or row.get('remark')!='非交大作者'):
        raise SafetyStop('非交大作者结案回执未确认，禁止重复提交。')
    return ClaimCompletion(reconcile_processed(roster, record, row),row,False)


def _norm(value):
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).casefold().split())


def claim_completion_note(record, comparison, claimed=False):
    """Return the only completion note allowed by the fresh comparison evidence."""
    if record.matches != 1 or not isinstance(comparison, list) or not comparison:
        raise SafetyStop("自动认领只处理单匹配且详情完整的记录。")
    labels = set()
    for field in comparison:
        if (not isinstance(field, dict) or set(field) != {"label", "sa", "library"} or
                any(not isinstance(field.get(key), str) for key in ("label", "sa", "library")) or
                not field["label"] or field["label"] in labels):
            raise SafetyStop("比对详情结构异常或字段重复，不能自动认领。")
        labels.add(field["label"])
    if not _AUTHOR_FIELDS <= labels:
        raise SafetyStop("缺少作者或认领详情，不能自动认领。")
    if '题名' in labels and _norm(detail_value(comparison,'题名','sa')) != _norm(detail_value(comparison,'题名','library')):
        raise SafetyStop('文献题名待核验，未自动认领或结案。')
    for label in ('DOI','WOS记录号'):
        if label in labels and not ('DOI' in str(record.reason) and 'WOS' in str(record.reason)):
            left=detail_value(comparison,label,'sa')
            if left and _norm(left)!=_norm(detail_value(comparison,label,'library')):
                raise SafetyStop('文献标识待核验，未自动认领或结案。')
    # The modern card embeds both roles inside 作者信息. A label-only router
    # missed these disagreements and could close them as a mere claim issue.
    # Parse explicit flags only; order==1 never proves absence of co-first roles.
    info=[detail_value(comparison,'作者信息',side) for side in ('sa','library')]
    state = _norm(detail_value(comparison, "认领状态", "library"))
    unbound_card = not claimed and state in {_norm(value) for value in _UNCLAIMED} and info[1] == ''
    for role in ('第一作者','通讯作者'):
        pattern=r'是否'+role+r'\s*[：:]\s*([是否])(?=\s|$)'
        flags=[re.findall(pattern,text) for text in info]
        if any('是否'+role in text for text in info):
            if unbound_card and len(flags[0]) == 1:
                continue # Roles are checked again from the person-bound card AFTER claiming.
            if any(len(value)!=1 for value in flags) or flags[0]!=flags[1]:
                raise SafetyStop(role+'待核验，需按原文确认后处理。')
    if claimed:
        if state != _norm(CLAIMED):
            raise SafetyStop("尚未回读到已认领，不能自动结案。")
    elif state not in {_norm(value) for value in _UNCLAIMED}:
        raise SafetyStop("当前记录不是未认领状态。")

    reason = str(record.reason or "")
    if reason == "作者不一致":
        return CLAIMED
    if "DOI" in reason and "WOS" in reason:
        sa_doi = detail_value(comparison, "DOI", "sa")
        sa_wos = detail_value(comparison, "WOS记录号", "sa")
        library_doi = detail_value(comparison, "DOI", "library")
        library_wos = detail_value(comparison, "WOS记录号", "library")
        if sa_doi or sa_wos:
            raise SafetyStop("SA 的 DOI 和 WOSID 并非同时为空，本条保留人工核验。")
        if not library_doi or not library_wos:
            raise SafetyStop("本库未同时读到 DOI 和 WOSID，不能据此自动结案。")
        return COMBINED_MISSING_IDS_CLAIM
    raise SafetyStop("当前待处理原因不属于可自动认领结案的范围。")


def verify_claim_result(record, result, person, author):
    """A successful transport response alone is not a verified author claim."""
    if (result.get("row", {}).get("saLzkId") != record.sa_id or result.get("verified") is not True
            or result.get("claimed") is not True or result.get("staff_id") != person["wno"]
            or result.get("scholar_id") != person["id"] or result.get("author") != author["fullname"]
            or result.get("order") != author["order"]):
        raise SafetyStop("已发出认领，但回读结果不一致。请核验网页，禁止直接重试。")


def verified_claim_rejection(result, before, person, author):
    """A typed backend rejection plus an independent unchanged native read."""
    if result.get('rejected') is not True:
        return None
    error=result.get('rejection',{})
    if (type(result.get('claim_response_protocol')) is not int or result['claim_response_protocol']!=1
            or result.get('unchanged') is not True or result.get('row')!=before
            or result.get('staff_id')!=person['wno'] or result.get('scholar_id')!=person['id']
            or type(result.get('order')) is not type(author['order']) or result['order']!=author['order']
            or type(error.get('status')) is not int or error['status'] not in (200,400)
            or type(error.get('code')) is not int or not isinstance(error.get('message'),str)
            or not error['message'].strip()):
        raise SafetyStop('后台拒绝证据未完整核验，不重复认领。')
    return error


def auto_complete_claim(roster, record, bridge, before, comparison, proof, person, author,
                        confirmed=False, note=None, current_match=False):
    """Continue only the just-verified claim, never an uncertain retry."""
    context=current_claim_record(record,before,current_match)
    if (confirmed is not True or record.owner != "谭勋策" or record not in roster.records or record.done
            or context.matches != 1):
        raise SafetyStop("自动批注只允许本次确认的谭勋策单匹配作者认领。")
    expected_note = claim_completion_note(context, comparison, claimed=False)
    note = expected_note if note is None else note
    if note != expected_note:
        raise SafetyStop("自动认领备注与本次差异证据不一致。")
    verify_claim_result(record, proof, person, author)
    if (not isinstance(before, dict) or proof.get("row") != before or
            before.get("saLzkId") != record.sa_id or not record.staff_id or
            person.get("wno") != record.staff_id or before.get("gh") != record.staff_id):
        raise SafetyStop("本次认领证据与名单/原快照不一致，停止自动批注。")
    roster.assert_unchanged()
    latest = bridge.call("search", {"sa_id": record.sa_id})
    row = latest.get("row")
    if isinstance(row, dict) and row.get("markStatus") == "已处理" and row.get("remark") != note:
        raise SafetyStop("认领后记录已被处理，但备注与本次证据不一致；Excel 未同步，请人工核验。")
    completion = reconcile_processed(roster, record, row)
    if completion:
        return ClaimCompletion(completion, row, True)
    # Only claim/audit fields may change as a consequence of the one claim.
    mutable = {"claimStatus", "updateTime", "updateUsername"}
    if ({k: v for k, v in before.items() if k not in mutable} !=
            {k: v for k, v in row.items() if k not in mutable}):
        raise SafetyStop("认领后条目或备注发生变化，停止自动批注，请人工核对。")
    def stable_fields(fields):
        if not isinstance(fields, list) or not fields:
            raise SafetyStop("比对详情缺失，停止自动批注。")
        labels, stable = set(), []
        for field in fields:
            if (not isinstance(field, dict) or set(field) != {"label", "sa", "library"} or
                    any(not isinstance(value, str) for value in field.values()) or
                    not field["label"] or field["label"] in labels):
                raise SafetyStop("比对详情结构异常或字段重复，停止自动批注。")
            labels.add(field["label"])
            stable.append((field["label"], field["sa"],
                           "" if field["label"] in {"作者信息", "认领状态"} else field["library"]))
        if not {"作者信息", "认领状态"} <= labels:
            raise SafetyStop("缺少作者或认领详情，停止自动批注。")
        return sorted(stable)
    current = latest.get("comparison")
    if stable_fields(comparison) != stable_fields(current):
        raise SafetyStop("认领前后 SA 或论文信息发生变化，停止自动批注。")
    # Re-read once more inside complete_claim, immediately before submitting.
    return complete_claim(roster, record, bridge, row, current, reviewed=True, note=note,current_match=current_match)


def complete_claim(roster, record, bridge, expected, comparison, reviewed=False, note=None,current_match=False):
    if record.owner != "谭勋策" or record not in roster.records or record.done:
        raise SafetyStop("认领结案只允许谭勋策名单中的未完成记录。")
    if reviewed is not True:
        raise SafetyStop("请先核对当前记录、认领结果和处理备注。")
    context=current_claim_record(record,expected,current_match)
    if context.matches != 1 or not isinstance(expected, dict) or expected.get("saLzkId") != record.sa_id:
        raise SafetyStop("本按钮仅处理单匹配的认领结案，请重新定位。")
    expected_note = claim_completion_note(context, comparison, claimed=True)
    note = expected_note if note is None else note
    if note != expected_note:
        raise SafetyStop("认领结案备注与当前差异证据不一致。")
    validate_note(note, comparison, context.reason, context.matches)
    roster.assert_unchanged()
    # Search reads the current status first and can safely reconcile the owned
    # read-only detail. Never switch an already-processed row back to pending.
    latest = bridge.call("search", {"sa_id": record.sa_id})
    row = latest.get("row")
    if isinstance(row, dict) and row.get("markStatus") == "已处理" and row.get("remark") != note:
        raise SafetyStop("记录已被处理，但备注与本次认领证据不一致；Excel 未同步，请人工核验。")
    completion = reconcile_processed(roster, record, row)
    if completion:
        return ClaimCompletion(completion, row, True)
    if row != expected or latest.get("comparison") != comparison:
        raise SafetyStop("网页在核对后发生变化，请重新定位并核对，未提交结案。")
    if str(row.get("matchCount")) != "1" or row.get("reason") != context.reason:
        raise SafetyStop("后台匹配数或待处理原因与名单不一致，不能认领结案。")
    if row.get("remark", "") not in ("", note):
        raise SafetyStop("后台已有其他备注，保留原文，请人工核对后结案。")
    fields = [field for field in comparison or [] if field.get("label") == "认领状态"]
    if len(fields) != 1 or fields[0].get("library", "").strip() != "已认领":
        raise SafetyStop("尚未回读到已认领，不能保存完成状态。")
    _, staff_id = sa_claim_source(comparison)
    if not record.staff_id or staff_id != record.staff_id or row.get("gh") != staff_id:
        raise SafetyStop("名单、SA 和后台人员编号不一致，不能结案。")
    roster.assert_unchanged()
    result = bridge.call("complete", {"sa_id": record.sa_id, "expected": row,
        "expected_comparison": comparison, "reviewed": True, "note": note})
    verified = result.get("row", {})
    if (result.get("verified") is not True or verified.get("saLzkId") != record.sa_id or
            verified.get("markStatus") != "已处理" or verified.get("remark") != note):
        raise SafetyStop("网页结案结果未完整核验，Excel 未修改。禁止重复提交，请先核验后台。")
    try:
        completion = reconcile_processed(roster, record, verified)
    except Exception as exc:
        raise SafetyStop("网页已保存为已处理，但 Excel 同步失败。不要重复结案；重读名单并预检可同步。" + str(exc)) from exc
    return ClaimCompletion(completion, verified, False)
