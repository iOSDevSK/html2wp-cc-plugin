"""The DOM an h2wp/element page shows, normalized for comparison.

One normalizer for two readers: tools/test-gutenberg-element-equivalence.py
(an element's saved HTML against the theme's render, on fixtures) and
gutenberg-verify-local.py's withoutTheme row (the same on delivered pages).
Tag names, attribute sets (class as a set, style as its set of declarations,
values entity-decoded), text entity-decoded with white space collapsed; block
comments dropped.
"""
from html.parser import HTMLParser
import re

VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'source', 'track', 'wbr'}


class Tree(HTMLParser):
    """[tag, {attribute: value}, children] with text nodes as strings."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = ['#root', {}, []]; self.stack = [self.root]
    def handle_starttag(self, tag, attrs):
        node = [tag, {k: (v if v is not None else '') for k, v in attrs}, []]
        self.stack[-1][2].append(node)
        if tag not in VOID: self.stack.append(node)
    def handle_startendtag(self, tag, attrs):
        self.stack[-1][2].append([tag, {k: (v if v is not None else '') for k, v in attrs}, []])
    def handle_endtag(self, tag):
        if tag in VOID: return
        # Close up to the matching open element (an unclosed <p> or <li> in
        # rendered HTML ends where its parent does).
        for depth in range(len(self.stack) - 1, 0, -1):
            if self.stack[depth][0] == tag:
                del self.stack[depth:]
                return
    def handle_data(self, data):
        self.stack[-1][2].append(data)


def dom(html, block_styles=None):
    """The normalized DOM of an HTML fragment (block comments dropped). With
    block_styles ({name: classes}), a class token is-style-<name> reads as
    the variation's classes, as the theme renders it."""
    parser = Tree(); parser.feed(re.sub(r'<!-- /?wp:.*? -->', '', html, flags=re.S)); parser.close()
    styles = block_styles or {}
    def classes(value):
        out = []
        for token in value.split():
            name = token[9:] if token.startswith('is-style-') else None
            out.extend(styles[name].split() if name is not None and name in styles else [token])
        return ' '.join(sorted(set(out)))
    def norm(node):
        if isinstance(node, str):
            text = re.sub(r'\s+', ' ', node)
            return text if text.strip() else None
        tag, attrs, children = node
        # class as a set; style as its set of declarations (the PHP and JS
        # style engines order them differently).
        attrs = {k: (classes(v) if k == 'class' else ';'.join(sorted(d.strip() for d in v.split(';') if d.strip())) if k == 'style' else v) for k, v in attrs.items()}
        kids, pending = [], ''
        for child in (norm(c) for c in children):
            if child is None: continue
            if isinstance(child, str): pending += child; continue
            if pending.strip(): kids.append(re.sub(r'\s+', ' ', pending).strip())
            pending = ''; kids.append(child)
        if pending.strip(): kids.append(re.sub(r'\s+', ' ', pending).strip())
        return [tag, attrs, kids]
    return norm(parser.root)[2]


def first_difference(a, b, path=''):
    """Where two normalized DOMs first differ, as {path, a, b}; None when equal."""
    if isinstance(a, str) or isinstance(b, str):
        return None if a == b else {'path': path or '/', 'a': a, 'b': b}
    if isinstance(a, list) and a and isinstance(a[0], str) and len(a) == 3 and isinstance(a[1], dict):
        if not (isinstance(b, list) and len(b) == 3 and isinstance(b[1], dict)) or a[0] != b[0]:
            return {'path': path or '/', 'a': a[0] if isinstance(a, list) else a, 'b': b[0] if isinstance(b, list) and b else b}
        here = path + '/' + a[0]
        if a[1] != b[1]:
            return {'path': here, 'a': a[1], 'b': b[1]}
        return first_difference(a[2], b[2], here)
    for i, (x, y) in enumerate(zip(a, b)):
        found = first_difference(x, y, f'{path}[{i}]')
        if found: return found
    if len(a) != len(b):
        return {'path': f'{path}[{min(len(a), len(b))}]', 'a': a[len(b)] if len(a) > len(b) else None, 'b': b[len(a)] if len(b) > len(a) else None}
    return None
