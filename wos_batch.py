"""Batch WOS metadata export.

This only downloads: it drives the user's own bound WOS tab through the existing
``wos_search``/``wos_export`` extension commands and copies the captured Full Record
TXT into the submission intake folder. It never uploads, imports or pushes, because
those write to the production library and stay single-record with human confirmation.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from automation import ImportStore, MAX_TXT, doi, wos, norm, parse_wos
from core import SafetyStop

# Outcomes that are about this one paper rather than about the session being broken.
# "WOS has no record" and "the result set is not a single record" are the expected,
# useful answers for a roster whose titles are often wrong -- treating them as
# failures would misrepresent the batch summary.
PER_RECORD_OUTCOMES = (
    '未找到记录',
    '不是可确认的唯一记录',
    '与名单冲突',
    '没有明确上海交通大学署名',
    '禁止导入',
)

# These bridge states mean no subsequent paper can be dispatched safely. Keep the
# timeout markers specific to the desktop/extension command channel: ordinary WOS
# page/search timeouts are per-record diagnostics and must be allowed to continue.
DISCONNECTED_OUTCOMES = (
    '浏览器未连接',
    '浏览器通信已断开',
    '扩展连接已断开',
    '命令超时（网页已接收命令但未返回结果）',
    '命令超时（扩展没有取得命令）',
    '上一条浏览器命令仍在执行',
    '已有命令正在执行',
)


def per_record_outcome(message):
    """True when the message describes this paper, not a broken session."""
    text = str(message)
    return any(marker in text for marker in PER_RECORD_OUTCOMES)


def disconnected_outcome(message):
    """True only for an unavailable desktop-to-extension command channel."""
    text = str(message)
    return any(marker in text for marker in DISCONNECTED_OUTCOMES)


def preflight(bridge):
    """Read extension capabilities before sending any Search or export click."""
    try:
        result=bridge.call('wos_diagnose',{},timeout=15)
    except SafetyStop as exc:
        if '未知 WOS 调度命令' in str(exc):
            raise SafetyStop('当前运行的插件仍是旧版本。请在 Edge 扩展管理页重载到 0.3.28 或更新版本，'
                             '刷新 WOS 页并重新连接；本轮未提交检索或下载。') from exc
        raise
    if (not isinstance(result,dict) or type(result.get('wos_download_protocol')) is not int
            or result.get('wos_download_protocol')!=1
            or result.get('result_reader')!='shared-diagnostic'
            or result.get('read_results_world')!='ISOLATED'
            or not re.fullmatch(r'\d+\.\d+\.\d+',str(result.get('extension_version','')))
            or tuple(map(int,result['extension_version'].split('.'))) < (0,3,28)):
        raise SafetyStop('插件下载接口不兼容或版本过旧。请重载 0.3.28 或更新版本、刷新 WOS 页并重新连接；'
                         '本轮未提交检索或下载。')
    return {'extension_version':result['extension_version']}


@dataclass
class DownloadRosterUpdate:
    roster: object
    source_count: int = 0
    skipped_count: int = 0
    source_error: str = ''
    workflow_error: str = ''


def persist_download_outcomes(roster, targets, result):
    """Persist only proven downloads and the exact attempted failures.

    Never mark a download complete or mark the unattempted tail as skipped.
    Each underlying writer backs up and verifies its own narrow XML transaction;
    a failed second transaction must not discard the first transaction's roster.
    """
    from roster_write import mark_skipped_many, record_data_sources
    targets = list(targets)
    roster.assert_unchanged()
    selected = {record.sa_id: record for record in targets}
    if (len(selected) != len(targets) or len({r.owner for r in targets}) > 1
            or any(r not in roster.records or r.done or r.matches != 0 for r in targets)):
        raise SafetyStop('下载回写范围与当前负责人名单不一致，未修改名单。')
    attempted = result.get('attempted')
    if type(attempted) is not int or not 0 <= attempted <= len(targets):
        raise SafetyStop('无法确认实际尝试条数，未修改名单。')
    seen, successful, reasons = set(), set(), {}
    outcomes = [('exported', entry.get('sa_id'), entry) for entry in result.get('exported', [])]
    outcomes += [('unconfirmed', entry.get('sa_id'), entry) for entry in result.get('unconfirmed', [])]
    outcomes += [('failed', sa_id, entry) for sa_id, entry in result.get('failed', {}).items()]
    attempted_ids = {record.sa_id for record in targets[:attempted]}
    for kind, sa_id, entry in outcomes:
        if (sa_id not in attempted_ids or sa_id in seen or entry.get('row') != selected[sa_id].row):
            raise SafetyStop('下载结果未能对应实际尝试的唯一名单行，未修改名单。')
        seen.add(sa_id)
        if kind == 'exported':
            successful.add(sa_id)
        elif kind == 'unconfirmed':
            reasons[sa_id] = 'WOS TXT 已下载，但文献身份待核验；未上传或导入，待人工核对。'
        else:
            error = entry.get('error')
            if not isinstance(error, str) or not error.strip():
                raise SafetyStop('失败原因缺失，未修改名单。')
            error = re.sub(r'\bsk-[A-Za-z0-9_-]{10,}\b|Bearer\s+[A-Za-z0-9._~-]+', '[已隐藏凭据]', error)
            error = re.sub(r'[\x00-\x1f]', ' ', error).strip()
            reasons[sa_id] = ('WOS 下载未完成：' + error)[:2000]
    if len(seen) != attempted:
        raise SafetyStop('下载结果不完整，未修改名单；请核对完整报告。')
    update = DownloadRosterUpdate(roster)
    if successful:
        # Duplicated SA rows share provenance only when ALL supplied paper facts
        # agree, including WOS ID. A different owner/identifier is never borrowed.
        keys = {(r.owner, r.title, r.doi, r.wos, r.skipped) for r in targets if r.sa_id in successful}
        rows = [r for r in roster.records if not r.done and r.matches == 0
                and (r.owner, r.title, r.doi, r.wos, r.skipped) in keys]
        try:
            saved = record_data_sources(update.roster, rows, 'WOS')
            update.roster, update.source_count = saved.roster, len(saved.rows)
        except (SafetyStop, OSError) as exc:
            update.source_error = str(exc)
    if reasons:
        try:
            if not update.roster.status_separate:
                raise SafetyStop('请重新读取名单，先建立末尾“是否识别”列；不在备注写入数字状态。')
            rows = [r for r in update.roster.records if r.sa_id in reasons]
            saved = mark_skipped_many(update.roster, rows, reasons=reasons)
            update.roster, update.skipped_count = saved.roster, len(rows)
        except (SafetyStop, OSError) as exc:
            update.workflow_error = str(exc)
    return update


def wos_targets(roster, classification, papers, owner=None):
    """Zero-match, unfinished records whose saved classification recommends WOS."""
    channel = {}
    for paper in papers:
        saved = classification.get(paper['id'])
        route = saved.get('import_route') if isinstance(saved, dict) else None
        value = route.get('recommended_channel') if isinstance(route, dict) else None
        for number in paper['rows']:
            channel[number] = value
    return [record for record in roster.records
            if record.matches == 0 and not record.done and not record.skipped
            and (owner is None or record.owner == owner) and channel.get(record.row) == 'WOS']


def roster_rows(roster, owner=None):
    """Eligible rows for one optional owner, before duplicate papers are merged."""
    return [record for record in roster.records if record.matches == 0 and not record.done
            and not record.skipped and (owner is None or record.owner == owner)]


def skipped_roster_rows(roster, owner=None):
    """Persistently skipped rows in one optional owner scope.

    This is deliberately separate from :func:`roster_rows`: the normal WOS queue must
    continue to exclude numeric-2 rows, while the dedicated retry entry may inspect
    them without clearing or otherwise changing their Excel workflow marker.
    """
    return [record for record in roster.records if record.matches == 0 and not record.done
            and record.skipped and (owner is None or record.owner == owner)]


def _one_per_paper(records):
    """Return the first row for each exact title+DOI paper key."""
    seen, targets = set(), []
    for record in records:
        key = (record.title, record.doi)
        if key in seen:
            continue
        seen.add(key)
        targets.append(record)
    return targets


def all_targets(roster, owner=None):
    """One record per paper, across every zero-match, unfinished paper.

    Two things are deliberate here:

    * The roster decides the scope, not the classification. The SA title is often the
      wrong one and WOS is exactly where the correct record is looked up, so limiting
      the run to records a model happened to label WOS would skip the ones that need it.
    * Rows are grouped by the same title+DOI key the intake uses, so a paper listed
      several times (each row carrying its own SA ID) produces a single TXT. The first
      row of the group represents the paper and supplies the SA ID in the filename.
    """
    return _one_per_paper(roster_rows(roster, owner))


def skipped_targets(roster, owner=None):
    """One record per zero-match paper whose Excel workflow marker is numeric 2."""
    return _one_per_paper(skipped_roster_rows(roster, owner))


def safe_name(record):
    """``<paper title>+<sa_lzk table ID>.txt``, sanitised and length-capped.

    The title is what a human reads when deciding which file to import, so it leads;
    the SA ID keeps two papers that share a title apart. Windows forbids \\ / : * ? " < >
    | in a name, and the whole path has to stay well inside MAX_PATH, so the title is
    truncated rather than the identifier.
    """
    title = re.sub(r'[\\/:*?"<>|\x00-\x1f]', ' ', str(record.title or ''))
    title = re.sub(r'\s+', ' ', title).strip(' .')
    ident = re.sub(r'[\\/:*?"<>|\x00-\x1f]', '_', str(record.sa_id or '')).strip() or 'record'
    room = 180 - len(ident) - len('+.txt')
    if room < 1:
        return f'{ident}.txt'
    title = title[:room].strip(' .')
    return f'{title}+{ident}.txt' if title else f'{ident}.txt'


class WOSDownload:
    """Search and download only. No SA status, affiliation or import transitions."""
    def __init__(self,bridge,store,unchanged,stop,audit):
        self.bridge,self.store,self.unchanged,self.stop,self.audit=bridge,store,unchanged,stop,audit

    def prepare(self,record,progress=lambda text: None):
        self.unchanged()
        cached=self.store.get(record)
        if cached:
            progress('复用已保存的 TXT；不重复检索或下载')
            return cached
        query={'sa_id':record.sa_id,'title':record.title,'doi':doi(record.doi),'wos':wos(record.wos)}
        result=None
        for action in ('wos_search','wos_export'):
            if self.stop is not None and self.stop.is_set():
                raise SafetyStop('已暂停下载。')
            self.unchanged()
            timeout=120 if action == 'wos_search' else 75
            progress('检索并核验唯一文献（本步最多 120 秒，不重复检索）' if action == 'wos_search'
                     else '导出完整记录并等待 TXT（本步最多 75 秒）')
            result=self.bridge.call(action,query,timeout=timeout)
            self.audit(action,'已执行',record.sa_id)
        progress('核验下载文件的题名、DOI 和 WOS 号')
        path=Path(result.get('path',''))
        if result.get('sa_id')!=record.sa_id or not path.is_absolute() or path.suffix.lower()!='.txt' or path.is_symlink():
            raise SafetyStop('无法确定本次导出的 TXT 文件。')
        if not path.is_file() or not 1<=path.stat().st_size<=MAX_TXT:
            raise SafetyStop('下载未完成或文件大小异常。')
        raw=path.read_bytes()
        candidate=parse_wos(raw)
        for name in ('doi','wos'):
            if query[name] and query[name]!=candidate[name]:
                raise SafetyStop(f'下载记录的 {name.upper()} 与名单冲突，未采纳。')
        confirmed=bool((query['doi'] or query['wos']) and norm(record.title)==norm(candidate['title']))
        self.store.archive(raw)
        state={'phase':'downloaded','candidate':candidate,'identity_confirmed':confirmed}
        self.store.save(record,state)
        return state


def export(targets, bridge, store, inbox,
           stop=None, progress=lambda text: None, unchanged=lambda: None,
           audit=lambda action, result, sa_id: None):
    """Export each target's Full Record through the user's own logged-in WOS tab.

    Takes the target list directly so the caller decides the scope; the WOS query field
    (WOS ID, else DOI, else title) is chosen by the extension per record.
    """
    targets = list(targets)
    inbox = Path(inbox)
    inbox.mkdir(parents=True, exist_ok=True)
    flow = WOSDownload(bridge, store, unchanged, stop, audit)
    exported, failed, unconfirmed = [], {}, []
    disconnected = False
    attempted = 0
    for index, record in enumerate(targets):
        if stop is not None and stop.is_set():
            progress(f'WOS 导出已暂停：已成功 {len(exported)} 条。')
            break
        prefix=f'WOS TXT · 第 {index+1}/{len(targets)} 篇 · 原表第 {record.row} 行'
        progress(prefix+' · 准备检索')
        attempted += 1
        try:
            # prepare() is idempotent: an already archived export is reused, not re-downloaded.
            state = flow.prepare(record,progress=lambda text: progress(prefix+' · '+text))
            raw = store.bytes(state)
        except SafetyStop as exc:
            message = str(exc)
            failed[record.sa_id] = {'row': record.row, 'error': message,
                                    'per_record': per_record_outcome(message)}
            if disconnected_outcome(message):
                disconnected = True
                progress(f'浏览器会话不可用，已停止整批；剩余 {len(targets)-attempted} 条未执行。')
                break
            progress(f'第 {record.row} 行未导出，已记录并继续下一条：{message}')
            continue
        sha = state['candidate']['sha256']
        if not state.get('identity_confirmed'):
            # The single-record flow asks a human here. The intake folder is adopted
            # automatically, so an unverified record must never be dropped into it.
            # The download stays in the archive for the automation page to confirm.
            unconfirmed.append({'sa_id': record.sa_id, 'row': record.row, 'title': record.title,
                                'doi': record.doi, 'archive': str(Path(store.root) / (sha + '.txt'))})
            progress(f'第 {record.row} 行已导出但身份未获强匹配，未放入待收目录，'
                     f'请核对存档文件后再用于提交准备。')
            continue
        target = inbox / safe_name(record)
        target.write_bytes(raw)
        exported.append({'sa_id': record.sa_id, 'row': record.row, 'title': record.title,
                         'doi': record.doi, 'file': str(target), 'sha256': sha})
        progress(prefix+f' · TXT 已保存；本轮已核验 {len(exported)} 篇')
    per_record = sum(1 for value in failed.values() if value['per_record'])
    return {'total': len(targets), 'exported': exported, 'failed': failed,
            'unconfirmed': unconfirmed, 'inbox': str(inbox),
            'not_exported': per_record, 'session_failures': len(failed) - per_record,
            'attempted': attempted, 'remaining': len(targets)-attempted,
            'disconnected': disconnected,
            'stopped': bool(disconnected or (stop is not None and stop.is_set()))}


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
    return ImportStore(BASE / 'runtime' / 'wos-downloads')


def default_inbox():
    from paper_classify import BASE
    from submission_prepare import INBOX_NAME
    return BASE / 'runtime' / 'submission' / INBOX_NAME
