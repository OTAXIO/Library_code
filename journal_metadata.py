"""Publisher journal identity + original author-affiliation evidence + Crossref.

The original OpenAlex response is never corrected, synthesized or relabelled.
Only the two observed publisher layouts below are supported, fail closed.
"""
from datetime import date
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import xml.etree.ElementTree as ET

from core import SafetyStop, digest, file_hash
from metadata_identity import MetadataSkip, doi, name_tokens, norm, text, title_key


class Page(HTMLParser):
    VOID = {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}

    def __init__(self, raw):
        super().__init__(convert_charrefs=True)
        self.root=ET.Element('page'); self.stack=[self.root]; self.count=0
        self.feed(raw)

    def handle_starttag(self, tag, attrs):
        self.count+=1
        if self.count>50000 or len(self.stack)>150:
            raise SafetyStop('出版社页面结构超过核验范围。')
        child=ET.SubElement(self.stack[-1],tag,{k:v or '' for k,v in attrs})
        if tag not in self.VOID:self.stack.append(child)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag,attrs)
        if tag not in self.VOID:self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack)-1,0,-1):
            if self.stack[index].tag==tag:
                del self.stack[index:];break

    def handle_data(self, data):
        parent=self.stack[-1]
        if len(parent):parent[-1].tail=(parent[-1].tail or '')+data
        else:parent.text=(parent.text or '')+data

    def has_class(self, name):
        return [node for node in self.root.iter() if name in node.get('class','').split()]


def content(node):
    return text(''.join(node.itertext()))


def unique(values, label):
    if len(values)!=1:raise SafetyStop('出版社正文未唯一对应：'+label)
    return values[0]


def publisher_authors(raw, citation, source):
    """Read actual printed affiliation IDs, not normalized institution guesses."""
    page=Page(raw)
    names=citation.get('citation_author',[])
    if not isinstance(names,list) or not 1<=len(names)<=100:
        raise SafetyStop('出版社作者列表缺失。')
    addresses=[]; authors=[]; mappings=[]
    if source=='Nature出版社':
        group=unique(page.has_class('c-article-author-affiliation__list'),'单位列表')
        for number,item in enumerate(group,1):
            if item.tag!='li' or item.get('id')!='Aff'+str(number):
                raise SafetyStop('Nature 正文单位序号异常。')
            address=unique([n for n in item if 'c-article-author-affiliation__address' in n.get('class','').split()],'单位正文')
            addresses.append(content(address))
        rows=page.has_class('c-article-authors-search__title')
        if len(rows)!=len(names):raise SafetyStop('Nature 作者正文不完整。')
        parents={child:parent for parent in page.root.iter() for child in parent}
        for deposited,row in zip(names,rows):
            printed=content(row).split('(')[0].strip()
            if name_tokens(printed)!=name_tokens(deposited):raise MetadataSkip('姓名冲突')
            parent=parents[row]
            indices=[int(n) for n in re.findall(r'-Aff([1-9])(?=-|$)',parent.get('id',''))]
            if parent.tag!='li' or not parent.get('id','').startswith('auth-') or not indices:
                raise SafetyStop('Nature 作者正文单位映射不明确。')
            authors.append(printed);mappings.append(indices)
    elif source=='SciOpen出版社':
        for row in page.has_class('art-authors-unit-item'):
            supers=[n for n in row.iter() if n.tag in ('label','sup') and re.fullmatch('[1-9]',content(n))]
            if not supers:continue  # Equal-contribution note is not an address.
            number=int(content(unique(supers,'单位序号')))
            value=content(row)
            if number!=len(addresses)+1 or not value.startswith(str(number)):
                raise SafetyStop('SciOpen 正文单位序号不连续。')
            addresses.append(value[1:].strip())
        rows=[r for r in page.has_class('art-authors') if any(n.tag=='sup' for n in r)]
        if len(rows)!=len(names):raise SafetyStop('SciOpen 作者正文不完整。')
        for deposited,row in zip(names,rows):
            first=next(iter(row),None)
            if first is None:raise SafetyStop('SciOpen 作者正文缺失。')
            printed=content(first)
            if name_tokens(printed)!=name_tokens(deposited):raise MetadataSkip('姓名冲突')
            supers=unique([n for n in row if n.tag=='sup'],'作者上标')
            suffix=content(supers)
            if not re.fullmatch(r'[1-9,§\s]+',suffix):raise SafetyStop('SciOpen 作者上标不明确。')
            indices=[int(n) for n in re.findall('[1-9]',suffix)]
            authors.append(printed);mappings.append(indices)
    else:raise SafetyStop('出版社正文格式尚未支持。')
    if not 1<=len(addresses)<=9 or any(not a or re.search(r'[;\r\n]',a) for a in addresses):
        raise SafetyStop('出版社正文单位信息异常。')
    if any(not ids or ids!=sorted(set(ids)) or max(ids)>len(addresses) for ids in mappings):
        raise SafetyStop('出版社作者单位对应异常。')
    return authors,addresses,mappings


def rebuild_journal(record, folder, evidence):
    from primary_metadata import CitationParser, trusted_page, compact, SJTU, KIND
    if (type(evidence.get('version')) is not int or evidence['version']!=2 or evidence.get('reviewed') is not True
            or evidence.get('evidence_type')!='publisher-journal-v1'):
        raise SafetyStop('出版社期刊资料未完成逐字段核验。')
    documents=evidence.get('documents')
    if not isinstance(documents,list) or not 2<=len(documents)<=3:raise SafetyStop('出版社期刊原始资料不足。')
    expected=['source.html','crossref.json']+(['source.pdf'] if len(documents)==3 else [])
    raw={};urls={}
    for doc,name in zip(documents,expected):
        if not isinstance(doc,dict) or doc.get('path')!=name or not isinstance(doc.get('sha256'),str):
            raise SafetyStop('出版社期刊原件描述异常。')
        path=Path(folder)/name
        if (Path(folder).is_symlink() or path.is_symlink() or not path.is_file()
                or not 0<path.stat().st_size<=(20_000_000 if name.endswith('pdf') else 2_000_000)
                or file_hash(path)!=doc['sha256']):raise SafetyStop('出版社期刊原件或哈希改变，未上传。')
        raw[name]=path.read_bytes();urls[name]=doc.get('url')
    source=trusted_page(urls['source.html'])
    if source not in ('Nature出版社','SciOpen出版社'):raise SafetyStop('期刊来源与模型不一致。')
    parser=CitationParser();html=raw['source.html'].decode('utf-8');parser.feed(html);citation=parser.values
    def one(key):return text(unique(citation.get(key,[]),key))
    title=one('citation_title');identifier=doi(one('citation_doi'));journal=one('citation_journal_title')
    if title_key(title)!=title_key(record.title) or record.doi and doi(record.doi)!=identifier or record.wos:
        raise MetadataSkip('出版社题名或标识待核验')
    if not re.fullmatch(r'10\.\d{4,9}/\S+',identifier):raise SafetyStop('出版社 DOI 不完整。')
    if (source=='Nature出版社' and (not identifier.startswith('10.1038/')
            or urls['source.html']!='https://www.nature.com/articles/'+identifier[8:])
            or source=='SciOpen出版社' and (not identifier.startswith('10.26599/')
            or urls['source.html'].lower()!='https://www.sciopen.com/article/'+identifier)):
        raise SafetyStop('出版社规范网址与 DOI 不一致。')
    from urllib.parse import quote
    if urls['crossref.json']!='https://api.crossref.org/works/'+quote(identifier,safe=''):
        raise SafetyStop('Crossref 证据未绑定真实 DOI。')
    deposited=json.loads(raw['crossref.json'])
    if (not isinstance(deposited,dict) or deposited.get('status')!='ok' or not isinstance(deposited.get('message'),dict)):
        raise SafetyStop('Crossref 原始回复结构异常。')
    cr=deposited['message']
    if (doi(cr.get('DOI'))!=identifier or cr.get('type')!='journal-article' or cr.get('container-title')!=[journal]
            or not isinstance(cr.get('title'),list) or len(cr['title'])!=1 or title_key(cr['title'][0])!=title_key(title)):
        raise MetadataSkip('书目信息冲突')
    authors,addresses,mappings=publisher_authors(html,citation,source)
    byline=cr.get('author')
    if (not isinstance(byline,list) or len(byline)!=len(authors) or any(not isinstance(a,dict) or
            name_tokens(text(a.get('given'))+' '+text(a.get('family')))!=name_tokens(name) for a,name in zip(byline,authors))):
        raise MetadataSkip('作者顺序冲突')
    if len(set(map(norm,authors)))!=len(authors) or any(re.search(r'[;()\r\n]',name) for name in authors):
        raise MetadataSkip('出版社作者身份待核验')
    if not any(SJTU.search(a) for a in addresses):raise MetadataSkip('未确认交大署名')
    issued_parts=[int(p) for p in one('citation_online_date').split('/')]
    try:
        issued=date(*issued_parts)
        if issued>date.today():raise ValueError()
    except (ValueError,TypeError):raise SafetyStop('出版社在线发表日期不明确。') from None
    if cr.get('published-online') and cr['published-online'].get('date-parts')!=[issued_parts]:
        raise MetadataSkip('发表日期冲突')
    if cr.get('published-print'):
        issue_parts=[int(p) for p in one('citation_publication_date').split('/')]
        declared=cr['published-print'].get('date-parts')
        if not isinstance(declared,list) or len(declared)!=1 or issue_parts[:len(declared[0])]!=declared[0]:
            raise SafetyStop('出版社与 Crossref 正式刊期不一致。')
    first=one('citation_firstpage');last=citation.get('citation_lastpage',[])
    pages=first+('-'+text(unique(last,'末页')) if last else '')
    if cr.get('page')!=pages or text(cr.get('volume'))!=one('citation_volume') or text(cr.get('issue'))!=one('citation_issue'):
        raise MetadataSkip('卷期页码冲突')
    issn=one('citation_issn')
    if not re.fullmatch(r'\d{4}-\d{3}[\dX]',issn) or not isinstance(cr.get('ISSN'),list) or issn not in cr['ISSN']:
        raise MetadataSkip('ISSN冲突')
    if source=='SciOpen出版社':
        if 'source.pdf' not in raw or urls['source.pdf']!=one('citation_pdf_url'):
            raise SafetyStop('SciOpen 缺官方页面链接的正式 PDF。')
        if urls['source.pdf'].lower()!='https://www.sciopen.com/local/article_pdf/'+identifier+'.pdf':
            raise SafetyStop('SciOpen 正式 PDF 网址与 DOI 不一致。')
        from pypdf import PdfReader
        pdf=compact(PdfReader(Path(folder)/'source.pdf').pages[0].extract_text())
        if compact(title) not in pdf or compact(identifier) not in pdf:
            raise SafetyStop('SciOpen 正式 PDF 题名或 DOI 不一致。')
        for name,ids in zip(authors,mappings):
            byline=compact(name+','.join(map(str,ids)))
            if byline not in pdf:raise SafetyStop('SciOpen 正式 PDF 作者及上标不一致。')
        for number,address in enumerate(addresses,1):
            if compact(str(number)+address) not in pdf:raise SafetyStop('SciOpen 正式 PDF 单位不一致。')
        months=('January','February','March','April','May','June','July','August','September','October','November','December')
        if compact(f'Published date: {issued.day} {months[issued.month-1]} {issued.year}') not in pdf:
            raise SafetyStop('SciOpen 正式 PDF 发表日期不一致。')
    fields={'题名':title,'作者':';'.join(f"{name}({','.join(map(str,ids))})" for name,ids in zip(authors,mappings)),
            '作者单位':';'.join(f'({i}){a}' for i,a in enumerate(addresses,1)),'发表日期':issued.isoformat(),
            '年份':str(issued.year),'发表期刊':journal,'DOI':identifier,'URL':urls['source.html'],
            '页码':pages,'卷号':one('citation_volume'),'期号':one('citation_issue'),'ISSN':one('citation_issn')}
    candidate={'kind':KIND,'source':source,'primary_url':urls['source.html'],'primary_evidence_sha256':digest(evidence),
               'title':title,'doi':identifier,'wos':'','document_type':'journal-article','model_name':'期刊论文','sjtu':True,
               'publication_status':'published-online','publication_date_basis':'publisher-online',
               'authors':';'.join(authors),'author_names':authors,'addresses':addresses,'author_mapping':mappings,
               'affiliation':';'.join(addresses),'fields':fields}
    candidate['sha256']=digest(candidate)
    return candidate
