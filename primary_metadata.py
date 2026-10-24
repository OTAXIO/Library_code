"""Reviewed publisher records backed by original HTML, PDFs and public APIs.

Public indexes can omit proceedings without DOIs. Such records use their real
publisher URL, never an invented DOI or a preprint's identifiers/authors.
"""
from datetime import date
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import shutil
from urllib.parse import urlparse

from core import SafetyStop, digest, file_hash
from metadata_identity import MetadataSkip, doi, name_tokens, norm, text, title_key

KIND = 'publisher-verified-v1'
SOURCES = {'proceedings.iclr.cc': 'ICLR官方论文集', 'proceedings.mlr.press': 'PMLR官方论文集'}
SOURCES.update({'www.nature.com':'Nature出版社','www.sciopen.com':'SciOpen出版社'})
SJTU = re.compile(r'\bShanghai\s+(?:Jiao\s*Tong|Jiaotong)\s+Univ(?:ersity)?\b|上海交通大学', re.I)


class CitationParser(HTMLParser):
    def __init__(self):
        super().__init__(); self.values = {}

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs); name = fields.get('name', '')
        if tag == 'meta' and name.startswith('citation_') and 'content' in fields:
            self.values.setdefault(name, []).append(fields['content'])


def trusted_page(url):
    if not isinstance(url, str):
        raise SafetyStop('官方论文集网址无效。')
    parsed = urlparse(url)
    if (parsed.scheme != 'https' or parsed.hostname not in SOURCES or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.query or parsed.fragment):
        raise SafetyStop('官方论文集网址未确认。')
    pattern = {'proceedings.iclr.cc':r'/paper_files/paper/\d{4}/hash/[a-f0-9]{32}-Abstract-Conference\.html',
               'proceedings.mlr.press':r'/v\d+/[a-z0-9]+\.html','www.nature.com':r'/articles/s\d+-\d{3}-\d+-[a-z0-9]+',
               'www.sciopen.com':r'/article/10\.26599/[A-Za-z0-9.()-]+'}[parsed.hostname]
    if not re.fullmatch(pattern, parsed.path):
        raise SafetyStop('不是已核验的官方单篇论文页。')
    return SOURCES[parsed.hostname]


def compact(value):
    if not isinstance(value, str):
        raise SafetyStop('官方原文证据必须是文本。')
    return re.sub(r'\s+', '', norm(re.sub(r'-\r?\n', '', value)))


def rebuild_primary(record, folder, evidence):
    """Reparse the immutable originals on every resume; evidence is not a flag."""
    if isinstance(evidence,dict) and evidence.get('evidence_type')=='publisher-journal-v1':
        from journal_metadata import rebuild_journal
        return rebuild_journal(record,folder,evidence)
    if (not isinstance(evidence, dict) or type(evidence.get('version')) is not int
            or evidence['version'] != 1 or evidence.get('reviewed') is not True):
        raise SafetyStop('官方来源尚未完成人工逐字段核验。')
    documents = evidence.get('documents')
    facts = evidence.get('facts')
    if not isinstance(documents, list) or len(documents) != 2 or not isinstance(facts, dict):
        raise SafetyStop('官方来源必须包含原始论文页及 PDF。')
    raw = {}
    for entry, name in zip(documents, ('source.html', 'source.pdf')):
        if (not isinstance(entry, dict) or entry.get('path') != name
                or not re.fullmatch(r'[a-f0-9]{64}', entry.get('sha256', ''))):
            raise SafetyStop('官方来源文件描述不完整。')
        path = Path(folder) / name
        if (Path(folder).is_symlink() or path.is_symlink() or not path.is_file()
                or not 0 < path.stat().st_size <= (2_000_000 if name.endswith('html') else 20_000_000)
                or file_hash(path) != entry['sha256']):
            raise SafetyStop('官方来源原件缺失或哈希改变；未上传或重传。')
        raw[name] = path.read_bytes()
    primary_url = documents[0].get('url'); source = trusted_page(primary_url)
    parser = CitationParser(); parser.feed(raw['source.html'].decode('utf-8'))
    citation = parser.values

    def one(key, optional=False):
        values = citation.get(key, [])
        if optional and not values:
            return ''
        if len(values) != 1 or not isinstance(values[0], str) or not text(values[0]):
            raise SafetyStop('官方页面书目字段不唯一：' + key)
        return text(values[0])

    title = one('citation_title')
    if not title_key(title) or title_key(title) != title_key(record.title):
        raise MetadataSkip('官方题名未精确匹配')
    identifier = doi(one('citation_doi', optional=True))
    if record.wos or record.doi and doi(record.doi) != identifier:
        raise MetadataSkip('官方标识待核验')
    pdf_url = one('citation_pdf_url')
    if documents[1].get('url') != pdf_url:
        raise SafetyStop('PDF 不是官方单篇页面实际链接的版本。')
    pdf_parts = urlparse(pdf_url)
    if (pdf_parts.scheme != 'https' or pdf_parts.username or pdf_parts.password or pdf_parts.query
            or pdf_parts.fragment or pdf_parts.hostname not in ('proceedings.iclr.cc', 'raw.githubusercontent.com')
            or pdf_parts.hostname == 'raw.githubusercontent.com' and not pdf_parts.path.startswith('/mlresearch/')):
        raise SafetyStop('论文 PDF 的官方发布位置未确认。')
    issued = one('citation_publication_date').replace('/', '-')
    try:
        published = date.fromisoformat(issued)
        if published > date.today():
            raise ValueError('future date')
    except ValueError:
        raise MetadataSkip('官方发表日期待核验') from None
    venue = one('citation_conference_title' if 'citation_conference_title' in citation else 'citation_journal_title')
    if not (('ICLR' in source and venue == 'International Conference on Learning Representations')
            or ('PMLR' in source and venue == 'International Conference on Machine Learning')):
        raise SafetyStop('尚未支持或确认的官方会议类型。')
    names = citation.get('citation_author')
    authors = facts.get('authors'); addresses = facts.get('affiliations')
    if (not isinstance(names, list) or not 1 <= len(names) <= 100 or not isinstance(authors, list)
            or len(authors) != len(names) or not isinstance(addresses, list) or not 1 <= len(addresses) <= 9):
        raise SafetyStop('官方作者或逐作者单位映射不完整。')
    from pypdf import PdfReader
    paper_text = PdfReader(Path(folder)/'source.pdf').pages[0].extract_text()
    original = compact(paper_text)
    if compact(title) not in original:
        raise SafetyStop('正式 PDF 首页题名未对应官方书目。')
    if ('ICLR' in source and f'iclr{published.year}' not in original or
            'PMLR' in source and f',{published.year}.' not in original):
        raise SafetyStop('PDF 未确认对应正式发表的会议及年份。')
    mappings, author_names = [], []
    for deposited, author in zip(names, authors):
        if not isinstance(author, dict) or not isinstance(author.get('name'), str):
            raise SafetyStop('官方作者字段异常。')
        name = text(author['name']); byline = author.get('byline', '')
        if (not name or name_tokens(deposited) != name_tokens(name) or re.search(r'[;()\r\n]', name)
                or not compact(byline).startswith(compact(name)) or compact(byline) not in original):
            raise SafetyStop('正式论文作者与官方书目未逐一对应。')
        suffix = compact(byline)[len(compact(name)):]
        if not re.fullmatch(r'[1-9,⋆*†∗]+', suffix):
            raise SafetyStop('作者原文单位序号不明确。')
        indices = sorted({int(number) for number in re.findall(r'[1-9]', suffix)})
        if indices != author.get('affiliations') or any(number > len(addresses) for number in indices):
            raise SafetyStop('作者单位映射未与原文上标一致。')
        mappings.append(indices); author_names.append(name)
    for number, address in enumerate(addresses, 1):
        if (not isinstance(address, str) or not text(address) or re.search(r'[;\r\n]', address)
                or compact(str(number) + address) not in original):
            raise SafetyStop('原文单位序号及完整署名未确认。')
    if not any(SJTU.search(address) for address in addresses):
        raise MetadataSkip('未确认交大署名')
    if len({norm(name) for name in author_names}) != len(author_names):
        raise MetadataSkip('同名作者待核验')
    fields = {'题名': title,
              '作者': ';'.join(f"{name}({','.join(map(str, units))})" for name, units in zip(author_names, mappings)),
              '作者单位': ';'.join(f'({i}){address}' for i, address in enumerate(addresses, 1)),
              '发表日期': issued, '年份': str(published.year), '会议名称': venue, '出处': venue,
              '相关网址': primary_url}
    if identifier:
        fields['DOI'] = identifier
    first, last = one('citation_firstpage', optional=True), one('citation_lastpage', optional=True)
    if first and last:
        fields['页码'] = first + '-' + last
    candidate = {'kind': KIND, 'source': source, 'primary_url': primary_url,
                 'primary_evidence_sha256': digest(evidence), 'title': title, 'doi': identifier, 'wos': '',
                 'document_type': 'conference-paper', 'model_name': '会议论文', 'sjtu': True,
                 'authors': ';'.join(author_names), 'author_names': author_names,
                 'addresses': addresses, 'author_mapping': mappings, 'affiliation': ';'.join(addresses), 'fields': fields}
    candidate['sha256'] = digest(candidate)
    return candidate


def prepare_primary(client, record):
    """Only explicitly reviewed evidence is a fallback; never fabricate a hit."""
    registry = client.root.parent / 'primary-evidence'
    key = hashlib.sha256(title_key(record.title).encode('utf-8')).hexdigest()
    source_folder = registry / key
    evidence_path = source_folder / 'evidence.json'
    if not evidence_path.exists():
        return None
    if (registry.is_symlink() or source_folder.is_symlink() or evidence_path.is_symlink()
            or not source_folder.resolve().is_relative_to(registry.resolve())):
        raise SafetyStop('官方来源目录包含链接，未读取。')
    evidence = json.loads(evidence_path.read_text(encoding='utf-8'))
    candidate = rebuild_primary(record, source_folder, evidence)
    folder = client.root / digest({'record': record.key, 'protocol': KIND})
    if folder.is_symlink() or not folder.resolve().is_relative_to(client.root.resolve()):
        raise SafetyStop('官方来源归档路径异常。')
    if (folder/'metadata.json').exists():
        return client.read_cache(folder, record)
    folder.mkdir(exist_ok=True)
    for name in (doc['path'] for doc in evidence['documents']):
        target = folder/name
        if target.is_symlink() or target.exists() and file_hash(target) != file_hash(source_folder/name):
            raise SafetyStop('同篇官方来源归档冲突，保留原件。')
        if not target.exists():
            with target.open('xb') as stream, (source_folder/name).open('rb') as original:
                shutil.copyfileobj(original, stream)
    client.publish_cache(folder, {'record_key': record.key, 'candidate': candidate, 'primary': evidence})
    return client.read_cache(folder, record)
