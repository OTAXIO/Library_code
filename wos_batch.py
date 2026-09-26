"""Batch WOS metadata export driven by the saved classification results.

This only downloads: it drives the user's own bound WOS tab through the existing
``wos_search``/``wos_export`` extension commands and copies the captured Full Record
TXT into the submission intake folder. It never uploads, imports or pushes, because
those write to the production library and stay single-record with human confirmation.
"""
from __future__ import annotations

import re
from pathlib import Path

from automation import ImportStore, WOSFlow
from core import SafetyStop

# A bound tab that has been switched away or logged out fails every record, so stop
# instead of walking the whole roster with a dead bridge.
MAX_CONSECUTIVE_FAILURES = 3


def wos_targets(roster, classification, papers):
    """Zero-match, unfinished records whose saved classification recommends WOS."""
    channel = {}
    for paper in papers:
        saved = classification.get(paper['id'])
        route = saved.get('import_route') if isinstance(saved, dict) else None
        value = route.get('recommended_channel') if isinstance(route, dict) else None
        for number in paper['rows']:
            channel[number] = value
    return [record for record in roster.records
            if record.matches == 0 and not record.done and channel.get(record.row) == 'WOS']


def safe_name(record, sha):
    base = re.sub(r'[^0-9A-Za-z_.-]', '_', str(record.sa_id))[:80] or 'record'
    return f'WOS-{base}-{sha[:8]}.txt'


def export(roster, classification, papers, bridge, store, inbox,
           stop=None, progress=lambda text: None, unchanged=lambda: None,
           audit=lambda action, result, sa_id: None):
    """Export each target's Full Record through the user's own logged-in WOS tab."""
    inbox = Path(inbox)
    inbox.mkdir(parents=True, exist_ok=True)
    flow = WOSFlow(bridge, store, unchanged=unchanged, progress=progress, audit=audit)
    targets = wos_targets(roster, classification, papers)
    exported, failed, unconfirmed = [], {}, []
    streak = 0
    for record in targets:
        if stop is not None and stop.is_set():
            progress(f'WOS 导出已暂停：已成功 {len(exported)} 条。')
            break
        progress(f'WOS 导出：已处理 {len(exported)}/{len(targets)}；原表第 {record.row} 行')
        try:
            # prepare() is idempotent: an already archived export is reused, not re-downloaded.
            state = flow.prepare(record)
            raw = store.bytes(state)
        except SafetyStop as exc:
            failed[record.sa_id] = {'row': record.row, 'error': str(exc)}
            streak += 1
            progress(f'第 {record.row} 行导出暂停：{exc}')
            if streak >= MAX_CONSECUTIVE_FAILURES:
                raise SafetyStop(
                    f'连续 {streak} 条 WOS 导出失败，已停止以免继续操作网页；'
                    f'已成功 {len(exported)} 条。请检查扩展里的 WOS 标签页绑定、登录状态和当前页面。')
            continue
        streak = 0
        sha = state['candidate']['sha256']
        if not state.get('identity_confirmed'):
            # The single-record flow asks a human here. The intake folder is adopted
            # automatically, so an unverified record must never be dropped into it.
            # The download stays in the archive for the automation page to confirm.
            unconfirmed.append({'sa_id': record.sa_id, 'row': record.row, 'title': record.title,
                                'doi': record.doi, 'archive': str(Path(store.root) / (sha + '.txt'))})
            progress(f'第 {record.row} 行已导出但身份未获强匹配，未放入待收目录，'
                     f'请在“自动化 / 认领”页人工核验。')
            continue
        target = inbox / safe_name(record, sha)
        target.write_bytes(raw)
        exported.append({'sa_id': record.sa_id, 'row': record.row, 'title': record.title,
                         'doi': record.doi, 'file': str(target), 'sha256': sha})
    return {'total': len(targets), 'exported': exported, 'failed': failed,
            'unconfirmed': unconfirmed, 'inbox': str(inbox),
            'stopped': bool(stop is not None and stop.is_set())}


def plan(document, classification_dir=None):
    """(classification, papers) for a roster document.

    Takes the papers document produced by ``read_papers``. Three roster shapes float
    around this codebase (a papers document, a ``core.Roster``, a zero-match queue) and
    only the document carries the fingerprint under the same key shape the saved
    results were written against, so the roster object is deliberately not accepted.
    """
    from submission_prepare import CLASSIFICATION_DIR, saved_classification
    classification = saved_classification(document['sha256'], classification_dir or CLASSIFICATION_DIR)
    if not classification:
        raise SafetyStop('没有与当前名单指纹匹配的分类结果，请先运行“开始 / 继续分类”。')
    return classification, document['papers']


def default_store():
    from paper_classify import BASE
    return ImportStore(BASE / 'runtime' / 'wos-imports')


def default_inbox():
    from paper_classify import BASE
    from submission_prepare import INBOX_NAME
    return BASE / 'runtime' / 'submission' / INBOX_NAME
