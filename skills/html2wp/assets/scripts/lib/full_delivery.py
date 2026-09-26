"""Full HTML delivery: retain an artifact independently of its quality verdict."""
import json
import os
import re
import stat
import zipfile
import subprocess
import hashlib
from pathlib import Path


def read(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def write(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(value, indent=2) + '\n')
    os.replace(tmp, path)


def issues(ws):
    return (read(Path(ws) / 'delivery-issues.json') or {}).get('issues', [])


def record(ws, stage, reason):
    rows = [r for r in issues(ws) if (r['stage'], r['reason']) != (stage, reason[:1000])]
    rows.append({'stage': stage, 'reason': reason[:1000]})
    write(Path(ws) / 'delivery-issues.json', {'schema': 'h2wp-delivery-issues/1', 'issues': rows})


CHECK_REPORTS = {
    'prerender-spa.py': ('prerender-report.json',),
    'install-theme.py': ('install-theme/report.json',),
    'quick-check.py': ('quick-check.json',),
    'verify-wp.py': ('verify-wp/report.json',),
    'smoke-editor.py': ('smoke-editor/report.json',),
    'audit-woo-coverage.py': ('woo-coverage/report.json',),
    'verify-static.py': ('verify-static/report.json',),
    'verify-parity.mjs': ('parity-report.json', 'verify-parity/report.json'),
    'preflight-listings.mjs': ('preflight-listings.json',),
}


def verification_state(ws, checker):
    from repair_budget import input_digest
    if checker == 'prerender-spa.py':
        report = read(Path(ws) / 'prerender-report.json') or {}
        project = report.get('project')
        capture = report.get('out')
        if not project or not capture:
            return None
        paths = [Path(report.get('dist') or Path(project) / 'dist'), Path(capture)]
        digest = hashlib.sha256()
        for root in paths:
            if not root.is_absolute():
                root = Path(ws) / root
            if not root.is_dir():
                return None
            digest.update(str(root.resolve()).encode())
            for file in sorted(root.rglob('*')):
                if file.is_file() and not file.is_symlink():
                    digest.update(str(file.relative_to(root)).encode())
                    digest.update(file.read_bytes())
        return digest.hexdigest()
    manifest = read(Path(ws) / 'conversion-manifest.json') or {}
    output = ('astro-project/dist' if checker in ('verify-static.py', 'verify-parity.mjs', 'preflight-listings.mjs')
              else 'theme/' + (manifest.get('site') or {}).get('slug', ''))
    return input_digest(ws, ['conversion-manifest.json', output])


def valid_zip(path):
    """Structural artifact check, never a visual/functional pass."""
    try:
        with zipfile.ZipFile(path) as z:
            names = [i.filename for i in z.infolist() if not i.is_dir()]
            roots = {n.split('/')[0] for n in names}
            if len(roots) != 1 or not names or z.testzip():
                return False
            for i in z.infolist():
                p = Path(i.filename)
                if p.is_absolute() or '..' in p.parts or '\\' in i.filename or stat.S_ISLNK(i.external_attr >> 16):
                    return False
                if re.search(r'(credentials|secret|\.env)|\.(py|pyc|log|sql)$', i.filename, re.I):
                    return False
            root = next(iter(roots)) + '/'
            if root + 'style.css' not in names or root + 'theme.json' not in names:
                return False
            if not re.search(rb'Theme Name\s*:', z.read(root + 'style.css'), re.I):
                return False
            if not isinstance(json.loads(z.read(root + 'theme.json')), dict):
                return False
            if root + 'templates/index.html' not in names and root + 'index.php' not in names:
                return False
            bundle_name = root + 'clara-content/manifest.json'
            if bundle_name not in names:
                return False
            bundle = json.loads(z.read(bundle_name))
            if bundle.get('format') != 'clara-content/1':
                return False
            sources = [n for n in names if n.startswith(root + 'clara-content/sources/') and n.endswith('.html')]
            if len(sources) < (bundle.get('contains', {}).get('sources') or 0):
                return False
            for required in ('functions.php', 'inc/content-import.php', 'clara-content/sources/index.json'):
                if root + required not in names:
                    return False
            index = json.loads(z.read(root + 'clara-content/sources/index.json'))
            if not isinstance(index, list) or not index or len(index) != len(sources):
                return False
            referenced = set()
            for row in index:
                if not isinstance(row, dict) or not row.get('key') or not isinstance(row.get('file'), str):
                    return False
                file = root + 'clara-content/' + row['file']
                if file not in sources or file in referenced or not z.getinfo(file).file_size:
                    return False
                referenced.add(file)
                # Existing post-delivery edits can leave importer metadata
                # hashes unchanged. Artifact identity is the whole ZIP digest;
                # an old per-source digest is not proof of missing content.
            for name in names:
                if name.endswith('.php') and subprocess.run(['php', '-l'], input=z.read(name), capture_output=True, timeout=30).returncode:
                    return False
            return any(n.startswith(root + 'clara-content/sources/') and n.endswith('.html')
                       and z.getinfo(n).file_size > 0 for n in names)
    except (OSError, ValueError, KeyError, zipfile.BadZipFile, RuntimeError, TypeError, AttributeError, subprocess.TimeoutExpired):
        return False


def usable_capture(folder):
    from html.parser import HTMLParser
    class Content(HTMLParser):
        def __init__(self):
            super().__init__()
            self.hidden = 0
            self.body = False
            self.real = False
        def handle_starttag(self, tag, attrs):
            if tag == 'body': self.body = True
            if tag in ('script', 'style', 'head'): self.hidden += 1
            if self.body and not self.hidden and tag in ('img', 'video', 'svg', 'canvas'):
                self.real = True
        def handle_endtag(self, tag):
            if tag in ('script', 'style', 'head'): self.hidden = max(0, self.hidden - 1)
        def handle_data(self, data):
            if self.body and not self.hidden and data.strip(): self.real = True
    try:
        parser = Content()
        parser.feed((Path(folder) / 'index.html').read_text())
        return parser.real
    except (OSError, ValueError):
        return False
