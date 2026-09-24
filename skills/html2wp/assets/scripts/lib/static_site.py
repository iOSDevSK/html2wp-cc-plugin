# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
"""A static-site-generator build (Astro) as the converter's static input.

The generator already wrote every page as complete HTML, so no browser capture
is needed: its pages are written in the shape prerender-spa.py produces (flat
`about.html` for `about/index.html`, internal page links relative to the
linking page, files the pages load relative to them) and the site keeps its own scripts,
the same bar as a static HTML import. When the output is not a plain static
site (an app shell, a hydrating framework root, a <base> element, a stale
build), assess() says so and the caller falls back to the browser capture.
"""
import json
import posixpath
import re
import shutil
from pathlib import Path
from urllib.parse import unquote

SCHEMA = 'h2wp-static-site/1'
# A framework root that re-mounts over the page discards edits made in
# WordPress; prerender strips those bundles, so they keep the browser path.
HYDRATION = [
    ('id="__next"', 'Next.js'), ('__NEXT_DATA__', 'Next.js'), ('id="___gatsby"', 'Gatsby'),
    ('id="__nuxt"', 'Nuxt'), ('data-reactroot', 'React'), ('$_TSR', 'TanStack Start'),
    ('__TSR', 'TanStack Start'), ('data-sveltekit-hydrate', 'SvelteKit'),
]
MASK = re.compile(r'<script\b.*?</script\s*>|<style\b.*?</style\s*>|<template\b.*?</template\s*>|<!--.*?-->', re.S | re.I)
ANCHOR = re.compile(r'<(?:a|area)\b[^>]*>', re.I)
RESOURCE_TAG = re.compile(r'<(?:img|source|video|audio|track|script|link|iframe|embed|input)\b[^>]*>', re.I)
HREF = re.compile(r'(\shref\s*=\s*)(["\'])(.*?)\2', re.I | re.S)
RESOURCE_ATTR = re.compile(r'(\s(?:src|href|poster|srcset)\s*=\s*)(["\'])(.*?)\2', re.I | re.S)
URL_ATTRS = re.compile(r'(\s(?:href|src|srcset|poster)\s*=\s*)(["\']).*?\2', re.I | re.S)
INLINE_URL = re.compile(r'url\(\s*["\']?(?![a-z][a-z0-9+.-]*:|/|#|data:)([^"\')]+)', re.I)
EXTERNAL = re.compile(r'^(?:[a-z][a-z0-9+.-]*:|//|#)', re.I)


def page_files(dist):
    """Page files of the build: every .html except the 404 page and the
    generator's internal or asset folders (as prerender's routes_from_output)."""
    pages = []
    for path in sorted(dist.rglob('*.html')):
        rel = path.relative_to(dist).as_posix()
        if rel == '404.html' or rel.startswith(('_', 'assets/')) or '/_' in rel:
            continue
        pages.append(rel)
    return pages


def target_of(rel):
    """Where a page lands: `x/index.html` -> `x.html`, the rest keep their name."""
    if rel == 'index.html':
        return rel
    return rel[:-len('/index.html')] + '.html' if rel.endswith('/index.html') else rel


def route_key(path):
    """One key per page however a link spells it: `/about/`, `/about`,
    `/about/index.html` and `/about.html` all name `about`; `/` is ``."""
    path = unquote(path).strip('/')
    for suffix in ('/index.html', 'index.html', '.html', '/index'):
        if path == suffix.strip('/') or path.endswith(suffix):
            path = path[:-len(suffix)] if path != suffix.strip('/') else ''
            break
    return path.strip('/')


def split_url(url):
    fragment = query = ''
    if '#' in url:
        url, fragment = url.split('#', 1)
        fragment = '#' + fragment
    if '?' in url:
        url, query = url.split('?', 1)
        query = '?' + query
    return url, query, fragment


OPENING = re.compile(r'^<script\b[^>]*>', re.I)


def masked(html):
    """Scripts, styles, templates and comments hidden from the rewriting; a
    <script> keeps its opening tag in view, so its src is still a URL."""
    kept = []
    def hide(m):
        block = m.group(0)
        opening = OPENING.match(block)
        head = opening.group(0) if opening else ''
        kept.append(block[len(head):])
        return f'{head}\x00{len(kept) - 1}\x00'
    return MASK.sub(hide, html), kept


def unmasked(html, kept):
    return re.sub('\x00(\\d+)\x00', lambda m: kept[int(m.group(1))], html)


def visible_text(html):
    body = MASK.sub(' ', html)
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', body)).strip()


def assess(dist, pages, built_after=None):
    """Why the build cannot be taken as it is, or None."""
    index = dist / 'index.html'
    if not index.is_file():
        return 'the build wrote no index.html'
    if built_after is not None and index.stat().st_mtime < built_after:
        return 'the build did not write dist/index.html (it is older than this build)'
    shells = 0
    for rel in pages:
        path = dist / rel
        if path.is_symlink():
            return f'{rel} is a symlink'
        html = path.read_text(errors='replace')
        for marker, framework in HYDRATION:
            if marker in html:
                return f'{rel} is rendered by {framework}, which re-mounts the page in the browser'
        if re.search(r'<base\b', MASK.sub(' ', html), re.I):
            return f'{rel} has a <base> element'
        if len(visible_text(html)) < 100 and re.search(r'<script\b[^>]*\bsrc=', html, re.I):
            shells += 1
            if rel == 'index.html':
                return 'the front page is an app shell that renders in the browser'
    if pages and shells * 2 > len(pages):
        return f'{shells} of {len(pages)} pages are app shells that render in the browser'
    return None


def resolve(page_rel, url):
    """A link's site path, from the page's own address (`about/index.html`
    is served as `/about/`)."""
    if url.startswith('/'):
        return url
    here = '/' + (page_rel[:-len('index.html')] if page_rel.endswith('index.html') else posixpath.dirname(page_rel) + '/')
    joined = posixpath.normpath(posixpath.join(here, url))
    return joined + ('/' if url.endswith('/') and joined != '/' else '')


def rewrite_page(html, rel, routes, dist, links):
    """The page with its internal page links pointing at the flat files and
    its resource URLs (src, srcset, poster, <link href>) relative to the page's
    new place, the shape an html2wp export has: a root-absolute script on a
    self-contained front page is not rebased into the theme. Nothing else
    changes."""
    target = target_of(rel)
    depth = '../' * target.count('/')
    moved = posixpath.dirname(target) != posixpath.dirname(rel)
    body, kept = masked(html)

    def page_link(m):
        url = m.group(3)
        if not url.strip() or EXTERNAL.match(url.strip()):
            return m.group(0)
        path, query, fragment = split_url(url.strip())
        if not path:
            return m.group(0)
        site_path = resolve(rel, path)
        key = route_key(site_path)
        if key in routes:
            new = depth + routes[key] + query + fragment
            links['rewritten'] += 1
            links.setdefault('targets', []).append((target, posixpath.normpath(posixpath.join(posixpath.dirname(target), depth + routes[key]))))
            return f'{m.group(1)}{m.group(2)}{new}{m.group(2)}'
        if (dist / unquote(site_path).lstrip('/')).exists():
            if moved and not url.startswith('/'):
                return f'{m.group(1)}{m.group(2)}{site_path}{query}{fragment}{m.group(2)}'
            return m.group(0)
        links['unmapped'].append({'page': target, 'href': url})
        return m.group(0)

    def as_file(url):
        """A resource URL as a path relative to the page's new place, when it
        names a file of the build; anything else is left as written."""
        stripped = url.strip()
        if not stripped or EXTERNAL.match(stripped):
            return url
        path, query, fragment = split_url(stripped)
        if not path:
            return url
        site_path = resolve(rel, path)
        if not (dist / unquote(site_path).lstrip('/')).is_file():
            return site_path + query + fragment if moved and not path.startswith('/') else url
        return depth + site_path.lstrip('/') + query + fragment

    def resource(m):
        url = m.group(3)
        if m.group(1).strip().lower().startswith('srcset'):
            parts = []
            for candidate in url.split(','):
                bits = candidate.strip().split(None, 1)
                if bits:
                    bits[0] = as_file(bits[0])
                parts.append(' '.join(bits))
            return f'{m.group(1)}{m.group(2)}{", ".join(parts)}{m.group(2)}'
        return f'{m.group(1)}{m.group(2)}{as_file(url)}{m.group(2)}'

    body = ANCHOR.sub(lambda t: HREF.sub(page_link, t.group(0)), body)
    body = RESOURCE_TAG.sub(lambda t: RESOURCE_ATTR.sub(resource, t.group(0)), body)
    out = unmasked(body, kept)
    if moved:
        for m in INLINE_URL.finditer(MASK.sub(' ', out)):
            raise ValueError(f'{rel} uses a relative url({m.group(1)}) in an inline style; moving the page would break it')
    return out


def only_urls_changed(before, after):
    strip = lambda html: URL_ATTRS.sub(r'\1""', html)
    return strip(before) == strip(after)


def run(dist, out, report_path, built_after=None, generator='astro'):
    """Write the build's pages to `out` in the flat shape. Returns the result
    the runner hands back; `fallback: prerender` asks for the browser path."""
    dist, out, report_path = Path(dist), Path(out), Path(report_path)
    pages = page_files(dist) if dist.is_dir() else []
    report = {'schema': SCHEMA, 'ok': False, 'generator': generator, 'dist': str(dist), 'pages': [],
              'links': {'rewritten': 0, 'unmapped': []}, 'notFoundPage': (dist / '404.html').is_file(),
              'warnings': [], 'brokenLinks': []}

    def refuse(reason):
        report.update(reason=reason, fallback='prerender')
        report_path.write_text(json.dumps(report, indent=2))
        return {'ok': False, 'fallback': 'prerender',
                'message': f'Static build not usable: {reason}. The browser capture (prerender) applies instead.'}

    reason = assess(dist, pages, built_after) if pages else 'the build wrote no pages'
    if reason:
        return refuse(reason)
    routes = {}
    for rel in pages:
        key = route_key('/' + rel)
        if key in routes:
            first = next(p for p in pages if route_key('/' + p) == key)
            if (dist / first).read_bytes() != (dist / rel).read_bytes():
                return refuse(f'{first} and {rel} are two different pages at one address')
            report['warnings'].append(f'{rel} repeats {first}; kept once')
            continue
        routes[key] = target_of(rel)
    written = {}
    try:
        for rel in pages:
            key = route_key('/' + rel)
            if routes.get(key) != target_of(rel) or target_of(rel) in written:
                continue
            html = (dist / rel).read_text(errors='replace')
            new = rewrite_page(html, rel, routes, dist, report['links'])
            if not only_urls_changed(html, new):
                return refuse(f'rewriting {rel} changed more than its links')
            written[target_of(rel)] = new
            report['pages'].append({'route': '/' + key + ('/' if key else ''), 'file': rel, 'target': target_of(rel)})
            if '<astro-island' in html:
                report['warnings'].append(f'{rel} has interactive Astro islands; they keep their own scripts')
    except ValueError as e:
        return refuse(str(e))
    # Every link this step rewrote must land on a page it wrote.
    for page, linked in report['links'].pop('targets', []):
        if linked not in written:
            report['brokenLinks'].append({'page': page, 'href': linked})
    if report['brokenLinks']:
        return refuse(f"{len(report['brokenLinks'])} rewritten link(s) point at no page")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    page_set = set(pages) | {'404.html'}
    for path in sorted(dist.rglob('*')):
        rel = path.relative_to(dist).as_posix()
        if path.is_symlink():
            return refuse(f'{rel} is a symlink')
        if path.is_dir() or rel in page_set:
            continue
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest)
    for target, html in written.items():
        dest = out / target
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(html)
    report['ok'] = True
    report_path.write_text(json.dumps(report, indent=2))
    unmapped = len(report['links']['unmapped'])
    note = f'; {unmapped} internal link(s) name no page and were left as written' if unmapped else ''
    return {'ok': True, 'pages': len(written),
            'message': f'Static site built: {len(written)} page(s) taken from the generator\'s output, '
                       f'{report["links"]["rewritten"]} page link(s) made relative; no browser capture, '
                       f'the site\'s own scripts are kept{note}.'}
