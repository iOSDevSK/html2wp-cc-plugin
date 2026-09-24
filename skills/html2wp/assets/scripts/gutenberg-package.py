#!/usr/bin/env python3
"""Package only a locally verified, unchanged native Gutenberg theme."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlparse
import zipfile


def theme_digest(root):
    hashed = hashlib.sha256()
    for path in sorted(root.rglob('*')):
        if path.is_symlink():
            raise ValueError('Theme contains a symlink: ' + str(path))
        if not path.is_file() or path.relative_to(root).as_posix() == 'screenshot.png':
            continue
        hashed.update(path.relative_to(root).as_posix().encode() + b'\0')
        hashed.update(hashlib.sha256(path.read_bytes()).digest())
    return hashed.hexdigest()


def screenshot_info(path):
    """Decode the actual PNG; a nonempty file or claimed dimensions is not proof."""
    from PIL import Image, ImageStat
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 25 * 1024 * 1024:
        raise ValueError('A regular screenshot.png of at most 25 MiB is required')
    data = path.read_bytes()
    with Image.open(path) as image:
        if image.format != 'PNG' or image.size != (1200, 900):
            raise ValueError('screenshot.png must be a genuine 1200x900 PNG')
        image.verify()
    with Image.open(path) as image:
        rgb = image.convert('RGB')
        spans = [high - low for low, high in rgb.getextrema()]
        nonblank = max(spans) >= 16 and max(ImageStat.Stat(rgb).stddev) >= 2
        if 'A' in image.getbands() and image.getchannel('A').getextrema() != (255, 255):
            raise ValueError('screenshot.png must be an opaque frontend screenshot')
    if not nonblank:
        raise ValueError('screenshot.png is blank or effectively uniform')
    return {'sha256': hashlib.sha256(data).hexdigest(), 'width': 1200, 'height': 900,
            'format': 'PNG', 'nonblank': True}


def local_url(value):
    try:
        url = urlparse(value)
        return (url.scheme == 'http' and url.hostname in ('localhost', '127.0.0.1', '::1')
                and not url.username and not url.password)
    except (TypeError, ValueError):
        return False


def valid_diff(row):
    diff = row.get('diff')
    return (row.get('passed') is True and type(diff) in (int, float)
            and math.isfinite(diff) and 0 <= diff <= .01)


def valid_editor_visual(row, region):
    canvas = row.get('canvas')
    return (valid_diff(row) and row.get('region') == region
            and row.get('actualWidth') == row.get('width')
            and isinstance(canvas, dict) and bool(canvas.get('selector'))
            and canvas.get('iframe') is True
            and all(type(canvas.get(key)) in (int, float)
                    and math.isfinite(canvas[key]) and canvas[key] > 0 for key in ('width', 'height'))
            and bool(row.get('frontendScreenshot')) and bool(row.get('editorScreenshot')))


def valid_empty_part(row, region):
    """A part that renders nothing at this width on the site (a bar shown
    only on phones) and nothing in its editor canvas either."""
    boxes = [row.get('editorBox'), row.get('referenceBox')]
    return (row.get('kind') == 'template-parts' and row.get('empty') is True and valid_diff(row)
            and row.get('region') == region and row.get('actualWidth') == row.get('width')
            and all(isinstance(box, dict) and not (box.get('width', 0) > 0 and box.get('height', 0) > 0) for box in boxes))


def validate_new_post(root, report):
    proof = report.get('newPost')
    if not isinstance(proof, dict):
        raise ValueError('Missing new-post template proof; verify a genuinely new article with --edit-roundtrip')
    editor = proof.get('editorDefault') or {}
    roundtrip = proof.get('roundtrip') or {}
    layout = proof.get('layout') or {}
    path = proof.get('path', '')
    if (not isinstance(path, str) or not path.startswith('/') or path.startswith('//')
            or any(character in path for character in ('?', '#', '\\'))):
        raise ValueError('New-post template proof has an invalid frontend path')
    slug = path.rstrip('/').rsplit('/', 1)[-1]
    hierarchy = (['single-post-' + slug] if re.fullmatch(r'[a-zA-Z0-9_-]+', slug) else []) + ['single-post', 'single', 'singular', 'index']
    template = next((name for name in hierarchy if (root / 'templates' / (name + '.html')).is_file()), None)
    imported_ids = {row.get('id') for row in report['editor'] if row.get('kind') in ('page', 'post', 'product')}
    if (proof.get('passed') is not True or proof.get('deleted') is not True
            or type(proof.get('id')) is not int or proof['id'] <= 0 or proof['id'] in imported_ids
            or proof.get('template') != '' or not template
            or editor.get('mode') != 'template-locked' or editor.get('template') != root.name + '//' + template
            or proof.get('httpStatus') != 200
            or type(roundtrip.get('count')) is not int or roundtrip['count'] <= 0
            or roundtrip.get('invalid') != [] or roundtrip.get('unknown') != []
            or roundtrip.get('textPersisted') is not True
            or layout.get('titles') != 1 or layout.get('allH1') != 1 or layout.get('body') is not True
            or proof.get('error')):
        raise ValueError('New-post template proof must confirm default layout, valid save/reopen, public title/body and deletion')
    # Header/footer counts remain diagnostic: a valid shared part can be empty
    # or use a neutral wrapper. Full template visual gates cover shared chrome.


BLOCK_COMMENT = re.compile(r'<!--\s+wp:([a-z][a-z0-9-]*(?:/[a-z][a-z0-9-]*)?)(\s+|(?=/?-->))')


def part_root(root, name):
    """Return the rendered root element of a shared template part.

    Reads the first block comment of ``parts/<name>.html``. ``h2wp/element``
    and ``core/group`` render a predictable root element: ``{'tag','classes',
    'anchor'}`` is returned for them. A neutral root (a bare ``div`` without
    classes or anchor), another first block, or a missing part yields a
    fallback ``{'tag': name-derived, 'fallback': True}`` counted by parity
    with the front page. ``None`` means the theme has no such part.
    """
    path = Path(root) / 'parts' / (name + '.html')
    if not path.is_file():
        return None
    fallback = {'tag': name if name in ('header', 'footer') else 'div', 'classes': [], 'anchor': '', 'fallback': True}
    text = path.read_text()
    match = BLOCK_COMMENT.search(text)
    if not match:
        return fallback
    block = match.group(1)
    block = block if '/' in block else 'core/' + block
    attrs = {}
    rest = text[match.end():].lstrip()
    if rest.startswith('{'):
        try:
            attrs, _ = json.JSONDecoder().raw_decode(rest)
        except ValueError:
            return fallback
        if not isinstance(attrs, dict):
            return fallback
    if block not in ('h2wp/element', 'core/group'):
        return fallback
    tag = attrs.get('tagName') or 'div'
    if not isinstance(tag, str) or not re.fullmatch(r'[a-z][a-z0-9]*', tag):
        return fallback
    class_name = attrs.get('className') if isinstance(attrs.get('className'), str) else ''
    classes = class_name.split()
    if block == 'core/group':
        classes = ['wp-block-group'] + classes
    anchor = attrs.get('anchor') if isinstance(attrs.get('anchor'), str) else ''
    if not classes and not anchor:
        # A bare tag also matches the template-part wrapper (<header
        # class="wp-block-template-part">); count per part by parity instead.
        return fallback if tag == 'div' else {**fallback, 'tag': tag}
    return {'tag': tag, 'classes': classes, 'anchor': anchor, 'fallback': False}


def theme_report_for(root, explicit=None):
    """Load the compiler's theme-report.json (written beside ``theme/<slug>``)."""
    candidates = [Path(explicit)] if explicit else []
    root = Path(root)
    if root.parent.name == 'theme':
        candidates.append(root.parent.parent / 'theme-report.json')
    for path in candidates:
        if path.is_file():
            value = json.loads(path.read_text())
            if not isinstance(value, dict):
                raise ValueError('theme-report.json must be an object')
            return value
    if explicit:
        raise ValueError('theme report not found: ' + str(explicit))
    return {}


def contract_schema(theme_report, report):
    schemas = {value for value in (theme_report.get('contractSchema'), report.get('contractSchema')) if value}
    if len(schemas) > 1:
        raise ValueError('Verification report and theme-report.json disagree on the contract schema')
    return next(iter(schemas), None)


def validate_new_page(root, report):
    """A new owner page must inherit exactly one shared header and footer."""
    proof = report.get('newPage')
    if not isinstance(proof, dict):
        raise ValueError('Missing new-page chrome proof; h2wp-blocks/2 themes require --edit-roundtrip verification')
    layout = proof.get('layout') or {}
    imported_ids = {row.get('id') for row in report['editor'] if row.get('kind') in ('page', 'post', 'product')}
    if (proof.get('passed') is not True or proof.get('deleted') is not True
            or type(proof.get('id')) is not int or proof['id'] <= 0 or proof['id'] in imported_ids
            or proof.get('status') != 'draft' or proof.get('template') != ''
            or proof.get('httpStatus') != 200 or layout.get('paragraph') is not True
            or proof.get('error')):
        raise ValueError('New-page chrome proof must confirm a deleted draft page rendering its paragraph')
    chrome = proof.get('chrome') or {}
    for name in ('header', 'footer'):
        expected = part_root(root, name)
        if expected is None:
            continue
        row = chrome.get(name) or {}
        if row.get('root') != expected:
            raise ValueError(f'New-page {name} proof does not match parts/{name}.html of this theme')
        count, front = row.get('count'), row.get('frontPageCount')
        # front is None when the front page's own template renders no shared
        # parts (a self-contained front page): only the new page's count proves
        # anything then, and it must be one — unless the theme's front-page
        # template does carry the part, in which case the count is required.
        front_template = root / 'templates' / 'front-page.html'
        front_has_part = (not front_template.is_file()) or f'"slug":"{name}"' in front_template.read_text(errors='ignore')
        if type(count) is not int or (type(front) is not int and (front is not None or front_has_part)):
            raise ValueError(f'New-page {name} proof has no element counts')
        if expected['fallback'] and not (count >= 1 and (front is None or count == front)):
            raise ValueError(f'New page renders {count} {name} elements; the front page renders {front}')
        if not expected['fallback'] and not (count == 1 and front in (1, None)):
            raise ValueError(f'New page must render the shared {name} exactly once (found {count}, front page {front})')


def flash_refusal(root):
    """Why the block plan that built this theme is no packaging evidence yet,
    or None. A Flash conversion records the findings nobody reviewed in the
    plan's ledger (checkpoint `flash`); an entry no reason (12 characters or
    more) resolves still stands in for a review, and so does a check that
    accepted the ledger (`--flash`: check-report `unreviewed`). The plan lives
    in the workspace beside theme/<slug>."""
    root = Path(root)
    if root.parent.name != 'theme':
        return None
    state = root.parent.parent / '.gutenberg'
    checkpoint = json.loads((state / 'checkpoint.json').read_text()) if (state / 'checkpoint.json').is_file() else {}
    checked = json.loads((state / 'check-report.json').read_text()) if (state / 'check-report.json').is_file() else {}
    ledger = checkpoint.get('flash') if isinstance(checkpoint.get('flash'), dict) else {}
    resolutions = checkpoint.get('resolutions') if isinstance(checkpoint.get('resolutions'), dict) else {}
    standing = [name for name in ledger if not (isinstance(resolutions.get(name), str) and len(resolutions[name].strip()) >= 12)]
    accepted = checked.get('unreviewed') if isinstance(checked.get('unreviewed'), int) else 0
    if not standing and accepted <= 0:
        return None
    count = len(standing) or accepted
    pages = len({name.split(':', 1)[0] for name in standing})
    return (f'Flash conversion: {count} block plan finding(s)' + (f' on {pages} page(s)' if pages else '')
            + ' were recorded unreviewed, so this theme is not packaging evidence. Run Full check & build:'
            ' review every Flash ledger entry (correct the proposal or resolve it with a measured reason),'
            ' finalize without --flash, convert and verify again')


def validate_evidence(root, report, theme_report=None):
    """Raise ValueError with the first concrete missing or stale acceptance gate."""
    refusal = flash_refusal(root)
    if refusal:
        raise ValueError(refusal)
    # A smoke run (gutenberg-verify-local.py --scope smoke) measures one width
    # and skips the save/reload gates: a diagnosis, never evidence. A report
    # from before the field existed was a full run.
    if report.get('scope', 'full') != 'full':
        raise ValueError(f'A {report.get("scope")!r}-scope verification report is not packaging evidence; run gutenberg-verify-local.py with --scope full')
    if (report.get('schema') != 'h2wp-local-verification/2' or report.get('passed') is not True
            or not report.get('editor') or not report.get('visual') or not report.get('editorVisual')):
        raise ValueError('Schema v2 real editor, frontend visual and editor visual gates must pass before packaging')
    if report.get('themeDigest') != theme_digest(root):
        raise ValueError('Theme changed or was not fingerprinted by the gate; verify it again')
    if report.get('installedThemeDigest') != report['themeDigest']:
        raise ValueError('Installed WordPress theme bytes were not verified')
    info = screenshot_info(root / 'screenshot.png')
    preview = report.get('preview') or {}
    if (preview.get('passed') is not True or any(preview.get(key) != value for key, value in info.items())
            or preview.get('installedSha256') != info['sha256']):
        raise ValueError('Preview evidence does not match local and installed screenshot.png bytes')
    if not local_url(preview.get('sourceUrl')) or not local_url(preview.get('remoteUrl')):
        raise ValueError('Preview capture and installed preview must use HTTP localhost URLs')
    if urlparse(preview['remoteUrl']).path.rsplit('/', 1)[-1] != 'screenshot.png':
        raise ValueError('Installed preview URL must name screenshot.png')
    if any(part in urlparse(preview['sourceUrl']).path.split('/') for part in ('wp-admin', 'wp-login.php')):
        raise ValueError('Preview must show the frontend, not WordPress administration')
    content_file = root / 'content/content.json'
    bundle = json.loads(content_file.read_text())
    if bundle.get('schema') != 'h2wp-content/1' or not bundle.get('pages'):
        raise ValueError('Missing native content bundle')
    config = json.loads((root / 'content/config.json').read_text())
    media = json.loads((root / 'content/assets.json').read_text())
    expected_counts = {'pages': sum(page['kind'] not in ('post', 'product') for page in bundle['pages']),
                       'posts': sum(page['kind'] == 'post' for page in bundle['pages']),
                       'products': sum(page['kind'] == 'product' for page in bundle['pages']),
                       'media': len(media), 'menus': len(bundle.get('menus', []))}
    imported = report.get('import') or {}
    if (imported.get('schema') != 'h2wp-import-status/1' or imported.get('passed') is not True
            or imported.get('complete') is not True or imported.get('phase') != 'done'
            or imported.get('pending') != 0 or imported.get('errors') != []
            or imported.get('stylesheet') != root.name
            or imported.get('bundleSha256') != hashlib.sha256(content_file.read_bytes()).hexdigest()
            or not imported.get('bundleDigest') or imported.get('stateDigest') != imported['bundleDigest']
            or imported.get('counts') != expected_counts or imported.get('importedCounts') != expected_counts):
        raise ValueError('Import evidence is missing, incomplete, failed or does not match this bundle')
    if expected_counts['posts']:
        validate_new_post(root, report)
    if theme_report is None:
        theme_report = theme_report_for(root)
    if contract_schema(theme_report, report) == 'h2wp-blocks/2':
        validate_new_page(root, report)
    tested = {(row.get('path', '').strip('/'), row.get('width')) for row in report['visual'] if valid_diff(row)}
    edited = {(row.get('path', '').strip('/'), row.get('kind')) for row in report['editor']
              if not row.get('invalid') and not row.get('unknown') and row.get('count', 0) > 0
              and row.get('roundtrip', {}).get('textPersisted')
              and not row['roundtrip'].get('invalid') and not row['roundtrip'].get('unknown')}
    # A page's live path is the importer's record of it (WordPress may suffix
    # a slug: a numeric '404' page is '404-2'); the bundle slug otherwise.
    live = {row.get('key'): urlparse(row.get('path') or '').path.strip('/') for row in imported.get('entities') or []
            if isinstance(row, dict) and row.get('key') and row.get('path')}
    for page in bundle['pages']:
        path = '' if page['key'] == config.get('frontPage') else live.get(page['key'], page['slug'].strip('/'))
        kind = page['kind'] if page['kind'] in ('post', 'product') else 'page'
        # WooCommerce draws the shop page with its Product Catalog template,
        # never the page's own (empty) content; that template is the editor
        # evidence (the template rows and their visual gate), not a page save.
        catalog = page['kind'] == 'shop' and any(row.get('kind') == 'templates' and row.get('path', '').strip('/') == path
                                                  and valid_editor_visual(row, 'products') for row in report.get('editorVisual', []))
        if (path, kind) not in edited and not catalog:
            raise ValueError(f'Missing editor save/reload gate for {page["key"]}; run --edit-roundtrip')
        for width in (1440, 820, 390):
            if page['kind'] not in ('cart', 'checkout') and (path, width) not in tested:
                raise ValueError(f'Missing passing visual gate for {page["key"]} at {width}px')
            # WooCommerce's separate product editor is covered by native block
            # serialization/REST roundtrip and storefront gates, not fake canvas shots.
            # The shop page's canvas is the Product Catalog template's product grid;
            # cart and checkout are compared as whole documents around WooCommerce's
            # preview basket (gutenberg-editor-visual.py).
            want_kind, want_region = (('templates', 'products') if page['kind'] == 'shop'
                                      else (kind, 'document') if page['kind'] in ('cart', 'checkout') else (kind, 'content'))
            if page['kind'] != 'product' and not any(
                    row.get('kind') == want_kind and row.get('path', '').strip('/') == path
                    and row.get('width') == width and valid_editor_visual(row, want_region)
                    for row in report['editorVisual']):
                raise ValueError(f'Missing matching editor content visual for {page["key"]} at {width}px')
    for folder, kind in (('templates', 'templates'), ('parts', 'template-parts')):
        for path in (root / folder).glob('*.html'):
            expected = root.name + '//' + path.stem
            if not any(row.get('kind') == kind and row.get('id') == expected and row.get('count', 0) > 0
                       and not row.get('invalid') and not row.get('unknown') and not row.get('unresolvedTokens')
                       for row in report['editor']):
                raise ValueError('Missing native template gate for ' + str(path.relative_to(root)))
            region = 'document' if kind == 'templates' else path.stem
            needs_visual = path.stem in (('front-page', 'home', 'single') if kind == 'templates' else ('header', 'footer'))
            if needs_visual:
                for width in (1440, 820, 390):
                    if not any(row.get('kind') == kind and row.get('id') == expected and row.get('width') == width
                               and (valid_editor_visual(row, region) or valid_empty_part(row, region)) for row in report['editorVisual']):
                        raise ValueError(f'Missing matching editor {region} visual for {expected} at {width}px')
    # The editor writes back what the import stored (gutenberg-verify-local.py
    # SERIALIZATION): a passing "serialization" row for every surface with blocks.
    serialized = {(row.get('kind'), row.get('id')) for row in report.get('serialization') or []
                  if row.get('passed') is True and row.get('byteIdentical') is True and not row.get('attributeLoss')}
    for row in report['editor']:
        if row.get('count', 0) > 0 and row.get('kind') in ('page', 'post', 'templates', 'template-parts') and (row.get('kind'), row.get('id')) not in serialized:
            raise ValueError(f'Missing passing serialization gate for {row.get("kind")} {row.get("slug") or row.get("id")}: the editor would rewrite its stored blocks')
    for name in ('style.css', 'theme.json', 'templates/index.html', 'functions.php'):
        if not (root / name).is_file():
            raise ValueError('Missing ' + name)
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--theme', required=True)
    parser.add_argument('--report', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--theme-report', help='Compiler theme-report.json (default: <workspace>/theme-report.json beside theme/<slug>)')
    args = parser.parse_args()
    root, out = Path(args.theme).resolve(), Path(args.out).resolve()
    if root == out or root in out.parents:
        parser.error('ZIP output must be outside the theme')
    try:
        report = json.loads(Path(args.report).read_text())
        preview = validate_evidence(root, report, theme_report_for(root, args.theme_report))
    except (ValueError, OSError, KeyError, TypeError) as error:
        parser.error(str(error))
    for path in root.rglob('*.php'):
        subprocess.run(['php', '-l', str(path)], check=True, stdout=subprocess.DEVNULL)
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=out.name + '.', suffix='.pending', dir=out.parent, delete=False) as temporary:
        staging = Path(temporary.name)
    try:
        with zipfile.ZipFile(staging, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob('*')):
                if path.is_file():
                    archive.write(path, root.name + '/' + path.relative_to(root).as_posix())
        # Bind the thumbnail separately since it is intentionally excluded from
        # runtime fingerprinting. A concurrent replacement cannot escape into ZIP.
        with zipfile.ZipFile(staging) as archive:
            archived_preview = archive.read(root.name + '/screenshot.png')
            if hashlib.sha256(archived_preview).hexdigest() != preview['sha256']:
                raise ValueError('Preview changed while packaging; verify it again')
            archived_digest = hashlib.sha256()
            for member in sorted(archive.namelist()):
                relative = member.removeprefix(root.name + '/')
                if relative == 'screenshot.png':
                    continue
                archived_digest.update(relative.encode() + b'\0')
                archived_digest.update(hashlib.sha256(archive.read(member)).digest())
            if archived_digest.hexdigest() != report['themeDigest']:
                raise ValueError('Archived theme bytes do not match the verified build')
        if theme_digest(root) != report['themeDigest']:
            raise ValueError('Theme changed while packaging; verify it again')
        staging.replace(out)
    except Exception:
        staging.unlink(missing_ok=True)
        raise
    print(json.dumps({'zip': str(out), 'themeDigest': report['themeDigest'],
                      'screenshotSha256': preview['sha256'], 'bytes': out.stat().st_size}))


if __name__ == '__main__':
    main()
