#!/usr/bin/env python3
"""Deterministic Gutenberg inventory/proposals and local worker checkpoints.

Source HTML is data. This program never evaluates scripts or executes workers.
"""
import argparse
import hashlib
import html
from html.parser import HTMLParser
import json
from pathlib import Path, PurePosixPath
import re
import sys
import time
from datetime import datetime
from urllib.parse import urlsplit, unquote


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
label time sup sub svg path circle rect line polyline polygon ellipse g title b i br hr blockquote'''.split())
VOID_ELEMENT_TAGS = {'br', 'hr'}
# Spec 2 C: plain wrappers of these tags become core/group.
GROUP_TAGS = {'div', 'section', 'article', 'aside', 'header', 'footer', 'main'}
# Spec 2 B: h2wp/icon node tags/attributes (element-allowlist.json svgTags/attributes).
SVG_TAGS = {'svg', 'path', 'circle', 'rect', 'line', 'polyline', 'polygon', 'ellipse', 'g', 'title'}
# Mirrors element-allowlist.json `attributes` (lowercase, as the HTML parser
# reports them; viewbox -> viewBox). The test suite asserts they stay equal.
ELEMENT_ATTRIBUTES = set('''href target rel type role tabindex title viewbox d fill stroke stroke-width stroke-linecap
stroke-linejoin cx cy r x y x1 y1 x2 y2 width height points xmlns hidden fill-rule clip-rule transform opacity
fill-opacity stroke-opacity stroke-dasharray stroke-dashoffset stroke-miterlimit vector-effect'''.split())
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
                ('%d %B %Y', 'j F Y'), ('%d %b %Y', 'j M Y'), ('%Y-%m-%d', 'Y-m-d'), ('%B %Y', 'F Y'), ('%b %Y', 'M Y')]


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


def classify(values, metas, card=False, static=False):
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
    if all(v and len(v) <= 40 for v in values):
        return 'postTerms', None
    if card and all(len(v) > 40 for v in values):
        return 'postExcerpt', None
    return None


def leaf_pieces(node):
    pieces = []
    for text in node.children:
        pieces.extend(piece for piece in SEPARATOR.split(text) if piece)
    return pieces


def is_leaf(node):
    return node.tag not in VOID_ELEMENT_TAGS and bool(node.children) and all(isinstance(c, str) for c in node.children) and bool(plain(node).strip())


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

    def record(bind, fmt, per_instance):
        for store, value in zip(values or [], per_instance):
            store.setdefault(bind, (norm(value), fmt))

    if all(is_leaf(n) for n in nodes):
        pieces = [leaf_pieces(n) for n in nodes]
        whole = [''.join(n.children) for n in nodes]
        changed = len(set(texts)) > 1
        if len(pieces[0]) == 1 or (changed and classify(whole, metas, card) in (('postTitle', None), ('postExcerpt', None))):
            kind = classify(whole, metas, card, static=not changed) if changed or card else None
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
            kind = classify(column, metas, card, static=same) if (not same or card) else None
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
    if not owned[0] or any(o != owned[0] for o in owned) or owned[0] != list(range(owned[0][0], owned[0][-1] + 1)):
        return fail('article-template-variant', 'Post pages do not share one contiguous section layout; no article template was derived')
    index = owned[0]
    if any(shallow(e['items'][i][0]) != shallow(entries[0]['items'][i][0]) for e in entries for i in index):
        return fail('article-template-variant', 'Post pages do not share one section structure; no article template was derived')
    tree = lambda node: node.tree() if isinstance(node, Node) else node
    differing = [i for i in index if len({digest(tree(e['items'][i][0])) for e in entries}) > 1 and isinstance(entries[0]['items'][i][0], Node)]
    if not differing:
        return fail('article-template-variant', 'Post pages have identical sections; no article body found')
    body_index = max(differing, key=lambda i: sum(text_size(e['items'][i][0]) for e in entries))

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
    overrides = {id(bodies[0]): {'body': True}}
    body_ids = {id(b) for b in bodies}
    post_metas = [metas[e['key']] for e in entries]
    values = [{} for _ in entries]

    def stop(nodes):
        return id(nodes[0]) in body_ids or all(e['mapper'].card_list(n) for e, n in zip(entries, nodes))
    mapper = entries[0]['mapper']
    for i in differing:
        mapper.section = entries[0]['sections'][i]
        diff_walk([e['items'][i][0] for e in entries], post_metas, overrides, values, mapper, True, stop)
    if not any(o.get('title') for o in overrides.values()):
        return fail('article-title-unmapped', 'No heading outside the article body carries the post title (page h1); no article template was derived')
    for meta, found in zip(post_metas, values):
        if 'postDate' in found and parse_date(found['postDate'][0]):
            meta['date'] = parse_date(found['postDate'][0])[0]
        if 'postTerms' in found:
            meta['categories'] = [found['postTerms'][0]]
        if 'postReadTime' in found:
            meta['readTime'] = found['postReadTime'][0]
    return {'owned': index, 'body_index': body_index, 'bodies': {e['key']: b for e, b in zip(entries, bodies)}, 'overrides': overrides}


class Mapper:
    def __init__(self, dist, page, links):
        self.dist, self.page, self.links = dist, page, links
        self.findings, self.styles, self.scripts = [], set(), set()
        self.inline_styles = {}
        self.inline_scopes = {}
        self.labels = {}
        self.in_form = False
        self.section = ''
        # Round-2 derivation state (spec 2 sections B-D).
        self.overrides = {}        # id(node) -> {'body'|'title'|'leaf'|'link': ...}
        self.posts = {}            # post key -> metadata used to classify card text
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
                    return 'page:' + self.links[candidate] + (('#' + url.fragment) if url.fragment else '')
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
        if not name:
            self.finding('field-name', 'Set a stable field name before form delivery')
            name = 'field-' + digest(node.tree())[:10]
        attrs, extra = self.attrs(node, ('name', 'type', 'placeholder', 'required', 'autocomplete', 'rows'))
        if extra or attrs.get('anchor'):
            self.finding('field-attributes', 'Review field attributes and labels: ' + name)
        result = {'name': name, 'type': kind, 'label': label or node.attrs.get('placeholder') or name, 'required': 'required' in node.attrs, 'placeholder': node.attrs.get('placeholder', ''), 'inputClassName': attrs.get('className', ''), 'labelClassName': label_attrs.get('class', '')}
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
        attrs['content'] = content
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

    def card_target(self, node):
        """The single post key a card links to, else None."""
        targets = set()
        for n in walk(node):
            if n.tag == 'a' and n.attrs.get('href'):
                target = self.resolve_quiet(n.attrs['href'])
                if target.startswith('page:') and target[5:].split('#')[0] in self.posts:
                    targets.add(target[5:].split('#')[0])
        return targets.pop() if len(targets) == 1 else None

    def card_list(self, node):
        """Post keys when node's children are >= 2 identical cards linking to distinct posts."""
        if not self.posts or not isinstance(node, Node) or node.tag not in ELEMENT_TAGS or node.tag in ('a', 'svg'):
            return None
        if any(isinstance(c, str) and c.strip() for c in node.children):
            return None
        cards = [c for c in node.children if isinstance(c, Node)]
        if len(cards) < 2 or len({shape(c) for c in cards}) != 1:
            return None
        targets = [self.card_target(c) for c in cards]
        if None in targets or len(set(targets)) != len(targets):
            return None
        return targets

    def query_block(self, node, targets):
        """core/query > core/post-template > first card with element binds (spec 2 D6)."""
        cards = [c for c in node.children if isinstance(c, Node)]
        overrides = {}
        diff_walk(cards, [self.posts[key] for key in targets], overrides, None, self, native_title=False)
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
        attrs, extra = self.attrs(node)
        wrap = bool(extra or attrs.get('anchor'))
        template = {'name': 'core/post-template', 'attributes': {**({'className': attrs['className']} if attrs.get('className') and not wrap else {}), 'layout': {'type': 'default'}}, 'innerBlocks': [card] if card else []}
        query = {'perPage': len(cards), 'pages': 0, 'offset': 0, 'postType': 'post', 'order': 'desc', 'orderBy': 'date', 'inherit': False}
        block = {'name': 'core/query', 'attributes': {'queryId': self.counter[0], 'query': query, **({'namespace': self.query_namespace} if self.query_namespace else {})}, 'innerBlocks': [template]}
        if wrap:
            return {'name': 'h2wp/element', 'attributes': {**attrs, 'tagName': node.tag, **({'htmlAttributes': extra} if extra else {})}, 'innerBlocks': [block]}
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
        if override.get('body'):
            return self.container_block(node, [{'name': 'core/post-content', 'attributes': {'layout': {'type': 'default'}}}])
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
            return {'name': 'h2wp/element', 'attributes': {'tagName': 'span', 'text': node}} if node.strip() else None
        tag = node.tag
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
            attrs, extra = self.attrs(node, ('action', 'method'))
            if node.attrs.get('action'):
                self.finding('form-delivery', 'Replace source action with configured Gutenberg form delivery: ' + node.attrs['action'])
            if extra or attrs.get('anchor'):
                self.finding('form-attributes', 'Review form metadata and anchor')
            previous = self.in_form
            self.in_form = True
            children = [self.block(child) for child in node.children]
            self.in_form = previous
            form_id = node.attrs.get('id') or 'form-' + digest(node.tree())[:12]
            return {'name': 'h2wp/form', 'attributes': {'formId': form_id, 'className': attrs.get('className', '')}, 'innerBlocks': [c for c in children if c]}
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
                return {'name': 'h2wp/submit', 'attributes': {'label': node.attrs.get('value', 'Submit'), 'className': node.attrs.get('class', '')}}
            return self.field(node, self.labels.get(node.attrs.get('id'), ''))
        if self.in_form and tag == 'button' and node.attrs.get('type', 'submit') == 'submit':
            if any(isinstance(c, Node) for c in node.children):
                self.finding('submit-markup', 'Preserve nested submit button imagery')
            return {'name': 'h2wp/submit', 'attributes': {'label': plain(node), 'className': node.attrs.get('class', '')}}
        if tag == 'img':
            attrs, extra = self.attrs(node, ('src', 'alt', 'width', 'height', 'loading', 'decoding'))
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
            attrs['text'] = ''.join(node.children)
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


def split_frame(body, chrome):
    """Unwrap the page frame (spec section 1).

    Returns (wrapper, main, items) where items are (node, partName|None, place)
    in source order. place is 'before'/'after' (outside the wrapper), 'head'/
    'tail' (inside the wrapper, before/after <main>) or 'main' (main content;
    every non-part wrapper child when there is no <main>). Without a wrapper or
    a body-level <main> the body children are returned unchanged."""
    kids = meaningful(body.children)
    wrapper = None
    if len(kids) == 1 and isinstance(kids[0], Node) and kids[0].tag in ('div', 'section'):
        wrapper = kids[0]
    elif not any(isinstance(c, Node) and c.tag in ('main', 'header', 'footer') for c in kids):
        # A React/Vite prerender: one app wrapper holding the chrome, plus
        # portal siblings (toasts, dialogs) that stay ordinary sections.
        holders = [c for c in kids if isinstance(c, Node) and c.tag in ('div', 'section') and any(isinstance(g, Node) and g.tag in ('main', 'header', 'footer') for g in c.children)]
        wrapper = holders[0] if len(holders) == 1 else None
    before, after, level = [], [], kids
    if wrapper is not None:
        index = kids.index(wrapper)
        before, after, level = kids[:index], kids[index + 1:], meaningful(wrapper.children)
    mains = [c for c in level if isinstance(c, Node) and c.tag == 'main']
    main = mains[0] if len(mains) == 1 else None
    if wrapper is None and main is None:
        return None, None, [(c, None, 'main') for c in kids]
    main_index = level.index(main) if main is not None else None
    outside = [(i, c) for i, c in enumerate(level) if isinstance(c, Node) and c is not main]

    def pick(name, candidates, fallback_tags):
        hint = ((chrome or {}).get(name) or {}).get('selector') if isinstance((chrome or {}).get(name), dict) else None
        for predicate in ([lambda n: selector_matches(n, hint)] if hint else []) + [lambda n, t=t: n.tag == t for t in fallback_tags]:
            found = [(i, c) for i, c in candidates if predicate(c)]
            if found:
                return found[0] if name == 'header' else found[-1]
        return None

    header = pick('header', [(i, c) for i, c in outside if main_index is None or i < main_index], ('header', 'nav'))
    footer = pick('footer', [(i, c) for i, c in outside if (main_index is None or i > main_index) and (header is None or c is not header[1])], ('footer',))
    parts = {}
    if header:
        parts[id(header[1])] = 'header'
    if footer:
        parts[id(footer[1])] = 'footer'
    items = [(c, None, 'before') for c in before]
    seen_main = False
    for child in level:
        if child is main:
            seen_main = True
            items.extend((c, None, 'main') for c in meaningful(main.children))
        elif main is None:
            part = parts.get(id(child))
            items.append((child, part, ('head' if part == 'header' else 'tail') if part else 'main'))
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


def chrome_equivalent(first, other, first_file='', other_file=''):
    """Header/footer trees equal after removing active-link state.

    Ignored: ACTIVE_ATTRIBUTES, ACTIVE_CLASSES, class differences on an element
    marked active (aria-current / data-status="active") on either page, and
    class-set swaps that occur symmetrically between two links (A→B on one,
    B→A on another). Relative href/src values compare by resolved target, so
    nested pages (../about.html) match top-level ones (about.html)."""
    swaps = []
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
        attrs = lambda n, page: {k: resolved(v, page) if k in ('href', 'src') else v for k, v in n.attrs.items() if k not in ACTIVE_ATTRIBUTES and k != 'class'}
        if a.tag != b.tag or attrs(a, first_file) != attrs(b, other_file) or len(a.children) != len(b.children):
            return False
        classes_a = set((a.attrs.get('class') or '').split()) - ACTIVE_CLASSES
        classes_b = set((b.attrs.get('class') or '').split()) - ACTIVE_CLASSES
        if classes_a != classes_b and not (is_active_marker(a) or is_active_marker(b)):
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
    query = {k: v for k, v in listing['attributes']['query'].items() if k != 'perPage'}
    query['inherit'] = True
    listing['attributes']['query'] = query
    for zone in zones.values():
        for block in blocks(zone):
            block.pop('sourceIds', None)
    return frame_shell(frame, zones['head'], zones['main'], zones['tail'], zones['before'], zones['after'])


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
    for page in pages:
        source = local_file(dist, page['file']).read_bytes()
        parser = Parser(source.decode('utf-8'))
        nodes = list(walk(parser.root))
        body = next((n for n in nodes if n.tag == 'body'), parser.root)
        mapper = Mapper(dist, page, links)
        mapper.counter = counter
        mapper.labels = {n.attrs['for']: {'text': plain(n).strip(), 'class': n.attrs.get('class', '')} for n in nodes if n.tag == 'label' and n.attrs.get('for')}
        for node in nodes:
            if node.tag == 'link' and 'stylesheet' in (node.attrs.get('rel') or '').split():
                target = mapper.url(node.attrs.get('href', ''), True)
                if target.startswith('asset:'):
                    mapper.styles.add(target[6:].split('?')[0])
                else:
                    mapper.finding('external-stylesheet', target)
            if node.tag == 'script':
                if node.attrs.get('src'):
                    target = mapper.url(node.attrs['src'], True)
                    if target.startswith('asset:'):
                        mapper.scripts.add(target[6:].split('?')[0])
                mapper.finding('source-runtime', 'Review and replace source script: ' + node.attrs.get('src', 'inline'))
            if node.tag == 'style':
                mapper.finding('inline-stylesheet', 'Extract inline CSS to dist asset and contract.styles')
        wrapper, main, items = split_frame(body, manifest.get('chrome'))
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
        entries.append({'key': page['key'], 'page': page, 'kind': page.get('kind'), 'source': source, 'mapper': mapper, 'items': items, 'frame': frame,
                        'sections': ['source-' + str(i + 1).zfill(4) for i in range(len(items))], 'title': title, 'description': description,
                        'h1': norm(plain(heading)) if heading is not None else norm(page.get('title') or '')})
    # Post metadata drives card/article classification (spec 2 D3/D6).
    metas = {}
    for entry in entries:
        if entry['kind'] == 'post':
            given = entry['page'].get('post') if isinstance(entry['page'].get('post'), dict) else {}
            metas[entry['key']] = {'title': entry['h1'], 'excerpt': given.get('excerpt') or entry['description'], 'categories': [c for c in given.get('categories', []) if isinstance(c, str)]}
    for entry in entries:
        entry['mapper'].posts = metas
    article = analyze_articles([e for e in entries if e['kind'] == 'post'], metas)

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
                kids = [b for b in (mapper.block(c) for c in article['bodies'][key].children) if b]
                mapper.queries = True
                if not kids:
                    # Nothing mappable (e.g. only unmapped elements): keep coverage on a placeholder.
                    mapper.finding('article-body-empty', 'Article body has no mappable content; an empty group carries its source ids')
                    kids = [{'name': 'core/group', 'attributes': {'layout': {'type': 'default'}}, 'innerBlocks': []}]
                kids[0]['sourceIds'] = [sections[i] for i in owned if i < index] + [section]
                kids[-1]['sourceIds'] = kids[-1].get('sourceIds', []) + [sections[i] for i in owned if i > index]
                blocks.extend(kids)
                continue
            mapper.queries = not part and not is_post
            block = mapper.block(strip_active(child) if part else child)
            mapper.queries = True
            if block and part:
                page_parts[part] = {'section': section, 'blocks': [block], 'source': child, 'active': has_active(child)}
                block = {'name': 'core/template-part', 'attributes': {'slug': part}}
            elif block:
                label(block, child, mapper)
            if block:
                block['sourceIds'] = [section]
                blocks.append(block)
                mapped.append((block, place))
        frames.append((key, entry['frame'], page_parts))
        entry['mapped'] = mapped
        source_hash = digest(entry['source'])
        contract['pages'].append({'key': key, 'file': entry['page']['file'], 'sha256': source_hash, 'sections': sections})
        contract['styles'] = sorted(set(contract['styles']) | mapper.styles)
        # Source application scripts are inventoried but never automatically
        # enqueued: hydration can erase WordPress content and interactivity.
        proposal = {'key': key, 'sourceHash': source_hash, 'sections': sections, 'blocks': blocks, 'seo': {'title': entry['title'], 'description': entry['description']}}
        for field in ('slug', 'template', 'post', 'product'):
            if field in entry['page']:
                proposal[field] = entry['page'][field]
        if is_post:
            # Spec 2 D4: inferred post metadata; manifest values win.
            meta = metas[key]
            post = dict(proposal.get('post') or {})
            inferred = {'date': meta.get('date'), 'categories': meta.get('categories') or None, 'readTime': meta.get('readTime'), 'excerpt': meta.get('excerpt') or None}
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

    # One frame for the whole theme: the most common page frame (ties → first page).
    counts = {}
    for _, frame, _ in frames:
        counts.setdefault(digest(frame), [0, frame])[0] += 1
    chosen = max(counts.values(), key=lambda entry: entry[0])[1] if counts else None
    if chosen is not None:
        contract['frame'] = chosen
    owners, active_state = {}, None
    files = {p['key']: p['file'] for p in pages}
    for key, frame, page_parts in frames:
        if digest(frame) != digest(chosen):
            page_finding(key, 'frame-variant', '', 'Page frame ' + json.dumps(frame, sort_keys=True) + ' differs from the shared frame ' + json.dumps(chosen, sort_keys=True) + '; review the page wrapper/main markup')
        for part, entry in page_parts.items():
            if entry['active'] and active_state is None:
                active_state = (key, entry['section'])
            if part not in contract['parts']:
                contract['parts'][part] = entry['blocks']
                owners[part] = (key, entry['source'])
            elif not chrome_equivalent(owners[part][1], entry['source'], files[owners[part][0]], files[key]):
                page_finding(key, 'chrome-variant', entry['section'], 'Source ' + part + ' differs from the shared ' + part + ' part (taken from ' + owners[part][0] + ') beyond active-link state; page-specific ' + part + ' markup is replaced by the shared part')
    if active_state:
        # Informational and resolvable like every other finding; raised once per workspace.
        page_finding(active_state[0], 'chrome-active-state', active_state[1], "Shared header keeps the first page's link classes; per-page active styling needs core/navigation or CSS")
    for key, frame, page_parts in frames:
        for part in contract['parts']:
            if part not in page_parts and frame is not None:
                page_finding(key, 'chrome-variant', '', 'Page has no source ' + part + '; the shared ' + part + ' part will be rendered')
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
    if inline_styles:
        css_path = local_file(dist, 'assets/gutenberg-inline.css')
        css_path.parent.mkdir(parents=True, exist_ok=True)
        css_path.write_text('\n'.join('.' + name + inline_scopes.get(name, '') + '{' + style + '}' for name, style in sorted(inline_styles.items())) + '\n')
        contract['styles'].append('assets/gutenberg-inline.css')
    contract_hash = digest(contract)
    write(output / 'contract.json', contract)
    for key, proposal in proposals.items():
        write(output / 'pages' / (key + '.json'), proposal)
    write(state / 'inventory.json', {'schema': 'h2wp-inventory/1', 'pages': inventory, 'fragments': [p for p in manifest.get('pages', []) if p.get('kind') == 'fragment']})
    families = {}
    for page in pages:
        families.setdefault(page.get('family') or page.get('kind') or 'page', []).append(page['key'])
    write(state / 'tasks.json', {'schema': 'h2wp-tasks/1', 'maxWorkers': 3, 'contractHash': contract_hash, 'tasks': [{'id': 'family-' + str(i + 1), 'family': family, 'pages': keys, 'representative': keys[0], 'owner': None, 'status': 'pending'} for i, (family, keys) in enumerate(families.items())]})
    write(state / 'checkpoint.json', {'schema': 'h2wp-checkpoint/1', 'contractHash': contract_hash, 'sourceHashes': {p['key']: p['sha256'] for p in contract['pages']}, 'assetHashes': asset_hashes(dist, contract), 'startedAt': time.time(), 'resolutions': {}, 'metrics': []})
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', default='prepare', choices=['prepare', 'check', 'finalize', 'freeze', 'claim', 'complete'])
    parser.add_argument('--manifest', required=True)
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
        return checkpoint_task(args)
    except (ValueError, KeyError, OSError, TypeError) as error:
        print('gutenberg plan: ' + str(error), file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
