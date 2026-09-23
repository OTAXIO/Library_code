"""Upload code-only Git checkpoints through GitHub's API when Git transport fails.

Uses existing Git credentials in memory, only with the explicitly chosen repo.
Never prints credentials, force-pushes or uploads runtime / workbook files.
"""
import base64
import concurrent.futures
import datetime
import json
import re
import subprocess
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
API = "https://api.github.com/repos/OTAXIO/Library_code"


def git(*args):
    return subprocess.check_output(["git", "-c", "core.quotepath=false", *args], cwd=ROOT)


def main():
    if git('remote', 'get-url', 'origin').decode().strip() != 'https://github.com/OTAXIO/Library_code.git':
        raise RuntimeError('Unexpected remote; stopped')
    result = subprocess.run(['git', '-c', 'credential.interactive=never', 'credential', 'fill'],
                            input='protocol=https\nhost=github.com\n\n', text=True,
                            capture_output=True, timeout=15, cwd=ROOT)
    if result.returncode:
        raise RuntimeError('Cached Git authentication unavailable')
    credentials = dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)

    def api(method, path, data=None):
        request = urllib.request.Request(API + path,
            data=None if data is None else json.dumps(data).encode(), method=method,
            headers={'Authorization': 'Bearer ' + credentials['password'], 'Accept': 'application/vnd.github+json',
                     'Content-Type': 'application/json', 'User-Agent': 'SA-code-checkpoint'})
        with urllib.request.urlopen(request, timeout=25) as response:
            return json.load(response)

    head = git('rev-parse', 'HEAD').decode().strip()
    remote = api('GET', '/git/ref/heads/main')['object']['sha']
    if remote == head:
        print('Already uploaded:', head)
        return
    subprocess.check_call(['git', 'merge-base', '--is-ancestor', remote, head], cwd=ROOT)
    revisions = git('rev-list', '--reverse', remote + '..' + head).decode().splitlines()
    allowed = {'.py', '.js', '.cjs', '.json', '.md', '.txt', '.vbs', '.html', '.css'}
    for revision in revisions:
        parent = git('rev-parse', revision + '^').decode().strip()
        if parent != remote or api('GET', '/git/ref/heads/main')['object']['sha'] != remote:
            raise RuntimeError('Remote changed or history not linear; refusing overwrite')
        tree_lines = git('ls-tree', '-r', revision).decode().splitlines()
        tree = []
        for line in tree_lines:
            metadata, path = line.split('\t', 1)
            mode, kind, sha = metadata.split()
            if path.startswith(('runtime/', 'logs/', 'node_modules/', '.venv/')) or (Path(path).suffix not in allowed and path != '.gitignore'):
                raise RuntimeError('Non-code path rejected: ' + path)
            if kind != 'blob' or mode not in ('100644', '100755'):
                raise RuntimeError('Unsupported Git object')
            tree.append({'path': path, 'mode': mode, 'type': 'blob', 'sha': sha})

        def upload(entry):
            data = git('cat-file', 'blob', entry['sha'])
            uploaded = api('POST', '/git/blobs', {'content': base64.b64encode(data).decode(), 'encoding': 'base64'})
            if uploaded['sha'] != entry['sha']:
                raise RuntimeError('Blob verification failed')
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(upload, tree))
        created_tree = api('POST', '/git/trees', {'tree': tree})['sha']
        if created_tree != git('rev-parse', revision + '^{tree}').decode().strip():
            raise RuntimeError('Tree verification failed')
        headers, message = git('cat-file', 'commit', revision).decode().split('\n\n', 1)

        def identity(kind):
            line = next(x[len(kind) + 1:] for x in headers.splitlines() if x.startswith(kind + ' '))
            matched = re.fullmatch(r'(.*) <([^>]*)> (\d+) ([+-])(\d{2})(\d{2})', line)
            name, email, stamp, sign, hours, minutes = matched.groups()
            zone = datetime.timezone(datetime.timedelta(minutes=(int(hours) * 60 + int(minutes)) * (1 if sign == '+' else -1)))
            return {'name': name, 'email': email, 'date': datetime.datetime.fromtimestamp(int(stamp), zone).isoformat()}
        created = api('POST', '/git/commits', {'message': message, 'tree': created_tree, 'parents': [parent],
                                              'author': identity('author'), 'committer': identity('committer')})['sha']
        if created != revision:
            raise RuntimeError('Commit verification failed')
        api('PATCH', '/git/refs/heads/main', {'sha': revision, 'force': False})
        remote = api('GET', '/git/ref/heads/main')['object']['sha']
        if remote != revision:
            raise RuntimeError('Remote verification failed')
        subprocess.check_call(['git', 'update-ref', 'refs/remotes/origin/main', remote], cwd=ROOT)
        print('Verified checkpoint:', remote)


if __name__ == '__main__':
    main()
