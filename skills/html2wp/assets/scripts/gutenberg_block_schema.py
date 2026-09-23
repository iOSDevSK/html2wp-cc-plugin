#!/usr/bin/env python3
"""Pre-flight check of a Gutenberg block plan, before it goes to the service.

The service's compiler refuses a plan whose block trees break its rules, and
used to say so one violation per round trip. This checks the same trees on
this machine, against block-schema.json (exported from the compiler's own
tables), and returns every violation at once, in the service's own words, so
the planner repairs them in one pass. The service stays the authority: it
re-validates everything, and this module only ever reports what the service
would refuse. A rule it cannot mirror exactly, it leaves to the service.

    validate_trees(plan_dir) -> [{'at': ..., 'message': ...}, ...]

plan_dir is the workspace's block-plan/ directory (contract.json and
pages/<key>.json). The conversion manifest is read from the workspace
(plan_dir/../conversion-manifest.json) unless one is passed; without it the
checks that need page kinds or the shop flag are skipped.

    python3 gutenberg_block_schema.py <block-plan dir> [--manifest <path>]

prints the list as JSON and exits 1 when it is not empty.
"""
import json
import math
import posixpath
import re
import sys
from pathlib import Path

SCHEMA_FILE = Path(__file__).with_name('block-schema.json')
SCHEMA_ID = 'h2wp-block-schema/1'

# JavaScript's \s, for patterns ported from the compiler.
WS = '\t\n\x0b\x0c\r \xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff'
AI = re.ASCII | re.IGNORECASE


class _Undefined:
    """JavaScript's undefined: a key that is not there."""
    def __repr__(self):
        return 'undefined'


UNDEFINED = _Undefined()


class Violation(Exception):
    """One broken rule; the text is the service's `${at}: ${what}`."""


def violation(text):
    """The service's {at, message} split of a rule's text."""
    at, sep, message = text.partition(': ')
    return {'at': at, 'message': message} if sep else {'at': '', 'message': text}


def _raise(text):
    raise Violation(text)


# ---- JavaScript values -------------------------------------------------------

def _get(value, key):
    return value.get(key, UNDEFINED) if isinstance(value, dict) else UNDEFINED


def _truthy(value):
    if value is UNDEFINED or value is None or value is False:
        return False
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value != 0 and not (isinstance(value, float) and math.isnan(value))
    if isinstance(value, str):
        return value != ''
    return True


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite(value):
    return _number(value) and math.isfinite(value)


def _integer(value):
    return _finite(value) and float(value).is_integer()


def _u16(text):
    """String length as JavaScript counts it (UTF-16 code units)."""
    return len(text.encode('utf-16-le', 'surrogatepass')) // 2


def _js(value):
    """String(value)."""
    if value is UNDEFINED:
        return 'undefined'
    if value is None:
        return 'null'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, float):
        if math.isnan(value):
            return 'NaN'
        if math.isinf(value):
            return 'Infinity' if value > 0 else '-Infinity'
        return str(int(value)) if value.is_integer() and abs(value) < 1e21 else repr(value)
    if isinstance(value, list):
        return ','.join('' if item is None else _js(item) for item in value)
    if isinstance(value, dict):
        return '[object Object]'
    return str(value)


_INDEX = re.compile(r'0|[1-9][0-9]*')


def _keys(value):
    """Object.keys order: array-index keys ascending, then the rest as written."""
    keys = list(value)
    index = sorted((k for k in keys if _INDEX.fullmatch(k) and int(k) < 2 ** 32 - 1), key=int)
    return index + [k for k in keys if k not in index]


def _strict(value, allowed, at, fail=_raise):
    if not isinstance(value, dict):
        return fail(f'{at}: expected object')
    for key in _keys(value):
        if key not in allowed:
            fail(f'{at}: unknown attribute {key}')


def _js_pattern(pattern):
    """A compiler (JavaScript) pattern for Python: `$` is the end of input."""
    out, in_class, i = [], False, 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == '\\':
            out.append(pattern[i:i + 2])
            i += 2
            continue
        if ch == '[':
            in_class = True
        elif ch == ']':
            in_class = False
        elif ch == '$' and not in_class:
            ch = r'\Z'
        out.append(ch)
        i += 1
    return re.compile(''.join(out), re.ASCII)


# ---- the compiler's value rules (gutenberg-blocks.mjs, -extra.mjs) -----------

_CSS_FORBIDDEN = re.compile(rf'(?:var|url|expression|image|image-set|element|attr|env)[{WS}]*\(', AI)
_HEX = re.compile(r'#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})', AI)
_COLOR_FN = re.compile(rf'(?:rgba?|hsla?|oklch|oklab|lab|lch|hwb)\([0-9a-z.%,/{WS}+-]*\)', AI)
_LENGTH = re.compile(r'-?(?:[0-9]+|[0-9]*\.[0-9]+)(?:px|rem|em|%|vh|vw|vmin|vmax|svh|lvh|dvh|svw|lvw|dvw|ch|ex|pt|cqw|cqh|cqi|cqb|cqmin|cqmax)', AI)
_SIZE_FN = re.compile(rf'(?:clamp|min|max|calc)\([0-9a-z.%,+*/{WS}()-]*\)', AI)
_PRESET_VAR = re.compile(r'var:preset\|[a-z][a-z-]{0,31}\|[a-z0-9][a-z0-9-]{0,63}')
_UNITLESS = re.compile(r'(?:[0-9]+|[0-9]*\.[0-9]+)')
_STYLE_KEYWORDS = {'auto', 'inherit', 'initial', 'unset', 'normal', 'transparent', 'currentcolor', 'none'}
_SLUG = re.compile(r'[a-z0-9][a-z0-9-]{0,63}')
_KEY = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]*')
_RICH_TAG = re.compile(r'<[^>]*>')
_RICH_CLOSE = re.compile(r'</(?:a|strong|em|b|i|code|s|sub|sup|span)>')
_RICH_OPEN = re.compile(r'<(?:strong|em|b|i|code|s|sub|sup)>')
_RICH_BR = re.compile(rf'<br[{WS}]*/?[{WS}]*>')
_RICH_ATTRS = re.compile(rf'<(?:a|span)(?:[{WS}]+[A-Za-z0-9_-]+="[^"]*")*[{WS}]*>')
_RICH_ATTR = re.compile(r'([A-Za-z0-9_-]+)="([^"]*)"')
_EXTRA_COLOR = re.compile(rf'(?:#(?:[0-9a-f]{{3,4}}|[0-9a-f]{{6}}|[0-9a-f]{{8}})|(?:rgba?|hsla?|oklch|oklab|lab|lch|hwb)\([0-9a-z.%,/{WS}+-]*\))', AI)
_REFERENCE = re.compile(rf'(?<![A-Za-z0-9_])(asset|page):([^{WS}<>"\')]+)')
_MENU_CLASS = re.compile(rf'(?:^|[{WS}])h2wp-menu-([a-zA-Z0-9][a-zA-Z0-9_-]*)(?=[{WS}]|\Z)')
_CUT = re.compile(r'cut:[0-9]{1,3}:(?:\.\.\.|…)')
_TAXONOMY = re.compile(r'[a-z0-9_-]{1,32}')


def _tax_query(value, named):
    """taxQuery (gutenberg-blocks.mjs taxQuery): {taxonomy: [id…]}, or on a
    post query {include|exclude: {taxonomy: [id or term name…]}}."""
    if not isinstance(value, dict):
        return False
    parts = _keys(value)
    if not named or not any(k in ('include', 'exclude') for k in parts):
        return all(isinstance(a, list) and all(_integer(n) for n in a) for a in value.values())
    term = lambda n: _integer(n) or (isinstance(n, str) and not re.fullmatch(rf'[{WS}]*', n) and _u16(n) <= 200)
    return all(k in ('include', 'exclude') and isinstance(value[k], dict) and all(_TAXONOMY.fullmatch(t) and isinstance(terms, list) and all(term(n) for n in terms) for t, terms in value[k].items()) for k in parts)


def _safe_color(value):
    if not isinstance(value, str) or _u16(value) > 200:
        return False
    return bool(_HEX.fullmatch(value)) or (bool(_COLOR_FN.fullmatch(value)) and not _CSS_FORBIDDEN.search(value))


def _safe_size(value):
    if not isinstance(value, str) or _u16(value) > 200:
        return False
    if value == '0' or _LENGTH.fullmatch(value):
        return True
    if not _SIZE_FN.fullmatch(value) or _CSS_FORBIDDEN.search(value):
        return False
    depth = 0
    for ch in value:
        if ch == '(':
            depth += 1
        if ch == ')':
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _style_value(value):
    if not isinstance(value, str):
        return False
    if _PRESET_VAR.fullmatch(value) or value.lower() in _STYLE_KEYWORDS or _UNITLESS.fullmatch(value):
        return True
    return _safe_color(value) or _safe_size(value)


def _validate_url(value, at, category=True):
    if (not isinstance(value, str) or re.search(r'[\x00-\x20\\<>"\']', value)
            or re.match(r'(?:javascript|data|vbscript):', value, AI) or re.search(r'&(?:#|colon)', value, AI)):
        _raise(f'{at}: unsafe URL')
    schemes = r'(?:https?|mailto|tel|asset|page|category):' if category else r'(?:https?|mailto|tel|asset|page):'
    if re.match(r'[a-z][A-Za-z0-9_+.-]*:', value, AI) and not re.match(schemes, value, AI):
        _raise(f'{at}: unsupported URL scheme')
    if category and re.match(r'category:', value, AI) and not re.fullmatch(r'category:[A-Za-z0-9%._~-]+', value):
        _raise(f'{at}: invalid category link')


def _validate_rich(value, at, category=True):
    if not isinstance(value, str):
        _raise(f'{at}: expected rich text string')
    for tag in _RICH_TAG.findall(value):
        if _RICH_CLOSE.fullmatch(tag) or _RICH_OPEN.fullmatch(tag) or _RICH_BR.fullmatch(tag):
            continue
        if _RICH_ATTRS.fullmatch(tag):
            for name, attr in _RICH_ATTR.findall(tag):
                if name not in ('href', 'class', 'target', 'rel'):
                    _raise(f'{at}: unsupported rich text attribute {name}')
                if name == 'href':
                    _validate_url(attr, at, category)
            continue
        _raise(f'{at}: unsupported rich text markup {tag}')
    if '<' in _RICH_TAG.sub('', value):
        _raise(f'{at}: malformed rich text')


class _Checker:
    def __init__(self, schema):
        self.schema = schema
        self.blocks = schema['blocks']
        self.types = schema['types']
        element = schema['element']
        self.html_attributes = set(element['attributes'])
        self.html_patterns = []
        for pattern in element['attributePatterns']:
            try:
                self.html_patterns.append(_js_pattern(pattern))
            except re.error:
                self.html_patterns.append(None)  # accept: the service decides
        self.icon_tags = set(schema['iconTags'])
        self.icon_limits = schema['iconLimits']
        self.text_binds = schema['binds']['text']
        self.link_binds = schema['binds']['link']
        self.share = re.compile('|'.join(re.escape(p) for p in schema['binds'].get('sharePlaceholders', [])) or '(?!)')

    # check(value, type, at)
    def check(self, value, kind, at):
        spec = self.types.get(kind)
        if spec is None:
            return  # a type this client does not know: the service decides
        if 'enum' in spec:
            if isinstance(value, str) and value in spec['enum']:
                return
            if kind == 'groupTag':
                _raise(f'{at}: expected tag ' + '|'.join(spec['enum']))
            if kind == 'formHandler':
                _raise(f'{at}: unknown form handler')
            if kind == 'pageContentWrapper':
                _raise(f'{at}: expected page cart|checkout')
            if kind == 'productCollection':
                _raise(f'{at}: unsupported product collection')
            if kind == 'bind':
                _raise(f'{at}: unknown bind {_js(value)}')
            if kind == 'x:service':
                _raise(f'{at}: unsupported social service')
            if kind.startswith('x:'):
                _raise(f'{at}: invalid {kind[2:]}')
            _raise(f'{at}: expected {kind}')
        rule = getattr(self, '_' + kind.replace(':', '_'), None)
        if rule is None:
            return
        rule(value, at, kind)

    def _rich(self, value, at, kind):
        _validate_rich(value, at)

    def _url(self, value, at, kind):
        _validate_url(value, at)

    def _strings(self, value, at, kind):
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            _raise(f'{at}: expected string array')

    def _lock(self, value, at, kind):
        _strict(value, ['move', 'remove'], at)
        for k in _keys(value):
            if not isinstance(value[k], bool):
                _raise(f'{at}.{k}: expected boolean')

    def _metadata(self, value, at, kind):
        _strict(value, ['name', 'bindings'], at)
        name = _get(value, 'name')
        if name is not UNDEFINED and (not isinstance(name, str) or _u16(name) > 80):
            _raise(f'{at}.name: expected string of at most 80 characters')
        bindings = _get(value, 'bindings')
        if bindings is not UNDEFINED:
            _strict(bindings, ['url', 'alt'], f'{at}.bindings')
            for attribute in _keys(bindings):
                binding = bindings[attribute]
                _strict(binding, ['source', 'args'], f'{at}.bindings.{attribute}')
                if binding.get('source', UNDEFINED) != 'h2wp/post-image':
                    _raise(f'{at}.bindings.{attribute}: unknown binding source')
                args = binding.get('args', UNDEFINED)
                if args is not UNDEFINED and json.dumps(args) != json.dumps({'key': 'alt'} if attribute == 'alt' else {}):
                    _raise(f'{at}.bindings.{attribute}: unsupported args')

    def _elementStyle(self, value, at, kind):
        _strict(value, ['color', 'typography', 'spacing'], at)

        def leaf(v, where):
            if not _style_value(v):
                _raise(f'{where}: unsafe style value')

        def box(v, where):
            if isinstance(v, str):
                return leaf(v, where)
            _strict(v, ['top', 'right', 'bottom', 'left'], where)
            for side in _keys(v):
                leaf(v[side], f'{where}.{side}')

        for group, allowed, each in (('color', ['text', 'background'], leaf), ('typography', ['fontSize', 'lineHeight'], leaf), ('spacing', ['padding', 'margin'], box)):
            if group in value:
                _strict(value[group], allowed, f'{at}.{group}')
                for k in _keys(value[group]):
                    each(value[group][k], f'{at}.{group}.{k}')

    def _presetSlug(self, value, at, kind):
        if not isinstance(value, str) or not _SLUG.fullmatch(value):
            _raise(f'{at}: expected preset slug')

    def _layout(self, value, at, kind):
        _strict(value, ['type', 'orientation', 'justifyContent', 'flexWrap', 'columnCount'], at)
        if value.get('type', UNDEFINED) not in ('default', 'constrained', 'flex', 'grid'):
            _raise(f'{at}: invalid layout type')
        for k in _keys(value):
            v = value[k]
            if (k != 'columnCount' and not isinstance(v, str)) or (k == 'columnCount' and (not _integer(v) or v < 1)):
                _raise(f'{at}: invalid layout {k}')

    def _fieldMap(self, value, at, kind):
        if not isinstance(value, dict) or any(not re.fullmatch(r'[a-z0-9_-]{1,64}', k, AI) or not isinstance(v, str) or _u16(v) > 64 for k, v in value.items()):
            _raise(f'{at}: expected field name → plugin field name')

    def _searchQuery(self, value, at, kind):
        if not isinstance(value, dict) or any(not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}', k, AI) or k == 's' or not isinstance(v, str) or _u16(v) > 200 for k, v in value.items()):
            _raise(f'{at}: expected search parameters (name → text, not s)')

    def _formSuccess(self, value, at, kind):
        _strict(value, ['kind', 'html', 'list', 'region', 'ms'], at)
        html = value.get('html', UNDEFINED)
        if (value.get('kind', UNDEFINED) not in ('toast', 'inline', 'replace') or not isinstance(html, str) or _u16(html) > 20000
                or re.search(rf'<script|<iframe|[{WS}]on[a-z]+=|javascript:', html, AI)):
            _raise(f'{at}: invalid form success feedback')
        tag = re.compile(rf'<(?:ol|ul|div|section)(?:[{WS}]+[a-zA-Z-]+="[^"<>]*")*[{WS}]*>(?:</(?:ol|ul|div|section)>)?')
        for k in ('list', 'region'):
            v = value.get(k, UNDEFINED)
            if v is not UNDEFINED and v != '' and (not isinstance(v, str) or not tag.fullmatch(v)):
                _raise(f'{at}.{k}: expected an opening list/region tag')
        ms = value.get('ms', UNDEFINED)
        if ms is not UNDEFINED and (not _integer(ms) or ms < 0 or ms > 60000):
            _raise(f'{at}.ms: expected milliseconds')

    def _displayLayout(self, value, at, kind):
        _strict(value, ['type', 'columns', 'shrinkColumns'], at)
        columns, shrink = value.get('columns', UNDEFINED), value.get('shrinkColumns', UNDEFINED)
        if (value.get('type', UNDEFINED) not in ('flex', 'list') or (columns is not UNDEFINED and (not _integer(columns) or columns < 1 or columns > 6))
                or (shrink is not UNDEFINED and not isinstance(shrink, bool))):
            _raise(f'{at}: invalid displayLayout')

    def _query(self, value, at, kind):
        extra = ['isProductCollectionBlock', 'relatedBy'] if kind == 'productQuery' else []
        _strict(value, ['perPage', 'pages', 'offset', 'postType', 'order', 'orderBy', 'author', 'search', 'exclude', 'sticky', 'inherit', 'taxQuery', 'parents', *extra], at)
        flag = value.get('isProductCollectionBlock', UNDEFINED)
        if flag is not UNDEFINED and not isinstance(flag, bool):
            _raise(f'{at}: invalid isProductCollectionBlock')
        related = value.get('relatedBy', UNDEFINED)
        if related is not UNDEFINED:
            _strict(related, ['categories', 'tags'], f'{at}.relatedBy')
            if any(not isinstance(v, bool) for v in related.values()):
                _raise(f'{at}: invalid relatedBy')
        for k in _keys(value):
            v = value[k]
            if k in ('perPage', 'pages', 'offset') and (not _integer(v) or v < 0):
                _raise(f'{at}: invalid {k}')
            if k == 'inherit' and not isinstance(v, bool):
                _raise(f'{at}: invalid inherit')
            if k in ('postType', 'order', 'orderBy', 'search', 'sticky') and not isinstance(v, str):
                _raise(f'{at}: invalid {k}')
            if k in ('exclude', 'parents') and (not isinstance(v, list) or any(not _integer(n) for n in v)):
                _raise(f'{at}: invalid {k}')
            if k == 'author' and not isinstance(v, str) and not _integer(v):
                _raise(f'{at}: invalid author')
            if k == 'taxQuery' and not _tax_query(v, kind == 'query'):
                _raise(f'{at}: invalid taxQuery')

    _productQuery = _query

    def _htmlAttributes(self, value, at, kind):
        if not isinstance(value, dict):
            _raise(f'{at}: expected object')
        for k in _keys(value):
            v = value[k]
            named = k in self.html_attributes or any(p is None or p.search(k) for p in self.html_patterns)
            if not named or not (isinstance(v, (str, bool)) or _number(v)):
                _raise(f'{at}: unsafe HTML attribute {k}')
            if k == 'hidden' and not isinstance(v, bool):
                _raise(f'{at}: hidden must be boolean')
            if k == 'href':
                _validate_url(v, at)
            if k == 'type' and v not in ('button', 'submit', 'reset'):
                _raise(f'{at}: unsafe button type')
            if k == 'target' and v not in ('_blank', '_self'):
                _raise(f'{at}: unsafe link target')
            if k != 'href' and re.search(rf'url[{WS}]*\(', _js(v), AI):
                _raise(f'{at}: external SVG paint URL forbidden')
            if k == 'xmlns' and v != 'http://www.w3.org/2000/svg':
                _raise(f'{at}: invalid SVG namespace')

    def _bindFormat(self, value, at, kind):
        if value == '' or (isinstance(value, str) and (re.fullmatch(r'[A-Za-z ,.:/\-]{1,32}', value) or _CUT.fullmatch(value))):
            return
        _raise(f'{at}: unsafe date format')

    def _iconNodes(self, nodes, at, kind, depth=1, budget=None):
        budget = budget if budget is not None else [0]
        if not isinstance(nodes, list):
            _raise(f'{at}: expected node array')
        if depth > self.icon_limits['depth']:
            _raise(f"{at}: icon nodes exceed depth {self.icon_limits['depth']}")
        for i, node in enumerate(nodes):
            where = f'{at}[{i}]'
            _strict(node, ['tagName', 'htmlAttributes', 'children'], where)
            budget[0] += 1
            if budget[0] > self.icon_limits['nodes']:
                _raise(f"{at}: icon exceeds {self.icon_limits['nodes']} nodes")
            tag = node.get('tagName', UNDEFINED)
            if not isinstance(tag, str) or tag not in self.icon_tags:
                _raise(f'{where}.tagName: unsupported SVG element {_js(tag)}')
            if 'htmlAttributes' in node:
                self.check(node['htmlAttributes'], 'htmlAttributes', f'{where}.htmlAttributes')
            if 'children' in node:
                self._iconNodes(node['children'], f'{where}.children', kind, depth + 1, budget)

    def _rows(self, value, at, kind):
        if not (_integer(value) and 0 < value <= 100):
            _raise(f'{at}: expected rows')

    def _level(self, value, at, kind):
        if not (_integer(value) and 1 <= value <= 6):
            _raise(f'{at}: expected level')

    def _key(self, value, at, kind):
        if not (isinstance(value, str) and _KEY.fullmatch(value)):
            _raise(f'{at}: expected key')

    def _imageDimension(self, value, at, kind):
        if (_finite(value) and value > 0) or (isinstance(value, str) and re.fullmatch(r'(?:[0-9]+(?:\.[0-9]+)?(?:px|rem|em|vh|vw|%)|auto)', value)):
            return
        _raise(f'{at}: expected imageDimension')

    def _dimension(self, value, at, kind):
        if not (isinstance(value, str) and re.fullmatch(r'[0-9]+(?:\.[0-9]+)?(?:px|rem|em|vh|vw|%)', value)):
            _raise(f'{at}: expected dimension')

    def _number(self, value, at, kind):
        if not _finite(value):
            _raise(f'{at}: expected number')

    def _boolean(self, value, at, kind):
        if not isinstance(value, bool):
            _raise(f'{at}: expected boolean')

    def _string(self, value, at, kind):
        if not isinstance(value, str):
            _raise(f'{at}: expected string')

    def _classList(self, value, at, kind):
        if not (isinstance(value, str) and _u16(value) <= 2000 and not re.search(r'[<>"\'&\\]', value)):
            _raise(f'{at}: expected classList')

    # the extra blocks' x: types
    def _x_slug(self, value, at, kind):
        if not isinstance(value, str) or not _SLUG.fullmatch(value):
            _raise(f'{at}: invalid slug')

    def _x_columns(self, value, at, kind):
        if not _integer(value) or value < 1 or value > 8:
            _raise(f'{at}: columns must be an integer 1-8')

    def _x_percent(self, value, at, kind):
        if not _integer(value) or value < 0 or value > 100:
            _raise(f'{at}: expected integer 0-100')

    def _x_mediaWidth(self, value, at, kind):
        if not _finite(value) or value < 15 or value > 85:
            _raise(f'{at}: mediaWidth must be 15-85')

    def _x_positive(self, value, at, kind):
        if not _finite(value) or value <= 0 or value > 10000:
            _raise(f'{at}: expected positive number')

    def _x_color(self, value, at, kind):
        if not isinstance(value, str) or not _EXTRA_COLOR.fullmatch(value) or re.search(r'var\(|url\(', value, AI):
            _raise(f'{at}: unsafe color')

    def _x_cssUrl(self, value, at, kind):
        _validate_url(value, at, category=False)
        if re.search(rf'[(){WS};]', value):
            _raise(f'{at}: unsafe URL for CSS')

    def _x_focalPoint(self, value, at, kind):
        if not isinstance(value, dict) or ','.join(_keys(value)) != 'x,y':
            _raise(f'{at}: focalPoint must be {{x,y}}')
        for k in ('x', 'y'):
            v = value[k]
            if not _number(v) or not (0 <= v <= 1):
                _raise(f'{at}: focalPoint.{k} must be 0-1')

    def _x_tableRows(self, value, at, kind):
        if not isinstance(value, list) or len(value) > 1000:
            _raise(f'{at}: expected table rows')
        for r, row in enumerate(value):
            if not isinstance(row, dict) or ','.join(_keys(row)) != 'cells' or not isinstance(row['cells'], list) or len(row['cells']) > 100:
                _raise(f'{at}: row {r}: expected {{cells:[...]}}')
            for c, cell in enumerate(row['cells']):
                where = f'{at}[{r}].cells[{c}]'
                if not isinstance(cell, dict):
                    _raise(f'{where}: expected cell object')
                for k in _keys(cell):
                    if k not in ('content', 'tag', 'scope', 'align', 'colspan', 'rowspan'):
                        _raise(f'{where}: unknown cell key {k}')
                if 'content' in cell:
                    _validate_rich(cell['content'], f'{where}.content', category=False)
                if 'tag' in cell:
                    self.check(cell['tag'], 'x:cellTag', f'{where}.tag')
                if 'scope' in cell:
                    self.check(cell['scope'], 'x:scope', f'{where}.scope')
                    if cell.get('tag', UNDEFINED) != 'th':
                        _raise(f'{where}: scope requires a th cell')
                if 'align' in cell:
                    self.check(cell['align'], 'x:align', f'{where}.align')
                for k in ('colspan', 'rowspan'):
                    if k in cell and (not isinstance(cell[k], str) or not re.fullmatch(r'[1-9][0-9]{0,2}', cell[k])):
                        _raise(f'{where}: {k} must be a numeric string')


# ---- block trees (validateBlocks) --------------------------------------------

class _Trees:
    def __init__(self, schema, menus):
        self.schema = schema
        self.checker = _Checker(schema)
        self.menus = menus
        self.violations = []
        self.references = []

    def fail(self, text):
        self.violations.append(violation(text))

    def rule(self, run, *args):
        try:
            run(*args)
        except Violation as error:
            self.fail(str(error))

    def tree(self, blocks, at, depth=0, form=None, source_ids=None):
        limits = self.schema['limits']
        if not isinstance(blocks, list) or depth > limits['depth']:
            return self.fail(f'{at}: invalid block tree')
        if len(blocks) > limits['blocks']:
            return self.fail(f'{at}: too many blocks')
        fields = self.schema['nodeFields']
        for i, node in enumerate(blocks):
            where = f'{at}[{i}]'
            if not isinstance(node, dict):
                self.fail(f'{where}: expected object')
                continue
            _strict(node, fields, where, self.fail)
            name = node.get('name', UNDEFINED)
            definition = self.schema['blocks'].get(name) if isinstance(name, str) else None
            if definition is None:
                self.fail(f'{where}: unsupported block {_js(name)}')
                continue
            a = node.get('attributes', UNDEFINED)
            a = a if _truthy(a) else {}
            if not isinstance(a, dict):
                self.fail(f'{where}.attributes: expected object')
                continue
            attributes = definition['attributes']
            _strict(a, attributes, f'{where}.attributes', self.fail)
            for key in _keys(a):
                if key not in attributes:
                    continue
                self.rule(self.checker.check, a[key], attributes[key], f'{where}.{key}')
                if key != 'metadata':
                    self.collect(a[key], where)
            self.rules(node, name, a, where)
            child_form = self.form(name, a, where, form)
            if 'sourceIds' in node:
                try:
                    self.checker.check(node['sourceIds'], 'strings', where)
                    if source_ids is not None:
                        source_ids.extend(node['sourceIds'])
                except Violation as error:
                    self.fail(str(error))
            if 'innerBlocks' in node:
                if not definition['children']:
                    self.fail(f'{where}: leaf block cannot have innerBlocks')
                else:
                    self.tree(node['innerBlocks'], f'{where}.innerBlocks', depth + 1, child_form, source_ids)
            kids = node['innerBlocks'] if isinstance(node.get('innerBlocks'), list) else []
            name_of = lambda b: b.get('name', UNDEFINED) if isinstance(b, dict) else UNDEFINED
            only = self.schema['childBlocks'].get(name)
            if only and any(name_of(b) not in only for b in kids):
                self.fail(f"{where}: {name[5:]} children must be {' or '.join(c[5:] for c in only)} blocks")
            self.extra(node, name, a, kids, where, name_of)

    def collect(self, value, where):
        if isinstance(value, str):
            for match in _REFERENCE.finditer(value):
                self.references.append({'type': match.group(1), 'path': match.group(2), 'at': where})
        elif isinstance(value, list):
            for item in value:
                self.collect(item, where)
        elif isinstance(value, dict):
            for key in _keys(value):
                self.collect(value[key], where)

    def rules(self, node, name, a, where):
        get = lambda key: a.get(key, UNDEFINED)
        binds = self.schema['binds']
        if name == 'core/image' and not _truthy(get('url')):
            self.fail(f'{where}: image URL required')
        metadata = get('metadata')
        if isinstance(metadata, dict) and _truthy(metadata.get('bindings', UNDEFINED)) and name != 'core/image':
            self.fail(f'{where}: block bindings are only supported on core/image')
        bind, fmt = get('bind'), get('bindFormat')
        if name == 'h2wp/element' and _truthy(bind):
            inner = node.get('innerBlocks', UNDEFINED)
            if bind in binds['text'] and _truthy(inner) and isinstance(inner, (list, str)) and len(inner):
                self.fail(f'{where}: text bind {bind} requires an element without innerBlocks')
            if bind in binds['link'] and get('tagName') != 'a':
                self.fail(f'{where}: link bind {bind} requires tagName a')
        html = get('htmlAttributes')
        href = html.get('href') if name == 'h2wp/element' and isinstance(html, dict) and isinstance(html.get('href'), str) else ''
        if name == 'h2wp/element' and bind == 'postShare' and not self.checker.share.search(href):
            self.fail(f'{where}: bind postShare requires an href with {{postUrl}} or {{postTitle}}')
        if bind != 'postShare' and self.checker.share.search(href):
            self.fail(f'{where}: {{postUrl}} or {{postTitle}} in an href requires bind postShare')
        if name == 'h2wp/element' and _truthy(fmt) and not (not isinstance(fmt, str) and bind == 'postDate'):
            date = bind == 'postDate' and not fmt.startswith('cut:')
            excerpt = bind == 'postExcerpt' and _CUT.fullmatch(_js(fmt))
            if not date and not excerpt:
                self.fail(f'{where}: bindFormat requires bind postDate')
        if name == 'h2wp/navigation':
            menu = get('menu')
            if not _truthy(menu):
                self.fail(f'{where}: navigation menu is required')
            elif self.menus is not None and not (isinstance(menu, str) and menu in self.menus):
                self.fail(f'{where}: unknown navigation menu {_js(menu)}')
            if 'linkClassName' not in a and any(k in a for k in ('listClassName', 'listAttributes', 'itemClassName', 'currentClassName')):
                self.fail(f'{where}: listClassName/listAttributes/itemClassName/currentClassName require linkClassName')
            listed = get('listAttributes')
            if listed is not UNDEFINED and ('listClassName' not in a or (isinstance(listed, dict) and 'href' in listed)):
                self.fail(f'{where}: listAttributes require listClassName and cannot hold href')
        if name == 'core/navigation' and self.menus is not None:
            cls = get('className')
            match = _MENU_CLASS.search(_js(cls) if _truthy(cls) else '')
            if match and match.group(1) not in self.menus:
                self.fail(f'{where}: unknown navigation menu {match.group(1)}')

    def form(self, name, a, where, form):
        """The form context the children see, after the form and field rules."""
        child = form
        if name == 'h2wp/form':
            form_id = a.get('formId', UNDEFINED)
            if not _truthy(form_id):
                self.fail(f'{where}: formId is required')
            if form:
                self.fail(f'{where}: forms cannot be nested')
            child = {'id': form_id, 'fields': set()}
        if name == 'h2wp/field':
            field = a.get('name', UNDEFINED)
            if not _truthy(field):
                self.fail(f'{where}: field name is required')
            elif isinstance(field, str):
                lowered = field.lower()
                if form and lowered in form['fields']:
                    self.fail(f"{where}: duplicate field name {field} in form {_js(form['id'])}")
                if form:
                    form['fields'].add(lowered)
        return child

    def extra(self, node, name, a, kids, where, name_of):
        get = lambda key: a.get(key, UNDEFINED)
        allowed = self.schema['extraChildBlocks'].get(name)
        if allowed and any(name_of(b) not in allowed for b in kids):
            self.fail(f"{where}: {name[5:]} children must be {' or '.join(allowed)} blocks")
        if name == 'core/social-link' and kids:
            self.fail(f'{where}: social-link cannot have innerBlocks')
        if name == 'core/embed' and not _truthy(get('url')):
            self.fail(f'{where}: embed url is required')
        if name in ('core/video', 'core/audio') and not _truthy(get('src')):
            self.fail(f'{where}: {name[5:]} src is required')
        if name == 'core/file' and not _truthy(get('href')):
            self.fail(f'{where}: file href is required')
        if name == 'core/table' and not any(isinstance(get(k), (list, str)) and len(get(k)) for k in ('head', 'body', 'foot')):
            self.fail(f'{where}: table needs at least one row')
        if name == 'core/gallery' and not kids:
            self.fail(f'{where}: gallery needs at least one image')
        if name == 'core/media-text' and _truthy(get('mediaUrl')) and not _truthy(get('mediaType')):
            self.fail(f'{where}: mediaType is required with mediaUrl')
        if name == 'core/cover' and 'minHeightUnit' in a and 'minHeight' not in a:
            self.fail(f'{where}: minHeightUnit requires minHeight')
        if name == 'core/social-link' and not _truthy(get('service')):
            self.fail(f'{where}: social-link service is required')


# ---- the plan ----------------------------------------------------------------

def _read(path):
    def constant(name):
        raise ValueError(f'{name} is not JSON')
    return json.loads(path.read_text(encoding='utf-8'), parse_constant=constant)


def _kinds(manifest):
    """{key: (kind, manifest page)}, kinds as the compiler normalizes them."""
    utility = _get(_get(manifest, 'utilityPages'), '404')
    kinds = {}
    for page in manifest.get('pages') or []:
        if not isinstance(page, dict) or not isinstance(page.get('key'), str):
            continue
        kind = page.get('kind')
        if kind == 'article':
            kind = 'post'
        elif kind == 'utility':
            kind = '404' if utility in (page.get('file'), page.get('key')) else 'page'
        kinds[page['key']] = (kind, page)
    return kinds


def load_schema(path=SCHEMA_FILE):
    schema = _read(Path(path))
    if schema.get('schema') != SCHEMA_ID:
        raise ValueError(f'{path}: expected schema {SCHEMA_ID}')
    return schema


def validate_trees(plan_dir, manifest=None, schema=None):
    """Every violation of the plan's block trees, as [{'at', 'message'}].

    The trees and their order are the service's: each page (with its source
    coverage), the parts, the page frame, the templates, the patterns. Then
    the rules the service checks on the assembled plan: the template graph,
    the post and page template slots, and the pages and media a block links.
    """
    plan_dir = Path(plan_dir)
    schema = schema or load_schema()
    workspace = plan_dir.parent
    if manifest is None and (workspace / 'conversion-manifest.json').is_file():
        manifest = workspace / 'conversion-manifest.json'
    if isinstance(manifest, (str, Path)):
        manifest = _read(Path(manifest))
    if not isinstance(manifest, dict):
        manifest = None
    contract_file = plan_dir / 'contract.json'
    if not contract_file.is_file():
        return [{'at': 'block-plan/contract.json', 'message': 'missing'}]
    try:
        contract = _read(contract_file)
    except ValueError as error:
        return [{'at': 'block-plan/contract.json', 'message': f'not valid JSON: {error}'}]
    if not isinstance(contract, dict):
        return [{'at': 'block-plan/contract.json', 'message': 'expected object'}]
    menus = contract.get('menus') or []
    trees = _Trees(schema, {m['key'] for m in menus if isinstance(m, dict) and isinstance(m.get('key'), str)} if isinstance(menus, list) else set())
    kinds = _kinds(manifest) if manifest else None
    shop = manifest is not None and _get(manifest.get('shop'), 'present') is True
    inventory = {p['key']: p for p in contract.get('pages') or [] if isinstance(p, dict) and isinstance(p.get('key'), str)}

    if kinds is not None:
        keys = [k for k, (kind, _) in kinds.items() if kind != 'fragment']
    else:
        keys = sorted(p.stem for p in (plan_dir / 'pages').glob('*.json'))
    proposals, clean = {}, []

    def woo(blocks, at):
        if not isinstance(blocks, list):
            return
        for i, block in enumerate(blocks):
            if not isinstance(block, dict):
                continue
            if isinstance(block.get('name'), str) and block['name'].startswith('woocommerce/'):
                trees.fail(f'{at}[{i}]: WooCommerce blocks require manifest.shop.present=true')
            woo(block.get('innerBlocks'), f'{at}[{i}].innerBlocks')

    def tree(blocks, at):
        before = len(trees.violations)
        if manifest is not None and not shop:
            woo(blocks, at)
        source_ids = []
        trees.tree(blocks, at, source_ids=source_ids)
        if len(trees.violations) != before:
            return None
        clean.append(at)
        return source_ids

    for key in keys:
        path = plan_dir / 'pages' / (key + '.json')
        if not path.is_file():
            continue
        try:
            proposal = _read(path)
        except ValueError as error:
            trees.fail(f'block-plan/pages/{key}.json: not valid JSON: {error}')
            continue
        proposals[key] = proposal
        if not isinstance(proposal, dict):
            continue
        source_ids = tree(proposal.get('blocks', UNDEFINED), key)
        entry = inventory.get(key)
        if source_ids is not None and entry is not None and source_ids != entry.get('sections'):
            trees.fail(f'{key}: Missing, duplicate or reordered source coverage for {key}')
    parts = contract.get('parts')
    if isinstance(parts, dict):
        for name in _keys(parts):
            tree(parts[name], f'parts.{name}')
    frame = contract.get('frame')
    if isinstance(frame, dict):
        for slot in ('wrapper', 'main'):
            element = frame.get(slot)
            if isinstance(element, dict) and all(k in ('tagName', 'className', 'anchor', 'htmlAttributes') for k in element):
                tree([{'name': 'h2wp/element', 'attributes': element}], f'frame.{slot}')
    templates = contract.get('templates')
    if isinstance(templates, dict):
        for name in _keys(templates):
            tree(templates[name], f'templates.{name}')
    patterns = contract.get('patterns')
    if isinstance(patterns, list):
        for pattern in patterns:
            if isinstance(pattern, dict) and isinstance(pattern.get('slug'), str):
                tree(pattern.get('blocks', UNDEFINED), f"patterns.{pattern['slug']}")

    # The service reads the assembled plan only once every tree is clean.
    if not trees.violations:
        _graph(trees, schema, contract, proposals, kinds)
        if kinds is not None:
            _references(trees, schema, workspace / 'astro-project' / 'dist', kinds)
    seen, out = set(), []
    for v in trees.violations:
        if (v['at'], v['message']) not in seen:
            seen.add((v['at'], v['message']))
            out.append(v)
    return out


def _references(trees, schema, dist, kinds):
    """The pages and media the clean trees link, as the service resolves them."""
    media = set(schema['mediaExtensions'])
    for ref in trees.references:
        path, at = ref['path'], ref['at']
        if ref['type'] == 'page':
            target = re.split(r'[?#]', path)[0]
            if kinds.get(target, ('fragment', None))[0] == 'fragment':
                trees.fail(f'{at}: unknown page reference {path}')
            continue
        if '\\' in path or any(not s or s.startswith('.') for s in path.split('/')) or re.search(r'[\x00-\x1f?#]', path):
            trees.fail(f'{at}: unsafe path')
            continue
        target = dist / path
        if not dist.is_dir() or target.is_symlink() or not target.is_file():
            if dist.is_dir():
                trees.fail(f'{at}: missing or unsafe file {path}')
            continue
        if posixpath.splitext(path)[1].lower() not in media:
            trees.fail(f'{at}: unsupported media asset {path}')


class _Stop(Exception):
    """visitTree's first failure: the service stops that walk there."""


def _graph(trees, schema, contract, proposals, kinds):
    """The template graph and slot rules, each walk stopping where the service's does."""
    rules = schema['templates']
    query_context = set(rules['queryContext'])
    scope_binds = set(schema['binds'].get('scope', []))
    parts = contract.get('parts', UNDEFINED)
    parts = parts if _truthy(parts) else {'header': [], 'footer': []}
    if not isinstance(parts, dict):
        return
    templates = contract.get('templates') if isinstance(contract.get('templates'), dict) else {}
    schema2 = contract.get('schema') == 'h2wp-blocks/2'
    shell_parts = {name for name in ('header', 'footer') if _truthy(parts.get(name, UNDEFINED))}

    def visit(blocks, at, on_block, in_query=False, stack=(), budget=None):
        budget = budget if budget is not None else [100000]
        for block in blocks if isinstance(blocks, list) else []:
            budget[0] -= 1
            if budget[0] < 0:
                raise _Stop(f'{at}: expanded template graph exceeds 100000 blocks')
            name = _get(block, 'name')
            attributes = _get(block, 'attributes')
            context = in_query or name in query_context or (name == 'h2wp/element' and isinstance(attributes, dict) and attributes.get('bind') in scope_binds)
            on_block(block, context)
            if name == 'core/template-part':
                part = _get(_get(block, 'attributes'), 'slug')
                if not (isinstance(part, str) and part in parts):
                    raise _Stop(f'{at}: missing template part {_js(part)}')
                if part in stack:
                    raise _Stop(f"{at}: template part cycle {' -> '.join([*stack, part])}")
                if len(stack) >= 50:
                    raise _Stop(f'{at}: template part nesting exceeds 50')
                visit(parts[part], at, on_block, context, (*stack, part), budget)
            visit(_get(block, 'innerBlocks'), at, on_block, context, stack, budget)

    def walk(blocks, at, on_block=lambda block, context: None, stack=()):
        try:
            visit(blocks, at, on_block, stack=stack)
            return True
        except _Stop as stop:
            trees.fail(str(stop))
            return False

    for name in _keys(parts):
        walk(parts[name], f'parts.{name}', stack=(name,))
    for name in _keys(templates):
        walk(templates[name], f'templates.{name}')

    def count(name, block_names):
        found = {b: 0 for b in block_names}

        def on_block(block, context):
            if not context and _get(block, 'name') in found:
                found[_get(block, 'name')] += 1
        return found if walk(templates[name], f'templates.{name}', on_block) else None

    pages = []
    if kinds is not None:
        for key, (kind, page) in kinds.items():
            proposal = proposals.get(key)
            if kind == 'fragment' or not isinstance(proposal, dict):
                continue
            pages.append((key, kind, proposal, page))
    declared = contract.get('themeJson', {}).get('customTemplates') if isinstance(contract.get('themeJson'), dict) else None
    declared = [d for d in declared if isinstance(d, dict)] if isinstance(declared, list) else []

    post_templates = ['single', *[n for n in _keys(templates) if re.match(r'single-post(?:-|\Z)', n)]]
    post_templates += [_template_of(p) or 'single' for _, kind, p, _ in pages if kind == 'post']
    post_templates += [d.get('name') for d in declared if isinstance(d.get('postTypes'), list) and 'post' in d['postTypes']]
    for name in dict.fromkeys(post_templates):
        if not isinstance(name, str) or name not in templates:
            continue  # the service's own template, or one it refuses elsewhere
        found = count(name, ('core/post-title', 'core/post-content'))
        if found is None:
            continue
        if not found['core/post-title']:
            trees.fail(f'Post template {name} requires a native core/post-title outside query loops so new posts inherit their title')
        if found['core/post-content'] != 1:
            trees.fail(f"Post template {name} requires exactly one core/post-content outside query loops; found {found['core/post-content']}")

    generated = {'index', 'page', 'front-page', 'single', 'home', 'archive', 'search', '404',
                 'single-product', 'archive-product', 'page-cart', 'page-checkout', 'page-my-account'}
    not_found = next((key for key, kind, _, _ in pages if kind == '404'), None)
    page_templates = ['page', 'front-page', 'index']
    for key, kind, proposal, page in pages:
        blocks = proposal.get('blocks') if isinstance(proposal.get('blocks'), list) else []
        template = _template_of(proposal)
        if not template and key == not_found and not any(_get(b, 'name') == 'core/template-part' for b in blocks):
            template = 'page-not-found'  # its own chrome-less template, only its content
        if not template:
            if kind == 'front':
                template = 'front-page'
            elif kind == 'post':
                # The permalink: the proposal's slug ?? the manifest page's slug || key.
                slug = proposal.get('slug')
                if slug is None:
                    slug = page.get('slug') if _truthy(page.get('slug', UNDEFINED)) else key
                name = posixpath.basename(slug.rstrip('/')).lower() if isinstance(slug, str) else ''
                template = next(n for n in (f'single-post-{name}', 'single-post', 'single') if n in templates or n == 'single')
            elif kind == 'product':
                template = 'single-product'
            else:
                template = 'page'
        if kind != 'post':
            page_templates.append(template)
        shared = set()
        if template in templates:
            def on_part(block, context):
                if not context and _get(block, 'name') == 'core/template-part':
                    shared.add(_get(_get(block, 'attributes'), 'slug'))
            if not walk(templates[template], f'templates.{template}', on_part):
                continue
        elif template == 'page-not-found':
            pass
        elif template in generated:
            shared = set(shell_parts)
        else:
            continue  # a template the service refuses elsewhere
        body = [b for b in blocks if not (_get(b, 'name') == 'core/template-part' and _get(_get(b, 'attributes'), 'slug') in shared)]

        def on_body(block, context, key=key, kind=kind, template=template, shared=shared):
            if context:
                return
            name = _get(block, 'name')
            slug = _get(_get(block, 'attributes'), 'slug')
            if kind != 'post':
                if name == 'core/template-part' and slug in shared and schema2:
                    raise _Stop(f'Page {key}: shared template part {_js(slug)} remains nested in the page body')
                return
            if name in rules['articleBody']['forbidden']:
                raise _Stop(f'Post {key}: {name} belongs in template {template}, not the article body')
            if name == 'core/template-part' and slug in shared:
                raise _Stop(f'Post {key}: shared template part {_js(slug)} remains nested in the article body')
        walk(body, f'page {key}', on_body)

    if schema2:
        for name in dict.fromkeys(page_templates):
            if name not in templates:
                continue
            found = count(name, ('core/post-content',))
            if found is not None and found['core/post-content'] != 1:
                trees.fail(f"Page template {name} requires exactly one core/post-content outside query loops; found {found['core/post-content']}")

    patterns = contract.get('patterns')
    for pattern in patterns if isinstance(patterns, list) else []:
        if isinstance(pattern, dict) and isinstance(pattern.get('slug'), str):
            walk(pattern.get('blocks'), f"patterns.{pattern['slug']}")


def _template_of(proposal):
    template = proposal.get('template')
    return template if isinstance(template, str) and template else None


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    manifest = None
    if '--manifest' in args:
        i = args.index('--manifest')
        manifest = Path(args[i + 1])
        del args[i:i + 2]
    if len(args) != 1:
        print('usage: gutenberg_block_schema.py <block-plan dir> [--manifest <path>]', file=sys.stderr)
        return 2
    found = validate_trees(Path(args[0]), manifest)
    print(json.dumps(found, ensure_ascii=False, indent=2))
    return 1 if found else 0


if __name__ == '__main__':
    sys.exit(main())
