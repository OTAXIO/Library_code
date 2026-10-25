"""Positive correspondence evidence from a publisher, never from author order."""
from datetime import datetime, timezone
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.request import Request, HTTPRedirectHandler, build_opener

from core import SafetyStop, digest, file_hash
from metadata_identity import doi, title_key


class Citation(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True); self.meta = {}

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == 'meta' and values.get('name', '').startswith('citation_'):
            self.meta.setdefault(values['name'], []).append(values.get('content', ''))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def public_open(request, timeout):
    return build_opener(NoRedirect()).open(request, timeout=timeout)


def parse_correspondence(raw, url, candidate):
    """Only the observed OAE citation block; unsupported publishers stay review-only."""
    expected = 'https://www.oaepublish.com/articles/' + doi(candidate.get('doi')).removeprefix('10.20517/')
    if (not doi(candidate.get('doi')).startswith('10.20517/') or url != expected
            or not re.fullmatch(r'https://www\.oaepublish\.com/articles/[A-Za-z0-9.()-]+', url)):
        raise SafetyStop('出版社角色来源未适配，保留待核。')
    p = Citation(); p.feed(raw.decode('utf-8')); m = p.meta
    if (m.get('citation_doi') != [candidate['doi']]
            or len(m.get('citation_title', [])) != 1
            or title_key(m['citation_title'][0]) != title_key(candidate['title'])
            or m.get('citation_fulltext_html_url') != [url]
            or m.get('citation_author') != candidate['author_names']):
        raise SafetyStop('出版社角色页与归档 DOI、题名或完整作者顺序不一致。')
    notes = [v for v in m.get('citation_author_institution', []) if v.startswith('Correspondence to: ')]
    if len(notes) != 1:
        raise SafetyStop('出版社未提供唯一明确的通讯作者说明。')
    names = re.findall(r'(?:Prof\.|Dr\.)\s+([^,;]+),', notes[0])
    if (not names or len(set(names)) != len(names)
            or any(n not in candidate['author_names'] for n in names)):
        raise SafetyStop('出版社通讯作者姓名未唯一对应完整署名。')
    return {'protocol': 1, 'url': url, 'doi': candidate['doi'], 'title': candidate['title'],
            'candidate_sha256': candidate['sha256'], 'correspondent_names': names,
            'source_sha256': __import__('hashlib').sha256(raw).hexdigest()}


def correspondence_evidence(folder, candidate):
    """Archive one public GET, then reparse immutable evidence on every resume."""
    d = doi(candidate.get('doi'))
    if candidate.get('source')=='ACL官方论文集':
        from types import SimpleNamespace
        from primary_metadata import rebuild_primary
        folder=Path(folder)
        archive=folder/'metadata.json'
        if archive.is_symlink():raise SafetyStop('ACL 角色归档路径异常。')
        saved=json.loads(archive.read_text(encoding='utf8'))
        rebuilt=rebuild_primary(SimpleNamespace(title=candidate['title'],doi=d,wos=''),folder,saved['primary'])
        if rebuilt!=candidate or saved['candidate']!=candidate:
            raise SafetyStop('ACL 正式角色证据与当前论文不一致。')
        if not candidate.get('correspondent_names'):return None
        return {'protocol':2,'url':candidate['primary_url'],'doi':d,'title':candidate['title'],
            'candidate_sha256':candidate['sha256'],'correspondent_names':candidate['correspondent_names'],
            'source_sha256':file_hash(folder/'source.pdf'),'pdf_url':candidate['primary_url'].rstrip('/')+'.pdf'}
    if not d.startswith('10.20517/'):
        return None
    folder = Path(folder); path = folder/'publisher-correspondence.html'
    url = 'https://www.oaepublish.com/articles/' + d.removeprefix('10.20517/')
    if not re.fullmatch(r'https://www\.oaepublish\.com/articles/[A-Za-z0-9.()-]+', url):
        raise SafetyStop('出版社通讯作者网址格式未确认。')
    if not path.exists():
        # No cookies, login headers, proxy changes, redirects or TLS bypass.
        with public_open(Request(url, headers={'User-Agent': 'SA-Library-Assistant source verification'}), timeout=25) as response:
            raw = response.read(3_000_001)
            if response.url != url or response.status != 200 or len(raw) > 3_000_000:
                raise SafetyStop('出版社通讯作者原文获取未确认。')
        evidence = parse_correspondence(raw, url, candidate)
        with path.open('xb') as stream: stream.write(raw)
        (folder/'publisher-correspondence.json').write_text(json.dumps({**evidence,
            'retrieved_utc': datetime.now(timezone.utc).isoformat()}, ensure_ascii=False, indent=2), encoding='utf8')
    if path.is_symlink() or path.stat().st_size > 3_000_000:
        raise SafetyStop('出版社角色原文路径或大小异常。')
    evidence = parse_correspondence(path.read_bytes(), url, candidate)
    archived = json.loads((folder/'publisher-correspondence.json').read_text(encoding='utf8'))
    if any(archived.get(k) != v for k, v in evidence.items()) or file_hash(path) != evidence['source_sha256']:
        raise SafetyStop('出版社角色原文归档哈希不一致。')
    return evidence


def repair_correspondence(record, comparison, row, state, store, call):
    """Repair only a source-confirmed positive flag for an already exact claimed person."""
    roles = [v for v in comparison if v.get('label') == '作者信息']
    if (len(roles) != 1 or any(not isinstance(roles[0].get(side), str) for side in ('sa', 'library'))
            or re.findall(r'是否通讯作者\s*[：:]\s*([是否])(?=\s|$)', roles[0]['sa']) != ['是']
            or re.findall(r'是否通讯作者\s*[：:]\s*([是否])(?=\s|$)', roles[0]['library']) != ['否']):
        return False
    evidence = correspondence_evidence(Path(state['path']).parent, state['candidate'])
    if evidence is None:
        return False
    capabilities = call('status', {})
    if (type(capabilities.get('publisher_correspondent_protocol')) is not int
            or capabilities['publisher_correspondent_protocol'] not in (1,2) or
            evidence['protocol']==2 and capabilities['publisher_correspondent_protocol']!=2 or capabilities.get('row') != row):
        raise SafetyStop('请重载扩展 0.4.48 后核验通讯作者；未准备或提交保存意图。')
    inspected = call('inspect_claim', {'expected': row,
        'sa_text': next(v['sa'] for v in comparison if v['label'] == '认领状态'),
        'staff_id': record.staff_id, 'roster_staff_id': record.staff_id})
    prepared = inspected.get('prepared', {}); person = prepared.get('person', {})
    if (inspected.get('verified') is not True or inspected.get('claimed') is not True
            or inspected.get('staff_id') != record.staff_id or person.get('wno') != record.staff_id
            or inspected.get('scholar_id') != person.get('id')
            or inspected.get('author') not in evidence['correspondent_names']):
        return False
    payload = {'expected': row, 'staff_id': record.staff_id, 'person': person,
               'author': inspected['author'], 'order': inspected['order'], 'item_id': state['item_id'],
               'evidence': evidence, 'candidate': state['candidate']}
    previous = state.get('correspondence_repair')
    if previous:
        # Never replay an unknown role write. A read-only check reconciles it.
        if previous.get('evidence_sha256') != digest(payload):
            raise SafetyStop('通讯作者修正断点与当前证据不一致，未重提。')
        return False
    state['correspondence_repair'] = {'phase': 'intent', 'evidence_sha256': digest(payload), 'payload': payload}
    store.save(record, state)
    answer = call('repair_correspondent', payload)
    if (answer.get('verified') is not True or answer.get('staff_id') != record.staff_id
            or answer.get('item_id') != state['item_id'] or answer.get('scholar_id') != person['id']
            or answer.get('order') != inspected['order'] or answer.get('correspondent') is not True):
        raise SafetyStop('通讯作者保存尚未回读确认，不重复保存。')
    state['correspondence_repair']['phase'] = 'verified'; store.save(record, state)
    return True
