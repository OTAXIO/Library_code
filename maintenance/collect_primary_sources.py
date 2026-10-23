"""Archive public proceedings pages/PDFs for local bibliographic review only.

This never reads credentials, modifies a roster, or sends an import command.
URLs come from a reviewed local manifest; only named scholarly hosts are read.
"""
import argparse
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

HOSTS = {'proceedings.iclr.cc', 'proceedings.mlr.press', 'openreview.net',
         'raw.githubusercontent.com', 'www.nature.com', 'www.sciopen.com', 'arxiv.org'}


def allowed(url):
    parsed = urlparse(url)
    if parsed.scheme != 'https' or parsed.hostname not in HOSTS or parsed.username or parsed.password:
        raise ValueError('Unapproved public source URL')
    if parsed.hostname == 'raw.githubusercontent.com' and not parsed.path.startswith('/mlresearch/'):
        raise ValueError('Only publisher-linked mlresearch PDFs are allowed')
    return url


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        allowed(new_url)
        return super().redirect_request(request, fp, code, message, headers, new_url)


class Citations(HTMLParser):
    def __init__(self):
        super().__init__(); self.values = {}

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        name = fields.get('name', '')
        if tag == 'meta' and name.startswith('citation_') and 'content' in fields:
            self.values.setdefault(name, []).append(fields['content'])


def collect(item, root):
    slug = item['slug']
    if not re.fullmatch(r'[a-z0-9-]{1,60}', slug):
        raise ValueError('Invalid archive name')
    folder = root / slug
    folder.mkdir(parents=True, exist_ok=True)
    output = []
    for key in ('page', 'pdf'):
        if key not in item:
            continue
        url = allowed(item[key]); path = folder / ('source.html' if key == 'page' else 'source.pdf')
        if path.exists():
            # Existing evidence is retained, never silently refreshed in place.
            data = path.read_bytes(); fetched = None
        else:
            request = Request(url, headers={'User-Agent': 'SA-Library-Assistant/0.4.36 (public source verification)'})
            with build_opener(SafeRedirect()).open(request, timeout=25) as response:
                allowed(response.url)
                data = response.read(20_000_001)
            if len(data) > 20_000_000:
                raise ValueError('Source exceeds 20 MB')
            if key == 'pdf' and not data.startswith(b'%PDF-'):
                raise ValueError('Publisher did not return a PDF')
            with path.open('xb') as stream:
                stream.write(data)
            fetched = datetime.now(timezone.utc).isoformat()
        entry = {'url': url, 'path': path.name, 'sha256': hashlib.sha256(data).hexdigest()}
        if fetched:
            entry['fetched_utc'] = fetched
        if key == 'page':
            parser = Citations(); parser.feed(data.decode('utf-8'))
            entry['citations'] = parser.values
        else:
            from pypdf import PdfReader
            reader = PdfReader(path)
            entry['pages'] = len(reader.pages)
            entry['first_page_text'] = reader.pages[0].extract_text()
        output.append(entry)
    report = {'title': item['title'], 'documents': output}
    (folder / 'review.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'slug': slug, 'documents': len(output), 'archive': str(folder)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    for item in json.loads(args.manifest.read_text(encoding='utf-8')):
        try:
            print(json.dumps(collect(item, args.output), ensure_ascii=False), flush=True)
        except Exception as error:
            print(json.dumps({'slug': item['slug'], 'error': str(error)}, ensure_ascii=False), flush=True)
