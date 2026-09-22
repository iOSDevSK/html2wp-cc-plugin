#!/usr/bin/env python3
"""Deterministic Gutenberg inventory/proposals and local worker checkpoints.

Source HTML is data. This program never evaluates scripts or executes workers.
"""
import argparse
import contextlib
import hashlib
import html
from html.parser import HTMLParser
import io
import json
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import sys
import tempfile
import time
from datetime import datetime
from urllib.parse import parse_qsl, quote, urlsplit, unquote


def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    return hashlib.sha256(value).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + '\n')
    temp.replace(path)


def read(path):
    return json.loads(path.read_text())


def local_file(root, name):
    if not isinstance(name, str) or not name or '\\' in name:
        raise ValueError('invalid relative file path')
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts:
        raise ValueError('file must stay inside the workspace: ' + name)
    result = root / name
    if any(part.is_symlink() for part in [result, *list(result.parents)[:len(path.parts)-1]]):
        raise ValueError('symlink is not an upload file: ' + name)
    if result.is_symlink() or not result.resolve().is_relative_to(root.resolve()):
        raise ValueError('symlink/path escapes workspace: ' + name)
    return result


def collapse(text):
    """Source text as HTML lays it out: a run of whitespace is one space. An
    element's text is edited as rich text, where a source line break (the
    markup's indentation) is a real line break (rich() does the same)."""
    return re.sub(r'\s+', ' ', text)


class Node:
    def __init__(self, tag, attrs=(), children=None):
        self.tag, self.attrs, self.children = tag, dict(attrs), children or []

    def tree(self):
        return [self.tag, self.attrs, [c.tree() if isinstance(c, Node) else c for c in self.children]]


class Parser(HTMLParser):
    void = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self, source):
        super().__init__(convert_charrefs=True)
        self.root = Node('document')
        self.stack = [self.root]
        self.feed(source)
        self.close()

    def handle_starttag(self, tag, attrs):
        node = Node(tag, attrs)
        self.stack[-1].children.append(node)
        if tag not in self.void:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.void:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                self.stack = self.stack[:index]
                return

    def handle_data(self, value):
        self.stack[-1].children.append(value)


def walk(node):
    if isinstance(node, Node):
        yield node
        for child in node.children:
            yield from walk(child)


def plain(node):
    return ''.join(plain(c) if isinstance(c, Node) else c for c in node.children)


INLINE_TAGS = {'strong', 'em', 'b', 'i', 's', 'code', 'sub', 'sup', 'br', 'a', 'span'}
HEADINGS = {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
# Mirrors server/core/templates/gutenberg/element-allowlist.json (spec section 5).
ELEMENT_TAGS = set('''div section article aside main header footer nav span p h1 h2 h3 h4 h5 h6 ul ol li figure
figcaption details summary address dl dt dd table thead tbody tfoot tr th td caption strong em small a button
label time sup sub svg path circle rect line polyline polygon ellipse g title b i br hr blockquote
cite q abbr mark s u code kbd del ins'''.split())
VOID_ELEMENT_TAGS = {'br', 'hr'}
# Spec 2 C: plain wrappers of these tags become core/group.
GROUP_TAGS = {'div', 'section', 'article', 'aside', 'header', 'footer', 'main'}
# Spec 2 B: h2wp/icon node tags/attributes (element-allowlist.json svgTags/attributes).
SVG_TAGS = {'svg', 'path', 'circle', 'rect', 'line', 'polyline', 'polygon', 'ellipse', 'g', 'title'}
# Mirrors element-allowlist.json `attributes` (lowercase, as the HTML parser
# reports them; viewbox -> viewBox). The test suite asserts they stay equal.
ELEMENT_ATTRIBUTES = set('''href target rel type role tabindex title viewbox d fill stroke stroke-width stroke-linecap
stroke-linejoin cx cy r x y x1 y1 x2 y2 width height points xmlns hidden fill-rule clip-rule transform opacity
fill-opacity stroke-opacity stroke-dasharray stroke-dashoffset stroke-miterlimit vector-effect rx ry'''.split())
ICON_ATTRIBUTES = ELEMENT_ATTRIBUTES - {'href', 'target', 'rel', 'type', 'hidden'}
CSS_URL = re.compile(r'url\s*\(', re.I)


def norm(text):
    return ' '.join((text or '').split())


def text_size(node):
    return len(''.join(plain(node).split())) if isinstance(node, Node) else len(''.join(node.split()))


def class_key(node):
    return ' '.join((node.attrs.get('class') or '').split())


def shallow(node):
    """Alignment key of one node: tag + class (+ inline style, which becomes a class)."""
    return (node.tag, class_key(node), norm(node.attrs.get('style'))) if isinstance(node, Node) else ('#text',)


def shape(node):
    """Structural signature: tag + class of every element, text ignored."""
    return shallow(node) + (tuple(shape(c) for c in node.children if isinstance(c, Node)),)


# Spec 2 D3: dynamic text classification.
SEPARATOR = re.compile(r'(\s*[·•|]\s*|\s+[—–]\s+)')
READ_TIME = re.compile(r'\d+\s*min(?:ute)?s?(?:\s+read)?', re.I)
DATE_FORMATS = [('%B %d, %Y', 'F j, Y'), ('%b %d, %Y', 'M j, Y'), ('%b. %d, %Y', 'M. j, Y'), ('%B %d %Y', 'F j Y'),
                ('%d %B %Y', 'j F Y'), ('%d %b %Y', 'j M Y'), ('%Y-%m-%d', 'Y-m-d'), ('%B %Y', 'F Y'), ('%b %Y', 'M Y'),
                # With the weekday first ("Thursday, Feb 15, 2024").
                ('%A, %B %d, %Y', 'l, F j, Y'), ('%A, %b %d, %Y', 'l, M j, Y'), ('%a, %B %d, %Y', 'D, F j, Y'), ('%a, %b %d, %Y', 'D, M j, Y'),
                ('%A %d %B %Y', 'l j F Y'), ('%a %d %b %Y', 'D j M Y')]


def parse_date(text):
    """(ISO 'YYYY-MM-DD 12:00:00', PHP date format) for common English dates, else None."""
    text = norm(text)
    for pattern, fmt in DATE_FORMATS:
        try:
            value = datetime.strptime(text, pattern)
        except ValueError:
            continue
        if 'j' in fmt and re.search(r'(?:^|\s)0\d', text):
            fmt = fmt.replace('j', 'd')
        return value.strftime('%Y-%m-%d 12:00:00'), fmt
    return None


def classify(values, metas, card=False, static=False, tag=''):
    """(bind, bindFormat) for per-instance text values, else None.

    Metadata matches (title/excerpt/category/date/read time of the instance's
    post) win; pattern rules follow. `static` values (identical everywhere) are
    bound only on an exact metadata match."""
    values = [norm(v) for v in values]
    if all(m.get('title') and v == norm(m['title']) for v, m in zip(values, metas)):
        return 'postTitle', None
    if all(m.get('excerpt') and (v == norm(m['excerpt']) or (card and len(v) > 20 and norm(m['excerpt']).startswith(v.rstrip('.… ')))) for v, m in zip(values, metas)):
        return 'postExcerpt', None
    if all(m.get('categories') and v in m['categories'] for v, m in zip(values, metas)):
        return 'postTerms', None
    if all(m.get('readTime') and v == norm(m['readTime']) for v, m in zip(values, metas)):
        return 'postReadTime', None
    dates = [parse_date(v) for v in values]
    if all(d and m.get('date') and d[0] == m['date'] for d, m in zip(dates, metas)):
        return 'postDate', dates[0][1]
    if static:
        return None
    if all(dates) and len({d[1] for d in dates}) == 1:
        return 'postDate', dates[0][1]
    if all(READ_TIME.fullmatch(v) for v in values):
        return 'postReadTime', None
    # A card's own paragraph that differs per post is that post's summary —
    # the words the listing shows for it — however short it is; a 40-character
    # sentence is not a category.
    if card and tag == 'p' and len(set(values)) == len(values) and all(values):
        return 'postExcerpt', None
    if all(v and len(v) <= 40 for v in values):
        return 'postTerms', None
    if card and all(len(v) > 40 for v in values):
        return 'postExcerpt', None
    # An article's own lead paragraph (a subtitle beside the title) when the
    # posts carry no excerpt: it becomes each post's excerpt.
    if not card and all(len(v) > 40 for v in values) and len(set(values)) == len(values) and not any(m.get('excerpt') for m in metas):
        return 'postExcerpt', None
    return None


def leaf_pieces(node):
    pieces = []
    for text in node.children:
        pieces.extend(piece for piece in SEPARATOR.split(text) if piece)
    return pieces


def is_leaf(node):
    return node.tag not in VOID_ELEMENT_TAGS and bool(node.children) and all(isinstance(c, str) for c in node.children) and bool(plain(node).strip())


def excerpt_cut(texts):
    """'cut:N:…' when every card cuts its summary after the same number of
    characters and marks it ("… will tak..."), else None."""
    for mark in ('...', '…'):
        if texts and all(t.endswith(mark) for t in texts):
            stems = {len(t[:-len(mark)]) for t in texts}
            if len(stems) == 1 and 0 < next(iter(stems)) < 1000:
                return 'cut:' + str(stems.pop()) + ':' + mark
    return None


def diff_walk(nodes, metas, overrides, values, mapper, native_title=True, stop=None):
    """Compare corresponding nodes of several instances (posts or cards).

    Fills `overrides` (keyed by id of the FIRST instance's nodes) with the
    template transformation and `values` (one dict per instance, optional)
    with the classified per-instance values."""
    card = not native_title
    code = 'article-dynamic-unmapped' if native_title else 'query-card-unmapped'
    first = nodes[0]
    if stop and stop(nodes):
        return
    texts = [norm(plain(n)) for n in nodes]
    if first.tag in HEADINGS and all(m.get('title') and t == norm(m['title']) for t, m in zip(texts, metas)):
        overrides[id(first)] = {'title': True} if native_title else {'leaf': [(plain(first), 'postTitle', None)]}
        return
    # A card list whose headings are its posts' titles but for one (the source
    # listing named one post differently from its page): the heading is still
    # the title, and the odd card is reported.
    matched = [bool(m.get('title')) and t == norm(m['title']) for t, m in zip(texts, metas)]
    if card and first.tag in HEADINGS and len(nodes) >= 3 and matched.count(False) == 1 and all(texts) and len(set(texts)) == len(texts):
        odd = texts[matched.index(False)]
        mapper.finding('query-card-title', 'A card heading differs from its post title and shows the title instead: ' + odd[:120])
        overrides[id(first)] = {'leaf': [(plain(first), 'postTitle', None)]}
        return

    def record(bind, fmt, per_instance):
        for store, value in zip(values or [], per_instance):
            store.setdefault(bind, (norm(value), fmt))

    # Each instance's own image (an article hero, a card photo): the post's
    # featured image, bound instead of freezing the first instance's file.
    if first.tag == 'img' and all(n.tag == 'img' for n in nodes):
        sources = [n.attrs.get('src', '') for n in nodes]
        own = all(m.get('image') and mapper.resolve_quiet(src) == m['image'] for src, m in zip(sources, metas))
        # A picture the source never shipped stays the design's broken
        # reference: bound to the post, the live card would show a photograph
        # the original page never did.
        if all(sources) and all(mapper.resolve_quiet(src).startswith('asset:') for src in sources) and (len(set(sources)) > 1 or own):
            overrides.setdefault(id(first), {})['image'] = True
            for store, src in zip(values or [], sources):
                store.setdefault('postImage', (src, None))
        return

    if all(is_leaf(n) for n in nodes):
        pieces = [leaf_pieces(n) for n in nodes]
        whole = [''.join(n.children) for n in nodes]
        changed = len(set(texts)) > 1
        # A whole title/excerpt is one value even when it contains a separator
        # (an em dash inside an excerpt); a lone card (a listing's lead
        # article) only matches it exactly.
        if len(pieces[0]) == 1 or ((changed or card) and classify(whole, metas, card, static=not changed) in (('postTitle', None), ('postExcerpt', None))):
            kind = classify(whole, metas, card, static=not changed, tag=first.tag) if changed or card else None
            if kind and kind[0] == 'postExcerpt' and card:
                kind = (kind[0], excerpt_cut(texts) or kind[1])
            if kind:
                overrides.setdefault(id(first), {})['leaf'] = [(whole[0], kind[0], kind[1])]
                record(kind[0], kind[1], whole)
            elif changed:
                mapper.finding(code, first.tag + ': ' + ' | '.join(texts)[:160])
            return
        if len({len(p) for p in pieces}) != 1:
            mapper.finding(code, first.tag + ': ' + ' | '.join(texts)[:160])
            return
        spec, dynamic = [], False
        for j, piece in enumerate(pieces[0]):
            column = [p[j] for p in pieces]
            if not piece.strip() or SEPARATOR.fullmatch(piece):
                if len({norm(v) for v in column}) > 1:
                    mapper.finding(code, first.tag + ': ' + ' | '.join(texts)[:160])
                    return
                spec.append((piece, None, None))
                continue
            same = len({norm(v) for v in column}) == 1
            kind = classify(column, metas, card, static=same, tag=first.tag) if (not same or card) else None
            if kind is None and not same:
                mapper.finding(code, first.tag + ': ' + ' | '.join(norm(v) for v in column)[:160])
            if kind:
                dynamic = True
                record(kind[0], kind[1], column)
            spec.append((piece, kind[0] if kind else None, kind[1] if kind else None))
        if dynamic:
            overrides.setdefault(id(first), {})['leaf'] = spec
        return
    kids = [[c for c in n.children if isinstance(c, Node) or c.strip()] for n in nodes]
    aligned = all(len(k) == len(kids[0]) for k in kids) and all(shallow(a) == shallow(b) for k in kids[1:] for a, b in zip(kids[0], k))
    if not aligned:
        if len(set(texts)) > 1:
            mapper.finding(code, 'structure of ' + first.tag + '.' + class_key(first)[:60] + ' differs between instances')
        return
    for j, child in enumerate(kids[0]):
        if isinstance(child, str):
            if len({norm(k[j]) for k in kids}) > 1:
                mapper.finding(code, first.tag + ' mixed text: ' + ' | '.join(norm(k[j]) for k in kids)[:160])
        else:
            diff_walk([k[j] for k in kids], metas, overrides, values, mapper, native_title, stop)


def analyze_articles(entries, metas):
    """Spec 2 D1-D4: shared article template from the post pages, or None.

    entries: post page entries (manifest order); metas: post key -> metadata,
    updated in place with the inferred date/categories/read time."""
    if len(entries) < 2:
        return None

    def fail(code, detail):
        for entry in entries:
            entry['mapper'].section = ''
            entry['mapper'].finding(code, detail)
        return None
    owned = [[i for i, (node, part, place) in enumerate(e['items']) if part is None and place in ('head', 'main', 'tail')] for e in entries]
    # Contiguous up to the shared parts between them (a drawer or overlay
    # before the header): the template renders each part in its place.
    between = lambda e, o: [i for i in range(o[0], o[-1] + 1) if e['items'][i][1] is None] if o else []
    if not owned[0] or any(o != owned[0] for o in owned) or owned[0] != between(entries[0], owned[0]):
        return fail('article-template-variant', 'Post pages do not share one contiguous section layout; no article template was derived')
    index = owned[0]
    if any(shallow(e['items'][i][0]) != shallow(entries[0]['items'][i][0]) for e in entries for i in index):
        return fail('article-template-variant', 'Post pages do not share one section structure; no article template was derived')
    tree = lambda node: node.tree() if isinstance(node, Node) else node
    differing = [i for i in index if len({digest(tree(e['items'][i][0])) for e in entries}) > 1 and isinstance(entries[0]['items'][i][0], Node)]
    if not differing:
        return fail('article-template-variant', 'Post pages have identical sections; no article body found')
    body_index = max(differing, key=lambda i: sum(text_size(e['items'][i][0]) for e in entries))
    if any(e['items'][i][1] for e in entries for i in range(body_index, index[-1] + 1)):
        return fail('article-template-variant', 'A shared part sits between the article body and the sections after it; no article template was derived')

    def path(node):
        steps = []
        while True:
            total = text_size(node)
            kids = [c for c in node.children if isinstance(c, Node)]
            best = next((j for j, c in enumerate(kids) if total and text_size(c) >= 0.8 * total), None)
            if best is None:
                return steps
            steps.append(best)
            node = kids[best]
    bodies = [e['items'][body_index][0] for e in entries]
    for column in zip(*[path(b) for b in bodies]):
        if len(set(column)) != 1:
            break
        following = [[c for c in b.children if isinstance(c, Node)][column[0]] for b in bodies]
        if len({shallow(n) for n in following}) != 1:
            break
        bodies = following
    if any(not meaningful(b.children) for b in bodies):
        return fail('article-template-variant', 'An article body container has no content; no article template was derived')
    # A prose host that also holds the headline (<div class="article">
    # <span class="eyebrow">…</span><h1>…</h1><img>…<p>…</p>): its children
    # up to the one holding the <h1> are the article's head, rendered by the
    # template around a native post-title; the rest is the post content.
    def head_end(body):
        for j, child in enumerate(body.children):
            if isinstance(child, Node) and any(n.tag == 'h1' for n in walk(child)):
                return j + 1
        return 0
    body_from = head_end(bodies[0])
    if body_from and any(head_end(b) != body_from or any(shallow(x) != shallow(y) for x, y in zip(
            [c for c in b.children[:body_from] if isinstance(c, Node)], [c for c in bodies[0].children[:body_from] if isinstance(c, Node)])) for b in bodies):
        body_from = 0
    # The article's own picture right after the headline (each post its own
    # file) belongs to the head too: the template binds it to the post's
    # featured image, which the listing's cards then show.
    def next_node(body, start):
        return next(((j, c) for j, c in enumerate(body.children) if j >= start and isinstance(c, Node)), (None, None))
    if body_from:
        heroes = [next_node(b, body_from) for b in bodies]
        if all(n is not None and n.tag == 'img' and j == heroes[0][0] for j, n in heroes) and len({n.attrs.get('src') for _, n in heroes}) == len(heroes):
            body_from = heroes[0][0] + 1
    overrides = {id(bodies[0]): {'body': True, **({'from': body_from} if body_from else {})}}
    body_ids = {id(b) for b in bodies}
    post_metas = [metas[e['key']] for e in entries]
    values = [{} for _ in entries]

    def stop(nodes):
        return id(nodes[0]) in body_ids or all(e['mapper'].card_list(n) for e, n in zip(entries, nodes))
    mapper = entries[0]['mapper']
    for i in differing:
        mapper.section = entries[0]['sections'][i]
        diff_walk([e['items'][i][0] for e in entries], post_metas, overrides, values, mapper, True, stop)
        if body_from and i == body_index:
            for j in range(body_from):
                column = [b.children[j] for b in bodies]
                if all(isinstance(c, Node) for c in column):
                    diff_walk(column, post_metas, overrides, values, mapper, True, None)
    if not any(o.get('title') for o in overrides.values()):
        return fail('article-title-unmapped', 'No heading outside the article body carries the post title (page h1); no article template was derived')
    for entry, meta, found in zip(entries, post_metas, values):
        if 'postImage' in found:
            image = entry['mapper'].resolve_quiet(found['postImage'][0])
            if image.startswith('asset:'):
                meta['image'] = image
        if 'postDate' in found and parse_date(found['postDate'][0]):
            meta['date'] = parse_date(found['postDate'][0])[0]
        if 'postTerms' in found:
            meta['categories'] = [found['postTerms'][0]]
        if 'postReadTime' in found:
            meta['readTime'] = found['postReadTime'][0]
        if 'postExcerpt' in found and not meta.get('excerpt'):
            meta['excerpt'] = found['postExcerpt'][0]
    return {'owned': index, 'body_index': body_index, 'bodies': {e['key']: b for e, b in zip(entries, bodies)}, 'overrides': overrides, 'body_from': body_from}


class Mapper:
    def __init__(self, dist, page, links):
        self.dist, self.page, self.links = dist, page, links
        self.category_param, self.shop_keys = '', set()
        self.findings, self.styles, self.scripts = [], set(), set()
        self.sheets = []
        self.fonts = []
        self.inline_styles = {}
        self.inline_scopes = {}
        self.labels = {}
        self.in_form = False
        # The recorded empty-submit messages of the form being mapped (stage -1,
        # prerender-spa.py): {data-spa-vfield: text}.
        self.form_messages = {}
        self.form_unnamed, self.form_names = {}, set()
        self.recorded_messages = []
        self.spa_targets = set()
        self.section = ''
        # Round-2 derivation state (spec 2 sections B-D).
        self.overrides = {}        # id(node) -> {'body'|'title'|'leaf'|'link': ...}
        self.posts = {}            # post key -> metadata used to classify card text
        self.post_order = []       # post keys, newest first (what a query loop lists)
        self.queries = True        # card lists -> query loops (off in chrome/post bodies)
        self.query_namespace = None
        self.counter = [0]         # shared queryId counter

    def finding(self, code, detail):
        item = {'code': code, 'section': self.section, 'detail': detail}
        item['id'] = digest(item)[:20]
        if item not in self.findings:
            self.findings.append(item)

    def url(self, value, asset=False):
        url = urlsplit(value)
        if url.scheme in ('http', 'https', 'mailto', 'tel'):
            return value
        if url.scheme or value.startswith('//'):
            self.finding('unsafe-url', value)
            return ''
        if value.startswith('#'):
            return value
        path = unquote(url.path)
        base = PurePosixPath(self.page['file']).parent
        import posixpath
        relative = posixpath.normpath(path.lstrip('/') if path.startswith('/') else str(base / path))
        if relative == '..' or relative.startswith('../'):
            self.finding('outside-source', value)
            return ''
        if not asset:
            candidates = [relative, relative.rstrip('/') + '/index.html', relative + '.html']
            if relative == '.':
                candidates.insert(0, 'index.html')
            for candidate in candidates:
                if candidate in self.links:
                    key, fragment = self.links[candidate], (('#' + url.fragment) if url.fragment else '')
                    # A shop listing filtered by the manifest's category query
                    # parameter is WooCommerce's category archive; any other
                    # query string is kept.
                    query = dict(parse_qsl(url.query))
                    if self.category_param and key in self.shop_keys and query.get(self.category_param):
                        return 'category:' + quote(query[self.category_param].strip(), safe='')
                    return 'page:' + key + (('?' + url.query) if url.query else '') + fragment
            if not url.path:
                return value
        try:
            target = local_file(self.dist, relative)
            if target.is_file():
                return 'asset:' + relative + (('?' + url.query) if url.query else '') + (('#' + url.fragment) if url.fragment else '')
        except ValueError:
            pass
        self.finding('unresolved-link', value)
        return value

    def attrs(self, node, allowed=()):
        result = {}
        if node.attrs.get('class'):
            result['className'] = node.attrs['class']
        if node.attrs.get('id'):
            result['anchor'] = node.attrs['id']
        if node.attrs.get('style'):
            if any(key in node.attrs for key in ('data-spa-panel', 'data-spa-id', 'data-spa-style')):
                self.finding('runtime-inline-style', 'Extracted CSS classes survive runtime style removal; verify visible open/closed states and add explicit state-dependent CSS for ' + node.tag)
            # The source runtime opens a panel by removing its style attribute.
            # A class would keep hiding it, so the rule only applies to the
            # recorded initial state (closed = hidden / data-state).
            scope = ''
            if 'data-spa-panel' in node.attrs:
                if 'hidden' in node.attrs:
                    scope = '[hidden]'
                elif re.fullmatch(r'[a-z-]{1,20}', node.attrs.get('data-state') or ''):
                    scope = '[data-state="' + node.attrs['data-state'] + '"]'
            name = self.style_class(node.attrs['style'], scope)
            if name is None:
                self.finding('unsafe-inline-style', node.tag)
            elif name:
                result['className'] = (result.get('className', '') + ' ' + name).strip()
        extra = {}
        for key, value in node.attrs.items():
            if key in ('class', 'id', 'style') or key in allowed:
                continue
            if key == 'hidden':
                extra[key] = True
            elif key != 'href' and CSS_URL.search(value or ''):
                self.finding('unsafe-attribute', node.tag + '.' + key + ': url() is only allowed in href')
            elif key in ELEMENT_ATTRIBUTES:
                extra['viewBox' if key == 'viewbox' else key] = self.url(value or '') if key == 'href' else value or ''
            elif (key.startswith('aria-') or key.startswith('data-')) and re.fullmatch(r'(?:aria|data)-[a-z0-9-]+', key):
                extra[key] = value or ''
            else:
                self.finding('unmapped-attribute', node.tag + '.' + key + '=' + str(value))
        return result, extra

    def submit_class(self, node):
        """A submit button's classes, with its style attribute moved into a
        generated class like every other element's (style="border:0" had
        been dropped, and the button drew the browser's 2px outset border)."""
        classes = node.attrs.get('class', '') or ''
        style = node.attrs.get('style')
        name = self.style_class(style) if style else None
        return (classes + ' ' + name).strip() if name else classes

    def style_class(self, style, scope=''):
        """Move a safe inline style into a generated class; None when unsafe."""
        style = style.strip()
        if re.search(r'[{}<>]|expression\s*\(|javascript\s*:|@import|behavior\s*:|-moz-binding', style, re.I):
            return None
        # Preserve declarations without introducing executable markup.
        # Relative CSS URLs resolve against the original page location.
        def css_url(match):
            original = match.group(1).strip().strip('\"\'')
            target = self.url(original, True)
            if target.startswith('asset:'):
                target = '../' + target[6:]
            return 'url(' + json.dumps(target) + ')'
        style = re.sub(r'url\(([^)]*)\)', css_url, style, flags=re.I)
        name = 'h2wp-inline-' + digest(scope + style)[:16]
        self.inline_styles[name] = style
        if scope:
            self.inline_scopes[name] = scope
        return name

    def rich(self, node, preserve=False):
        """Inline-only content as rich text, or None when any child is not inline-safe."""
        pieces = []
        for child in node.children:
            if not isinstance(child, Node):
                # The editor's rich text is pre-wrap: source indentation and
                # line breaks would show and re-wrap lines. HTML collapses them.
                pieces.append(html.escape(child if preserve else re.sub(r'\s+', ' ', child), quote=False))
                continue
            if child.tag not in INLINE_TAGS:
                return None
            allowed = {'href', 'target', 'rel', 'class', 'style'} if child.tag == 'a' else {'class', 'style'} if child.tag == 'span' else set()
            if set(child.attrs) - allowed:
                return None
            if child.tag == 'br':
                if child.children:
                    return None
                pieces.append('<br>')
                continue
            attrs = {}
            classes = (child.attrs.get('class') or '').split()
            if child.attrs.get('style') is not None:
                if re.search(r'[{}<>]|expression\s*\(|javascript\s*:|@import|behavior\s*:|-moz-binding', child.attrs['style'], re.I):
                    return None
                classes.append(self.style_class(child.attrs['style']))
            if child.tag == 'a':
                if 'href' in child.attrs:
                    href = self.url(child.attrs['href'] or '')
                    if not href:
                        return None
                    attrs['href'] = href
                if child.attrs.get('target') in ('_blank', '_self'):
                    attrs['target'] = child.attrs['target']
                elif 'target' in child.attrs:
                    return None
                if attrs.get('target') == '_blank':
                    attrs['rel'] = 'noopener noreferrer'
                elif child.attrs.get('rel'):
                    attrs['rel'] = child.attrs['rel']
            if classes:
                attrs['class'] = ' '.join(classes)
            content = self.rich(child, preserve)
            if content is None:
                return None
            # The editor's rich text drops empty formats (decorative dots,
            # icon placeholders), so saving would silently delete them.
            if not content.replace('<br>', '').strip():
                return None
            attributes = ''.join(' ' + key + '="' + html.escape(value or '', quote=True) + '"' for key, value in attrs.items())
            pieces.append('<' + child.tag + attributes + '>' + content + '</' + child.tag + '>')
        return ''.join(pieces)

    def heading_name(self, node):
        """Editor List View label for a top-level section."""
        heading = next((n for n in walk(node) if n.tag in HEADINGS), None) if isinstance(node, Node) else None
        def spaced(item):
            # Spans/line breaks usually stack words visually ("Train<span>Swim</span>").
            if not isinstance(item, Node):
                return item
            inner = ''.join(spaced(child) for child in item.children)
            return inner if item.tag in {'strong', 'em', 'b', 'i', 's', 'sub', 'sup', 'code', 'a'} else ' ' + inner + ' '
        text = ' '.join(''.join(spaced(c) for c in heading.children).split()) if heading is not None else ''
        if text:
            return text[:60].rstrip()
        if not isinstance(node, Node):
            return ''
        first = (node.attrs.get('class') or '').split()
        return (node.tag + ('.' + first[0] if first else ''))[:60]

    def field(self, node, label):
        label_attrs = label if isinstance(label, dict) else {'text': label}
        label = label_attrs.get('text', '')
        kind = node.tag if node.tag in {'textarea', 'select'} else node.attrs.get('type', 'text')
        if kind not in {'text', 'email', 'tel', 'url', 'number', 'textarea', 'select', 'checkbox', 'radio'}:
            self.finding('unmapped-field', kind)
            return None
        name = node.attrs.get('name') or node.attrs.get('id')
        # The form's only unnamed email/tel/url field is named by its type
        # (what every mail handler expects); anything else stays a finding.
        if not name and kind in ('email', 'tel', 'url') and self.form_unnamed.get(kind) == 1 and kind not in self.form_names:
            name = kind
        if not name:
            self.finding('field-name', 'Set a stable field name before form delivery')
            name = 'field-' + digest(node.tree())[:10]
        attrs, extra = self.attrs(node, ('name', 'type', 'placeholder', 'required', 'autocomplete', 'rows', 'data-spa-vfield'))
        if extra or attrs.get('anchor'):
            self.finding('field-attributes', 'Review field attributes and labels: ' + name)
        result = {'name': name, 'type': kind, 'label': label or node.attrs.get('placeholder') or name, 'required': 'required' in node.attrs, 'placeholder': node.attrs.get('placeholder', ''), 'inputClassName': attrs.get('className', ''), 'labelClassName': label_attrs.get('class', '') if label else 'h2wp-label-hidden'}
        if self.form_messages.get(node.attrs.get('data-spa-vfield')):
            result['invalidMessage'] = self.form_messages[node.attrs['data-spa-vfield']]
        if kind == 'textarea' and str(node.attrs.get('rows', '')).isdigit():
            result['rows'] = int(node.attrs['rows'])
        if kind == 'select':
            options = [n for n in walk(node) if n.tag == 'option']
            result['options'] = [plain(n) for n in options]
            selected = next((n for n in options if 'selected' in n.attrs), options[0] if options else None)
            if selected is not None:
                result['defaultValue'] = selected.attrs.get('value', plain(selected))
            if any(n.attrs.get('value', plain(n)) != plain(n) for n in options):
                self.finding('select-values', 'Review option values/default selection: ' + name)
        if kind == 'textarea' and plain(node).strip():
            self.finding('field-default', 'Preserve textarea default content: ' + name)
        return {'name': 'h2wp/field', 'attributes': result}

    def simple_attrs(self, node, extra=()):
        """class/id/style only (no findings); None when other attributes exist."""
        if set(node.attrs) - {'class', 'id', 'style', *extra}:
            return None
        result = {}
        if node.attrs.get('class'):
            result['className'] = node.attrs['class']
        if node.attrs.get('id'):
            result['anchor'] = node.attrs['id']
        if node.attrs.get('style') is not None:
            name = self.style_class(node.attrs['style'])
            if name is None:
                return None
            result['className'] = (result.get('className', '') + ' ' + name).strip()
        return result

    def list_block(self, node):
        attrs = self.simple_attrs(node, ('start', 'reversed') if node.tag == 'ol' else ())
        if attrs is None:
            return None
        items = []
        for child in node.children:
            if not isinstance(child, Node):
                if child.strip():
                    return None
                continue
            if child.tag != 'li':
                return None
            item = self.simple_attrs(child)
            if item is None:
                return None
            # The editor renders list-item text inside its own rich-text box,
            # so a flex/grid <li> would lose its layout between the children.
            if re.search(r'(?:^|\s)(?:[a-z0-9]+:)?(?:inline-)?(?:flex|grid)(?:\s|$)', item.get('className', '')):
                return None
            kids, nested = list(child.children), []
            # Nested lists are list-item innerBlocks; they must trail the text.
            while kids and ((isinstance(kids[-1], str) and not kids[-1].strip()) or (isinstance(kids[-1], Node) and kids[-1].tag in ('ul', 'ol'))):
                last = kids.pop()
                if isinstance(last, Node):
                    nested.insert(0, last)
            content = self.rich(Node('li', (), kids))
            if content is None:
                return None
            inner = []
            for sub in nested:
                block = self.list_block(sub)
                if block is None:
                    return None
                inner.append(block)
            item['content'] = content
            items.append({'name': 'core/list-item', 'attributes': item, **({'innerBlocks': inner} if inner else {})})
        if not items:
            return None
        if node.tag == 'ol':
            attrs['ordered'] = True
            if re.fullmatch(r'-?\d{1,6}', str(node.attrs.get('start') or '')):
                attrs['start'] = int(node.attrs['start'])
            if 'reversed' in node.attrs:
                attrs['reversed'] = True
        return {'name': 'core/list', 'attributes': attrs, 'innerBlocks': items}

    def quote_block(self, node):
        attrs = self.simple_attrs(node)
        if attrs is None:
            return None
        inner, citation = [], None
        for child in node.children:
            if not isinstance(child, Node):
                if child.strip():
                    return None
                continue
            if citation is not None:
                return None  # core/quote renders the citation last
            if child.tag == 'p':
                paragraph = self.simple_attrs(child)
                content = self.rich(child)
                if paragraph is None or content is None:
                    return None
                paragraph['content'] = content
                inner.append({'name': 'core/paragraph', 'attributes': paragraph})
            elif child.tag == 'cite' and not child.attrs:
                citation = self.rich(child)
                if citation is None:
                    return None
            else:
                return None
        if not inner:
            return None
        if citation:
            attrs['citation'] = citation
        return {'name': 'core/quote', 'attributes': attrs, 'innerBlocks': inner}

    def table_block(self, node):
        """core/table dict, 'element' (keep h2wp/element tree) or 'content' (non-inline cells)."""
        attrs = self.simple_attrs(node)
        if attrs is None:
            return 'element'
        sections = {'head': [], 'body': [], 'foot': []}

        def rows(parent, target):
            for row in parent.children:
                if not isinstance(row, Node):
                    if row.strip():
                        return 'element'
                    continue
                if row.tag != 'tr' or row.attrs:
                    return 'element'
                cells = []
                for cell in row.children:
                    if not isinstance(cell, Node):
                        if cell.strip():
                            return 'element'
                        continue
                    if cell.tag not in ('th', 'td') or set(cell.attrs) - {'scope', 'colspan', 'rowspan', 'align'}:
                        return 'element'
                    content = self.rich(cell)
                    if content is None:
                        return 'content'
                    item = {'content': content, 'tag': cell.tag}
                    for key in ('scope', 'colspan', 'rowspan', 'align'):
                        if cell.attrs.get(key):
                            item[key] = cell.attrs[key]
                    cells.append(item)
                sections[target].append({'cells': cells})
            return None

        for child in node.children:
            if not isinstance(child, Node):
                if child.strip():
                    return 'element'
                continue
            if child.tag == 'caption' and not child.attrs:
                caption = self.rich(child)
                if caption is None:
                    return 'content'
                attrs['caption'] = caption
                continue
            section = {'thead': 'head', 'tbody': 'body', 'tfoot': 'foot'}.get(child.tag)
            if section and not child.attrs:
                problem = rows(child, section)
            elif child.tag == 'tr':
                problem = rows(Node('tbody', (), [child]), 'body')
            else:
                return 'element'
            if problem:
                return problem
        for key, value in sections.items():
            if value:
                attrs[key] = value
        return {'name': 'core/table', 'attributes': attrs}

    def media_url(self, value):
        return self.url(value, True) if value else ''

    def embed_block(self, node):
        src = (node.attrs.get('src') or '').strip()
        if src.startswith('//'):
            src = 'https:' + src
        match = re.match(r'^https?://(?:www\.)?(?:youtube\.com|youtube-nocookie\.com)/embed/([A-Za-z0-9_-]{6,})', src)
        vimeo = re.match(r'^https?://player\.vimeo\.com/video/(\d+)', src)
        if not match and not vimeo:
            self.finding('unmapped-element', 'iframe ' + src[:120])
            return None
        provider, url = ('youtube', 'https://www.youtube.com/watch?v=' + match.group(1)) if match else ('vimeo', 'https://vimeo.com/' + vimeo.group(1))
        aspect = '16-9'
        width, height = node.attrs.get('width', ''), node.attrs.get('height', '')
        if str(width).isdigit() and str(height).isdigit() and int(height):
            ratio = int(width) / int(height)
            aspect = min((('21-9', 21 / 9), ('18-9', 2), ('16-9', 16 / 9), ('4-3', 4 / 3), ('1-1', 1), ('9-16', 9 / 16), ('1-2', .5)), key=lambda item: abs(item[1] - ratio))[0]
        attrs = {'url': url, 'type': 'video', 'providerNameSlug': provider, 'responsive': True}
        classes = ['wp-embed-aspect-' + aspect, 'wp-has-aspect-ratio'] + (node.attrs.get('class') or '').split()
        attrs['className'] = ' '.join(classes)
        if node.attrs.get('id'):
            attrs['anchor'] = node.attrs['id']
        return {'name': 'core/embed', 'attributes': attrs}

    def av_block(self, node):
        kind = node.tag
        allowed = ('src', 'autoplay', 'loop', 'muted', 'controls', 'playsinline', 'poster', 'preload', 'width', 'height', 'crossorigin')
        attrs, extra = self.attrs(node, allowed)
        if extra:
            self.finding('media-attributes', kind + ' ' + str(extra))
        sources = [c for c in node.children if isinstance(c, Node) and c.tag == 'source' and c.attrs.get('src')]
        src = node.attrs.get('src') or (sources[0].attrs['src'] if sources else '')
        if not src:
            self.finding('unmapped-element', kind + ' without src')
            return None
        if len(sources) > (0 if node.attrs.get('src') else 1):
            self.finding('media-sources', kind + ': only the first source is kept')
        if any(isinstance(c, Node) and c.tag == 'track' for c in node.children):
            self.finding('media-tracks', kind + ': text tracks are not kept by core/' + kind)
        attrs['src'] = self.media_url(src)
        attrs['autoplay'] = 'autoplay' in node.attrs
        attrs['loop'] = 'loop' in node.attrs
        if kind == 'video':
            attrs['muted'] = 'muted' in node.attrs
            attrs['controls'] = 'controls' in node.attrs
            attrs['playsInline'] = 'playsinline' in node.attrs
            if node.attrs.get('poster'):
                attrs['poster'] = self.media_url(node.attrs['poster'])
        elif 'controls' not in node.attrs:
            self.finding('media-controls', 'core/audio always renders controls')
        if node.attrs.get('preload') in ('auto', 'metadata', 'none'):
            attrs['preload'] = node.attrs['preload']
        return {'name': 'core/' + kind, 'attributes': attrs}

    def pre_block(self, node):
        attrs = self.simple_attrs(node)
        if attrs is None:
            return None
        elements = [c for c in node.children if isinstance(c, Node)]
        texts = [c for c in node.children if not isinstance(c, Node) and c.strip()]
        if len(elements) == 1 and elements[0].tag == 'code' and not texts:
            code = elements[0]
            if set(code.attrs) - {'class'}:
                return None
            content = self.rich(code, True)
            name = 'core/code'
        else:
            content = self.rich(node, True)
            name = 'core/preformatted'
        if content is None:
            return None
        # A final line break draws nothing in a <pre>, but an empty last line in
        # the editor's editable code: it ends at the last character.
        attrs['content'] = content.rstrip('\n')
        # The source's own line wrapping (a <pre> keeps long lines; WordPress's
        # code and preformatted blocks wrap them): the theme resets it.
        attrs['className'] = (attrs.get('className', '') + ' h2wp-source-pre').strip()
        return {'name': name, 'attributes': attrs}

    def block(self, node):
        override = self.overrides.get(id(node)) if isinstance(node, Node) else None
        if override:
            return self.override_block(node, override)
        return self.block_default(node)

    def protected(self, node):
        """A descendant carries a derivation override: keep the element tree."""
        return bool(self.overrides) and any(id(n) in self.overrides for n in walk(node) if n is not node)

    def resolve_quiet(self, value):
        findings = list(self.findings)
        try:
            return self.url(value or '')
        finally:
            self.findings[:] = findings

    def structural(self, card):
        """Whether the page's stylesheets select this card's class by its place among siblings."""
        names = [re.escape(c) for c in (card.attrs.get('class') or '').split()]
        if not names:
            return False
        pattern = re.compile(r'\.(?:' + '|'.join(names) + r')(?![\w-])[^{},]*?(?::(?:first|last|nth|only)-(?:child|of-type)|\s*[+~])')
        for path in dict.fromkeys(self.sheets):
            cache = self.__dict__.setdefault('_css', {})
            if path not in cache:
                file = local_file(self.dist, path)
                cache[path] = file.read_text(errors='ignore') if file.is_file() else ''
            if pattern.search(cache[path]):
                return True
        return False

    def card_target(self, node):
        """The single post key a card links to, else None."""
        targets = set()
        for n in walk(node):
            if n.tag == 'a' and n.attrs.get('href'):
                target = self.resolve_quiet(n.attrs['href'])
                if target.startswith('page:') and target[5:].split('#')[0] in self.posts:
                    targets.add(target[5:].split('#')[0])
        return targets.pop() if len(targets) == 1 else None

    @staticmethod
    def rule(node):
        """An empty decorative box (a divider closing a list): no text, media or links."""
        return isinstance(node, Node) and not plain(node).strip() and not any(
            n.tag in ('img', 'svg', 'video', 'iframe', 'picture', 'canvas', 'a', 'button', 'input') for n in walk(node))

    def card_children(self, node):
        """node's element children less the dividers that open or close the list."""
        kids = [c for c in node.children if isinstance(c, Node)]
        start, end = 0, len(kids)
        while start < end and self.rule(kids[start]):
            start += 1
        while end > start and self.rule(kids[end - 1]):
            end -= 1
        return kids[start:end]

    def card_list(self, node):
        """Post keys when node's children are >= 2 identical cards linking to distinct posts."""
        if not self.posts or not isinstance(node, Node) or node.tag not in ELEMENT_TAGS or node.tag in ('a', 'svg'):
            return None
        if any(isinstance(c, str) and c.strip() for c in node.children):
            return None
        cards = self.card_children(node)
        if len(cards) < 2 or len({shape(c) for c in cards}) != 1:
            return None
        targets = [self.card_target(c) for c in cards]
        if None in targets or len(set(targets)) != len(targets):
            return None
        return targets

    def lead_card(self, node):
        """Post key of a lone card for the newest post (a listing's lead
        article): it links to that post and shows its title."""
        if not self.posts or not self.post_order or not isinstance(node, Node) or node.tag not in ('a', 'article', 'div', 'li'):
            return None
        key = self.card_target(node)
        title = norm(self.posts.get(key, {}).get('title') or '')
        if key != self.post_order[0] or not title or not any(n.tag in HEADINGS and norm(plain(n)) == title for n in walk(node)):
            return None
        return key

    def query_block(self, node, targets, cards=None):
        """core/query > core/post-template > first card with element binds (spec 2 D6).
        `cards` defaults to node's children; a lone lead card passes itself."""
        lone = cards is not None
        cards = cards or self.card_children(node)
        # Dividers that open or close the list stay the container's own.
        kids = [c for c in node.children if isinstance(c, Node)]
        rules = ([], []) if lone else (kids[:kids.index(cards[0])], kids[kids.index(cards[-1]) + 1:])
        overrides, found = {}, [{} for _ in targets]
        diff_walk(cards, [self.posts[key] for key in targets], overrides, found, self, native_title=False)
        # The summary a card prints becomes that post's excerpt (unless the
        # manifest states one), so the live card prints the same words.
        for position, (key, values) in enumerate(zip(targets, found)):
            if values.get('postExcerpt'):
                # A card that cuts the summary short ("… will tak...") is a
                # preview of one that prints it whole: the whole words win.
                seen, text = self.posts[key].get('cardExcerpt') or '', values['postExcerpt'][0]
                cut = lambda value: value.endswith(('...', '…'))
                if not seen or (cut(seen) and text.startswith(seen.rstrip('.… ')) and len(text) > len(seen)):
                    self.posts[key]['cardExcerpt'] = text
            # The date a card prints is that post's publication date.
            if values.get('postDate') and not self.posts[key].get('cardDate'):
                parsed = parse_date(values['postDate'][0])
                if parsed:
                    self.posts[key]['cardDate'] = parsed[0]
            # The picture a card shows is that post's featured image, so the
            # live card binds to it instead of repeating the first card's file.
            if values.get('postImage') and not self.posts[key].get('cardImage'):
                image = self.resolve_quiet(values['postImage'][0])
                if image.startswith('asset:'):
                    self.posts[key]['cardImage'] = image
            # The order a card list shows undated posts in is their recency.
            if not lone and 'listingOrder' not in self.posts[key]:
                self.posts[key]['listingOrder'] = position
        for n in walk(cards[0]):
            if n.tag == 'a' and n.attrs.get('href') and self.resolve_quiet(n.attrs['href']).split('#')[0] == 'page:' + targets[0]:
                overrides.setdefault(id(n), {})['link'] = True
        previous, queries = dict(self.overrides), self.queries
        self.overrides.update(overrides)
        self.queries = False
        try:
            card = self.block(cards[0])
        finally:
            self.overrides, self.queries = previous, queries
        self.counter[0] += 1
        attrs, extra = ({}, {}) if lone else self.attrs(node)
        # The container keeps its element, and the cards stay its own children,
        # when the design styles a card by its place among them
        # (`.row:first-child`, `.row:nth-child(4)`, `.row + .row`).
        wrap = bool(extra or attrs.get('anchor') or rules[0] or rules[1]) or (not lone and self.structural(cards[0]))
        template = {'name': 'core/post-template', 'attributes': {**({'className': attrs['className']} if attrs.get('className') and not wrap else {}), 'layout': {'type': 'default'}}, 'innerBlocks': [card] if card else []}
        # Cards listing the posts after the newest ones (a grid below a lead
        # article) skip those, so no post is shown twice.
        order = self.post_order
        # Related-post queries exclude the current article at render instead.
        offset = 0 if self.query_namespace else next((k for k in range(len(order)) if order[k:k + len(targets)] == targets), 0)
        query = {'perPage': len(cards), 'pages': 0, 'offset': offset, 'postType': 'post', 'order': 'desc', 'orderBy': 'date', 'inherit': False}
        block = {'name': 'core/query', 'attributes': {'queryId': self.counter[0], 'query': query, **({'namespace': self.query_namespace} if self.query_namespace else {})}, 'innerBlocks': [template]}
        if wrap:
            # The source container keeps its own element (its attributes need
            # it); the query and post-template boxes inside it take no box, so
            # the cards stay the container's own grid or flex items.
            block['attributes']['className'] = 'h2wp-query-contents'
            template['attributes']['className'] = 'h2wp-query-contents'
            around = [[b for b in (self.block(r) for r in side) if b] for side in rules]
            return {'name': 'h2wp/element', 'attributes': {**attrs, 'tagName': node.tag, **({'htmlAttributes': extra} if extra else {})}, 'innerBlocks': around[0] + [block] + around[1]}
        return block

    def container_block(self, node, inner):
        """core/group for a plain wrapper (spec 2 C), else h2wp/element."""
        attrs, extra = self.attrs(node)
        # An empty group shows the editor's layout picker instead of the
        # source's decorative box, so empty wrappers stay elements.
        if node.tag in GROUP_TAGS and not extra and inner:
            return {'name': 'core/group', 'attributes': {'tagName': node.tag, **attrs, 'layout': {'type': 'default'}}, 'innerBlocks': inner}
        return {'name': 'h2wp/element', 'attributes': {**attrs, 'tagName': node.tag, **({'htmlAttributes': extra} if extra else {})}, 'innerBlocks': inner}

    def override_block(self, node, override):
        if override.get('woo'):
            return override['woo'](node)
        if override.get('image'):
            block = self.block_default(node)
            if block and block['name'] == 'core/image':
                block['attributes']['metadata'] = {'bindings': {'url': {'source': 'h2wp/post-image'}, 'alt': {'source': 'h2wp/post-image', 'args': {'key': 'alt'}}}}
            return block
        if override.get('body'):
            head = [b for b in (self.block(c) for c in node.children[:override.get('from', 0)]) if b]
            # The body's own elements were the container's children in the
            # source: WordPress's post-content box (display:flow-root) would
            # stop their margins meeting the headline's, so it takes no box.
            return self.container_block(node, head + [{'name': 'core/post-content', 'attributes': {'className': 'h2wp-contents', 'layout': {'type': 'default'}}}])
        if override.get('title'):
            attrs, extra = self.attrs(node)
            if extra:
                self.finding('article-title-attributes', 'core/post-title keeps only class/id of ' + node.tag + ': ' + json.dumps(extra, sort_keys=True))
            return {'name': 'core/post-title', 'attributes': {'level': int(node.tag[1]), **attrs}}
        link, leaf = override.get('link'), override.get('leaf')
        if leaf is None:
            # Link bind only: map the element normally, then bind its href.
            del self.overrides[id(node)]
            try:
                block = self.block_default(node)
            finally:
                self.overrides[id(node)] = override
            if block and block['name'] == 'h2wp/element' and block['attributes'].get('tagName') == 'a':
                block['attributes']['bind'] = 'postLink'
            return block
        attrs, extra = self.attrs(node)
        attrs.update({'tagName': node.tag, **({'htmlAttributes': extra} if extra else {})})

        def bound(text, bind, fmt):
            result = {'text': text}
            if bind:
                result['bind'] = bind
                if fmt:
                    result['bindFormat'] = fmt
            return result
        if len(leaf) == 1 and not (link and leaf[0][1]):
            attrs.update(bound(*leaf[0]))
            if link:
                attrs['bind'] = 'postLink'
            return {'name': 'h2wp/element', 'attributes': attrs}
        if link:
            attrs['bind'] = 'postLink'
        # Mixed static/dynamic text (or a linked bound leaf): one span per piece.
        return {'name': 'h2wp/element', 'attributes': attrs, 'innerBlocks': [{'name': 'h2wp/element', 'attributes': {'tagName': 'span', **bound(*piece)}} for piece in leaf]}

    def icon_attributes(self, node, root):
        result = {}
        for key, value in node.attrs.items():
            if root and key in ('class', 'style'):
                continue
            value = value or ''
            if key == 'hidden':
                result[key] = True
            elif key in ICON_ATTRIBUTES or re.fullmatch(r'(?:aria|data)-[a-z0-9-]+', key):
                if CSS_URL.search(value) or (key == 'xmlns' and value != 'http://www.w3.org/2000/svg'):
                    return None
                result['viewBox' if key == 'viewbox' else key] = value
            else:
                return None
        return result

    def icon_block(self, node):
        """h2wp/icon for an svg subtree the icon block expresses exactly, else None (spec 2 B)."""
        budget = [0]

        def convert(parent, depth):
            nodes = []
            for child in parent.children:
                if isinstance(child, str):
                    if child.strip():
                        return None
                    continue
                budget[0] += 1
                if child.tag not in SVG_TAGS or child.tag == 'svg' or depth > 8 or budget[0] > 200:
                    return None
                attributes = self.icon_attributes(child, False)
                children = convert(child, depth + 1) if attributes is not None else None
                if children is None:
                    return None
                item = {'tagName': child.tag}
                if attributes:
                    item['htmlAttributes'] = attributes
                if children:
                    item['children'] = children
                nodes.append(item)
            return nodes
        root = self.icon_attributes(node, True)
        nodes = convert(node, 1) if root is not None else None
        if nodes is None:
            return None
        classes = ' '.join((node.attrs.get('class') or '').split())
        if node.attrs.get('style') is not None:
            name = self.style_class(node.attrs['style'])
            if name is None:
                return None
            classes = (classes + ' ' + name).strip()
        attrs = {}
        if classes:
            attrs['className'] = classes
        if root:
            attrs['htmlAttributes'] = root
        if nodes:
            attrs['nodes'] = nodes
        return {'name': 'h2wp/icon', 'attributes': attrs}

    def block_default(self, node):
        if isinstance(node, str):
            return {'name': 'h2wp/element', 'attributes': {'tagName': 'span', 'text': collapse(node)}} if node.strip() else None
        tag = node.tag
        # A recorded empty-submit message is carried by its field or form
        # (invalidMessage) and shown by the form runtime, not kept as an element
        # — wherever it sits, a toast outside the form included.
        if 'data-spa-invalid' in node.attrs:
            return None
        if (tag == 'button' or node.attrs.get('role') == 'button') and 'aria-expanded' in node.attrs and not node.attrs.get('data-spa-toggle'):
            self.finding('unrecorded-disclosure', 'Expanded-state control has no recorded behavior/panel: ' + plain(node).strip()[:120])
        if tag in {'script', 'style', 'link', 'meta'}:
            self.finding('embedded-runtime', tag + ' requires a reviewed WordPress replacement')
            return None
        if tag == 'iframe':
            return self.embed_block(node)
        if tag in ('video', 'audio'):
            return self.av_block(node)
        if tag == 'picture':
            image = next((n for n in walk(node) if n.tag == 'img'), None)
            if image is None:
                self.finding('unmapped-element', 'picture without img')
                return None
            sources = sum(1 for n in walk(node) if n.tag == 'source')
            self.finding('picture-sources', 'picture: ' + str(sources) + ' <source> candidate(s) and picture attributes dropped; core/image keeps the img src')
            return self.block(image)
        if tag == 'pre':
            block = self.pre_block(node)
            if block is None:
                self.finding('unmapped-element', 'pre with non-inline content or attributes')
            return block
        if tag in {'canvas', 'object', 'embed', 'source'}:
            self.finding('unmapped-element', tag)
            return None
        if tag == 'form':
            attrs, extra = self.attrs(node, ('action', 'method', 'data-spa-success', 'data-spa-validate'))
            # The feedback the source app showed on a successful submit
            # (stage -1 records it; see prerender-spa.py), shown by the
            # runtime after WordPress delivered the form.
            try:
                success = json.loads(node.attrs['data-spa-success']) if node.attrs.get('data-spa-success') else None
            except ValueError:
                success = None
            if node.attrs.get('action'):
                self.finding('form-delivery', 'Replace source action with configured Gutenberg form delivery: ' + node.attrs['action'])
            if extra or attrs.get('anchor'):
                self.finding('form-attributes', 'Review form metadata and anchor')
            # What the source printed on an empty submit: per field (the message
            # names its field) and for the whole form (a toast, or one before
            # every field). A toast outside the form belongs to it only when it
            # carries this form's token.
            token = node.attrs.get('data-spa-validate')
            recorded = [n for n in self.recorded_messages if n.attrs.get('data-spa-invalid') == token] if token else []
            previous, previous_messages = self.in_form, self.form_messages
            previous_unnamed, previous_names = self.form_unnamed, self.form_names
            fields = [n for n in walk(node) if n.tag in ('input', 'textarea', 'select')]
            self.form_names = {n.attrs.get('name') or n.attrs.get('id') for n in fields} - {None, ''}
            self.form_unnamed = {}
            for n in fields:
                if not (n.attrs.get('name') or n.attrs.get('id')):
                    t = n.tag if n.tag in ('textarea', 'select') else n.attrs.get('type', 'text')
                    self.form_unnamed[t] = self.form_unnamed.get(t, 0) + 1
            self.in_form = True
            self.form_messages = {n.attrs['data-spa-for']: plain(n).strip() for n in recorded if n.attrs.get('data-spa-for')}
            children = [self.block(child) for child in node.children]
            self.in_form, self.form_messages = previous, previous_messages
            self.form_unnamed, self.form_names = previous_unnamed, previous_names
            form_id = node.attrs.get('id') or 'form-' + digest(node.tree())[:12]
            # Forms ship disconnected: the owner turns submissions on in WordPress.
            form_attrs = {'formId': form_id, 'className': attrs.get('className', ''), 'acceptSubmissions': False}
            whole = next((plain(n).strip() for n in recorded if not n.attrs.get('data-spa-for')), '')
            if whole:
                form_attrs['invalidMessage'] = whole
            if isinstance(success, dict) and success.get('kind') in ('toast', 'inline', 'replace') and isinstance(success.get('html'), str):
                form_attrs['success'] = {k: success[k] for k in ('kind', 'html', 'list', 'region', 'ms') if success.get(k) is not None}
            return {'name': 'h2wp/form', 'attributes': form_attrs, 'innerBlocks': [c for c in children if c]}
        if self.in_form and tag == 'label':
            fields = [n for n in walk(node) if n.tag in {'input', 'select', 'textarea'}]
            if len(fields) == 1:
                label = {'text': plain(node).strip(), 'class': node.attrs.get('class', '')}
                field = self.field(fields[0], label)
                return field
            if node.attrs.get('for'):
                # The following field owns this label text; retaining another
                # label would duplicate accessible field names.
                return None
        if self.in_form and tag in {'input', 'textarea', 'select'}:
            if node.attrs.get('type') == 'submit':
                return {'name': 'h2wp/submit', 'attributes': {'label': node.attrs.get('value', 'Submit'), 'className': self.submit_class(node)}}
            return self.field(node, self.labels.get(node.attrs.get('id'), ''))
        if self.in_form and tag == 'button' and node.attrs.get('type', 'submit') == 'submit':
            if any(isinstance(c, Node) for c in node.children):
                self.finding('submit-markup', 'Preserve nested submit button imagery')
            return {'name': 'h2wp/submit', 'attributes': {'label': plain(node), 'className': self.submit_class(node)}}
        if tag == 'img':
            attrs, extra = self.attrs(node, ('src', 'alt', 'width', 'height', 'loading', 'decoding'))
            # The recorder's element id is bookkeeping unless a recorded
            # interaction changes this image (a gallery swapping its src).
            if extra.get('data-spa-id') and extra['data-spa-id'] not in self.spa_targets:
                extra = {k: v for k, v in extra.items() if k != 'data-spa-id'}
            if extra:
                self.finding('image-attributes', str(extra))
            attrs['className'] = (attrs.get('className', '') + ' h2wp-source-image').strip()
            attrs.update({'url': self.url(node.attrs.get('src', ''), True), 'alt': node.attrs.get('alt', '')})
            for size in ('width', 'height'):
                if str(node.attrs.get(size, '')).isdigit():
                    self.finding('image-dimensions', 'Preserve ' + size + '=' + node.attrs[size] + ' with reviewed image CSS')
            return {'name': 'core/image', 'attributes': attrs}
        if tag == 'svg':
            block = self.icon_block(node)
            if block:
                return block
        if self.queries:
            targets = self.card_list(node)
            if targets:
                return self.query_block(node, targets)
            lead = self.lead_card(node)
            if lead:
                return self.query_block(node, [lead], [node])
        protected = self.protected(node)
        if tag in ('ul', 'ol') and not protected:
            block = self.list_block(node)
            if block:
                return block
        if tag == 'blockquote' and not protected:
            block = self.quote_block(node)
            if block:
                return block
        if tag == 'table' and not protected:
            block = self.table_block(node)
            if isinstance(block, dict):
                return block
            if block == 'content':
                self.finding('unmapped-element', 'table with non-inline cell content kept as h2wp/element markup')
        attrs, extra = self.attrs(node)
        rich = self.rich(node) if (tag in HEADINGS or tag == 'p') and not protected else None
        if rich is not None and not extra:
            attrs['content'] = rich
            name = 'core/paragraph'
            if tag != 'p':
                name = 'core/heading'
                attrs['level'] = int(tag[1])
            return {'name': name, 'attributes': attrs}
        # Preserve semantic wrappers; no raw HTML escape hatch. Unsupported
        # attributes remain blocking findings instead of silently disappearing.
        if tag not in ELEMENT_TAGS:
            self.finding('unmapped-element', tag)
            return None
        attrs['tagName'] = tag
        if extra:
            attrs['htmlAttributes'] = extra
        # Text-only leaves keep the source tag and carry their text directly.
        # Mixed element/text content keeps per-node mapping: h2wp/element text
        # renders before inner blocks, so merging would reorder content.
        if node.children and all(isinstance(child, str) for child in node.children) and ''.join(node.children).strip() and tag not in VOID_ELEMENT_TAGS:
            text = ''.join(node.children)
            attrs['text'] = text if tag == 'textarea' else collapse(text)
            return {'name': 'h2wp/element', 'attributes': attrs}
        children = [b for b in (self.block(child) for child in node.children) if b]
        if tag in GROUP_TAGS and not extra and children:
            # Plain wrapper -> native core/group (spec 2 C); DOM gains layout classes only.
            group = {k: v for k, v in attrs.items() if k != 'tagName'}
            return {'name': 'core/group', 'attributes': {'tagName': tag, **group, 'layout': {'type': 'default'}}, 'innerBlocks': children}
        return {'name': 'h2wp/element', 'attributes': attrs, 'innerBlocks': children}


def meaningful(children):
    return [c for c in children if isinstance(c, Node) or c.strip()]


def selector_matches(node, selector):
    """Simple `tag.class#id` selectors from manifest chrome hints; anything else never matches."""
    match = re.fullmatch(r'([a-z][a-z0-9-]*)?((?:\.[A-Za-z0-9_-]+)*)(?:#([A-Za-z0-9_-]+))?', (selector or '').strip())
    if not match or not any(match.groups()) or not isinstance(node, Node):
        return False
    tag, classes, anchor = match.groups()
    return (not tag or node.tag == tag) and set(filter(None, classes.split('.'))) <= set((node.attrs.get('class') or '').split()) and (not anchor or node.attrs.get('id') == anchor)


# Body-level elements that render nothing; source scripts are inventoried separately.
NON_CONTENT = ('script', 'style', 'link', 'meta', 'template', 'noscript')


def split_frame(body, chrome):
    """Unwrap the page frame (spec section 1).

    Returns (wrapper, main, items) where items are (node, partName|None, place)
    in source order. place is 'before'/'after' (outside the wrapper), 'head'/
    'tail' (inside the wrapper, before/after <main>) or 'main' (main content;
    every non-part wrapper child when there is no <main>). Without a wrapper or
    a body-level <main> the body children are returned unchanged."""
    kids = [c for c in meaningful(body.children) if not (isinstance(c, Node) and c.tag in NON_CONTENT)]
    wrapper, before, after, level = None, [], [], kids
    chrome_tags = ('main', 'header', 'footer')

    def holds(node, tags):
        # Page-level chrome only: an article's own <header>/<footer> does not count.
        return isinstance(node, Node) and node.tag not in ('article', 'aside') and any(
            isinstance(c, Node) and (c.tag in tags or holds(c, tags)) for c in node.children)
    # Descend through app wrappers (React/Vite roots, layout divs) to the level
    # that holds the page chrome. Other siblings on the way (portals: toasts,
    # dialogs) stay ordinary sections before/after the frame. A lone body
    # wrapper is the frame even without chrome inside.
    while True:
        if any(isinstance(c, Node) and c.tag in chrome_tags for c in level):
            break
        wrappers = [c for c in level if isinstance(c, Node) and c.tag in ('div', 'section')]
        holders = [c for c in wrappers if holds(c, ('main',))] or [c for c in wrappers if holds(c, chrome_tags)]
        if len(holders) == 1:
            nxt = holders[0]
        elif wrapper is None and len(level) == 1 and wrappers:
            nxt = wrappers[0]
        else:
            break
        index = level.index(nxt)
        before, after = before + level[:index], level[index + 1:] + after
        wrapper, level = nxt, [c for c in meaningful(nxt.children) if not (isinstance(c, Node) and c.tag in NON_CONTENT)]
    mains = [c for c in level if isinstance(c, Node) and c.tag == 'main']
    main = mains[0] if len(mains) == 1 else None
    main_index = level.index(main) if main is not None else None
    outside = [(i, c) for i, c in enumerate(level) if isinstance(c, Node) and c is not main]

    def pick(name, candidates, fallback_tags):
        hint = ((chrome or {}).get(name) or {}).get('selector') if isinstance((chrome or {}).get(name), dict) else None
        hints = [hint] if hint else []
        if not hints and name == 'footer' and isinstance((chrome or {}).get('trailing'), list):
            # Stage 2 records a footer that is not a <footer> element (a
            # trailing <section>) as chrome.trailing[{role: footer, selectors}].
            hints = [s for t in chrome['trailing'] if isinstance(t, dict) and t.get('role') == 'footer'
                     for s in t.get('selectors') or [] if isinstance(s, str)]
        for predicate in [lambda n, h=h: selector_matches(n, h) for h in hints] + [lambda n, t=t: n.tag == t for t in fallback_tags]:
            found = [(i, c) for i, c in candidates if predicate(c)]
            if found:
                return found[0] if name == 'header' else found[-1]
        return None

    header = pick('header', [(i, c) for i, c in outside if main_index is None or i < main_index], ('header', 'nav'))
    footer = pick('footer', [(i, c) for i, c in outside if (main_index is None or i > main_index) and (header is None or c is not header[1])], ('footer',))
    if wrapper is None and main is None:
        # Body-level chrome without <main>: a header before all content and a
        # footer after some content frame the page (drawers or portals may
        # follow the footer); anything else stays content.
        content = [i for i, c in enumerate(level) if isinstance(c, Node) and (header is None or c is not header[1]) and (footer is None or c is not footer[1])]
        if header and content and header[0] > content[0]:
            header = None
        if footer and not any(i < footer[0] and (header is None or i > header[0]) for i in content):
            footer = None
        if header is None and footer is None:
            return None, None, [(c, None, 'main') for c in kids]
    parts = {}
    if header:
        parts[id(header[1])] = 'header'
    if footer:
        parts[id(footer[1])] = 'footer'
    # Siblings passed on the way down that render nothing (empty toast/dialog
    # portals a React app mounts into) are no content of any page.
    def renders(node):
        return not isinstance(node, Node) or bool(plain(node).strip()) or any(isinstance(n, Node) and n.tag in ('img', 'svg', 'video', 'iframe', 'picture', 'canvas', 'input', 'button', 'textarea', 'select') for n in walk(node))
    before, after = [c for c in before if renders(c)], [c for c in after if renders(c)]
    items = [(c, None, 'before') for c in before]
    seen_main = False
    for child in level:
        if child is main:
            seen_main = True
            items.extend((c, None, 'main') for c in meaningful(main.children))
        elif main is None:
            part = parts.get(id(child))
            # What follows the body-level footer (a drawer, a portal) is page
            # furniture after the frame, not page content: as 'main' it split
            # every article's content in two, so no article template could be
            # derived on a site whose drawer follows its footer.
            place = ('head' if part == 'header' else 'tail') if part else ('after' if footer and level.index(child) > footer[0] else 'main')
            items.append((child, part, place))
        else:
            items.append((child, parts.get(id(child)), 'tail' if seen_main else 'head'))
    items.extend((c, None, 'after') for c in after)
    return wrapper, main, items


ACTIVE_ATTRIBUTES = {'aria-current', 'data-status', 'data-state', 'data-active', 'data-spa-scroll'}
ACTIVE_CLASSES = {'active', 'is-active', 'current', 'current-menu-item'}


def is_active_marker(node):
    return isinstance(node, Node) and ('aria-current' in node.attrs or node.attrs.get('data-status') == 'active')


def has_active(node):
    return any(is_active_marker(n) for n in walk(node))


def strip_active(node):
    """Copy of a chrome tree without the first page's aria-current/data-status="active" state."""
    if not isinstance(node, Node):
        return node
    attrs = {k: v for k, v in node.attrs.items() if k != 'aria-current' and not (k == 'data-status' and v == 'active')}
    return Node(node.tag, attrs, [strip_active(c) for c in node.children])


def odd_links(tree):
    """Ids of links styled apart from all their sibling links: the current-page
    link of a nav that marks it by class alone (React Router's NavLink, a
    Tailwind `text-foreground` on the active item) and no aria-current. Needs
    three or more sibling links (a direct <a> or an <li> holding one) with
    exactly one odd one, so a two-link row or a deliberately mixed list is
    never read as state."""
    odd = set()
    for node in walk(tree):
        links = []
        for child in node.children:
            if not isinstance(child, Node):
                continue
            if child.tag == 'a':
                links.append(child)
            elif child.tag == 'li':
                inner = [c for c in child.children if isinstance(c, Node)]
                if len(inner) == 1 and inner[0].tag == 'a':
                    links.append(inner[0])
        if len(links) < 3:
            continue
        sets = [frozenset((a.attrs.get('class') or '').split()) for a in links]
        common = max(set(sets), key=sets.count)
        if sets.count(common) == len(links) - 1:
            odd.add(id(links[next(i for i, c in enumerate(sets) if c != common)]))
    return odd


def has_current(node):
    """A part tree carrying a current-page link, by attribute or by class alone."""
    return has_active(node) or bool(odd_links(node))


def chrome_equivalent(first, other, first_file='', other_file=''):
    """Header/footer trees equal after removing active-link state.

    Ignored: ACTIVE_ATTRIBUTES, ACTIVE_CLASSES, class differences on an element
    marked active (aria-current / data-status="active") on either page, and
    class-set swaps that occur symmetrically between two links (A→B on one,
    B→A on another), and class differences on a link styled apart from its
    sibling links on either page (odd_links: a current link marked by class
    alone). Relative href/src/poster/srcset values compare by resolved target,
    so nested pages (../about.html) match top-level ones (about.html)."""
    swaps = []
    current = odd_links(first) | odd_links(other)
    import posixpath

    def resolved(value, page):
        url = urlsplit(value or '')
        if url.scheme or (value or '').startswith(('#', '//')):
            return value
        path = url.path.lstrip('/') if url.path.startswith('/') else posixpath.join(posixpath.dirname(page), url.path)
        return posixpath.normpath(path) + ('?' + url.query if url.query else '') + ('#' + url.fragment if url.fragment else '')

    def same(a, b):
        if isinstance(a, Node) != isinstance(b, Node):
            return False
        if not isinstance(a, Node):
            return a == b
        def srcset(value, page):
            return ', '.join(' '.join([resolved(part.split()[0], page)] + part.split()[1:]) for part in (value or '').split(',') if part.strip())
        attrs = lambda n, page: {k: resolved(v, page) if k in ('href', 'src', 'poster') else srcset(v, page) if k == 'srcset' else v for k, v in n.attrs.items() if k not in ACTIVE_ATTRIBUTES and k != 'class'}
        if a.tag != b.tag or attrs(a, first_file) != attrs(b, other_file) or len(a.children) != len(b.children):
            return False
        classes_a = set((a.attrs.get('class') or '').split()) - ACTIVE_CLASSES
        classes_b = set((b.attrs.get('class') or '').split()) - ACTIVE_CLASSES
        if classes_a != classes_b and not (is_active_marker(a) or is_active_marker(b) or id(a) in current or id(b) in current):
            swaps.append((frozenset(classes_a - classes_b), frozenset(classes_b - classes_a)))
        return all(same(x, y) for x, y in zip(a.children, b.children))

    if not same(first, other):
        return False
    while swaps:
        added, removed = swaps.pop()
        if (removed, added) not in swaps:
            return False
        swaps.remove((removed, added))
    return True


def css_custom_properties(css):
    """Every custom property declaration as (scope, name, value), in source order.

    scope: 'root' (:root/:host, only inside @layer), 'dark' (.dark,
    [data-theme=dark] or :root inside @media (prefers-color-scheme: dark)) or
    'other' (any other selector or at-rule context)."""
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    found, stack, start, quote, index = [], [], 0, None, 0
    while index < len(css):
        char = css[index]
        if char == '\\':
            index += 1  # escaped character (Tailwind selectors escape quotes and brackets)
        elif quote:
            if char == quote:
                quote = None
        elif char in '"\'':
            quote = char
        elif char == '{':
            prelude = css[start:index].strip()
            kind = 'group' if prelude.startswith('@layer') else 'at' if prelude.startswith('@') else 'rule'
            stack.append((kind, prelude, index))
            start = index + 1
        elif char == ';' and (not stack or stack[-1][0] != 'rule'):
            start = index + 1
        elif char == '}':
            if stack:
                kind, prelude, opened = stack.pop()
                if kind == 'rule':
                    selectors = {re.sub(r'["\'\s]', '', s) for s in prelude.split(',')}
                    context = [(k, p) for k, p, _ in stack]
                    layered = all(k == 'group' for k, _ in context)
                    dark_media = [p for k, p in context if k == 'at']
                    if selectors <= {':root', ':host'} and layered:
                        scope = 'root'
                    elif selectors <= DARK_SELECTORS and layered:
                        scope = 'dark'
                    elif selectors <= {':root', ':host'} and len(dark_media) == 1 and all(k in ('group', 'at') for k, _ in context) and re.fullmatch(r'@media\s*\(\s*prefers-color-scheme\s*:\s*dark\s*\)', dark_media[0]):
                        scope = 'dark'
                    else:
                        scope = 'other'
                    for declaration in split_top(css[opened + 1:index], ';'):
                        name, colon, value = declaration.partition(':')
                        name = name.strip()
                        if colon and re.fullmatch(r'--[A-Za-z0-9_-]+', name):
                            found.append((scope, name, re.sub(r'\s*!important\s*$', '', value.strip())))
            start = index + 1
        index += 1
    return found


DARK_SELECTORS = {'.dark', ':root.dark', 'html.dark', '[data-theme=dark]', ':root[data-theme=dark]', 'html[data-theme=dark]'}


def class_rules(css):
    """Declarations of plain one-class rules (`.name{...}`), outside @media/@supports, by class name."""
    css = re.sub(r'/\*.*?\*/', '', css, flags=re.S)
    found, stack, start, quote, index = {}, [], 0, None, 0
    while index < len(css):
        char = css[index]
        if char == '\\':
            index += 1
        elif quote:
            if char == quote:
                quote = None
        elif char in '"\'':
            quote = char
        elif char == '{':
            prelude = css[start:index].strip()
            stack.append(('group' if prelude.startswith('@layer') else 'at' if prelude.startswith('@') else 'rule', prelude, index))
            start = index + 1
        elif char == ';' and (not stack or stack[-1][0] != 'rule'):
            start = index + 1
        elif char == '}':
            if stack:
                kind, prelude, opened = stack.pop()
                match = re.fullmatch(r'\.((?:[A-Za-z0-9_-]|\\.)+)', prelude)
                if kind == 'rule' and match and all(k == 'group' for k, _, _ in stack):
                    name = re.sub(r'\\(.)', r'\1', match.group(1))
                    for declaration in split_top(css[opened + 1:index], ';'):
                        prop, colon, value = declaration.partition(':')
                        if colon:
                            found.setdefault(name, {})[prop.strip().lower()] = value.strip().lower()
            start = index + 1
        index += 1
    return found


def page_container(entries, rules):
    """The design's content container: the most common class set that bounds
    ordinary page content (a max-width, centred by auto inline margins, with
    its horizontal padding), plus the vertical padding the source's own cart
    or checkout page gives it. WooCommerce's cart, checkout and account
    templates wrap their content in it."""
    def role(name):
        d = rules.get(name, {})
        if d.get('max-width', 'none') not in ('none', '100%', 'initial', 'unset'):
            return 'max'
        if d.get('margin-left') == 'auto' and d.get('margin-right') == 'auto' or d.get('margin-inline') == 'auto' or re.fullmatch(r'\S+\s+auto(\s+\S+)?', d.get('margin', '')):
            return 'auto'
        if 'padding-left' in d and 'padding-right' in d or 'padding-inline' in d:
            return 'inline-pad'
        if 'padding-top' in d and 'padding-bottom' in d or 'padding-block' in d:
            return 'block-pad'
        return None

    def elements(node, depth=0):
        if depth > 3 or not isinstance(node, Node):
            return
        yield node
        for child in node.children:
            yield from elements(child, depth + 1)

    def bounds(node):
        names = (node.attrs.get('class') or '').split()
        roles = {name: role(name) for name in names}
        if 'max' not in roles.values() or 'auto' not in roles.values():
            return None
        return tuple(name for name in names if roles[name] in ('max', 'auto', 'inline-pad'))

    counts = {}
    for entry in entries:
        if entry['kind'] in ('cart', 'checkout', 'product', 'fragment'):
            continue
        for item, part, _ in entry['items']:
            if part:
                continue
            for node in elements(item):
                key = bounds(node)
                if key:
                    counts[key] = counts.get(key, 0) + 1
    if not counts:
        return None
    chosen = max(counts, key=lambda key: counts[key])
    classes = list(chosen)
    for entry in entries:
        if entry['kind'] not in ('cart', 'checkout'):
            continue
        own = next((node for item, part, _ in entry['items'] if not part for node in elements(item) if bounds(node) and set(chosen) <= set(bounds(node))), None)
        if own is not None:
            classes += [name for name in (own.attrs.get('class') or '').split() if role(name) == 'block-pad' and name not in classes]
            break
    return {'tagName': 'div', 'className': ' '.join(classes)}


def css_declarations(css):
    """Custom properties declared on :root/:host (inside @layer only, never @media/@supports)."""
    found = {}
    for scope, name, value in css_custom_properties(css):
        if scope == 'root':
            found.pop(name, None)
            found[name] = value
    return found


def split_top(text, separator):
    """Split on a separator outside quotes and parentheses."""
    pieces, depth, quote, current, escaped = [], 0, None, '', False
    for char in text:
        if escaped or char == '\\':
            escaped = not escaped
            current += char
            continue
        if quote:
            quote = None if char == quote else quote
        elif char in '"\'':
            quote = char
        elif char == '(':
            depth += 1
        elif char == ')':
            depth = max(0, depth - 1)
        elif char == separator and depth == 0:
            pieces.append(current)
            current = ''
            continue
        current += char
    pieces.append(current)
    return pieces


def resolve_var(value, declarations, depth=0):
    """Resolve var() chains within the declaration map; None when unresolvable."""
    if depth > 16:
        return None
    while True:
        start = value.find('var(')
        if start < 0:
            return value.strip()
        level, end = 0, None
        for index in range(start + 3, len(value)):
            if value[index] == '(':
                level += 1
            elif value[index] == ')':
                level -= 1
                if level == 0:
                    end = index
                    break
        if end is None:
            return None
        name, _, fallback = [p.strip() for p in value[start + 4:end].partition(',')]
        if name in declarations:
            replacement = resolve_var(declarations[name], declarations, depth + 1)
        elif fallback:
            replacement = resolve_var(fallback, declarations, depth + 1)
        else:
            replacement = None
        if replacement is None:
            return None
        value = value[:start] + replacement + value[end + 1:]


COLOR_LITERAL = re.compile(r'#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})|(?:rgba?|hsla?|oklch|oklab|lab|lch|hwb)\([0-9a-z.%,/\s+-]*\)')
SIZE_LITERAL = re.compile(r'-?(?:\d+|\d*\.\d+)(?:px|rem|em|%|vh|vw|vmin|vmax|svh|dvh|lvh|ch|ex|pt|cqw|cqh)|0|(?:clamp|min|max|calc)\([0-9a-z.%,+*/\s()-]*\)')


def preset_name(slug):
    return ' '.join(word[:1].upper() + word[1:] for word in slug.split('-'))


def token_literal(name, value):
    """A variation value the compiler accepts: color, length/clamp, numeric tuple or font stack."""
    lower = value.lower()
    if COLOR_LITERAL.fullmatch(lower) or SIZE_LITERAL.fullmatch(lower):
        return True
    if re.fullmatch(r'-?(?:\d+|\d*\.\d+)(?:%|deg)?(?:\s+-?(?:\d+|\d*\.\d+)%?){1,3}', value):
        return True  # shadcn-style "222.2 84% 4.9%" channel tuples
    return name.startswith('--font-') and bool(re.search(r'[A-Za-z]', value)) and not re.search(r'[;{}<>\\]|url\(|var\(', value, re.I)


def design_tokens(dist, styles):
    """(theme.json settings, tokenBridge, styleVariations) from the source custom properties.

    Presets are one-way copies of :root/:host literals. A preset is bridged
    (spec 2 F) when its source variable (following plain var(--x) aliases)
    ends at a literal and every variable on that chain is declared only in
    :root/:host scopes. Dark scopes become a "dark" style variation."""
    declarations, dark, scopes = {}, {}, {}
    for name in styles:
        if not name.endswith('.css'):
            continue
        try:
            path = local_file(dist, name)
        except ValueError:
            continue
        if path.is_file():
            for scope, key, value in css_custom_properties(path.read_text(errors='replace')):
                scopes.setdefault(key, set()).add(scope)
                target = declarations if scope == 'root' else dark if scope == 'dark' else None
                if target is not None:
                    target.pop(key, None)
                    target[key] = value
    palette, families, sizes, sources = [], [], [], []
    seen = {'palette': set(), 'families': set(), 'sizes': set()}
    presets = {'palette': 'color', 'families': 'font-family', 'sizes': 'font-size'}

    def add(target, kind, slug, entry, variable):
        slug = slug.lower()
        if len(target) >= 64 or slug in seen[kind] or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', slug):
            return
        seen[kind].add(slug)
        target.append({'slug': slug, 'name': preset_name(slug), **entry})
        sources.append((presets[kind], slug, variable))

    resolved = {}
    for name in declarations:
        value = resolve_var(declarations[name], declarations)
        if value is not None and 'var(' not in value and 'url(' not in value.lower():
            resolved[name] = value
    # --color-* first so theme tokens win over same-named raw variables.
    for name, value in sorted(resolved.items(), key=lambda item: not item[0].startswith('--color-')):
        lower = value.lower()
        if name.startswith('--tw-') or not COLOR_LITERAL.fullmatch(lower):
            continue
        add(palette, 'palette', name[8:] if name.startswith('--color-') else name[2:], {'color': lower}, name)
    for name, value in resolved.items():
        slug = name[7:] if name.startswith('--font-') else None
        if slug and '--' not in slug and not slug.startswith(('weight', 'feature', 'variation')) and re.search(r'[A-Za-z]', value) and not re.search(r'[;{}<>\\]', value):
            add(families, 'families', slug, {'fontFamily': value}, name)
        slug = name[7:] if name.startswith('--text-') else None
        if slug and '--' not in slug and SIZE_LITERAL.fullmatch(value.lower()):
            # fluid:false keeps bridged sizes exactly the source value.
            add(sizes, 'sizes', slug, {'size': value, 'fluid': False}, name)
    settings = {}
    if palette:
        settings['color'] = {'palette': palette}
    if families or sizes:
        settings['typography'] = {**({'fontFamilies': families} if families else {}), **({'fontSizes': sizes} if sizes else {})}

    def holder(name):
        chain = []
        while name in declarations and scopes.get(name) == {'root'} and name not in chain:
            chain.append(name)
            alias = re.fullmatch(r'var\(\s*(--[A-Za-z0-9_-]+)\s*\)', declarations[name].strip())
            if not alias:
                return name if 'var(' not in declarations[name] and not name.startswith('--wp-') else None
            name = alias.group(1)
        return None
    bridge, bridged = [], set()
    for preset, slug, name in sources:
        variable = holder(name)
        if variable and variable not in bridged:
            bridged.add(variable)
            bridge.append({'preset': preset, 'slug': slug, 'variable': variable})
    variations = []
    if dark:
        merged = {**declarations, **dark}
        variables = {}
        for name, value in dark.items():
            value = resolve_var(value, merged)
            if value is not None and token_literal(name, value):
                variables[name] = value
        # Preset source variables whose value changes under the dark scope, so
        # the compiler can override the matching palette entry by name.
        for preset, slug, name in sources:
            value = resolve_var(declarations[name], merged)
            if name not in variables and value is not None and value != resolved.get(name) and token_literal(name, value):
                variables[name] = value
        if variables:
            variations.append({'slug': 'dark', 'title': 'Dark', 'variables': dict(list(variables.items())[:256])})
    return settings, bridge, variations


def frame_shell(frame, head, main, tail, before=(), after=()):
    """The compiler's /2 page shell with explicit content (spec 1)."""
    if frame and frame.get('main'):
        main_block = {'name': 'h2wp/element', 'attributes': dict(frame['main']), 'innerBlocks': list(main)}
    else:
        main_block = {'name': 'core/group', 'attributes': {'tagName': 'main', 'layout': {'type': 'default'}}, 'innerBlocks': list(main)}
    inner = list(head) + [main_block] + list(tail)
    if frame and frame.get('wrapper'):
        inner = [{'name': 'h2wp/element', 'attributes': dict(frame['wrapper']), 'innerBlocks': inner}]
    return list(before) + inner + list(after)


def article_template(entry, article, frame):
    """Spec 2 D5: templates.single from the first post's template-owned sections."""
    mapper = entry['mapper']
    zones = {'head': [], 'main': [], 'tail': []}
    mapper.overrides, mapper.query_namespace = dict(article['overrides']), 'h2wp/related-posts'
    try:
        for index, (child, part, place) in enumerate(entry['items']):
            if place not in zones or (not part and index not in article['owned']):
                continue
            mapper.section = entry['sections'][index]
            if part:
                zones[place].append({'name': 'core/template-part', 'attributes': {'slug': part}})
                continue
            block = mapper.block(child)
            if block:
                name = mapper.heading_name(child)
                if name:
                    block['attributes'] = {**block.get('attributes', {}), 'metadata': {'name': name}}
                zones[place].append(block)
    finally:
        mapper.overrides, mapper.query_namespace = {}, None
    return frame_shell(frame, zones['head'], zones['main'], zones['tail'])


def listing_template(entry, frame):
    """Spec 2 D6: templates.home/archive from the posts page with an inherited listing query."""
    def blocks(tree):
        for block in tree:
            yield block
            yield from blocks(block.get('innerBlocks', []))
    zones = {'before': [], 'head': [], 'main': [], 'tail': [], 'after': []}
    for block, place in entry['mapped']:
        zones[place].append(json.loads(json.dumps(block)))
    queries = [b for zone in zones.values() for b in blocks(zone) if b['name'] == 'core/query' and not b['attributes'].get('namespace')]
    if not queries:
        return None
    listing = max(queries, key=lambda b: b['attributes']['query'].get('perPage', 0))
    query = dict(listing['attributes']['query'])
    # The main query cannot skip posts; a listing below a lead article keeps
    # its own query (WordPress applies an offset only with a page size, so
    # it keeps the source's card count too).
    if not query.get('offset'):
        query.pop('perPage', None)
        query['inherit'] = True
    listing['attributes']['query'] = query
    for zone in zones.values():
        for block in blocks(zone):
            block.pop('sourceIds', None)
    return frame_shell(frame, zones['head'], zones['main'], zones['tail'], zones['before'], zones['after'])



# ---- WooCommerce templates from the shop and product pages -------------------
# The shop's listing and one product page are the design of WooCommerce's
# Product Catalog (archive-product) and single-product templates: the same
# markup with WooCommerce's live blocks where the pages print one product's
# data. Read from the pages and the manifest's shop.* hints (the regions
# build-products reads), never from class names.
MONEY = re.compile(r'(?:[^\d\s.,]{1,4}\s?\d[\d.,\s]*|\d[\d.,\s]*\s?[^\d\s.,]{1,4})')
BUY = re.compile(r'add to (?:cart|bag|basket|tote)|buy now', re.I)
SOLD_OUT = re.compile(r'sold\s*out|out of stock|unavailable', re.I)
LOW_STOCK = re.compile(r'\bonly\b|\bleft\b|available|in stock|remaining', re.I)
SPEC_LINE = re.compile(r'[^:]{2,24}:?')


def hint_matches(node, selector):
    """A manifest shop.* hint (`tag.class.class`, CSS-escaped classes such as
    `lg\\:pt-6` allowed; the last segment of a `>` path) against one node."""
    if not isinstance(node, Node) or not selector:
        return False
    segment = re.split(r'\s*>\s*', selector.strip())[-1]
    parts = [p.replace('\\', '') for p in re.split(r'(?<!\\)\.', segment)]
    tag, classes = parts[0], [c for c in parts[1:] if c]
    return (not tag or node.tag == tag) and set(classes) <= set((node.attrs.get('class') or '').split())


def first_hint(root, selector):
    return next((n for n in walk(root) if isinstance(n, Node) and hint_matches(n, selector)), None) if selector else None


def leaves(node):
    return [n for n in walk(node) if isinstance(n, Node) and is_leaf(n)]


def money_only(node):
    found = leaves(node)
    return bool(found) and all(MONEY.fullmatch(norm(plain(n))) for n in found)


def struck(node):
    return node.tag in ('del', 's', 'strike') or 'line-through' in (node.attrs.get('class') or '').split()


def parents_of(root):
    parents = {}
    for n in walk(root):
        if isinstance(n, Node):
            for c in n.children:
                if isinstance(c, Node):
                    parents[id(c)] = n
    return parents


def classes_of(node):
    return node.attrs.get('class', '') if isinstance(node, Node) else ''


def price_block(node, shown, extra):
    """WooCommerce's price in the design: the amount's own classes on the
    block, inside the row element that holds the amounts (when there is one)."""
    price = {'name': 'woocommerce/product-price', 'attributes': {**extra, **({'className': classes_of(shown)} if classes_of(shown) else {})}}
    if node is shown:
        return price
    return {'name': 'core/group', 'attributes': {'tagName': node.tag, **({'className': classes_of(node)} if classes_of(node) else {}), 'layout': {'type': 'default'}}, 'innerBlocks': [price]}


class Woo:
    """What the shop's pages say about its products, for the two templates."""

    def __init__(self, entries, manifest):
        self.shop = manifest.get('shop') or {}
        self.products = {e['key']: e for e in entries if e['kind'] == 'product'}
        self.meta = {}
        for key, entry in self.products.items():
            main = next((n for n, part, place in entry['items'] if not part and place == 'main' and isinstance(n, Node)), None)
            category = first_hint(entry['root'], self.shop.get('productCategory'))
            self.meta[key] = {'name': entry['h1'], 'category': norm(plain(category)) if category is not None else ''}
        self.categories = sorted({m['category'] for m in self.meta.values() if m['category']})

    def target(self, mapper, node):
        if not isinstance(node, Node) or node.tag != 'a' or not node.attrs.get('href'):
            return None
        target = mapper.resolve_quiet(node.attrs['href']).split('#')[0]
        return target[5:] if target.startswith('page:') and target[5:] in self.products else None

    def card_keys(self, mapper, node, exclude=None):
        """Product keys when node's children are >= 2 cards, each linking to one distinct product."""
        if not isinstance(node, Node) or node.tag in ('a', 'svg') or any(isinstance(c, str) and c.strip() for c in node.children):
            return None
        cards = [c for c in node.children if isinstance(c, Node)]
        if len(cards) < 2:
            return None
        keys = []
        for card in cards:
            found = {self.target(mapper, n) for n in walk(card) if isinstance(n, Node)} - {None}
            if len(found) != 1:
                return None
            keys.append(found.pop())
        return keys if len(set(keys)) == len(keys) and exclude not in keys else None

    def card_template(self, mapper, container, keys, loop_attr):
        """woocommerce/product-template around the first card, with WooCommerce's
        live data where the card prints its product's."""
        card = next(c for c in container.children if isinstance(c, Node))
        meta = self.meta[keys[0]]
        parents = parents_of(card)
        overrides = {}

        def group(tag):
            def build(node):
                attrs, _ = mapper.attrs(node)
                inner = [b for b in (mapper.block(c) for c in node.children) if b]
                return {'name': 'core/group', 'attributes': {'tagName': tag, **({'className': attrs['className']} if attrs.get('className') else {}), 'layout': {'type': 'default'}}, 'innerBlocks': inner}
            return build
        image = next((n for n in walk(card) if isinstance(n, Node) and n.tag == 'img'), None)
        for n in walk(card):
            if not isinstance(n, Node):
                continue
            if self.target(mapper, n):
                # The card's own link: WooCommerce's picture and title link instead.
                overrides[id(n)] = {'woo': group('div')}
            elif n is image:
                overrides[id(n)] = {'woo': lambda node: {'name': 'core/post-featured-image', 'attributes': {'isLink': True, 'sizeSlug': 'full', 'className': ('h2wp-source-image ' + classes_of(node)).strip()}}}
            elif n.tag in HEADINGS and norm(plain(n)) == meta['name']:
                overrides[id(n)] = {'woo': lambda node: {'name': 'core/post-title', 'attributes': {'level': int(node.tag[1]), 'isLink': True, **({'className': classes_of(node)} if classes_of(node) else {})}}}
            elif is_leaf(n) and meta['category'] and norm(plain(n)) == meta['category']:
                overrides[id(n)] = {'woo': terms_block}
            elif money_only(n) and not money_only(parents.get(id(n))):
                shown = next((x for x in leaves(n) if not struck(x)), leaves(n)[0])
                overrides[id(n)] = {'woo': lambda node, shown=shown: price_block(node, shown, {loop_attr: True})}
        previous, queries = dict(mapper.overrides), mapper.queries
        mapper.overrides.update(overrides)
        mapper.queries = False
        try:
            block = mapper.block(card)
        finally:
            mapper.overrides, mapper.queries = previous, queries
        return {'name': 'woocommerce/product-template', 'attributes': {'className': ('h2wp-source-layout ' + classes_of(container)).strip()}, 'innerBlocks': [block] if block else []}

    def collection(self, mapper, container, keys, query, extra=None, around=None):
        template = self.card_template(mapper, container, keys, 'isDescendentOfQueryLoop')
        mapper.counter[0] += 1
        attrs = {'queryId': mapper.counter[0], 'query': query, 'displayLayout': {'type': 'list'}, **(extra or {})}
        if around is None:
            return {'name': 'woocommerce/product-collection', 'attributes': attrs, 'innerBlocks': [template]}
        # The collection is the whole section (heading and links beside the
        # cards): WooCommerce drops an empty collection, heading and all.
        inner = [template if c is container else mapper.block(c) for c in around.children if isinstance(c, Node) or (isinstance(c, str) and c.strip())]
        return {'name': 'woocommerce/product-collection', 'attributes': {**attrs, 'tagName': around.tag, **({'className': classes_of(around)} if classes_of(around) else {})}, 'innerBlocks': [b for b in inner if b]}


INLINE_TAGS = {'span', 'a', 'small', 'em', 'strong', 'b', 'i', 'time', 'label', 'code'}


def terms_block(node):
    """The product's category (post-terms, a <div>): inline where the source
    printed it inline (a breadcrumb's last crumb)."""
    names = (classes_of(node) + (' h2wp-inline' if node.tag in INLINE_TAGS else '')).strip()
    return {'name': 'core/post-terms', 'attributes': {'term': 'product_cat', **({'className': names} if names else {})}}


def sort_block(node, rules):
    """WooCommerce's catalog sorting as the design's <select>. WooCommerce
    makes its select inherit the font size, so the select's own font-size
    classes (read from the stylesheet) ride on a box-less wrapper instead."""
    block = {'name': 'woocommerce/catalog-sorting', 'attributes': {'className': ('h2wp-source-control ' + classes_of(node)).strip()}}
    sized = [c for c in classes_of(node).split() if 'font-size' in (rules.get(c) or {})]
    if not sized:
        return block
    return {'name': 'core/group', 'attributes': {'tagName': 'div', 'className': ' '.join(sized) + ' h2wp-contents', 'layout': {'type': 'default'}}, 'innerBlocks': [block]}


def woo_listing_template(woo, entry, frame, menus, rules=None):
    """templates.archive-product from the shop page: its product grid becomes a
    product collection of the design's card, its sort <select> WooCommerce's
    catalog sorting, and a row of category filters the category menu."""
    mapper, rules = entry['mapper'], rules or {}
    overrides, grid, sort = {}, [None], None
    total = max(len(woo.products), len((woo.shop.get('products') or [])))
    for child, part, place in entry['items']:
        if part or not isinstance(child, Node):
            continue
        for n in walk(child):
            if not isinstance(n, Node):
                continue
            keys = woo.card_keys(mapper, n) if grid[0] is None else None
            if keys:
                grid[0] = n
                overrides[id(n)] = {'woo': lambda node, keys=keys: woo.collection(mapper, node, keys, {'perPage': max(total, len(keys)), 'pages': 0, 'offset': 0, 'postType': 'product', 'order': 'asc', 'orderBy': 'menu_order', 'inherit': True, 'isProductCollectionBlock': True})}
            elif n.tag == 'select' and sort is None:
                sort = n
                overrides[id(n)] = {'woo': lambda node: sort_block(node, rules)}
            elif woo.categories and n.tag not in ('select',) and id(n) not in overrides:
                items = [c for c in n.children if isinstance(c, Node)]
                labels = [norm(plain(c)) for c in items]
                named = [l for l in labels if l in woo.categories]
                if len(items) >= 3 and len(named) >= 2 and len(named) >= len(items) - 1 and len({shallow(c)[0] for c in items}) == 1 and all(is_leaf(c) for c in items):
                    menu = {'key': 'shop-categories', 'name': 'Shop categories', 'items': [{'label': plain(c).strip(), 'url': 'category:' + quote(l) if l in woo.categories else 'page:' + entry['key']} for c, l in zip(items, labels)]}
                    counts = {}
                    for c in items:
                        counts[classes_of(c)] = counts.get(classes_of(c), 0) + 1
                    idle = max(counts, key=counts.get)
                    active = next((classes_of(c) for c in items if classes_of(c) != idle), idle)
                    menus.append(menu)
                    overrides[id(n)] = {'woo': lambda node, idle=idle, active=active: {'name': 'core/group', 'attributes': {'tagName': node.tag, **({'className': classes_of(node)} if classes_of(node) else {}), 'layout': {'type': 'default'}}, 'innerBlocks': [{'name': 'h2wp/navigation', 'attributes': {'menu': 'shop-categories', 'linkClassName': idle, 'currentClassName': active}}]}}
    if grid[0] is None:
        return None
    zones = {'before': [], 'head': [], 'main': [], 'tail': [], 'after': []}
    mapper.overrides = overrides
    try:
        for index, (child, part, place) in enumerate(entry['items']):
            if part:
                zones[place].append({'name': 'core/template-part', 'attributes': {'slug': part}})
                continue
            mapper.section = entry['sections'][index]
            block = mapper.block(child)
            if block:
                zones[place].append(block)
    finally:
        mapper.overrides = {}
    return strip_sources(frame_shell(frame, zones['head'], zones['main'], zones['tail'], zones['before'], zones['after']))


def strip_sources(tree):
    for block in tree:
        block.pop('sourceIds', None)
        strip_sources(block.get('innerBlocks', []))
    return tree


def product_regions(woo, entry):
    """The regions of a product page WooCommerce draws ({id: kind}) and what
    the page shows in them; None when the page lacks its title."""
    mapper, root, shop = entry['mapper'], entry['root'], woo.shop
    main = next((n for n in walk(root) if isinstance(n, Node) and hint_matches(n, shop.get('productMain') or 'main')), root)
    parents = parents_of(root)
    regions = {}
    title = next((n for n in walk(main) if isinstance(n, Node) and n.tag == 'h1'), None)
    if title is None:
        return None
    regions[id(title)] = ('title', title)
    related = None
    for n in walk(main):
        if isinstance(n, Node) and id(n) not in regions and woo.card_keys(mapper, n, exclude=entry['key']):
            around = parents.get(id(n))
            heading = around is not None and any(isinstance(c, Node) and c is not n and any(isinstance(x, Node) and x.tag in HEADINGS for x in walk(c)) for c in around.children)
            related = (around if heading else n, n)
            regions[id(related[0])] = ('related', related)
            break
    inside_related = {id(x) for x in walk(related[0])} if related else set()
    price = first_hint(main, shop.get('productPrice'))
    if price is not None and id(price) not in inside_related:
        regions[id(price)] = ('price', price)
    body = first_hint(main, shop.get('productBody'))
    if body is not None:
        regions[id(body)] = ('body', body)
        # The spec lines right after it are the description's (compiler).
        siblings = [c for c in parents[id(body)].children if isinstance(c, Node)] if id(body) in parents else []
        for sibling in siblings[siblings.index(body) + 1:] if body in siblings else []:
            spans = [c for c in sibling.children if isinstance(c, Node)]
            if sibling.tag == 'p' and len(spans) == 1 and spans[0].tag == 'span' and sibling.children and sibling.children[0] is spans[0] and SPEC_LINE.fullmatch(norm(plain(spans[0]))) and norm(plain(sibling)) != norm(plain(spans[0])):
                regions[id(sibling)] = ('spec', sibling)
                continue
            break
    category = first_hint(main, shop.get('productCategory'))
    meta = woo.meta[entry['key']]
    for n in walk(main):
        if isinstance(n, Node) and id(n) not in inside_related and is_leaf(n) and id(n) not in regions and (n is category or (meta['category'] and norm(plain(n)) == meta['category'])):
            regions[id(n)] = ('category', n)
    button = next((n for n in walk(main) if isinstance(n, Node) and n.tag == 'button' and id(n) not in inside_related and (BUY.search(plain(n)) or SOLD_OUT.search(plain(n)))), None)
    if button is not None and id(button) in parents:
        row = [c for c in parents[id(button)].children if isinstance(c, Node)]
        at = row.index(button)
        start = at
        controls = lambda c: any(isinstance(x, Node) and (x.tag in ('button', 'select') or (x.tag == 'input' and x.attrs.get('type') in ('number', 'text', None))) for x in walk(c))
        while start > 0 and controls(row[start - 1]) and id(row[start - 1]) not in regions:
            start -= 1
        end = at
        if end + 1 < len(row) and is_leaf(row[end + 1]) and LOW_STOCK.search(plain(row[end + 1])) and re.search(r'\d', plain(row[end + 1])):
            end += 1
        regions[id(row[start])] = ('buy', row[start])
        for c in row[start + 1:end + 1]:
            regions[id(c)] = ('drop', c)
    thumbs = next((n for n in walk(main) if isinstance(n, Node) and id(n) not in inside_related and len([c for c in n.children if isinstance(c, Node) and c.tag == 'button' and sum(1 for x in walk(c) if isinstance(x, Node) and x.tag == 'img') == 1]) >= 2), None)
    if thumbs is not None and id(thumbs) in parents:
        regions[id(parents[id(thumbs)])] = ('gallery', parents[id(thumbs)])
    else:
        image = next((n for n in walk(main) if isinstance(n, Node) and n.tag == 'img' and id(n) not in inside_related), None)
        if image is not None:
            regions[id(image)] = ('gallery', image)
    return regions


def product_signature(entry, regions):
    """The page's markup with WooCommerce's regions left out: what every
    product page must share for one template to draw them all."""
    def sig(node):
        if not isinstance(node, Node):
            return norm(node)
        if id(node) in regions:
            return '<' + regions[id(node)][0] + '>'
        # The buy controls a page has (a chooser, a stock note) are one region.
        return [node.tag, sorted((k, v) for k, v in node.attrs.items() if not k.startswith('data-spa')), [s for s in (sig(c) for c in node.children) if s and s != '<drop>']]
    return json.dumps([sig(child) for child, part, place in entry['items'] if not part])


def woo_product_template(woo, entries, frame, page_finding):
    """templates.single-product from the product pages, or None (a
    product-template-variant finding) when their shared markup differs."""
    products = [e for e in entries if e['kind'] == 'product']
    if not products:
        return None
    measured = [(e, product_regions(woo, e)) for e in products]
    if any(r is None for _, r in measured):
        return None
    first, regions = measured[0]
    base = product_signature(first, regions)
    differ = [e['key'] for e, r in measured[1:] if product_signature(e, r) != base]
    if differ:
        for key in differ:
            page_finding(key, 'product-template-variant', '', 'Product page markup outside the WooCommerce regions differs from ' + first['key'] + '; no single-product template was derived')
        return None
    mapper = first['mapper']
    kinds = {k: v for k, v in regions.items()}

    def replace(kind, node):
        attrs = {'className': classes_of(node)} if classes_of(node) else {}
        if kind == 'title':
            return {'name': 'core/post-title', 'attributes': {'level': 1, **attrs}}
        if kind == 'category':
            return terms_block(node)
        if kind == 'body':
            return {'name': 'core/post-content', 'attributes': {'className': 'h2wp-contents', 'layout': {'type': 'default'}}}
        if kind in ('spec', 'drop'):
            return None
        if kind == 'price':
            shown = next((x for x in leaves(node) if not struck(x)), None) or node
            block = price_block(node, shown, {'isDescendentOfSingleProductTemplate': True})
            price = block if block['name'] == 'woocommerce/product-price' else block['innerBlocks'][0]
            # The row lays out the amounts (sale beside regular): the theme
            # draws them into it (gutenberg-frontend.php).
            if price is not block:
                price['attributes']['className'] = (price['attributes'].get('className', '') + ' h2wp-price-in-row').strip()
            return block
        if kind == 'buy':
            return {'name': 'woocommerce/add-to-cart-form', 'attributes': {'className': ((classes_of(node) + ' ') if classes_of(node) else '') + 'h2wp-add-to-cart'}}
        if kind == 'gallery':
            return {'name': 'woocommerce/product-image-gallery', 'attributes': {}}
        around, container = node
        keys = woo.card_keys(mapper, container, exclude=first['key'])
        return woo.collection(mapper, container, keys, {'perPage': len(keys), 'pages': 0, 'offset': 0, 'postType': 'product', 'order': 'asc', 'orderBy': 'post__in', 'inherit': False, 'isProductCollectionBlock': True},
                              {'collection': 'woocommerce/product-collection/upsells'}, around if around is not container else None)
    mapper.overrides = {k: {'woo': (lambda node, kind=kind, value=value: replace(kind, value))} for k, (kind, value) in kinds.items()}
    zones = {'before': [], 'head': [], 'main': [], 'tail': [], 'after': []}
    try:
        for index, (child, part, place) in enumerate(first['items']):
            if part:
                zones[place].append({'name': 'core/template-part', 'attributes': {'slug': part}})
                continue
            mapper.section = first['sections'][index]
            block = mapper.block(child)
            if block:
                zones[place].append(block)
    finally:
        mapper.overrides = {}
    # Reviews show once a product has one (the theme), so a design without a
    # reviews section is unchanged until then.
    reviews = {'name': 'woocommerce/product-reviews', 'attributes': {}, 'innerBlocks': [{'name': 'woocommerce/product-reviews-title', 'attributes': {}},
        {'name': 'woocommerce/product-review-template', 'attributes': {}, 'innerBlocks': [{'name': n, 'attributes': {}} for n in ('woocommerce/product-review-author-name', 'woocommerce/product-review-rating', 'woocommerce/product-review-date', 'woocommerce/product-review-content')]},
        {'name': 'woocommerce/product-review-form', 'attributes': {}}]}
    main = zones['main']
    at = next((i for i, b in enumerate(main) if any(x['name'] == 'woocommerce/add-to-cart-form' for x in walk_blocks([b]))), len(main) - 1)
    main.insert(at + 1, reviews)
    return strip_sources(frame_shell(frame, zones['head'], main, zones['tail'], zones['before'], zones['after']))


def walk_blocks(tree):
    for block in tree:
        yield block
        yield from walk_blocks(block.get('innerBlocks', []))

def site_title(blocks, front, name):
    """Spec 2 D7: the header brand link text -> bind:siteTitle. Returns extra inline styles."""
    name = norm(name)
    extra_styles = {}
    if not name or not front:
        return extra_styles

    def visit(block, upper):
        attrs = block.get('attributes', {})
        upper = upper or 'uppercase' in (attrs.get('className') or '').split()
        if block['name'] == 'h2wp/element' and attrs.get('tagName') == 'a' and (attrs.get('htmlAttributes') or {}).get('href') == 'page:' + front:
            kids = block.get('innerBlocks') or []
            leaf = block if 'text' in attrs and not kids else kids[0] if len(kids) == 1 and kids[0]['name'] == 'h2wp/element' and 'text' in kids[0]['attributes'] and not kids[0].get('innerBlocks') else None
            if leaf is not None and not leaf['attributes'].get('bind'):
                text = norm(leaf['attributes']['text'])
                caps = upper or 'uppercase' in (leaf['attributes'].get('className') or '').split()
                if text == name or (caps and text.lower() == name.lower()):
                    leaf['attributes']['bind'] = 'siteTitle'
                    return
                # Brand typed in capitals ("NOVAK SWIM" for "Novak Swim"):
                # render the site title uppercase so the frontend stays 1:1.
                if text == name.upper() and text != name:
                    style = 'text-transform:uppercase'
                    klass = 'h2wp-inline-' + digest(style)[:16]
                    leaf['attributes']['bind'] = 'siteTitle'
                    leaf['attributes']['className'] = ((leaf['attributes'].get('className') or '') + ' ' + klass).strip()
                    extra_styles[klass] = style
                    return
        for child in block.get('innerBlocks', []):
            visit(child, upper)
    for block in blocks:
        visit(block, False)
    return extra_styles


# Web-font stylesheets of font services; the theme loads them as the source does.
FONT_STYLESHEET = re.compile(r'https://(fonts\.googleapis\.com/css2?|fonts\.bunny\.net/css2?|use\.typekit\.net/[a-z0-9]+\.css)(\?[^\s"\'<>\\]*)?')
RECORDER_RUNTIME = b'/* spa-runtime.js \xe2\x80\x94 generated by html2wp-sub prerender-spa.py.'


# A page the manifest marks self-contained keeps its complete shell as
# content; this template renders it with no shared header/footer parts.
SELF_CONTAINED_TEMPLATE = 'page-self-contained'


# A style attribute moved into a generated class keeps (most of) the
# attribute's precedence: the class is repeated, so it outranks the source's
# own selectors (`.split-lead img{aspect-ratio:3/4}`, 0,1,1, beat an image's
# style="aspect-ratio:1/1" moved to a 0,1,0 class, and the image drew 181px
# taller). Not !important: a script that opens a panel by setting its style
# attribute must still win, as it did over the source's own attribute.
INLINE_WEIGHT = 3


def page_styles(entries):
    """Split each page's head stylesheets (source order) into the part EVERY
    page loads and the part only it loads.

    contract.styles loads everywhere and before a page's own sheets, so only
    the longest common PREFIX of all pages' lists may go there — anything
    else would change a page's cascade. A site whose pages each carry their
    own <style> (one design per home-page variant, a typography sheet only
    some pages load) keeps every page's list intact and in order."""
    lists = [e.get('sheets', []) for e in entries]
    shared = []
    for group in zip(*lists) if lists else []:
        if len(set(group)) != 1:
            break
        shared.append(group[0])
    return shared, {e['key']: e.get('sheets', [])[len(shared):] for e in entries}


def recorder_runtime(dist, path):
    """The interaction runtime html2wp's prerender wrote (not a source script)."""
    try:
        target = local_file(dist, path)
        return target.is_file() and target.read_bytes().startswith(RECORDER_RUNTIME)
    except ValueError:
        return False


NAV_HREF = re.compile(r'page:[^\s?#]+(\?[^\s#]*)?(#[^\s]*)?|category:[A-Za-z0-9%._~-]+|#[A-Za-z][\w-]*')
# Utilities that style an element through its children or its position among
# siblings; WordPress's navigation markup changes both.
NAV_STRUCTURAL = re.compile(r'(^|:)(space-[xy]|divide-[xy]?)|(^|:)\*:|\[&|(^|:)(first|last|odd|even|only|nth-[a-z-]*)(-of-type)?:')


def nav_link(block):
    """(classes, label HTML, url) of a plain link block, or None."""
    a = block.get('attributes') or {}
    if block.get('innerBlocks'):
        return None
    if block['name'] == 'h2wp/element' and a.get('tagName') == 'a' and set(a) <= {'className', 'tagName', 'htmlAttributes', 'text', 'metadata'}:
        attrs, text = a.get('htmlAttributes') or {}, a.get('text') or ''
        if set(attrs) == {'href'} and text.strip() and NAV_HREF.fullmatch(attrs['href']):
            return a.get('className') or '', html.escape(text, quote=False), attrs['href']
    if block['name'] == 'core/list-item' and set(a) <= {'content', 'metadata'}:
        match = re.fullmatch(r'<a\s+([^<>]*)>([^<>]*)</a>', (a.get('content') or '').strip())
        attrs = dict(re.findall(r'([a-z][a-z-]*)="([^"]*)"', match[1])) if match else {}
        if match and 'href' in attrs and set(attrs) <= {'href', 'class'} and match[2].strip() and NAV_HREF.fullmatch(html.unescape(attrs['href'])):
            return attrs.get('class', ''), match[2], html.unescape(attrs['href'])
    return None


def nav_item(block):
    """(item classes or None for a bare link, link) of a navigation candidate."""
    a = block.get('attributes') or {}
    if block['name'] == 'core/list-item':
        link = nav_link(block)
        return ('', link) if link else None
    if block['name'] == 'h2wp/element' and a.get('tagName') == 'li' and set(a) <= {'className', 'tagName', 'metadata'} and len(block.get('innerBlocks') or []) == 1:
        link = nav_link(block['innerBlocks'][0])
        return (a.get('className') or '', link) if link else None
    link = nav_link(block)
    return (None, link) if link else None


def variant_parts(part, groups, kinds, is_current):
    """Which variant of a header/footer each page renders.

    `groups` are the pages' equivalent sources, [[(key, source), ...], ...] in
    first-seen order. The variant most pages use is the shared part (a tie
    goes to the first seen), owned by a member without a current link so no
    page's active state is baked in. Another variant becomes its own part
    (`header-2`, …) for the front and ordinary pages using it, which render it
    through their own template; a variant used only by pages whose templates
    are shared with the rest (posts, shop, products) cannot, and those pages
    are `replaced` by the shared part. Returns {'parts': [(slug, owner key,
    member keys)], 'replaced': [keys]}."""
    ranked = sorted(groups, key=len, reverse=True)
    owner = lambda members: next((k for k, source in members if not is_current(source)), members[0][0])
    parts, replaced = [(part, owner(ranked[0]), [k for k, _ in ranked[0]])], []
    for group in ranked[1:]:
        own = [(k, source) for k, source in group if kinds.get(k) in ('front', 'page')]
        if own:
            parts.append((part + '-' + str(len(parts) + 1), owner(own), [k for k, _ in own]))
        replaced += [k for k, _ in group if kinds.get(k) not in ('front', 'page')]
    return {'parts': parts, 'replaced': replaced}


def link_states(to_url, owner, others):
    """[(text, url, own classes, normal classes, current classes or None)] for every <a> of
    a shared part, in document order. `others` are the same part on the other
    pages (structurally equal, see chrome_equivalent): a link's classes where
    it is not marked active are its normal classes, where it is marked active
    its current-page classes."""
    def anchors(node, path=()):
        if isinstance(node, Node):
            if node.tag == 'a':
                yield path, node
            for index, child in enumerate(node.children):
                yield from anchors(child, path + (index,))
    seen = {}
    for tree in [owner, *others]:
        current = odd_links(tree)
        for path, node in anchors(tree):
            seen.setdefault(path, []).append((is_active_marker(node) or id(node) in current, (node.attrs.get('class') or '').split()))
    common = lambda lists: max(lists, key=lists.count) if lists else None
    return [(norm(plain(node)), to_url(node.attrs.get('href', '')), (node.attrs.get('class') or '').split(), common([c for active, c in seen[path] if not active]) or (node.attrs.get('class') or '').split(), common([c for active, c in seen[path] if active]))
            for path, node in anchors(owner)]


def resets_lists(css):
    """True when a stylesheet zeroes <ul> margins and padding (a CSS reset rule
    for `ul` or `*`), so a list can stand in for another link container."""
    for selectors, body in re.findall(r'([^{}]+)\{([^{}]*)\}', re.sub(r'/\*.*?\*/', '', css, flags=re.S)):
        names = {s.strip() for s in selectors.split(',')}
        body = re.sub(r'\s+', '', body)
        if names & {'ul', '*'} and re.search(r'(^|;)margin:0(px)?(;|$)', body) and re.search(r'(^|;)padding:0(px)?(;|$)', body):
            return True
    return False


def navigation_menus(parts, sources, manifest_nav, lists_reset=True):
    """Turn the link groups of the shared parts into editable navigation.

    A group is a run of at least two plain page/anchor links (or list items
    each holding one) sharing one normal class set. It becomes h2wp/navigation
    bound to a menu; identical link sequences (desktop nav and mobile drawer)
    share one menu, so an edit in Site Editor applies everywhere. When the
    group is the whole content of its container, WordPress's list takes the
    container's classes; otherwise the navigation sits transparently in the
    container (a non-list container only when the source CSS resets lists,
    `lists_reset`). `sources` maps a part to its link_states(); the part blocks
    were mapped from the first page, whose own current link keeps its active
    classes there. Returns (menus, skipped group labels)."""
    menus, keys, skipped, states = [], set(), [], {}

    def link_blocks(block):
        if nav_link(block):
            yield block
        for child in block.get('innerBlocks') or []:
            yield from link_blocks(child)
    # Pair link blocks with their source links (both in document order; the
    # block keeps the classes the link had on the page it was mapped from).
    # Links that did not become plain link blocks are skipped.
    for part, links in sources.items():
        queue = iter(links)
        for block in (b for root in parts.get(part) or [] for b in link_blocks(root)):
            own_classes, label, url = nav_link(block)
            for text, source_url, own, normal, current in queue:
                if text == norm(html.unescape(re.sub(r'<[^>]+>', '', label))) and source_url == url and own == own_classes.split():
                    states[id(block)] = (normal, current)
                    break

    def classes_of(link_block):
        return states.get(id(link_block), ((nav_link(link_block)[0] or '').split(), None))

    def menu_for(part, items, heading):
        sequence = [(i['label'], i['url']) for i in items]
        for menu in menus:
            if [(i['label'], i['url']) for i in menu['items']] == sequence:
                return menu['key']
        labels = [norm(html.unescape(label)) for label, _ in sequence]
        name = next((n.get('label') for n in manifest_nav if isinstance(n, dict) and n.get('region') == part and [norm(l.get('text', '')) for l in n.get('links') or [] if isinstance(l, dict)] == labels and n.get('label')), None)
        name = name or (part.capitalize() + (': ' + heading if heading else ' menu'))
        base = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-') or 'menu'
        key, n = base, 2
        while key in keys:
            key, n = base + '-' + str(n), n + 1
        keys.add(key)
        menus.append({'key': key, 'name': name, 'items': items})
        return key

    def heading_text(block):
        a = block.get('attributes') or {}
        text = a.get('text') if block['name'] == 'h2wp/element' and not block.get('innerBlocks') else a.get('content') if block['name'] in ('core/heading', 'core/paragraph') else None
        text = norm(re.sub(r'<[^>]+>', '', html.unescape(text or '')))
        return text if 0 < len(text) <= 40 else ''

    def link_of(block):
        return block['innerBlocks'][0] if block['name'] == 'h2wp/element' and (block.get('attributes') or {}).get('tagName') == 'li' else block

    def same(x, y):
        return x and y and x[0] == y[0] and set(classes_of(x[2])[0]) == set(classes_of(y[2])[0])

    def heading_before(children, index):
        return next((t for t in (heading_text(c) for c in reversed(children[:index])) if t), '')

    def visit(block, part, above=''):
        # A list nested in a list item is a submenu: WordPress's list-item
        # block holds only lists, so it stays as source markup.
        if block['name'] == 'core/list-item' or (block.get('attributes') or {}).get('tagName') == 'li':
            return None
        children = block.get('innerBlocks') or []
        a = block.get('attributes') or {}
        listy = block['name'] == 'core/list' or a.get('tagName') in ('ul', 'ol')
        i = 0
        while i < len(children):
            item_of = lambda block: (lambda found: found and (found[0], found[1], link_of(block)))(nav_item(block))
            first, j = item_of(children[i]), i + 1
            while first and j < len(children) and same(first, item_of(children[j])):
                j += 1
            if not first or j - i < 2:
                replacement = visit(children[i], part, heading_before(children, i))
                if replacement:
                    children[i] = replacement
                i += 1
                continue
            run = [item_of(c) for c in children[i:j]]
            item, links = run[0][0], [link for _, link, _ in run]
            classes = classes_of(run[0][2])[0]
            whole = j - i == len(children)
            # WordPress's list replaces the container when the links are all it holds.
            # (Its recorded attributes, e.g. a toggled drawer panel's, move along.)
            replace = whole and (set(a) <= {'className', 'metadata'} if block['name'] == 'core/list' else block['name'] in ('h2wp/element', 'core/group') and set(a) <= {'className', 'tagName', 'layout', 'metadata', 'htmlAttributes'} and 'href' not in (a.get('htmlAttributes') or {}) and (listy or lists_reset))
            container = (a.get('className') or '').split()
            # Child/sibling utilities of a replaced container land on the list's
            # items: bare links then get item boxes that wrap them without
            # changing their layout (h2wp-nav-wrap). Link classes that depend on
            # the link's own siblings cannot survive the item wrapping.
            wrap = replace and item is None and any(NAV_STRUCTURAL.search(c) for c in container)
            structural = any(NAV_STRUCTURAL.search(c) for c in ([] if replace else container) + (classes if item is None else []))
            # List items stay list items: only a whole list becomes the menu's list.
            if structural or (item is not None and not (listy and replace)):
                skipped.append(' / '.join(norm(html.unescape(label)) for _, label, _ in links))
                i = j
                continue
            heading = '' if part == 'header' else heading_before(children, i) or (above if replace else '')
            nav = {'name': 'h2wp/navigation', 'attributes': {'menu': menu_for(part, [{'label': label, 'url': url} for _, label, url in links], heading), 'linkClassName': ' '.join(classes)}}
            if item is not None or wrap:
                nav['attributes']['itemClassName'] = 'h2wp-nav-wrap' if wrap else item
            if replace:
                nav['attributes']['listClassName'] = ' '.join(container)
                if a.get('htmlAttributes'):
                    nav['attributes']['listAttributes'] = a['htmlAttributes']
            # Current page: the source's active classes, as the change they make.
            changes = [(tuple(c for c in current if c not in classes), tuple(c for c in classes if c not in current)) for _, current in (classes_of(r[2]) for r in run) if current]
            changes = [change for change in changes if change != ((), ())]
            if changes:
                added, removed = max(changes, key=changes.count)
                nav['attributes']['currentClassName'] = ' '.join([c for c in classes if c not in removed] + list(added))
            if replace:
                return nav
            children[i:j] = [nav]
            i += 1
        return None

    for part in ('header', 'footer'):
        for index, block in enumerate(parts.get(part) or []):
            replacement = visit(block, part)
            if replacement:
                parts[part][index] = replacement
    return menus, skipped


STYLE_CLASS = re.compile(r'[A-Za-z0-9_:/\[\]().%#!,-]+')


def block_styles(proposals, extra, contract, template_weight=1):
    """Spec 2 E: repeated button class strings -> blockStyles + is-style-<name> classes."""
    def blocks(tree):
        for block in tree:
            yield block
            yield from blocks(block.get('innerBlocks', []))

    def button(block):
        attrs = block.get('attributes', {})
        classes = norm(attrs.get('className'))
        if block['name'] != 'h2wp/element' or attrs.get('tagName') not in ('a', 'button') or not classes or len(classes) > 400:
            return None
        if not (('rounded' in classes or 'bg-' in classes or 'border' in classes) and 'px-' in classes):
            return None
        if not all(STYLE_CLASS.fullmatch(token) and not token.startswith('is-style-') for token in classes.split()):
            return None
        return classes

    def text(block):
        return norm(block['attributes'].get('text', '') + ' ' + ' '.join(text(b) for b in block.get('innerBlocks', []) if b['name'] == 'h2wp/element'))
    counts, labels, order = {}, {}, []
    # Source occurrences: the article template stands for every post it renders.
    counted = [(p['blocks'], 1) for p in proposals.values()] + [(tree, 1) for tree in extra]
    if 'single' in contract['templates']:
        counted.append((contract['templates']['single'], template_weight))
    for tree, weight in counted:
        for block in blocks(tree):
            classes = button(block)
            if classes:
                if classes not in counts:
                    order.append(classes)
                    labels[classes] = text(block)
                counts[classes] = counts.get(classes, 0) + weight
    chosen = sorted((c for c in order if counts[c] >= 2), key=lambda c: (-counts[c], order.index(c)))[:12]
    names = {classes: 'button-' + str(i + 1) for i, classes in enumerate(chosen)}
    for tree in [p['blocks'] for p in proposals.values()] + list(contract['parts'].values()) + list(contract['templates'].values()):
        for block in blocks(tree):
            classes = button(block)
            if classes in names:
                block['attributes']['className'] = 'is-style-' + names[classes]
    return [{'name': names[c], 'label': ('Button — ' + (labels[c] or names[c]))[:60].rstrip(), 'classes': c} for c in chosen]


def load_workspace(args):
    manifest_path = Path(args.manifest).resolve()
    manifest = read(manifest_path)
    dist = manifest_path.parent / 'astro-project/dist'
    if not dist.is_dir():
        raise ValueError('missing astro-project/dist')
    for page in manifest.get('pages', []):
        if page.get('kind') in ('article', 'listing'):
            page['kind'] = {'article': 'post', 'listing': 'blog'}[page['kind']]
        elif page.get('kind') == 'utility':
            page['kind'] = '404' if (manifest.get('utilityPages') or {}).get('404') in (page.get('file'), page.get('key')) else 'page'
    pages = [p for p in manifest.get('pages', []) if p.get('kind') != 'fragment']
    keys = [p.get('key', '') for p in pages]
    if len(set(keys)) != len(keys) or any(not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,159}', k) for k in keys):
        raise ValueError('page keys must be unique safe file names')
    if not pages:
        raise ValueError('manifest needs at least one page')
    return manifest_path, manifest, dist, pages


def asset_hashes(dist, contract):
    return {name: digest(local_file(dist, name).read_bytes()) for name in sorted(set(contract.get('styles', []) + contract.get('scripts', [])))}


def prepare(args):
    manifest_path, manifest, dist, pages = load_workspace(args)
    workspace = manifest_path.parent
    output, state = workspace / 'block-plan', workspace / '.gutenberg'
    if (output / 'contract.json').exists():
        # Resuming never overwrites worker edits. Detect source/contract drift.
        return check(args)
    contract = {'schema': 'h2wp-blocks/2', 'pages': [], 'themeJson': {'version': 3}, 'styles': [], 'scripts': [], 'parts': {}, 'templates': {}, 'menus': [], 'redirects': []}
    links = {p['file']: p['key'] for p in pages}
    post_keys = [p['key'] for p in pages if p.get('kind') == 'post']
    inventory, proposals, frames, entries = [], {}, [], []
    counter = [0]
    # html2wp's own recorder output rides along: the interaction runtime the
    # prerender emitted (never a source application script) and its entrance
    # animation <style>. Source inline CSS stays a finding: it can be page
    # specific and its cascade position matters.
    runtime_scripts, head_styles, head_boots = set(), {}, {}
    for page in pages:
        source = local_file(dist, page['file']).read_bytes()
        parser = Parser(source.decode('utf-8'))
        nodes = list(walk(parser.root))
        body = next((n for n in nodes if n.tag == 'body'), parser.root)
        mapper = Mapper(dist, page, links)
        mapper.category_param = (manifest.get('shop') or {}).get('categoryQueryParam') or ''
        mapper.shop_keys = {p['key'] for p in pages if p.get('kind') == 'shop'}
        mapper.counter = counter
        mapper.labels = {n.attrs['for']: {'text': plain(n).strip(), 'class': n.attrs.get('class', '')} for n in nodes if n.tag == 'label' and n.attrs.get('for')}
        mapper.recorded_messages = [n for n in nodes if 'data-spa-invalid' in n.attrs]
        # The ids the recorded interactions change (data-spa-attrs targets).
        for n in nodes:
            try:
                changes = json.loads(n.attrs['data-spa-attrs']) if n.attrs.get('data-spa-attrs') else []
            except ValueError:
                changes = []
            mapper.spa_targets.update(c.get('id') for c in changes if isinstance(c, dict) and c.get('id'))
        for node in nodes:
            if node.tag == 'link' and 'stylesheet' in (node.attrs.get('rel') or '').split():
                target = mapper.url(node.attrs.get('href', ''), True)
                if target.startswith('asset:'):
                    mapper.styles.add(target[6:].split('?')[0])
                    mapper.sheets.append(target[6:].split('?')[0])
                elif FONT_STYLESHEET.fullmatch(html.unescape(target)):
                    mapper.fonts.append(html.unescape(target))
                else:
                    mapper.finding('external-stylesheet', target)
            if node.tag == 'script' and 'data-spa-reveals' in node.attrs and not node.attrs.get('src'):
                # The prerender's reveal boot (hides reveals before first paint).
                boot = ''.join(c for c in node.children if isinstance(c, str)).strip()
                if boot:
                    head_boots.setdefault(boot, None)
                continue
            if node.tag == 'script':
                if node.attrs.get('src'):
                    target = mapper.url(node.attrs['src'], True)
                    if target.startswith('asset:'):
                        path = target[6:].split('?')[0]
                        if recorder_runtime(dist, path):
                            runtime_scripts.add(path)
                            continue
                        mapper.scripts.add(path)
                elif (node.attrs.get('type') or 'text/javascript').lower().split(';')[0].strip() in ('text/javascript', 'module', 'application/javascript'):
                    # An inline script is extracted to a dist asset so the
                    # coordinator can list it in this page's own `scripts`
                    # after review; it is never enqueued by the planner.
                    js = ''.join(c for c in node.children if isinstance(c, str)).strip()
                    if js:
                        name = 'assets/gutenberg-script-' + hashlib.sha256(js.encode()).hexdigest()[:12] + '.js'
                        target = local_file(dist, name)
                        if not target.is_file() or target.read_text() != js + '\n':
                            target.parent.mkdir(parents=True, exist_ok=True)
                            target.write_text(js + '\n')
                        mapper.finding('source-runtime', 'Review and replace source script: inline, extracted as ' + name)
                        continue
                mapper.finding('source-runtime', 'Review and replace source script: ' + node.attrs.get('src', 'inline'))
            if node.tag == 'style':
                css = ''.join(c for c in node.children if isinstance(c, str)).strip()
                # The recorder's entrance rule carries the page's own duration,
                # which its data-spa-enter value repeats: scope it to that value
                # so every page keeps its duration in the one shared stylesheet.
                css = re.sub(r'\[data-spa-enter\]\{animation:spa-enter (\d+)ms', r'[data-spa-enter="\1"]{animation:spa-enter \1ms', css)
                if '@keyframes spa-enter' in css or 'html.spa-reveal' in css:
                    head_styles.setdefault(css, []).append(mapper)
                elif css:
                    # Source CSS keeps its page scope and cascade position: each
                    # distinct block becomes a dist asset listed in this page's
                    # head order (proposal styles, see page_styles below).
                    name = 'assets/gutenberg-style-' + hashlib.sha256(css.encode()).hexdigest()[:12] + '.css'
                    target = local_file(dist, name)
                    if not target.is_file() or target.read_text() != css + '\n':
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_text(css + '\n')
                    mapper.sheets.append(name)
        wrapper, main, items = split_frame(body, manifest.get('chrome'))
        if page.get('chrome') == 'self-contained':
            # The manifest's own verdict (stage 0): this page keeps its whole
            # shell — its header lives in its hero, its footer is its own. Its
            # chrome stays content, and it renders through a template with no
            # shared parts (SELF_CONTAINED_TEMPLATE) so nothing doubles.
            items = [(node, None, place) for node, _part, place in items]
            wrapper = main = None
        frame = None
        if wrapper is not None or main is not None:
            frame = {}
            for key, node in (('wrapper', wrapper), ('main', main)):
                if node is None:
                    frame[key] = None
                    continue
                attrs, extra = mapper.attrs(node)
                frame[key] = {'tagName': node.tag, **attrs, **({'htmlAttributes': extra} if extra else {})}
        title = next((plain(n) for n in nodes if n.tag == 'title'), page.get('title', page['key']))
        description = next((n.attrs.get('content', '') for n in nodes if n.tag == 'meta' and n.attrs.get('name') == 'description'), '')
        heading = next((n for n in walk(body) if n.tag == 'h1'), None)
        entries.append({'key': page['key'], 'page': page, 'kind': page.get('kind'), 'source': source, 'mapper': mapper, 'items': items, 'frame': frame, 'root': body,
                        'body_class': body.attrs.get('class', '') if body is not parser.root else '',
                        'sections': ['source-' + str(i + 1).zfill(4) for i in range(len(items))], 'title': title, 'description': description,
                        'h1': norm(plain(heading)) if heading is not None else norm(page.get('title') or '')})
    # Post metadata drives card/article classification (spec 2 D3/D6).
    metas = {}
    for entry in entries:
        if entry['kind'] == 'post':
            given = entry['page'].get('post') if isinstance(entry['page'].get('post'), dict) else {}
            # A description every page shares (an SPA's one <meta>) is no excerpt.
            description = entry['description'] if sum(e['description'] == entry['description'] for e in entries) == 1 else ''
            metas[entry['key']] = {'title': entry['h1'], 'excerpt': given.get('excerpt') or description, 'categories': [c for c in given.get('categories', []) if isinstance(c, str)]}
    for entry in entries:
        entry['mapper'].posts = metas
    article = analyze_articles([e for e in entries if e['kind'] == 'post'], metas)
    order = sorted(metas, key=lambda key: metas[key].get('date') or '', reverse=True)
    for entry in entries:
        entry['mapper'].post_order = order

    def label(block, child, mapper):
        name = mapper.heading_name(child)
        if name:
            block['attributes'] = {**block.get('attributes', {}), 'metadata': {'name': name}}
        return block

    for entry in entries:
        mapper, items, sections, key = entry['mapper'], entry['items'], entry['sections'], entry['key']
        blocks, section_inventory, page_parts, mapped = [], [], {}, []
        is_post = entry['kind'] == 'post'
        owned = article['owned'] if article and key in article['bodies'] else []
        for index, (child, part, place) in enumerate(items):
            section = sections[index]
            mapper.section = section
            section_inventory.append({'id': section, 'sha256': digest(child.tree() if isinstance(child, Node) else child), 'tag': child.tag if isinstance(child, Node) else '#text', **({'part': part} if part else {})})
            if index in owned:
                if index != article['body_index']:
                    continue
                # Template-owned sections: only the article body is post content.
                mapper.queries = False
                kids = [b for b in (mapper.block(c) for c in article['bodies'][key].children[article.get('body_from', 0):]) if b]
                mapper.queries = True
                if not kids:
                    # Nothing mappable (e.g. only unmapped elements): keep coverage on a placeholder.
                    mapper.finding('article-body-empty', 'Article body has no mappable content; an empty group carries its source ids')
                    kids = [{'name': 'core/group', 'attributes': {'layout': {'type': 'default'}}, 'innerBlocks': []}]
                kids[0]['sourceIds'] = [sections[i] for i in owned if i < index and not any(items[j][1] for j in range(i, index))] + [section]
                kids[-1]['sourceIds'] = kids[-1].get('sourceIds', []) + [sections[i] for i in owned if i > index]
                blocks.extend(kids)
                continue
            mapper.queries = not part and not is_post
            block = mapper.block(strip_active(child) if part else child)
            mapper.queries = True
            if block and part:
                page_parts[part] = {'section': section, 'blocks': [block], 'source': child, 'active': has_active(child)}
                block = {'name': 'core/template-part', 'attributes': {'slug': part}}
                # Template-owned sections just before this part (an overlay
                # ahead of the header) are covered here, in source order: the
                # article body covers only those it is not cut off from by a part.
                body = article['body_index'] if owned else -1
                earlier = [sections[i] for i in owned if i < index and i < body and not any(items[j][1] for j in range(i, index))
                           and any(items[j][1] for j in range(i, body))]
            elif block:
                label(block, child, mapper)
            if block:
                block['sourceIds'] = (earlier if block['name'] == 'core/template-part' and owned else []) + [section]
                blocks.append(block)
                mapped.append((block, place))
        frames.append((key, entry['frame'], page_parts))
        entry['mapped'] = mapped
        source_hash = digest(entry['source'])
        contract['pages'].append({'key': key, 'file': entry['page']['file'], 'sha256': source_hash, 'sections': sections})
        entry['sheets'] = list(dict.fromkeys(mapper.sheets))
        # Source application scripts are inventoried but never automatically
        # enqueued: hydration can erase WordPress content and interactivity
        # (only the prerender's own runtime is, see runtime_scripts).
        proposal = {'key': key, 'sourceHash': source_hash, 'sections': sections, 'blocks': blocks, 'seo': {'title': entry['title'], 'description': entry['description']}}
        # The source <body>'s own classes: a design scopes whole pages by them
        # (`.v8 .wrap{max-width:1280px}`); the runtime puts them back on that page.
        body_class = ' '.join(c for c in (entry.get('body_class') or '').split() if re.fullmatch(r'[A-Za-z0-9_:/.%\[\]-]{1,60}', c))
        if body_class:
            proposal['bodyClass'] = body_class
        for field in ('slug', 'template', 'post', 'product'):
            if field in entry['page']:
                proposal[field] = entry['page'][field]
        if is_post:
            # Spec 2 D4: inferred post metadata; manifest values win.
            meta = metas[key]
            post = dict(proposal.get('post') or {})
            inferred = {'date': meta.get('date'), 'categories': meta.get('categories') or None, 'readTime': meta.get('readTime'), 'excerpt': meta.get('excerpt') or None, 'featuredImage': meta.get('image')}
            post.update({k: v for k, v in inferred.items() if v and k not in post})
            if post:
                proposal['post'] = post
        proposals[key] = proposal
        inventory.append({'key': key, 'sections': section_inventory, 'sourceScripts': sorted(mapper.scripts), 'findings': mapper.findings})

    def page_finding(key, code, section, detail):
        item = {'code': code, 'section': section, 'detail': detail}
        item['id'] = digest(item)[:20]
        findings = next(p for p in inventory if p['key'] == key)['findings']
        if item not in findings:
            findings.append(item)

    self_contained = {e['key'] for e in entries if e['page'].get('chrome') == 'self-contained'}
    # WordPress renders the static front page through front-page.html whatever
    # template the page selects (the Site Editor opens that one too), so a
    # self-contained front page's shell is front-page itself, not a selection.
    front_key = next((e['key'] for e in entries if e['kind'] == 'front'), None)
    if front_key in self_contained and front_key in proposals and not proposals[front_key].get('template'):
        contract['templates']['front-page'] = [{'name': 'core/post-content', 'attributes': {'layout': {'type': 'default'}}}]
    selecting = [key for key in self_contained if key != front_key and key in proposals and not proposals[key].get('template')]
    if selecting:
        contract['templates'][SELF_CONTAINED_TEMPLATE] = [{'name': 'core/post-content', 'attributes': {'layout': {'type': 'default'}}}]
        contract['themeJson'].setdefault('customTemplates', []).append({'name': SELF_CONTAINED_TEMPLATE, 'title': 'Self-contained page', 'postTypes': ['page']})
        for key in selecting:
            proposals[key]['template'] = SELF_CONTAINED_TEMPLATE
    frames = [f for f in frames if f[0] not in self_contained] or frames
    # One frame for the whole theme: the most common page frame (ties → first page).
    counts = {}
    for _, frame, _ in frames:
        counts.setdefault(digest(frame), [0, frame])[0] += 1
    chosen = max(counts.values(), key=lambda entry: entry[0])[1] if counts else None
    if chosen is not None:
        contract['frame'] = chosen
    owners, active_state = {}, None
    files = {p['key']: p['file'] for p in pages}
    kinds = {e['key']: e['kind'] for e in entries}
    by_key = {key: page_parts for key, _, page_parts in frames}
    # Every page's header/footer sorts into a design variant: pages whose part
    # is equivalent after current-link state share one (first seen first).
    groups = {}
    for key, frame, page_parts in frames:
        if digest(frame) != digest(chosen):
            page_finding(key, 'frame-variant', '', 'Page frame ' + json.dumps(frame, sort_keys=True) + ' differs from the shared frame ' + json.dumps(chosen, sort_keys=True) + '; review the page wrapper/main markup')
        for part, entry in page_parts.items():
            if entry['active'] and active_state is None:
                active_state = (key, entry['section'])
            for group in groups.setdefault(part, []):
                if chrome_equivalent(group[0][1], entry['source'], files[group[0][0]], files[key]):
                    group.append((key, entry['source']))
                    break
            else:
                groups[part].append([(key, entry['source'])])
    variant_of = {}
    for part, found in groups.items():
        variants = variant_parts(part, found, kinds, has_current)
        for slug, owner_key, members in variants['parts']:
            contract['parts'][slug] = by_key[owner_key][part]['blocks']
            owners[slug] = (owner_key, by_key[owner_key][part]['source'])
            for member in members:
                if slug != part:
                    variant_of[(member, part)] = slug
            if slug != part:
                contract['themeJson'].setdefault('templateParts', []).append({'name': slug, 'title': part.title() + ' ' + slug.rsplit('-', 1)[1], 'area': part if part in ('header', 'footer') else 'uncategorized'})
        for member in variants['replaced']:
            page_finding(member, 'chrome-variant', by_key[member][part]['section'], 'Source ' + part + ' differs from the shared ' + part + ' part (taken from ' + owners[part][0] + ') beyond active-link state; page-specific ' + part + ' markup is replaced by the shared part')
    # A page on a variant renders it through its own template: the front page's
    # front-page template, any other page a custom template assigned to it.
    for key in dict.fromkeys(k for k, _ in variant_of):
        for block in proposals[key]['blocks']:
            if block['name'] == 'core/template-part' and (key, block['attributes'].get('slug')) in variant_of:
                block['attributes']['slug'] = variant_of[(key, block['attributes']['slug'])]
        entry = next(e for e in entries if e['key'] == key)
        zones = {'head': [], 'tail': []}
        for _, part, place in entry['items']:
            if part and place in zones:
                zones[place].append({'name': 'core/template-part', 'attributes': {'slug': variant_of.get((key, part), part)}})
        shell = frame_shell(chosen, zones['head'], [{'name': 'core/post-content', 'attributes': {'layout': {'type': 'default'}}}], zones['tail'])
        if kinds[key] == 'front':
            contract['templates']['front-page'] = shell
        else:
            name = 'page-' + '-'.join(sorted({slug for (k, _), slug in variant_of.items() if k == key}))
            contract['templates'][name] = shell
            proposals[key]['template'] = name
    if active_state:
        # Informational and resolvable like every other finding; raised once per workspace.
        page_finding(active_state[0], 'chrome-active-state', active_state[1], "Shared header keeps the first page's link classes; per-page active styling needs core/navigation or CSS")
    for key, frame, page_parts in frames:
        if key in self_contained:
            continue
        for part in groups:
            if part not in page_parts and frame is not None:
                page_finding(key, 'chrome-variant', '', 'Page has no source ' + part + '; the shared ' + part + ' part will be rendered')
    # Header/footer link groups become menus the owner edits in Site Editor.
    mappers = {entry['key']: entry['mapper'] for entry in entries}

    def to_url(mapper):
        def convert(href):
            findings = list(mapper.findings)
            value = mapper.url(href) if href else ''
            mapper.findings[:] = findings
            return value
        return convert
    sources = {}
    for part, (owner_key, owner_source) in owners.items():
        others = [entry[part]['source'] for key, _, entry in frames if key != owner_key and part in entry and chrome_equivalent(owner_source, entry[part]['source'], files[owner_key], files[key])]
        sources[part] = link_states(to_url(mappers[owner_key]), owner_source, others)
    # A listing whose printed dates are not newest-first (the source ordered
    # its posts by hand): the theme lists posts in the source's order, and the
    # posts keep their printed dates.
    dated = [(meta['listingOrder'], meta['cardDate']) for meta in metas.values() if 'listingOrder' in meta and meta.get('cardDate')]
    if len(dated) >= 2 and len(dated) == sum(1 for m in metas.values() if 'listingOrder' in m) and [d for _, d in sorted(dated)] != sorted((d for _, d in dated), reverse=True):
        contract['postsOrder'] = 'listing'
    # Card summaries are read while pages map, possibly after a post's own
    # proposal was built: apply them now, unless the manifest names an excerpt.
    for entry in entries:
        meta = metas.get(entry['key'])
        given = entry['page'].get('post') if isinstance(entry['page'].get('post'), dict) else {}
        # A stated excerpt cut short ("… will tak...") that the card prints whole
        # yields to the card's words.
        stated = norm(given.get('excerpt') or '')
        cut = stated.endswith(('...', '…')) and norm(meta.get('cardExcerpt') or '').startswith(stated.rstrip('.… ')) if meta else False
        if entry['kind'] == 'post' and meta and meta.get('cardExcerpt') and (not given.get('excerpt') or cut):
            proposals[entry['key']].setdefault('post', {})['excerpt'] = meta['cardExcerpt']
        if entry['kind'] == 'post' and meta and meta.get('cardDate') and not given.get('date') and not (proposals[entry['key']].get('post') or {}).get('date'):
            proposals[entry['key']].setdefault('post', {})['date'] = meta['cardDate']
        if entry['kind'] == 'post' and meta and meta.get('cardImage') and not given.get('featuredImage') and not (proposals[entry['key']].get('post') or {}).get('featuredImage'):
            proposals[entry['key']].setdefault('post', {})['featuredImage'] = meta['cardImage']
        if entry['kind'] == 'post' and meta and 'listingOrder' in meta and (contract.get('postsOrder') or not (proposals[entry['key']].get('post') or {}).get('date')):
            proposals[entry['key']].setdefault('post', {})['listingOrder'] = meta['listingOrder']
    shared, page_sheets = page_styles(entries)
    contract['styles'] = shared + [p for p in contract['styles'] if p not in shared]
    for key, sheets in page_sheets.items():
        if sheets:
            proposals[key]['styles'] = sheets
    # Read off the sheets every page loads, so only once they are known.
    if manifest.get('shop', {}).get('present'):
        rules = {}
        for path in contract['styles']:
            if local_file(dist, path).is_file():
                rules.update(class_rules(local_file(dist, path).read_text(errors='ignore')))
        container = page_container(entries, rules)
        if container:
            contract['pageContainer'] = container
    every_sheet = list(dict.fromkeys(shared + [p for e in entries for p in e.get('sheets', [])]))
    lists_reset = any(local_file(dist, path).is_file() and resets_lists(local_file(dist, path).read_text(errors='ignore')) for path in every_sheet)
    contract['menus'], skipped = navigation_menus(contract['parts'], sources, manifest.get('nav') or [], lists_reset)
    if skipped and owners:
        owner = owners.get('header') or next(iter(owners.values()))
        page_finding(owner[0], 'navigation-static', '', 'Link groups kept as source elements (layout depends on child or sibling selectors): ' + '; '.join(skipped))
    # A post's section: when every post page's header marks its link to the
    # posts listing current (aria-current / data-status), the theme marks that
    # menu link current on single posts too.
    listing_key = next((e['key'] for e in entries if e['kind'] == 'blog'), None)
    post_frames = [page_parts for key, _, page_parts in frames if next((e for e in entries if e['key'] == key), {}).get('kind') == 'post']
    if listing_key and post_frames:
        def marks_listing(page_parts, mapper):
            for part in page_parts.values():
                for n in walk(part['source']):
                    if isinstance(n, Node) and n.tag == 'a' and is_active_marker(n) and mapper.resolve_quiet(n.attrs.get('href', '')).split('#')[0] == 'page:' + listing_key:
                        return True
            return False
        post_entries = [e for e in entries if e['kind'] == 'post']
        if all(marks_listing(pp, e['mapper']) for pp, e in zip(post_frames, post_entries)):
            contract['postsCurrent'] = True
    # WooCommerce's catalog and product templates in the design, from the shop
    # and product pages (the coordinator reviews them like any template).
    if manifest.get('shop', {}).get('present'):
        woo = Woo(entries, manifest)
        shop_entry = next((e for e in entries if e['kind'] == 'shop'), None)
        derived = set()
        sheet_rules = {}
        for path in contract['styles']:
            if local_file(dist, path).is_file():
                sheet_rules.update(class_rules(local_file(dist, path).read_text(errors='ignore')))
        listing_tpl = woo_listing_template(woo, shop_entry, chosen, contract['menus'], sheet_rules) if shop_entry else None
        if listing_tpl:
            contract['templates']['archive-product'] = listing_tpl
            derived.add(shop_entry['key'])
        product_tpl = woo_product_template(woo, entries, chosen, page_finding)
        if product_tpl:
            contract['templates']['single-product'] = product_tpl
            derived.update(woo.products)
        # WooCommerce's cart, checkout and account pages: the page's content
        # (WooCommerce's blocks) in the design's content container, inside the
        # frame and parts those pages render.
        container = contract.get('pageContainer')
        for kind in ('cart', 'checkout', 'account'):
            source = next((e for e in entries if e['kind'] == kind), None) or (next((e for e in entries if e['kind'] == 'cart'), None) if kind == 'account' else None)
            if not container or source is None:
                continue
            content = {'name': 'core/post-content', 'attributes': {'layout': {'type': 'default'}}}
            zones = {'before': [], 'head': [], 'tail': [], 'after': []}
            for child, part, place in source['items']:
                if part and place in zones:
                    zones[place].append({'name': 'core/template-part', 'attributes': {'slug': part}})
            body = [{'name': 'core/group', 'attributes': {'tagName': container.get('tagName') or 'div', 'className': container['className'], 'layout': {'type': 'default'}}, 'innerBlocks': [content]}]
            contract['templates']['page-my-account' if kind == 'account' else 'page-' + kind] = frame_shell(chosen, zones['head'], body, zones['tail'], zones['before'], zones['after'])
        # The shop's sort control and the product pages' buy controls are
        # WooCommerce's blocks in those templates, not page content.
        for page in inventory:
            if page['key'] in derived:
                page['findings'][:] = [f for f in page['findings'] if not (f['code'] == 'unmapped-element' and f['detail'] in ('select', 'input', 'button'))
                                       and not (f['code'] == 'image-attributes' and page['key'] in woo.products)]
    if article:
        contract['templates']['single'] = article_template(next(e for e in entries if e['key'] in article['bodies']), article, chosen)
    listing = next((e for e in entries if e['kind'] == 'blog'), None)
    home = listing_template(listing, chosen) if listing else None
    if home:
        contract['templates']['home'] = home
        contract['templates']['archive'] = json.loads(json.dumps(home))
    front = next((e['key'] for e in entries if e['kind'] == 'front'), links.get('index.html'))
    brand_styles = site_title(contract['parts'].get('header', []), front, (manifest.get('site') or {}).get('name'))
    styles = block_styles(proposals, [entry['blocks'] for _, _, page_parts in frames for entry in page_parts.values()], contract, len(article['bodies']) if article else 1)
    if styles:
        contract['blockStyles'] = styles
    # Tokens (theme.json presets and the bridge that maps the source's own
    # custom properties onto them) come from the sheets EVERY page loads. The
    # bridge loads after the source and wins; built from a sheet only some
    # pages load, it set every page's --font-display to that page's value.
    tokens, bridge, variations = design_tokens(dist, contract['styles'])
    if tokens:
        contract['themeJson']['settings'] = tokens
    if bridge:
        contract['tokenBridge'] = bridge
    if variations:
        contract['styleVariations'] = variations
    inline_styles, inline_scopes = dict(brand_styles), {}
    for entry in entries:
        inline_styles.update(entry['mapper'].inline_styles)
        inline_scopes.update(entry['mapper'].inline_scopes)
    if head_styles:
        css_path = local_file(dist, 'assets/gutenberg-head.css')
        css_path.parent.mkdir(parents=True, exist_ok=True)
        css_path.write_text('\n'.join(head_styles) + '\n')
        contract['styles'].append('assets/gutenberg-head.css')
    contract['scripts'] = sorted(runtime_scripts)
    if head_boots:
        boot_path = local_file(dist, 'assets/gutenberg-reveal-boot.js')
        boot_path.parent.mkdir(parents=True, exist_ok=True)
        boot_path.write_text('\n'.join(head_boots) + '\n')
        contract['headScripts'] = ['assets/gutenberg-reveal-boot.js']
    # Web fonts follow the page too: two services' stylesheets can declare the
    # same family differently (a variable DM Sans with an optical-size axis
    # on one page, static weights on another), and loading every page's
    # links everywhere let the last @font-face win on all of them — every
    # article's body text rendered from the other file. Shared prefix global,
    # the rest per page, exactly like the stylesheets.
    font_lists = [list(dict.fromkeys(e['mapper'].fonts)) for e in entries]
    shared_fonts = []
    for group in zip(*font_lists) if font_lists else []:
        if len(set(group)) != 1:
            break
        shared_fonts.append(group[0])
    if shared_fonts:
        contract['fontStyles'] = shared_fonts
    for entry, fonts in zip(entries, font_lists):
        if fonts[len(shared_fonts):]:
            proposals[entry['key']]['fontStyles'] = fonts[len(shared_fonts):]
    if inline_styles:
        css_path = local_file(dist, 'assets/gutenberg-inline.css')
        css_path.parent.mkdir(parents=True, exist_ok=True)
        css_path.write_text('\n'.join(('.' + name) * INLINE_WEIGHT + inline_scopes.get(name, '') + '{' + style + '}' for name, style in sorted(inline_styles.items())) + '\n')
        contract['styles'].append('assets/gutenberg-inline.css')
    contract_hash = digest(contract)
    write(output / 'contract.json', contract)
    # A <title> the source prints on several pages is the site's name, not
    # the page's: those pages get WordPress's own "Page – Site" title. The
    # front page keeps it.
    source_titles = [e['title'] for e in entries if e['title']]
    for entry in entries:
        if entry['kind'] != 'front' and source_titles.count(entry['title']) > 1:
            proposals[entry['key']]['seo'].pop('title', None)
    for key, proposal in proposals.items():
        write(output / 'pages' / (key + '.json'), proposal)
    write(state / 'inventory.json', {'schema': 'h2wp-inventory/1', 'pages': inventory, 'fragments': [p for p in manifest.get('pages', []) if p.get('kind') == 'fragment']})
    families = {}
    for page in pages:
        families.setdefault(page.get('family') or page.get('kind') or 'page', []).append(page['key'])
    write(state / 'tasks.json', {'schema': 'h2wp-tasks/1', 'maxWorkers': 3, 'contractHash': contract_hash, 'tasks': [{'id': 'family-' + str(i + 1), 'family': family, 'pages': keys, 'representative': keys[0], 'owner': None, 'status': 'pending'} for i, (family, keys) in enumerate(families.items())]})
    write(state / 'checkpoint.json', {'schema': 'h2wp-checkpoint/1', 'contractHash': contract_hash, 'sourceHashes': {p['key']: p['sha256'] for p in contract['pages']}, 'assetHashes': asset_hashes(dist, contract), 'startedAt': time.time(), 'resolutions': {}, 'metrics': []})
    # WordPress imports each page under its manifest title and slug. Stage 2
    # can leave every page titled after the site and without a slug; default
    # them from the source (post <h1>, page <title>, file route).
    shared = {p.get('title') for p in pages if p.get('title') and sum(q.get('title') == p.get('title') for q in pages) > 1}
    for entry in entries:
        page = entry['page']
        if not page.get('slug') and page.get('kind') != 'front':
            page['slug'] = re.sub(r'(^|/)index$', '', re.sub(r'\.html?$', '', page['file'])) or page['key']
        if not page.get('title') or page['title'] in shared:
            page['title'] = (entry['h1'] if entry['kind'] in ('post', 'product') else '') or entry['title'] or page['key']
        elif entry['kind'] == 'post' and entry['h1'] and norm(html.unescape(page['title'])) == norm(html.unescape(entry['title'])):
            # A post's title is its headline. Stage 0 records the document
            # <title> ("The Hook Comes First | Journal | Clara Hayes"), which
            # WordPress would print on every card and in the post title block.
            page['title'] = entry['h1']
    manifest.update({'schema': 'html2wp/2', 'target': 'gutenberg'})
    write(manifest_path, manifest)
    print(json.dumps({'prepared': len(pages), 'contractHash': contract_hash, 'findings': sum(len(p['findings']) for p in inventory), 'tasks': str(state / 'tasks.json')}))
    return 0


def check(args):
    manifest_path, manifest, dist, pages = load_workspace(args)
    workspace = manifest_path.parent
    output, state = workspace / 'block-plan', workspace / '.gutenberg'
    contract = read(local_file(workspace, 'block-plan/contract.json'))
    checkpoint = read(state / 'checkpoint.json')
    findings = []
    if asset_hashes(dist, contract) != checkpoint.get('assetHashes'):
        findings.append('shared CSS/JS changed: coordinator must freeze and re-review workers')
    if manifest.get('schema') != 'html2wp/2' or manifest.get('target') != 'gutenberg':
        findings.append('manifest must explicitly use html2wp/2 and target gutenberg')
    if digest(contract) != checkpoint.get('contractHash'):
        findings.append('contract changed: coordinator must run freeze after reviewing shared changes')
    expected = {p['key']: p for p in pages}
    actual = {p['key']: p for p in contract['pages']}
    if set(expected) != set(actual) or len(actual) != len(contract['pages']):
        findings.append('contract page inventory does not match manifest')
    for key, page in expected.items():
        entry = actual.get(key, {})
        source_hash = digest(local_file(dist, page['file']).read_bytes())
        if entry.get('file') != page['file'] or entry.get('sha256') != source_hash or checkpoint['sourceHashes'].get(key) != source_hash:
            findings.append(key + ': stale source; prepare a fresh workspace and reapply reviewed changes')
        path = local_file(workspace, 'block-plan/pages/' + key + '.json')
        if not path.is_file():
            findings.append(key + ': missing worker output')
            continue
        proposal = read(path)
        if proposal.get('key') != key or proposal.get('sourceHash') != source_hash or proposal.get('sections') != entry.get('sections'):
            findings.append(key + ': stale or mismatched worker output')
        coverage = []
        def visit(blocks):
            for block in blocks:
                coverage.extend(block.get('sourceIds', []))
                if block.get('name') in ('core/html', 'core/freeform'):
                    findings.append(key + ': raw HTML blocks are prohibited')
                visit(block.get('innerBlocks', []))
        visit(proposal.get('blocks', []))
        if coverage != entry.get('sections'):
            findings.append(key + ': incomplete, duplicate or reordered source coverage')
    for page in read(state / 'inventory.json')['pages']:
        for finding in page['findings']:
            resolution = checkpoint.get('resolutions', {}).get(page['key'] + ':' + finding['id'])
            if not isinstance(resolution, str) or len(resolution.strip()) < 12:
                findings.append(page['key'] + ': unresolved ' + finding['code'] + ' [' + finding['id'] + ']')
    task_file = read(state / 'tasks.json')
    if task_file.get('contractHash') != digest(contract):
        findings.append('stale task contract hash')
    assigned = [key for task in task_file['tasks'] for key in task['pages']]
    if sorted(assigned) != sorted(expected):
        findings.append('worker task coverage must contain every page exactly once')
    for task in task_file['tasks']:
        if task.get('status') != 'complete' or task.get('contractHash') != digest(contract):
            findings.append(task['id'] + ': worker review incomplete or stale')
        if task.get('status') == 'complete':
            for key in task['pages']:
                path = output / 'pages' / (key + '.json')
                if path.exists() and task.get('outputHashes', {}).get(key) != digest(read(path)):
                    findings.append(key + ': changed after worker checkpoint')
    report = {'ok': not findings, 'findings': findings, 'pages': len(pages), 'elapsedSeconds': round(time.time() - checkpoint['startedAt'], 3), 'metrics': checkpoint.get('metrics', [])}
    write(state / 'check-report.json', report)
    print(json.dumps(report, ensure_ascii=False))
    return 0 if report['ok'] else 1


def checkpoint_task(args):
    manifest_path, _, _, _ = load_workspace(args)
    state, output = manifest_path.parent / '.gutenberg', manifest_path.parent / 'block-plan'
    tasks = read(state / 'tasks.json')
    contract_hash = digest(read(output / 'contract.json'))
    if tasks['contractHash'] != contract_hash:
        raise ValueError('contract changed; coordinator must freeze before dispatch')
    task = next((t for t in tasks['tasks'] if t['id'] == args.task), None)
    if not task:
        raise ValueError('unknown --task')
    if not args.owner:
        raise ValueError('--owner is required')
    if task.get('owner') and task['owner'] != args.owner:
        raise ValueError('task already owned by ' + task['owner'])
    task['owner'] = args.owner
    if args.command == 'claim':
        if sum(t['status'] == 'running' for t in tasks['tasks'] if t is not task) >= 3:
            raise ValueError('maximum of three concurrent workers')
        task.update({'status': 'running', 'startedAt': time.time(), 'contractHash': contract_hash})
    else:
        if task['status'] != 'running':
            raise ValueError('claim task before completion')
        task.update({'status': 'complete', 'completedAt': time.time(), 'contractHash': contract_hash, 'outputHashes': {k: digest(read(output / 'pages' / (k + '.json'))) for k in task['pages']}})
    write(state / 'tasks.json', tasks)
    print(json.dumps(task))
    return 0


def freeze(args):
    manifest_path, _, dist, pages = load_workspace(args)
    state, output = manifest_path.parent / '.gutenberg', manifest_path.parent / 'block-plan'
    contract = read(output / 'contract.json')
    checkpoint = read(state / 'checkpoint.json')
    if {p['key']: digest(local_file(dist, p['file']).read_bytes()) for p in pages} != checkpoint['sourceHashes']:
        raise ValueError('source changed; create a fresh plan workspace instead of approving stale sections')
    tasks = read(state / 'tasks.json')
    if any(t['status'] == 'running' for t in tasks['tasks']):
        raise ValueError('finish/stop running workers before changing shared contract')
    contract_hash = digest(contract)
    checkpoint['assetHashes'] = asset_hashes(dist, contract)
    checkpoint['contractHash'] = tasks['contractHash'] = contract_hash
    # Shared design changes invalidate every worker review, even when source
    # bytes are unchanged. Existing page proposals are retained for revision.
    for task in tasks['tasks']:
        task.update({'status': 'pending', 'owner': None})
        task.pop('outputHashes', None)
    write(state / 'tasks.json', tasks)
    write(state / 'checkpoint.json', checkpoint)
    print(json.dumps({'contractHash': contract_hash, 'invalidatedTasks': len(tasks['tasks'])}))
    return 0


MISSING = object()
# A recorder value that names something (data-spa-id="menu", data-spa-for=...);
# message text and JSON payloads are content.
SPA_TOKEN = re.compile(r'[A-Za-z0-9_:.-]{1,80}')


class Diverged(Exception):
    """A reviewed edit and a source edit reshaped the same container."""


def merge3(base, reviewed, new, path=''):
    """The reviewed edits of `base` replayed onto `new` (planner JSON). A
    value both sides changed takes the edited source's."""
    if reviewed == base:
        return new
    if new == base or new == reviewed:
        return reviewed
    if all(isinstance(v, dict) for v in (base, reviewed, new)):
        merged = {}
        for key in list(reviewed) + [k for k in new if k not in reviewed]:
            value = merge3(base.get(key, MISSING), reviewed.get(key, MISSING), new.get(key, MISSING), path + '/' + key)
            if value is not MISSING:
                merged[key] = value
        return merged
    # Blocks merge slot by slot only while every side holds the same blocks
    # in the same order: a reordered slot would take another block's content.
    names = [[b.get('name') if isinstance(b, dict) else None for b in v] if isinstance(v, list) else None for v in (base, reviewed, new)]
    if all(isinstance(v, list) for v in (base, reviewed, new)) and len(base) == len(reviewed) == len(new) and names[0] == names[1] == names[2]:
        return [merge3(b, r, n, path + '/' + str(i)) for i, (b, r, n) in enumerate(zip(base, reviewed, new))]
    if any(isinstance(v, (dict, list)) for v in (base, reviewed, new)):
        raise Diverged(path or '/')
    return new


# Phrasing an owner adds while writing (bold, a link, a line break): inside
# text it is part of the text's rich content, not of the page's structure.
PHRASING_TAGS = {'strong', 'em', 'b', 'i', 'a', 'br', 'span', 'code', 'small', 'sup', 'sub', 'mark'}
TEXT_TAGS = HEADINGS | {'p', 'li', 'dt', 'dd', 'blockquote', 'figcaption', 'td', 'th', 'label', 'summary', 'caption'}


def phrasing(node):
    return isinstance(node, str) or (node.tag in PHRASING_TAGS and not any(k.startswith('data-spa-') for k in node.attrs)
                                     and all(phrasing(c) for c in node.children))


def fingerprint(node):
    """An element tree's structure without its content: tags, attribute names
    and the recorder's naming data-spa-* values. Phrasing inside text is
    content, and so is an element's class list: restyling an element that
    stays (a Tailwind `bg-soft` -> `bg-deep`) is merged like its text; a
    class that reshapes the plan (a card list no longer alike) still diverges
    in the three-way merge."""
    text = node.tag in TEXT_TAGS or any(isinstance(c, str) and c.strip() for c in node.children)
    return [node.tag, sorted(k for k in node.attrs if k != 'class'),
            sorted((k, v) for k, v in node.attrs.items() if k.startswith('data-spa-') and v and SPA_TOKEN.fullmatch(v)),
            [fingerprint(c) for c in node.children if isinstance(c, Node) and not (text and phrasing(c))]]


def page_fingerprints(dist, manifest, page):
    """(content, chrome) fingerprints of one page, split like prepare: the
    frame shell and header/footer parts are the chrome every page shares."""
    parser = Parser(local_file(dist, page['file']).read_bytes().decode('utf-8'))
    body = next((n for n in walk(parser.root) if n.tag == 'body'), parser.root)
    wrapper, main, items = split_frame(body, manifest.get('chrome'))
    if page.get('chrome') == 'self-contained':
        items, wrapper, main = [(node, None, place) for node, _part, place in items], None, None
    shell = [[n.tag, sorted((n.attrs.get('class') or '').split()), sorted(n.attrs)] if n is not None else None for n in (wrapper, main)]
    content = [[place, fingerprint(n) if isinstance(n, Node) else '#text'] for n, part, place in items if not part]
    chrome = [[part, place, fingerprint(n)] for n, part, place in items if part]
    return digest(content), digest([shell, chrome])


def planned(manifest, dist, root):
    """What an unmodified prepare makes of `dist`, in a scratch workspace."""
    shutil.copytree(dist, root / 'astro-project/dist', symlinks=True)
    write(root / 'conversion-manifest.json', manifest)
    with contextlib.redirect_stdout(io.StringIO()):
        prepare(argparse.Namespace(manifest=str(root / 'conversion-manifest.json')))
    contract = read(root / 'block-plan/contract.json')
    return {'manifest': read(root / 'conversion-manifest.json'), 'contract': contract, 'inventory': read(root / '.gutenberg/inventory.json'),
            'pages': {p['key']: read(root / 'block-plan/pages' / (p['key'] + '.json')) for p in contract['pages']}, 'dist': root / 'astro-project/dist'}


CSS_REF = re.compile(r'url\(\s*([\'"]?)([^)\'"]+)\1\s*\)|@import\s+([\'"])([^\'"]+)\3', re.I)


def carry_contract_files(contract, previous, dist):
    """Files the reviewed contract loads that the coordinator placed in the
    dist (a self-hosted font sheet and its woff2) and a rebuilt dist lacks:
    copied over from the previous dist, with the local files their CSS
    references. Returns the dist paths carried."""
    wanted = [p for key in ('styles', 'scripts', 'headScripts', 'editorStyles') for p in contract.get(key, []) if isinstance(p, str)]
    carried, seen = [], set()
    while wanted:
        name = posixpath.normpath(wanted.pop(0).lstrip('/'))
        if name in seen or name.startswith('../') or '/../' in name:
            continue
        seen.add(name)
        source, target = local_file(previous, name), local_file(dist, name)
        if target.is_file() or not source.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        carried.append(name)
        if name.endswith('.css'):
            for match in CSS_REF.finditer(source.read_text(errors='ignore')):
                ref = (match.group(2) or match.group(4) or '').split('?')[0].split('#')[0]
                if ref and not re.match(r'^(?:[a-z]+:|//|#)', ref, re.I):
                    wanted.append(ref.lstrip('/') if ref.startswith('/') else (PurePosixPath(name).parent / ref).as_posix())
    return carried


def refresh(args):
    """Carry a reviewed plan over an edit of its source dist. Pages whose
    structure is unchanged keep every reviewed edit (a three-way merge with
    the plan of the previous dist as base) and their completed review; a page
    whose structure changed gets a fresh proposal and its task reopens; a
    structural change to the shared chrome leaves the contract for freeze."""
    manifest_path, manifest, dist, pages = load_workspace(args)
    workspace = manifest_path.parent
    output, state = workspace / 'block-plan', workspace / '.gutenberg'
    if not args.previous_dist or not Path(args.previous_dist).is_dir():
        raise ValueError('refresh needs --previous-dist: the dist this plan was prepared from')
    previous = Path(args.previous_dist).resolve()
    contract, checkpoint, tasks = read(output / 'contract.json'), read(state / 'checkpoint.json'), read(state / 'tasks.json')
    inventory = read(state / 'inventory.json')
    keys = [p['key'] for p in pages]
    if sorted(keys) != sorted(p['key'] for p in contract['pages']) or sorted(keys) != sorted(checkpoint['sourceHashes']):
        raise ValueError('page inventory changed; prepare a fresh workspace')
    if any(t['status'] == 'running' for t in tasks['tasks']):
        raise ValueError('finish/stop running workers before refreshing the plan')
    before = {p['key']: digest(local_file(previous, p['file']).read_bytes()) for p in pages}
    if before != checkpoint['sourceHashes']:
        raise ValueError('--previous-dist is not the dist this plan was prepared from')
    after = {p['key']: digest(local_file(dist, p['file']).read_bytes()) for p in pages}
    reviewed = {k: read(output / 'pages' / (k + '.json')) for k in keys}
    carried_files = carry_contract_files(contract, previous, dist)
    raw = read(manifest_path)
    with tempfile.TemporaryDirectory() as temp:
        base = planned(raw, previous, Path(temp) / 'base')
        new = planned(raw, dist, Path(temp) / 'new')
        # Prepare adds files to the dist it plans (extracted inline CSS and JS,
        # the inline-style sheet), which a rebuilt dist lacks. Each is merged
        # like the plan: the delivered file unless the edit changed it.
        copied, drift, unused = [], [], {}
        for path in sorted(p for p in new['dist'].rglob('*') if p.is_file() and not p.is_symlink()):
            name = path.relative_to(new['dist']).as_posix()
            made, was = path.read_bytes(), base['dist'] / name
            delivered = local_file(previous, name).read_bytes() if local_file(previous, name).is_file() else None
            if was.is_file() and was.read_bytes() == made:
                if delivered is None:
                    # One the delivered plan never had: only if the merged plan loads it.
                    unused[name] = made
                made = delivered
            elif delivered is not None and was.is_file() and was.read_bytes() != delivered:
                drift.append(name)
            target = local_file(dist, name)
            if made is not None and (not target.is_file() or target.read_bytes() != made):
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(made)
                copied.append(name)
    fingerprints = {p['key']: (page_fingerprints(previous, manifest, p), page_fingerprints(dist, manifest, p)) for p in pages}
    chrome_changed = any(old[1] != now[1] for old, now in fingerprints.values())
    result, refreshed, reopened, unchanged, reasons = {}, [], [], [], {}
    for key in keys:
        if before[key] == after[key] and base['pages'][key] == new['pages'][key]:
            unchanged.append(key)
            continue
        old, now = fingerprints[key]
        try:
            if old[0] != now[0]:
                raise Diverged('structure')
            result[key] = merge3(base['pages'][key], reviewed[key], new['pages'][key])
            refreshed.append(key)
        except Diverged as error:
            # The source's structure changed, or a reviewed edit reshaped what
            # the source edit changed. The reviewed proposal stays at hand.
            result[key] = new['pages'][key]
            reopened.append(key)
            reasons[key] = 'structure' if str(error) == 'structure' else 'unmerged ' + str(error)
    contract_state, diverged = 'reopened' if chrome_changed else None, None
    try:
        merged_contract = merge3(base['contract'], contract, new['contract'])
    except Diverged as error:
        merged_contract, contract_state, diverged = new['contract'], 'reopened', str(error)
    contract_state = contract_state or ('refreshed' if merged_contract != contract else 'unchanged')
    try:
        merged_manifest = merge3(base['manifest'], raw, new['manifest'])
    except Diverged:
        merged_manifest = raw
    # A page's inventory entry is the planner's: the delivered one while the
    # edit left it as it was, the new one otherwise.
    delivered = {p['key']: p for p in inventory['pages']}
    before_entries = {p['key']: p for p in base['inventory']['pages']}
    merged_inventory = inventory if new['inventory'] == base['inventory'] else {**new['inventory'], 'pages': [
        delivered[p['key']] if p['key'] in delivered and before_entries.get(p['key']) == p else p for p in new['inventory']['pages']]}
    # Resolutions follow their finding: by id (a content digest), or, on a
    # page whose structure held, the finding a text edit re-worded (same code
    # and section, in order). A page whose inventory entry held keeps all of its own.
    resolutions = checkpoint.get('resolutions', {})
    kept_pages = {p['key'] for p in merged_inventory['pages'] if p is delivered.get(p['key'])}
    kept = {r: v for r, v in resolutions.items() if r.split(':', 1)[0] in kept_pages}
    carried = set(kept)
    base_findings = {p['key']: p['findings'] for p in base['inventory']['pages']}
    for page in merged_inventory['pages']:
        key = page['key']
        if key in kept_pages:
            continue
        ids = {f['id'] for f in page['findings']}
        before_ids = {f['id'] for f in base_findings.get(key, [])}
        spare = [f for f in base_findings.get(key, []) if f['id'] not in ids and key + ':' + f['id'] in resolutions]
        for finding in page['findings']:
            name = key + ':' + finding['id']
            if name in resolutions:
                kept[name] = resolutions[name]
                carried.add(name)
            elif key not in reopened and finding['id'] not in before_ids:
                match = next((f for f in spare if f['code'] == finding['code'] and f['section'] == finding['section']), None)
                if match:
                    spare.remove(match)
                    kept[name] = resolutions[key + ':' + match['id']]
                    carried.add(key + ':' + match['id'])
    loaded = {n for field in ('styles', 'scripts', 'headScripts') for plan in [merged_contract, *result.values(), *reviewed.values()] for n in plan.get(field) or []}
    for name in sorted(loaded & set(unused)):
        local_file(dist, name).parent.mkdir(parents=True, exist_ok=True)
        local_file(dist, name).write_bytes(unused[name])
        copied.append(name)
    contract_hash = digest(merged_contract)
    new_tasks, new_checkpoint = json.loads(json.dumps(tasks)), json.loads(json.dumps(checkpoint))
    tasks_reopened = []
    for task in new_tasks['tasks']:
        if any(k in reopened for k in task['pages']):
            if task.get('status') != 'pending' or task.get('owner'):
                tasks_reopened.append(task['id'])
            task.update({'status': 'pending', 'owner': None})
            task.pop('outputHashes', None)
        elif task.get('status') == 'complete':
            # Restamp only what the last check accepted: an output edited after
            # its checkpoint stays flagged.
            for key in task['pages']:
                if key in result and task.get('outputHashes', {}).get(key) == digest(reviewed[key]):
                    task['outputHashes'][key] = digest(result[key])
            if contract_state != 'reopened' and task.get('contractHash') == checkpoint.get('contractHash'):
                task['contractHash'] = contract_hash
    if contract_state != 'reopened':
        if new_tasks.get('contractHash') == checkpoint.get('contractHash'):
            new_tasks['contractHash'] = contract_hash
        new_checkpoint['contractHash'] = contract_hash
    new_checkpoint['sourceHashes'] = after
    if asset_hashes(previous, contract) == checkpoint.get('assetHashes'):
        new_checkpoint['assetHashes'] = asset_hashes(dist, merged_contract)
    new_checkpoint['resolutions'] = kept
    for key, value in result.items():
        if value != reviewed[key]:
            if key in reopened:
                write(state / 'displaced' / (key + '.json'), reviewed[key])
            write(output / 'pages' / (key + '.json'), value)
    for path, value, old in ((output / 'contract.json', merged_contract, contract), (state / 'inventory.json', merged_inventory, inventory),
                             (manifest_path, merged_manifest, raw), (state / 'tasks.json', new_tasks, tasks), (state / 'checkpoint.json', new_checkpoint, checkpoint)):
        if value != old:
            write(path, value)
    print(json.dumps({'ok': True, 'refreshed': refreshed, 'reopened': reopened, 'reopenedWhy': reasons, 'unchanged': unchanged, 'contract': contract_state, 'contractDiverged': diverged, 'carried': carried_files,
                      'assets': 'refreshed' if new_checkpoint.get('assetHashes') != checkpoint.get('assetHashes') else 'unchanged',
                      'distWritten': copied, 'plannerDrift': drift,
                      'tasksReopened': tasks_reopened, 'resolutionsKept': len(kept), 'resolutionsDropped': len(set(resolutions) - carried),
                      'unresolved': [p['key'] + ':' + f['id'] for p in merged_inventory['pages'] for f in p['findings'] if p['key'] + ':' + f['id'] not in kept],
                      'contractHash': new_checkpoint['contractHash']}))
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', default='prepare', choices=['prepare', 'check', 'finalize', 'freeze', 'claim', 'complete', 'refresh'])
    parser.add_argument('--manifest', required=True)
    parser.add_argument('--previous-dist')
    parser.add_argument('--task')
    parser.add_argument('--owner')
    args = parser.parse_args()
    try:
        if args.command == 'prepare':
            return prepare(args)
        if args.command in ('check', 'finalize'):
            return check(args)
        if args.command == 'freeze':
            return freeze(args)
        if args.command == 'refresh':
            return refresh(args)
        return checkpoint_task(args)
    except (ValueError, KeyError, OSError, TypeError) as error:
        print('gutenberg plan: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
