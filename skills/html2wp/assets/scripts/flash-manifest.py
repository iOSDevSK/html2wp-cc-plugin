#!/usr/bin/env python3
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
"""Flash, stage 0: the manifest drafted from analysis.json, and what is left
for the model to decide, found.

    flash-manifest.py --analysis {workspace}/analysis.json --input <input-dir>
                      --workspace {workspace} [--name "Site name"]
                      [--out {workspace}/conversion-manifest.json]
                      [--candidates {workspace}/flash-candidates.json]

Writes two files and decides neither the blog, the shop, the menus nor the
forms:

- the MANIFEST DRAFT (schema html2wp/1): site identity, input, workspace,
  every page with its key, kind and chrome mode, the chrome block and the
  design tokens. The chrome rule is the desktop app's Flash rule
  (runner.py flash_manifest): pages in both the largest header group and the
  largest footer group share the header and footer (consensus); every other
  page, typically a front page with its own hero navigation, keeps its own
  (self-contained); fewer than two such pages, every page keeps its own.
  design.templateMainClass is set when the pages' <main> carries a class the
  site's CSS names tag-qualified (`main.wrap { … }`): WordPress renders its
  own <main>, and without it every subpage renders its top spacing wrong
  (measured: gate B 12-34% on the subpages of such a site).
- the CANDIDATES (flash-candidates.json): what the model must decide at
  stage 0, measured — navigation groups worth a WordPress menu, blog listings
  with their article pages and the card container and card holding them (in
  the manifest's selector grammar, checked to match exactly one element), shop
  signals, and EVERY form with its fields (name, id, type, label), so each form
  lands in the manifest and can be connected in Visual Edit Lite.

The model reads both, writes nav, blog, shop and forms into the manifest (and
corrects anything else it disagrees with), then runs check-manifest.py
--flash, which refuses a manifest that left one of them undecided.

Exit 0; 2 = usage or an analysis with no pages; 3 = analysis refused the input
(its `refusal`).
"""
import argparse
import json
import posixpath
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from html_layout import children, compound, layout, unique_path  # noqa: E402

PRICE = re.compile(r'(?:[$€£¥]\s?\d[\d.,]*|\d[\d.,]*\s?(?:€|EUR|USD|Kč|CZK|zł))')
BUY = re.compile(r'\b(add to (?:cart|bag|basket)|buy now|checkout|do košíka|kúpiť)\b', re.I)
FIELD_TAGS = ('input', 'textarea', 'select')
SKIPPED_TYPES = {'submit', 'button', 'reset', 'image', 'hidden'}


def slugify(text):
    slug = re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')[:40].strip('-')
    return slug or 'site'


def read_page(root, rel):
    try:
        return (root / rel).read_text(errors='replace')
    except OSError:
        return ''


# ---------------------------------------------------------------- the draft

def draft(analysis, name, input_dir, workspace):
    pages = analysis.get('pages') or []
    consensus = analysis.get('consensus') or {}
    body = lambda p: p.get('body') or {}

    def largest(region):
        groups = (consensus.get(region) or {}).get('groups') or []
        return set(max(groups, key=lambda g: g.get('count', 0)).get('files') or []) if groups else set()

    together = largest('header') & largest('footer')
    shared_pages = [p for p in pages if p['file'] in together and body(p).get('headerStruct') and body(p).get('footerStruct')]
    if len(shared_pages) < 2:
        shared_pages = []
    shared = {p['file'] for p in shared_pages}
    front = next((p for p in pages if p['file'] == 'index.html'), pages[0])
    site_name = name or site_name_of(pages, front)
    slug = slugify(site_name)
    tokens = (analysis.get('design') or {}).get('tokens') or []
    manifest = {
        'schema': 'html2wp/1',
        'site': {'name': site_name, 'slug': slug, 'prefix': slug.replace('-', '_'), 'home': '',
                 'description': front.get('description') or '', 'version': '1.0.0'},
        'input': {'dir': str(input_dir), 'type': 'static-html', 'devHosts': []},
        'workspace': str(workspace),
        'pages': [{'file': p['file'], 'key': 'front-page' if p is front else p['key'],
                   'kind': 'front' if p is front else 'page', 'title': p.get('title') or p['key'],
                   'chrome': 'consensus' if p['file'] in shared else 'self-contained'} for p in pages],
        # Colours from the design's own custom properties; fonts stay as the
        # source loads them (theme.json only offers them to the editor).
        'design': {'tokenSource': (analysis.get('design') or {}).get('tokenSource') or '',
                   'palette': [{'slug': slugify(t['name']), 'color': t['value'], 'var': '--' + t['name']}
                               for t in tokens
                               if re.fullmatch(r'#[0-9a-fA-F]{3,8}|rgba?\([0-9.,\s%]+\)', str(t.get('value', '')).strip())],
                   'fonts': []},
    }
    if shared:
        canonical = next((p for p in shared_pages if p is not front), shared_pages[0])['file']
        manifest['chrome'] = {'header': {'selector': 'header', 'canonicalFrom': canonical},
                              'footer': {'selector': 'footer', 'canonicalFrom': canonical},
                              # A front page outside the group keeps its own footer.
                              'frontOwnsFooter': front['file'] not in shared}
        # Sections straight in <body>: the content is what lies between the chrome.
        if any(str(body(p).get('mainCount')) != '1' for p in shared_pages):
            manifest['chrome']['content'] = {'selector': 'between-chrome'}
    else:
        # Every page keeps its own header and footer, the front page included.
        manifest['chrome'] = {'frontOwnsFooter': True}
        # Coverage still needs the real header selector when the design uses a
        # non-semantic wrapper. This does NOT promote pages to shared chrome.
        candidates = [(body(p).get('headerCandidate') or {}).get('selector') for p in pages]
        if candidates and all(candidates) and len(set(candidates)) == 1:
            manifest['chrome']['header'] = {'selector': candidates[0]}
    main_class = template_main_class(analysis, input_dir, shared)
    if main_class:
        manifest['design']['templateMainClass'] = main_class
    return manifest


TITLE_SEPARATOR = re.compile(r'\s+[|\-–—·:]\s+')


def site_name_of(pages, front):
    """The part of the page titles most pages repeat ("About | Studio North",
    "Journal | Studio North" → "Studio North"); without one, the front page's
    title up to its first separator."""
    seen = Counter()
    for p in pages:
        seen.update({x.strip() for x in TITLE_SEPARATOR.split(p.get('title') or '') if x.strip()})
    common, count = seen.most_common(1)[0] if seen else (None, 0)
    if common and len(pages) >= 2 and count >= max(2, (len(pages) + 1) // 2):
        return common
    return TITLE_SEPARATOR.split((front.get('title') or 'Site').strip())[0].strip() or 'Site'


def template_main_class(analysis, input_dir, shared):
    """The <main> class the consensus pages share, when the site's CSS
    qualifies it by tag (`main.wrap`), which a demoted <div> cannot satisfy."""
    mains = Counter()
    for p in analysis.get('pages') or []:
        if p['file'] not in shared:
            continue
        m = re.search(r'class=["\']([^"\']+)', str((p.get('body') or {}).get('mainTag') or ''))
        if m:
            mains[m.group(1).strip()] += 1
    if not mains:
        return None
    cls, count = mains.most_common(1)[0]
    if count < max(2, len(shared) // 2):
        return None
    css = ''
    for sheet in sorted({s for p in analysis.get('pages') or [] for s in ((p.get('head') or {}).get('stylesheets') or [])}):
        if re.match(r'^(?:[a-z]+:)?//', sheet):
            continue
        css += read_page(Path(input_dir), sheet.split('?')[0].lstrip('./').lstrip('/'))
    for page in list(shared)[:3]:
        css += ''.join(re.findall(r'<style\b[^>]*>(.*?)</style>', read_page(Path(input_dir), page), re.S | re.I))
    qualified = [c for c in cls.split() if re.search(r'(?<![\w.#-])main\.' + re.escape(c) + r'(?![\w-])', css)]
    return ' '.join(qualified) or None


# ---------------------------------------------------------------- candidates

def nav_candidates(analysis):
    """Navigation groups, one per menu: the same group on pages at another
    depth (its links written ../about.html there) is still one menu."""
    seen, out = set(), []
    for g in analysis.get('navGroups') or []:
        links = g.get('links') or []
        if len(links) < 2 or not g.get('allTextual'):
            continue
        key = (g.get('selector'), g.get('region'), tuple(re.sub(r'^(?:\.\.?/)+', '', l.get('href') or '') for l in links))
        if key in seen:
            continue
        seen.add(key)
        chrome = g.get('region') in ('header', 'footer')
        out.append({'selector': g.get('selector'), 'region': g.get('region'),
                    'selectorUnique': bool(g.get('selectorUnique')),
                    'links': [{'text': (l.get('text') or '')[:80], 'href': l.get('href')} for l in links],
                    'suggest': chrome and bool(g.get('selectorUnique')),
                    'why': ('a navigation group in the ' + g['region'] + ' — a real WordPress menu')
                           if chrome else 'links in the page body: a menu only if it is one (a card list is not)'})
    return out


def resolve(page, href):
    href = (href or '').split('#')[0].split('?')[0]
    if not href or re.match(r'^(?:[a-z][a-z0-9+.-]*:|//)', href, re.I):
        return None
    base = posixpath.dirname(page)
    path = posixpath.normpath(posixpath.join(base, href)).lstrip('/') if not href.startswith('/') else href.lstrip('/')
    if path.endswith('/') or path == '':
        path = (path + 'index.html').lstrip('/')
    elif not path.endswith('.html'):
        path += '.html'
    return path


def blog_candidates(analysis, input_dir):
    """Article families: pages that share a directory (blog/, posts/, …) or
    the analysis's own blog candidates, each with the listing that links to
    them and the container that holds their cards."""
    files = [p['file'] for p in analysis.get('pages') or []]
    families = {}
    for f in files:
        d = posixpath.dirname(f)
        if d:
            families.setdefault(d, []).append(f)
    for c in analysis.get('blogCandidates') or []:
        arts = [a for a in (c.get('articles') or c.get('files') or []) if a in files]
        if len(arts) >= 2:
            families.setdefault('~' + (c.get('stem') or c.get('listing') or 'blog'), arts)
    out = []
    for name, articles in families.items():
        if len(articles) < 2:
            continue
        best = None
        for listing in files:
            if listing in articles:
                continue
            nodes = layout(read_page(Path(input_dir), listing))
            anchors = [n for n in nodes if n['tag'] == 'a' and resolve(listing, n['attrs'].get('href')) in articles]
            linked = {resolve(listing, a['attrs'].get('href')) for a in anchors}
            if len(linked) < 2 or (best and len(linked) <= best['linked']):
                continue
            best = {'listing': listing, 'linked': len(linked), **card_grid(nodes, anchors)}
        if best:
            out.append({'family': name.lstrip('~'), 'articles': sorted(articles), 'listing': best['listing'],
                        'linkedFromListing': best['linked'], 'cardContainer': best.get('cardContainer'),
                        'cardSelector': best.get('cardSelector'), 'cards': best.get('cards'),
                        'note': best.get('note')})
    return out


def card_grid(nodes, anchors):
    """The element whose direct children are the cards: the nearest ancestor
    of the article links that holds at least two of them in separate direct
    children, and the card as those children's shared compound."""
    for depth in range(1, 8):
        parents = Counter()
        for a in anchors:
            chain = [a] + list(reversed(a['ancestors']))
            if depth < len(chain):
                parents[id(chain[depth])] += 1
        for pid, count in parents.most_common():
            if count < 2:
                break
            parent = next(n for n in nodes if id(n) == pid)
            kids = [k for k in children(nodes, parent) if any(k is a or k in a['ancestors'] for a in anchors)]
            if len(kids) < 2:
                continue
            shapes = Counter(compound(k) for k in kids)
            card, n = shapes.most_common(1)[0]
            container = unique_path(nodes, parent)
            return {'cardContainer': container, 'cardSelector': card, 'cards': n,
                    'note': None if container else 'no selector in the grammar matches only the card grid: name it by hand'}
    return {'note': 'the article links share no card grid'}


def shop_signals(analysis, input_dir):
    """Pages that look like a shop: prices in the text, or a control (a
    button or link, not prose) that adds to a cart or buys."""
    rows = []
    for p in analysis.get('pages') or []:
        html = read_page(Path(input_dir), p['file'])
        text = re.sub(r'<[^>]+>', ' ', re.sub(r'<(script|style)\b.*?</\1>', ' ', html, flags=re.S | re.I))
        controls = [n for n in layout(html) if n['tag'] in ('button', 'a')
                    or (n['tag'] == 'input' and (n['attrs'].get('type') or '').lower() == 'submit')]
        buys = sum(1 for n in controls if BUY.search(n['text'] or n['attrs'].get('value') or ''))
        prices = len(PRICE.findall(text))
        if prices >= 3 or buys:
            rows.append({'page': p['file'], 'prices': prices, 'buyControls': buys})
    return rows


def purpose(fields):
    names = ' '.join(f"{f.get('name') or ''} {f.get('id') or ''} {f.get('type') or ''}" for f in fields).lower()
    if 'password' in names:
        return 'login'
    if re.search(r'\b(search|q|query|s)\b', names) and len(fields) <= 2:
        return 'search'
    if 'email' in names and len(fields) == 1:
        # Visual Edit Lite's mailing-list form type.
        return 'list'
    return 'contact'


def form_candidates(analysis, input_dir):
    out = []
    for p in analysis.get('pages') or []:
        nodes = layout(read_page(Path(input_dir), p['file']))
        labels = {n['attrs'].get('for'): n['text'] for n in nodes if n['tag'] == 'label' and n['attrs'].get('for')}
        for index, form in enumerate(n for n in nodes if n['tag'] == 'form'):
            fields = []
            for f in nodes:
                if f['tag'] not in FIELD_TAGS or form not in f['ancestors']:
                    continue
                kind = (f['attrs'].get('type') or ('textarea' if f['tag'] == 'textarea' else f['tag'])).lower()
                if kind in SKIPPED_TYPES:
                    continue
                label = labels.get(f['attrs'].get('id')) or next((a['text'] for a in f['ancestors'] if a['tag'] == 'label'), '')
                fields.append({'tag': f['tag'], 'type': kind, 'name': f['attrs'].get('name') or None,
                               'id': f['attrs'].get('id') or None,
                               'label': (label or f['attrs'].get('placeholder') or f['attrs'].get('aria-label') or '')[:80]})
            selector = ('form#' + form['attrs']['id']) if form['attrs'].get('id') else unique_path(nodes, form)
            out.append({'page': p['file'], 'selector': selector or 'form', 'index': index,
                        'purpose': purpose(fields), 'fields': fields,
                        'unnamed': sum(1 for f in fields if not f['name'])})
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--analysis', required=True)
    ap.add_argument('--input', required=True, help='the directory stage 0 analysed')
    ap.add_argument('--workspace', required=True)
    ap.add_argument('--name', default='', help="the site's name, when the owner or the UI gave one")
    ap.add_argument('--out', default='')
    ap.add_argument('--candidates', default='')
    args = ap.parse_args(argv)
    analysis = json.loads(Path(args.analysis).read_text())
    if analysis.get('refusal'):
        print('flash-manifest: the analysis refused this input: ' + '; '.join(map(str, analysis['refusal']))[:400], file=sys.stderr)
        return 3
    if not analysis.get('pages'):
        print('flash-manifest: the analysis found no pages', file=sys.stderr)
        return 2
    ws = Path(args.workspace).resolve()
    input_dir = Path(args.input).resolve()
    manifest = draft(analysis, args.name, input_dir, ws)
    candidates = {
        'schema': 'h2wp-flash-candidates/1',
        'decide': ['nav', 'blog', 'shop', 'forms'],
        'nav': nav_candidates(analysis),
        'blog': blog_candidates(analysis, input_dir),
        'shop': shop_signals(analysis, input_dir),
        'forms': form_candidates(analysis, input_dir),
    }
    out = Path(args.out) if args.out else ws / 'conversion-manifest.json'
    cand = Path(args.candidates) if args.candidates else ws / 'flash-candidates.json'
    out.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + '\n')
    cand.write_text(json.dumps(candidates, indent=2, ensure_ascii=False) + '\n')
    shared = sum(1 for p in manifest['pages'] if p['chrome'] == 'consensus')
    print(f"flash-manifest: {len(manifest['pages'])} page(s), {shared} sharing the header and footer"
          + (f", templateMainClass {manifest['design']['templateMainClass']!r}" if manifest['design'].get('templateMainClass') else '')
          + f" → {out}")
    print(f"  decide at stage 0: {len(candidates['nav'])} navigation group(s), {len(candidates['blog'])} blog family(ies), "
          f"{len(candidates['shop'])} page(s) with shop signals, {len(candidates['forms'])} form(s) → {cand}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
