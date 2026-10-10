"""Conservative WOS intake decisions based on the actual Full Record export.

No title, staff number, model suggestion or absent address proves affiliation.
Non-SJTU is a statement about the complete author-address evidence in this record,
not about an author's employment or whether a paper exists elsewhere.
"""
import re
import unicodedata

from core import SafetyStop


SJTU = re.compile(r"\bShanghai\s+(?:Jiao\s*Tong|Jiaotong)\s+Univ(?:ersity)?\b|上海交通大学", re.I)
AUTHOR_ISSUES = ('第一作者', '共同一作', '共同第一', '首位作者', '作者顺序',
                 '通讯作者', '通信作者', '第一单位', '第一署名单位')
POLICY_NOTES = ('非交大', '交大署名待核验', '涉及作者或第一单位判断，暂不处理')


class WOSPolicyStop(SafetyStop):
    def __init__(self, message, note):
        super().__init__(message)
        if note not in POLICY_NOTES:
            raise ValueError('Unknown intake note')
        self.note = note


def author_review_reason(*reasons):
    for reason in reasons:
        if isinstance(reason, str) and any(word in reason for word in AUTHOR_ISSUES):
            return '涉及作者或第一单位判断，暂不处理'
    return ''


def _name(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().strip(' ,.;').split())


def affiliation_status(candidate):
    """Return sjtu/non_sjtu/unknown, without trusting a cached boolean.

    A positive needs an explicit institution in C1. A negative additionally needs
    every exported author to be mapped to a nonempty, untruncated C1 address.
    Unbracketed, partial, absent or differently abbreviated mappings stay unknown.
    """
    affiliation = candidate.get('affiliation')
    authors = candidate.get('authors')
    if not isinstance(affiliation, str) or not affiliation.strip():
        return 'unknown'
    if SJTU.search(affiliation):
        return 'sjtu'
    if (not isinstance(authors, str) or not authors.strip()
            or re.search(r'\.\.\.|…|\bet\s+al\b|\b(?:unknown|unavailable|missing)\b',
                         affiliation + ';' + authors, re.I)):
        return 'unknown'
    # This is the Full Record C1 convention, not a guessed affiliation list.
    groups = list(re.finditer(r'\[([^\[\]]+)\]([^\[\]]+)', affiliation))
    if (not groups or affiliation[:groups[0].start()].strip(' ;')
            or any(not group[2].strip(' ;') for group in groups)):
        return 'unknown'
    if any(affiliation[left.end():right.start()].strip(' ;')
           for left, right in zip(groups, groups[1:])):
        return 'unknown'
    if affiliation[groups[-1].end():].strip(' ;'):
        return 'unknown'
    expected = {_name(name) for name in authors.split(';') if _name(name)}
    mapped = {_name(name) for group in groups for name in group[1].split(';') if _name(name)}
    if not expected or not expected.issubset(mapped):
        return 'unknown'
    # Empty/placeholder/geographical fragments are not complete institutions.
    institution = re.compile(r'\b(?:Univ(?:ersity)?|Inst(?:itute)?|Acad(?:emy)?|College|'
                             r'Hosp(?:ital)?|Lab(?:orator(?:y|ies))?|Cent(?:er|re)|'
                             r'Group|Corp(?:oration)?|Company)\b|大学|学院|研究所|医院|公司', re.I)
    if not all(institution.search(group[2]) for group in groups):
        return 'unknown'
    return 'non_sjtu'


def require_sjtu(candidate):
    status = affiliation_status(candidate)
    if status == 'non_sjtu':
        raise WOSPolicyStop('非交大：完整 WOS 记录已覆盖所有作者单位，未见交大署名；不上传或导入。', '非交大')
    if status != 'sjtu':
        raise WOSPolicyStop('WOS 完整记录没有明确上海交通大学署名，且单位证据不足；先核验，不自动判断为非交大。',
                            '交大署名待核验')


def require_no_author_review(*reasons):
    note = author_review_reason(*reasons)
    if note:
        raise WOSPolicyStop(note + '；不更改作者、认领或单位标记。', note)


def zero_result_note(message):
    """Only the extension's confirmed zero-result diagnostic gets wos未收录.

    A missing control/link, timeout, Oops or multiple results is not zero results.
    Both language UIs converge on this explicit diagnostic in the adapter.
    The user-selected label records this search outcome, not a universal assertion
    about coverage in the database or whether the paper has been published.
    """
    return 'wos未收录' if re.search(r'(?:^|\]\s*)WOS\s*未找到记录(?:[；;。]|$)', str(message)) else ''
