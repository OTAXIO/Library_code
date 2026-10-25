"""Formal ACL proceedings: retain the printed byline, not index sort order.

Reviewed facts are checked against immutable publisher HTML and PDF on every
resume. A missing coauthor affiliation stays empty; it is never guessed.
"""
from datetime import date
from pathlib import Path
import re

from core import SafetyStop, digest, file_hash
from metadata_identity import MetadataSkip, doi, name_tokens, text, title_key


def rebuild_acl(record, folder, evidence):
    from primary_metadata import CitationParser, compact, SJTU, KIND, trusted_page
    if (evidence.get('version') != 2 or type(evidence.get('version')) is not int or
            evidence.get('reviewed') is not True or evidence.get('evidence_type') != 'publisher-acl-v1'):
        raise SafetyStop('ACL 正式资料未完成核验。')
    documents=evidence.get('documents');raw={}
    if not isinstance(documents,list) or len(documents)!=2:
        raise SafetyStop('ACL 缺少正式页面或 PDF。')
    for doc,name in zip(documents,('source.html','source.pdf')):
        path=Path(folder)/name
        if (doc.get('path')!=name or Path(folder).is_symlink() or path.is_symlink() or not path.is_file()
                or not 0<path.stat().st_size<=(2_000_000 if name.endswith('html') else 20_000_000)
                or file_hash(path)!=doc.get('sha256')):
            raise SafetyStop('ACL 原件缺失或哈希改变。')
        raw[name]=path.read_bytes()
    url=documents[0].get('url');trusted_page(url)
    if not re.fullmatch(r'https://aclanthology\.org/\d{4}\.[a-z0-9-]+\.\d+/',url):
        raise SafetyStop('ACL 正式页面网址未确认。')
    p=CitationParser();p.feed(raw['source.html'].decode('utf8'));m=p.values
    def one(key):
        values=m.get(key,[])
        if len(values)!=1 or not text(values[0]):raise SafetyStop('ACL 字段未唯一对应：'+key)
        return text(values[0])
    title=one('citation_title');identifier=doi(one('citation_doi'));pdf_url=one('citation_pdf_url')
    if (title_key(title)!=title_key(record.title) or record.wos or
            record.doi and doi(record.doi)!=identifier):raise MetadataSkip('ACL 题名或标识待核验')
    if identifier!='10.18653/v1/'+url.removeprefix('https://aclanthology.org/').rstrip('/'):
        raise SafetyStop('ACL DOI 与正式网址不一致。')
    if documents[1].get('url')!=pdf_url or pdf_url!=url.rstrip('/')+'.pdf':
        raise SafetyStop('ACL PDF 不是正式单篇链接。')
    venue=one('citation_conference_title')
    if 'Association for Computational Linguistics' not in venue:
        raise SafetyStop('ACL 未确认为正式论文集。')
    parts=one('citation_publication_date').split('/')
    if len(parts) not in (2,3) or any(not v.isdigit() for v in parts):raise SafetyStop('ACL 发表日期不明确。')
    issued=[int(v) for v in parts]
    try:
        if date(*(issued+[1]*(3-len(issued))))>date.today():raise ValueError()
    except ValueError:raise MetadataSkip('ACL 发表日期待核验') from None
    from pypdf import PdfReader
    original=PdfReader(Path(folder)/'source.pdf').pages[0].extract_text()
    prefix=original.split('Abstract',1)[0];byline=compact(prefix)
    if (len(prefix)==len(original) or compact(title) not in byline or
            str(issued[0]) not in original or 'Association for Computational Linguistics' not in original):
        raise SafetyStop('ACL PDF 题名、正式会议或年份不一致。')
    facts=evidence.get('facts',{});authors=facts.get('authors');addresses=facts.get('affiliations')
    if (not isinstance(authors,list) or not 1<=len(authors)<=100 or not isinstance(addresses,list)
            or not 1<=len(addresses)<=9):raise SafetyStop('ACL 作者或单位资料不完整。')
    deposited=m.get('citation_author',[])
    names=[a.get('name') for a in authors]
    if (any(not isinstance(n,str) or not n or re.search(r'[;()\r\n]',n) for n in names) or
            len({tuple(name_tokens(n)) for n in names})!=len(names) or
            sorted(map(name_tokens,names))!=sorted(map(name_tokens,deposited))):
        raise MetadataSkip('ACL 作者身份冲突')
    affiliation_start=byline.find(compact('1'+addresses[0]))
    if affiliation_start<0:raise SafetyStop('ACL 缺少印刷单位起点。')
    author_block=byline[:affiliation_start]
    mappings=[];correspondents=[];position=-1
    role_markers=set(re.findall(r'([*∗†])\s*Corresponding authors?\b',original,re.I))
    for author in authors:
        printed=compact(author.get('byline',''));name=author['name'];bare=compact(name)
        at=author_block.find(printed)
        if (not printed.startswith(bare) or len(printed)<len(bare) or at<=position or
                author_block.count(bare)!=1 or author_block.count(printed)!=1):
            raise SafetyStop('ACL 正式 PDF 作者顺序或上标不一致。')
        position=at
        suffix=printed[len(bare):]
        if not re.fullmatch(r'[1-9,*∗⋄†]*(?:/letter)?',suffix):
            raise SafetyStop('ACL 作者上标含未确认内容。')
        ids=sorted({int(v) for v in re.findall('[1-9]',suffix)})
        if ids!=author.get('affiliations') or any(v>len(addresses) for v in ids):
            raise SafetyStop('ACL 逐作者单位映射不一致。')
        mappings.append(ids)
        if '/letter' in suffix or role_markers.intersection(suffix):correspondents.append(name)
    for number,address in enumerate(addresses,1):
        if (not isinstance(address,str) or not address or re.search(r'[;\r\n]',address) or
                compact(str(number)+address) not in byline):raise SafetyStop('ACL 单位原文不一致。')
    if not any(SJTU.search(a) for a in addresses):raise MetadataSkip('未确认交大署名')
    first,last=one('citation_firstpage'),one('citation_lastpage')
    if not first.isdigit() or not last.isdigit() or int(last)<int(first) or first not in original:
        raise SafetyStop('ACL 正式页码未确认。')
    fields={'题名':title,'作者':';'.join(n+('('+','.join(map(str,ids))+')' if ids else '') for n,ids in zip(names,mappings)),
        '作者单位':';'.join(f'({i}){a}' for i,a in enumerate(addresses,1)),
        '发表日期':'-'.join([str(issued[0]),*[f'{v:02d}' for v in issued[1:]]]),'年份':str(issued[0]),
        'DOI':identifier,'会议名称':venue,'出处':venue,'会议录名称':venue,'相关网址':url,'页码':first+'-'+last}
    candidate={'kind':KIND,'source':'ACL官方论文集','primary_url':url,'primary_evidence_sha256':digest(evidence),
        'title':title,'doi':identifier,'wos':'','document_type':'conference-paper','model_name':'会议论文','sjtu':True,
        'publication_status':'published-proceedings','authors':';'.join(names),'author_names':names,
        'addresses':addresses,'author_mapping':mappings,'affiliation':';'.join(addresses),'fields':fields,
        'correspondent_names':correspondents,'publisher_index_order_differs':list(map(name_tokens,names))!=list(map(name_tokens,deposited))}
    candidate['sha256']=digest(candidate)
    return candidate
