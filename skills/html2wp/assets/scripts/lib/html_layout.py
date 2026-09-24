# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
"""A page as a flat list of elements with their ancestors, and the manifest's
selector grammar over it.

Standard library only. The grammar is the one conversion-manifest.json
documents for its selector fields: `tag`, `tag.class` (several classes),
`#id`, and descendant or direct `>` paths — anything else parses to None and
the caller leaves it alone. Carried over from the desktop app's
manifest-check.py, where the same parser guarded the manifest before a
conversion; check-manifest.py and flash-manifest.py share it now.
"""
import re
from html.parser import HTMLParser

VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}


class Layout(HTMLParser):
    """nodes: [{tag, attrs, ancestors, text}] in document order; `text` is
    the element's own and its descendants' text, collected as it closes."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.nodes = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        node = {'tag': tag, 'attrs': {k: (v or '') for k, v in attrs}, 'ancestors': list(self.stack), 'text': []}
        self.nodes.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID:
            self.stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index]['tag'] == tag:
                del self.stack[index:]
                break

    def handle_data(self, data):
        for node in self.stack:
            node['text'].append(data)


def layout(html):
    parsed = Layout()
    parsed.feed(html)
    for node in parsed.nodes:
        node['text'] = re.sub(r'\s+', ' ', ''.join(node['text'])).strip()
    return parsed.nodes


def classes(node):
    return (node['attrs'].get('class') or '').split()


def children(nodes, parent):
    return [n for n in nodes if n['ancestors'] and n['ancestors'][-1] is parent]


COMPOUND = re.compile(r'^([a-zA-Z][a-zA-Z0-9-]*|\*)?((?:[.#][A-Za-z0-9_-]+)*)$')


def parse_selector(selector):
    """Steps of (combinator, tag, classes, id), or None outside the grammar."""
    tokens = re.findall(r'>|[^\s>]+', (selector or '').strip())
    steps, combinator = [], ' '
    for token in tokens:
        if token == '>':
            combinator = '>'
            continue
        m = COMPOUND.match(token)
        if not m or not token:
            return None
        parts = re.findall(r'[.#][A-Za-z0-9_-]+', m.group(2) or '')
        steps.append((combinator, (m.group(1) or '*').lower(), {p[1:] for p in parts if p[0] == '.'},
                      next((p[1:] for p in parts if p[0] == '#'), None)))
        combinator = ' '
    return steps or None


def matches(node, step):
    _, tag, want, ident = step
    if tag != '*' and node['tag'] != tag:
        return False
    if not want <= set(classes(node)):
        return False
    return ident is None or node['attrs'].get('id') == ident


def select(nodes, steps):
    def fits(node, index):
        if not matches(node, steps[index]):
            return False
        if index == 0:
            return True
        ancestors = node['ancestors']
        if steps[index][0] == '>':
            return bool(ancestors) and fits(ancestors[-1], index - 1)
        return any(fits(a, index - 1) for a in ancestors)
    return [n for n in nodes if fits(n, len(steps) - 1)]


SAFE_CLASS = re.compile(r'^[A-Za-z_-][A-Za-z0-9_-]*$')


def compound(node):
    """`tag.class.class` for a node, with only the classes the grammar can spell."""
    return node['tag'] + ''.join('.' + c for c in classes(node) if SAFE_CLASS.match(c))


def unique_path(nodes, node):
    """The shortest selector in the grammar that matches exactly this node:
    its own compound, else a `>` path up through its ancestors."""
    chain = [node]
    for ancestor in reversed(node['ancestors']):
        text = ' > '.join(compound(n) for n in chain)
        steps = parse_selector(text)
        if steps and select(nodes, steps) == [node]:
            return text
        chain.insert(0, ancestor)
    text = ' > '.join(compound(n) for n in chain)
    steps = parse_selector(text)
    return text if steps and select(nodes, steps) == [node] else None
