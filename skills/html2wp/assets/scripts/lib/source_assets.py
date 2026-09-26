"""Restore missing source images before capture, never invent a source host.

Only a working build/input is changed. Root-relative asset URLs keep their
address, so framework JS, CSS, and the captured HTML all use the same bytes.
Absolute hotlinks remain the responsibility of optimize-images --remote.
"""
import io
import hashlib
import http.client
import posixpath
import json
import os
import re
import shutil
import stat
import time
import tempfile
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote, unquote, urljoin, urlsplit

from PIL import Image
from net_guard import address_verdict

SCHEMA = 'html2wp-source-assets/1'
SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.avif', '.bmp', '.ico', '.svg'}
RASTER = {'PNG', 'JPEG', 'WEBP', 'GIF', 'AVIF', 'BMP', 'ICO'}
MAX_BYTES = 25_000_000
MAX_ASSETS = 100
MAX_SECONDS = 60
SKIP = {'node_modules', 'vendor', 'dist', 'build', 'out', 'coverage'}


def files(root, *, source=False):
    """Never follow symlinks, hidden entries or dependency directories."""
    root = Path(root)
    if root.is_symlink():
        return
    for parent, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not d.startswith('.') and d != 'node_modules'
                         and not (source and d in SKIP) and not (Path(parent) / d).is_symlink())
        for name in sorted(names):
            p = Path(parent) / name
            if not name.startswith('.') and stat.S_ISREG(p.lstat().st_mode):
                yield p


def safe_target(root, ref, page='index.html'):
    parsed = urlsplit(ref)
    if parsed.scheme or parsed.netloc or not parsed.path or '\\' in ref:
        raise ValueError('not a local image URL')
    path = unquote(parsed.path)
    # Do not normalize away untrusted traversal/hidden components.
    if any(x.startswith('.') and x not in ('.', '..') for x in path.split('/') if x) or '\\' in path or '\x00' in path:
        raise ValueError('unsafe asset path')
    joined = path.lstrip('/') if path.startswith('/') else (Path(page).parent / path).as_posix()
    normalized = posixpath.normpath(joined)
    if normalized == '..' or normalized.startswith('../'):
        raise ValueError('asset path escapes input')
    rel = Path(normalized)
    if rel.suffix.lower() not in SUFFIXES:
        raise ValueError('not a supported image path')
    root = Path(root).resolve()
    target = root / rel
    for p in [target, *target.parents]:
        if p == root:
            break
        if p.is_symlink():
            raise ValueError('symlink in asset path')
    if not target.resolve().is_relative_to(root):
        raise ValueError('asset path escapes input')
    return target, rel.as_posix()


def validate_image(data, suffix):
    if not data or len(data) > MAX_BYTES:
        raise ValueError('image is empty or exceeds 25 MB')
    if suffix == '.svg':
        # SVG is active markup; do not import new SVGs through this resolver.
        raise ValueError('SVG recovery is not supported; supply the original local asset')
    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.format not in RASTER or img.width * img.height > 40_000_000:
                raise ValueError('unsupported or oversized image')
            img.verify()
    except (OSError, ValueError, SyntaxError, EOFError, Image.DecompressionBombError) as err:
        raise ValueError('invalid image data (' + type(err).__name__ + ')') from None


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def fetch_image(url, deadline):
    # Ignore environment proxies and cookies; use the existing public-address policy.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    for _ in range(4):
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('asset download requires public HTTPS without credentials')
        if deadline <= time.monotonic():
            raise ValueError('source image recovery time budget exhausted')
        why = address_verdict(url)
        if why:
            raise ValueError('asset address refused: ' + why)
        req = urllib.request.Request(url, headers={'User-Agent': 'html2wp-source-assets/1', 'Accept': 'image/*'})
        try:
            resp = opener.open(req, timeout=max(0.1, min(10, deadline - time.monotonic())))
        except urllib.error.HTTPError as err:
            if err.code in (301, 302, 303, 307, 308) and err.headers.get('Location'):
                url = urljoin(url, err.headers['Location'])
                err.close()
                continue
            code = err.code
            err.close()
            raise ValueError(f'asset download HTTP {code}') from None
        with resp:
            ctype = resp.headers.get('Content-Type', '').split(';')[0].lower()
            if not ctype.startswith('image/') or ctype == 'image/svg+xml':
                raise ValueError('asset response is not a raster image')
            data = bytearray()
            while len(data) <= MAX_BYTES:
                if time.monotonic() >= deadline:
                    raise ValueError('source image recovery time budget exhausted')
                chunk = resp.read(min(65536, MAX_BYTES + 1 - len(data)))
                if not chunk:
                    return bytes(data)
                data.extend(chunk)
            raise ValueError('asset exceeds 25 MB')
    raise ValueError('too many asset redirects')


class ImageRefs(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.refs = []
        self.base = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'base':
            self.base = True
        if tag in ('img', 'source', 'input'):
            if tag != 'input' or attrs.get('type') == 'image':
                for key in ('src', 'data-src', 'poster'):
                    if attrs.get(key):
                        self.refs.append(attrs[key])
                for key in ('srcset', 'data-srcset'):
                    value = attrs.get(key, '')
                    if not value.lstrip().startswith('data:'):
                        self.refs.extend(x.strip().split()[0] for x in value.split(',') if x.strip())
        if tag == 'video' and attrs.get('poster'):
            self.refs.append(attrs['poster'])
        self.refs.extend(css_refs(attrs.get('style', '')))


def css_refs(text):
    return [m.group(2).strip() for m in re.finditer(r'''url\(\s*(["']?)(.*?)\1\s*\)''', text, re.I)]


def recover(root, project=None, origin='', report_path=None):
    """Recover local image references. A missing origin is a finding, not a guess.

    Metadata is considered only when its URL occurs in the actual build. The
    source tree is read-only and is searched by exact metadata filename only.
    """
    root = Path(root).resolve()
    project = Path(project or root).resolve()
    origin = origin or os.environ.get('H2WP_ASSET_ORIGIN', '')
    if origin:
        u = urlsplit(origin)
        if u.scheme != 'https' or not u.hostname or u.username or u.password or u.query or u.fragment or u.path not in ('', '/'):
            raise ValueError('--asset-origin must be an HTTPS origin, without path, query or credentials')
        origin = origin.rstrip('/')
    report = {'schema': SCHEMA, 'root': str(root), 'recovered': [], 'unresolved': [], 'checked': 0}
    texts = {}
    wanted = {}
    for p in files(root):
        if p.suffix.lower() not in ('.html', '.htm', '.css', '.js', '.mjs') or p.stat().st_size > 20_000_000:
            continue
        rel = p.relative_to(root).as_posix()
        text = p.read_text(errors='replace')
        texts[rel] = text
        if p.suffix.lower() in ('.html', '.htm'):
            parser = ImageRefs()
            parser.feed(text)
            # base href changes URL semantics. Do not guess which local path it means.
            refs = [] if parser.base else parser.refs + css_refs(text)
        elif p.suffix.lower() == '.css':
            refs = css_refs(text)
        else:
            refs = []
        for ref in refs:
            try:
                target, dest = safe_target(root, ref, rel)
            except ValueError:
                continue
            if not target.is_file():
                item = wanted.setdefault(dest, {'ref': ref, 'pages': [], 'metadata': [], 'queries': set()})
                item['pages'].append(rel)
                item['queries'].add(urlsplit(ref).query)
    source_files = list(files(project, source=True))
    by_name = {}
    for p in source_files:
        if p.suffix.lower() in SUFFIXES:
            by_name.setdefault(p.name, []).append(p)
    for p in source_files:
        if not p.name.endswith('.asset.json') or p.stat().st_size > 65536:
            continue
        try:
            meta = json.loads(p.read_text())
            ref = meta.get('url') if isinstance(meta, dict) else None
            if not isinstance(ref, str) or not ref.startswith('/') or ref.startswith('//'):
                continue
            target, dest = safe_target(root, ref)
            used = [name for name, text in texts.items() if ref in text or ref.replace('/', '\\/') in text]
            if not used or target.is_file():
                continue
            item = wanted.setdefault(dest, {'ref': ref, 'pages': [], 'metadata': [], 'queries': set()})
            item['queries'].add(urlsplit(ref).query)
            item['pages'].extend(used)
            candidates = []
            sibling = p.with_name(p.name[:-len('.asset.json')])
            if sibling in source_files:
                candidates.append(sibling)
            filename = meta.get('original_filename')
            if isinstance(filename, str) and filename == Path(filename).name:
                candidates.extend(by_name.get(filename, []))
            item['metadata'].append((p.relative_to(project).as_posix(), candidates))
        except (OSError, ValueError):
            continue
    deadline = time.monotonic() + MAX_SECONDS
    for i, (dest, item) in enumerate(sorted(wanted.items())):
        entry = {'file': dest, 'pages': sorted(set(item['pages'])),
                 'metadata': [m for m, _ in item['metadata']]}
        report['checked'] += 1
        try:
            if i >= MAX_ASSETS or time.monotonic() >= deadline:
                raise ValueError('source image recovery budget exhausted')
            if len(item['queries']) > 1:
                raise ValueError('conflicting query variants share one local image path; explicit URL mapping required')
            target, _ = safe_target(root, '/' + quote(dest, safe='/'))
            candidates = sorted(set(c for _, paths in item['metadata'] for c in paths))
            # Exact URL path in a public directory is also an explicit mapping.
            for rel in (dest, 'public/' + dest):
                c = project / rel
                if c in source_files and c not in candidates:
                    candidates.append(c)
            if len(candidates) > 1:
                raise ValueError('ambiguous local image candidates; supply an exact asset')
            if candidates:
                c = candidates[0]
                if c.stat().st_size > MAX_BYTES:
                    raise ValueError('local image exceeds 25 MB')
                data = c.read_bytes()
                method = 'local'
            elif origin:
                # Quote the decoded filesystem path; query strings are preserved.
                data = fetch_image(origin + '/' + quote(dest, safe='/') +
                                   (('?' + urlsplit(item['ref']).query) if urlsplit(item['ref']).query else ''), deadline)
                method = 'origin'
            else:
                raise ValueError('missing original image and source origin; provide the original file or H2WP_ASSET_ORIGIN')
            validate_image(data, target.suffix.lower())
            target.parent.mkdir(parents=True, exist_ok=True)
            # Exclusive creation: never overwrite a file or an owner's concurrent repair.
            with target.open('xb') as f:
                f.write(data)
            entry.update(method=method, bytes=len(data), sha256=hashlib.sha256(data).hexdigest(),
                         source=c.relative_to(project).as_posix() if method == 'local' else origin)
            report['recovered'].append(entry)
        except (OSError, ValueError, http.client.HTTPException) as err:
            # Do not echo network errors that may contain signed URLs or credentials.
            reason = str(err) if isinstance(err, ValueError) else type(err).__name__
            entry['reason'] = reason
            report['unresolved'].append(entry)
    report['passed'] = not report['unresolved']
    if report_path:
        Path(report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(report_path).write_text(json.dumps(report, indent=2) + '\n')
    print(f"source images: {len(report['recovered'])} recovered, {len(report['unresolved'])} unresolved")
    for entry in report['unresolved'][:10]:
        print(f"  missing image: {entry['file']} — {entry['reason']}")
    return report


def prepare_build(dist, project, out, origin='', report_path=None):
    """Isolate recovery, keeping a previous reference intact until promotion."""
    import sandbox
    dist, out = Path(dist), Path(out)
    target = out.parent / ('.' + out.name + '-source-build')
    if (target.is_symlink() or target.resolve().is_relative_to(dist.resolve())
            or dist.resolve().is_relative_to(target.resolve())):
        raise ValueError('unsafe overlapping source image build directory')
    marker = target / '.html2wp-source-assets'
    if target.exists():
        if not marker.is_file() or marker.is_symlink() or marker.read_text().strip() != SCHEMA:
            raise ValueError('source image build directory is not owned by this helper')
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.source-assets-', dir=out.parent) as tmp:
        stage = Path(tmp) / 'build'
        backup = Path(tmp) / 'previous'
        sandbox.copy_build_output(dist, stage)
        (stage / '.html2wp-source-assets').write_text(SCHEMA + '\n')
        result = recover(stage, project, origin)
        if target.exists():
            target.rename(backup)
        try:
            stage.rename(target)
        except BaseException:
            if backup.exists():
                backup.rename(target)
            raise
    result['root'] = str(target)
    if report_path:
        Path(report_path).write_text(json.dumps(result, indent=2) + '\n')
    return target, result
