"""No network: native public TLS, bounds, redaction and no replay."""
import json
import ssl
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from core import SafetyStop
from metadata_sources import MetadataClient
from metadata_transport import MAX_BODY, curl_open, PublicResponse

URL = 'https://api.openalex.org/works/https://doi.org/10.1234/test'


def response(body=b'{"id":"https://openalex.org/W123"}', status=200, url=URL, redirects=0, retry=b''):
    metadata = json.dumps({'url_effective': url, 'http_code': status, 'num_redirects': redirects}).encode()
    return subprocess.CompletedProcess([], 0, body + b'\n' + metadata + b'\n' + retry)


class NativeTransportTests(unittest.TestCase):
    def setUp(self):
        self.binary = patch('metadata_transport.windows_curl', return_value=Path('C:/Windows/System32/curl.exe'))
        self.binary.start(); self.addCleanup(self.binary.stop)

    def test_success_uses_fixed_binary_no_shell_no_curlrc_no_auth_or_retry(self):
        with patch('metadata_transport.subprocess.run', return_value=response()) as run:
            with curl_open(Request(URL)) as result:
                self.assertEqual(json.loads(result.read())['id'], 'https://openalex.org/W123')
        run.assert_called_once()
        args = run.call_args.args[0]
        self.assertEqual(Path(args[0]), Path('C:/Windows/System32/curl.exe'))
        self.assertEqual(args[1], '--disable')
        self.assertEqual(args[-2:], ['--url', URL])
        self.assertEqual(args[args.index('--proto')+1], '=https')
        self.assertEqual(args[args.index('--max-filesize')+1], str(MAX_BODY))
        self.assertFalse(run.call_args.kwargs.get('shell', False))
        self.assertEqual(run.call_args.kwargs['stderr'], subprocess.DEVNULL)
        self.assertTrue({'--insecure','--retry','--location','--cookie','--netrc'}.isdisjoint(args))

    def test_nonpublic_insecure_fragment_or_write_request_is_rejected_without_launch(self):
        requests = [Request(u) for u in ('http://api.openalex.org/works', 'https://other.invalid/works',
                    'https://user@api.openalex.org/works', URL+'#fragment')]
        requests.append(Request(URL, data=b'not-a-get'))
        with patch('metadata_transport.subprocess.run') as run:
            for req in requests:
                with self.subTest(url=req.full_url), self.assertRaises(SafetyStop): curl_open(req)
            run.assert_not_called()

    def test_only_404_becomes_source_absence(self):
        with tempfile.TemporaryDirectory() as temp:
            client = MetadataClient(temp, transport='windows-curl')
            with patch('metadata_transport.subprocess.run', return_value=response(status=404)) as run:
                self.assertIsNone(client.get(URL))
                run.assert_called_once()

    def test_http_errors_preserve_rate_wait_without_payload_or_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            client = MetadataClient(temp, transport='windows-curl')
            for status in (301, 401, 403, 429, 500):
                client.last_request = 0
                with self.subTest(status=status), patch('metadata_transport.subprocess.run',
                        return_value=response(b'SECRET_RESPONSE', status=status, retry=b'60')) as run:
                    with self.assertRaises(SafetyStop) as caught: client.get(URL)
                    self.assertIn('HTTP '+str(status), str(caught.exception))
                    self.assertNotIn('SECRET_RESPONSE', str(caught.exception))
                    if status == 429: self.assertIn('60 秒', str(caught.exception))
                    run.assert_called_once()

    def test_native_failures_never_retry_or_disable_tls(self):
        for code in (5, 6, 7, 28, 35, 60, 63, 99):
            with self.subTest(code=code), patch('metadata_transport.subprocess.run',
                    return_value=subprocess.CompletedProcess([], code, b'SECRET_ERROR')) as run:
                with self.assertRaises(SafetyStop) as caught: curl_open(Request(URL))
                self.assertIn('curl '+str(code), str(caught.exception))
                self.assertNotIn('SECRET_ERROR', str(caught.exception))
                run.assert_called_once()

    def test_timeout_is_one_request_not_fallback(self):
        with tempfile.TemporaryDirectory() as temp:
            client = MetadataClient(temp, transport='windows-curl')
            with patch('metadata_transport.subprocess.run', side_effect=subprocess.TimeoutExpired([], 28)) as run, \
                    patch('metadata_sources.urlopen') as fallback:
                with self.assertRaisesRegex(SafetyStop, 'OpenAlex 请求超时'): client.get(URL)
                run.assert_called_once(); fallback.assert_not_called()

    def test_invalid_receipts_cross_origin_and_oversize_stop(self):
        values = [response(url='https://other.invalid/works'), response(redirects=1), response(status=True),
                  response(body=b'x'*(MAX_BODY+1)), subprocess.CompletedProcess([], 0, b'not-a-receipt')]
        for value in values:
            with self.subTest(size=len(value.stdout)), patch('metadata_transport.subprocess.run', return_value=value):
                with self.assertRaises(SafetyStop): curl_open(Request(URL))

    def test_json_decode_and_string_envelope_still_use_common_validation(self):
        with tempfile.TemporaryDirectory() as temp:
            client = MetadataClient(temp, transport='windows-curl')
            for body in (b'{"title":"synthetic"}', b'"{\\"title\\":\\"synthetic\\"}"'):
                client.last_request = 0
                with patch('metadata_transport.subprocess.run', return_value=response(body=body)):
                    self.assertEqual(client.get(URL), {'title': 'synthetic'})
            with patch('metadata_transport.subprocess.run', return_value=response(body=b'not-json')):
                client.last_request = 0
                with self.assertRaisesRegex(SafetyStop, 'JSON'): client.get(URL)

    def test_missing_binary_or_os_failure_has_no_sensitive_message(self):
        with patch('metadata_transport.windows_curl', return_value=None), patch('metadata_transport.subprocess.run') as run:
            with self.assertRaisesRegex(SafetyStop, '不可用'): curl_open(Request(URL))
            run.assert_not_called()
        with patch('metadata_transport.subprocess.run', side_effect=OSError('SECRET_CONFIG')):
            with self.assertRaises(SafetyStop) as caught: curl_open(Request(URL))
            self.assertNotIn('SECRET_CONFIG', str(caught.exception))

    def test_auto_choice_does_not_remove_explicit_urllib_option(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch('metadata_sources.windows_curl', return_value=Path('C:/Windows/System32/curl.exe')):
                self.assertEqual(MetadataClient(temp).transport, 'urllib')
                self.assertEqual(MetadataClient(temp, transport='urllib').transport, 'urllib')
            with patch('metadata_sources.windows_curl', return_value=None):
                self.assertEqual(MetadataClient(temp).transport, 'urllib')
            with self.assertRaises(SafetyStop): MetadataClient(temp, transport='unknown')

    def test_auto_prefers_working_verified_python_tls(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch('metadata_sources.urlopen',return_value=PublicResponse(b'{"ok":true}',URL)) as py, \
                patch('metadata_sources.curl_open') as native:
            self.assertEqual(MetadataClient(temp).get(URL),{'ok':True})
            py.assert_called_once();native.assert_not_called()

    def test_auto_tls_fallback_is_once_and_never_for_http_timeout_or_explicit_transport(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch('metadata_sources.windows_curl',return_value=Path('curl.exe')), \
                patch('metadata_sources.urlopen',side_effect=URLError(ssl.SSLError('handshake'))), \
                patch('metadata_sources.curl_open',return_value=PublicResponse(b'{"ok":true}',URL)) as native:
            client=MetadataClient(temp);self.assertEqual(client.get(URL),{'ok':True})
            native.assert_called_once();self.assertEqual(client.transport,'windows-curl')
        failures=[HTTPError(URL,429,'limit',{},None),URLError(TimeoutError()),
                  URLError(OSError('connection'))]
        for transport in ('auto','urllib'):
            for failure in failures+([URLError(ssl.SSLError('TLS'))] if transport=='urllib' else []):
                with self.subTest(transport=transport,error=type(failure).__name__), \
                        tempfile.TemporaryDirectory() as temp, \
                        patch('metadata_sources.urlopen',side_effect=failure) as py, \
                        patch('metadata_sources.curl_open') as native:
                    with self.assertRaises(SafetyStop):MetadataClient(temp,transport=transport).get(URL)
                    py.assert_called_once();native.assert_not_called()


if __name__ == '__main__': unittest.main()
