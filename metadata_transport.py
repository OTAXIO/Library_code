"""Public GET transport using Windows' native TLS trust stack when available.

No browser, cookies, login profile, curlrc, TLS bypass or request retries.
Only the two fixed public metadata origins are permitted.
"""
import io
import json
import os
from pathlib import Path
import subprocess
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit

from core import SafetyStop

MAX_BODY = 4_000_000
ALLOWED = ('api.crossref.org', 'api.openalex.org')


def windows_curl():
    if os.name != 'nt':
        return None
    path = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/curl.exe'
    return path if path.is_file() else None


class PublicResponse(io.BytesIO):
    def __init__(self, body, url):
        super().__init__(body)
        self.url = url


def curl_open(request, timeout=25):
    url = request.full_url
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.netloc not in ALLOWED or parsed.fragment
            or request.get_method() != 'GET' or request.data is not None):
        raise SafetyStop('原生 HTTPS 仅允许固定公开元数据接口的 GET。')
    executable = windows_curl()
    if executable is None:
        raise SafetyStop('Windows 原生 HTTPS 客户端不可用，未关闭证书校验或另找程序。')
    service = 'Crossref' if parsed.netloc == 'api.crossref.org' else 'OpenAlex'
    # --disable must be first: never load a local curlrc, credentials or custom
    # output/retry directives. No --location: a redirect is never followed.
    args = [str(executable), '--disable', '--silent', '--show-error', '--proto', '=https',
            '--connect-timeout', '10', '--max-time', str(timeout), '--max-filesize', str(MAX_BODY),
            '--header', 'Accept: application/json', '--user-agent',
            'SA-Library-Assistant/0.4.35 (metadata verification)',
            '--write-out', '\n%{json}\n%header{retry-after}', '--url', url]
    try:
        result = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                timeout=timeout + 3, check=False,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except subprocess.TimeoutExpired:
        raise URLError(TimeoutError()) from None
    except OSError:
        raise SafetyStop(service + ' 原生 HTTPS 无法启动，未自动换通道或重试。') from None
    if result.returncode:
        labels = {5: '代理解析失败', 6: '域名解析失败', 7: '连接失败', 28: '请求超时',
                  35: 'TLS 连接中断或握手失败', 60: 'TLS 证书校验失败',
                  63: '响应超过大小限制'}
        hint = labels.get(result.returncode, '连接或响应失败')
        raise SafetyStop(f'{service} 原生 HTTPS {hint}（curl {result.returncode}），未判为未收录。')
    try:
        if len(result.stdout) > MAX_BODY + 65_536:
            raise ValueError('oversized response')
        payload, _, retry_after = result.stdout.rpartition(b'\n')
        body, separator, raw_meta = payload.rpartition(b'\n')
        meta = json.loads(raw_meta)
        effective = urlsplit(meta['url_effective'])
        status = meta['http_code']
        if (not separator or len(body) > MAX_BODY or type(status) is not int or not 100 <= status <= 599
                or effective.scheme != 'https' or effective.netloc != parsed.netloc
                or meta.get('num_redirects') != 0):
            raise ValueError('unverified response metadata')
    except (ValueError, TypeError, KeyError):
        raise SafetyStop(service + ' 原生 HTTPS 回执未确认，未判为未查询到。') from None
    if status != 200:
        headers = {}
        if retry_after.isdigit() and len(retry_after) <= 6:
            headers['Retry-After'] = retry_after.decode('ascii')
        raise HTTPError(url, status, 'public API status', headers, None)
    return PublicResponse(body, meta['url_effective'])
