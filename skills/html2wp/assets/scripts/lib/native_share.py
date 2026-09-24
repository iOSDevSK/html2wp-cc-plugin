"""S0, the native share of a Gutenberg theme: its blocks by type and how many
are WordPress's own core blocks, with a native-fallback ledger entry for each
h2wp/element that stays and why.

The compiler writes the same census over what the theme saves into
theme-report.json (nativeShare, server-side); this module reads a block plan
the same way (Flash, before the theme is compiled) and prints either one
(gverify). tools/native-share-fixtures.json holds the cases both must agree on.
"""
import json
import math
import re
from pathlib import Path

CODE = 'native-fallback'
ITEMS = 500


CLOSED_STATES = {'closed', 'inactive', 'hidden', 'collapsed'}


def _lowered(value):
    return value.strip().lower() if isinstance(value, str) else 'true' if value is True else ''


def hidden_at_rest(html):
    """Hidden at rest, as the planner reads a source element (gutenberg-plan.py
    hidden_at_rest): `closed-panel` (a recorded panel closed at rest), `hidden`
    (the attribute present, whatever its value), `aria-hidden` ("true")."""
    has_hidden = 'hidden' in html and html['hidden'] is not False
    style = html.get('style') if isinstance(html.get('style'), str) else ''
    if 'data-spa-panel' in html and (has_hidden or _lowered(html.get('data-state')) in CLOSED_STATES
                                     or re.search(r'(?:^|;)\s*display\s*:\s*none', style, re.I)):
        return 'closed-panel'
    if has_hidden:
        return 'hidden'
    return 'aria-hidden' if _lowered(html.get('aria-hidden')) == 'true' else None


def fallback_reason(block, frames=()):
    """Why an h2wp/element stays: the plan's own reason (a node's `fallback`),
    else what it is (frame, closed-panel, hidden, aria-hidden, card-anchor, bound,
    empty, kept)."""
    given = block.get('fallback')
    if isinstance(given, str) and given.strip():
        return given.strip()[:120]
    a = block.get('attributes') or {}
    html = a.get('htmlAttributes') or {}
    inner = block.get('innerBlocks') or []
    if any(frame == a for frame in frames):
        return 'frame'
    hidden = hidden_at_rest(html)
    if hidden:
        return hidden
    if a.get('tagName') == 'a' and inner:
        return 'card-anchor'
    if isinstance(a.get('bind'), str) and a['bind']:
        return 'bound'
    if not inner and not (isinstance(a.get('text'), str) and a['text'].strip()):
        return 'empty'
    return 'kept'


def _share(core, total):
    return math.floor(core / total * 1000 + 0.5) / 10 if total else 0


def _ranked(counts):
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


def census(templates=None, parts=None, content=None, frame=None):
    """The census of block trees: {surface name: [blocks]} per surface."""
    blocks, reasons, items, surfaces = {}, {}, [], {}
    frames = [frame[slot] for slot in ('wrapper', 'main') if isinstance(frame, dict) and frame.get(slot)]
    elements = 0

    def walk(tree, surface, where):
        nonlocal elements
        for block in tree or []:
            if not isinstance(block, dict) or not isinstance(block.get('name'), str):
                continue
            name = block['name']
            blocks[name] = blocks.get(name, 0) + 1
            tally = surfaces.setdefault(surface, {'total': 0, 'core': 0})
            tally['total'] += 1
            if name.startswith('core/'):
                tally['core'] += 1
            if name == 'h2wp/element':
                reason = fallback_reason(block, frames)
                elements += 1
                reasons[reason] = reasons.get(reason, 0) + 1
                if len(items) < ITEMS:
                    a = block.get('attributes') or {}
                    item = {'code': CODE, 'where': where, 'tagName': a.get('tagName') or 'div'}
                    if isinstance(a.get('className'), str) and a['className']:
                        item['className'] = a['className'][:80]
                    item['reason'] = reason
                    items.append(item)
            walk(block.get('innerBlocks'), surface, where)

    for surface, prefix, trees in (('templates', 'template', templates), ('parts', 'part', parts), ('content', 'page', content)):
        for name, tree in (trees or {}).items():
            walk(tree, surface, f'{prefix}:{name}')
    total = sum(s['total'] for s in surfaces.values())
    core = sum(s['core'] for s in surfaces.values())
    fallback = {'code': CODE, 'total': elements, 'reasons': _ranked(reasons), 'items': items}
    if elements > len(items):
        fallback['truncated'] = elements - len(items)
    return {
        'total': total, 'core': core, 'h2wp': sum(n for name, n in blocks.items() if name.startswith('h2wp/')),
        'coreShare': _share(core, total),
        'surfaces': {s: {**surfaces[s], 'coreShare': _share(surfaces[s]['core'], surfaces[s]['total'])} for s in ('templates', 'parts', 'content') if s in surfaces},
        'blocks': _ranked(blocks),
        'fallback': fallback,
    }


def plan_share(output):
    """The census of a block plan (block-plan/ as prepare and review leave it):
    its parts and page proposals. Templates are the compiler's, so a plan has
    none; theme-report.json's census counts them."""
    output = Path(output)
    contract = json.loads((output / 'contract.json').read_text())
    content = {}
    for path in sorted((output / 'pages').glob('*.json')):
        proposal = json.loads(path.read_text())
        if isinstance(proposal.get('blocks'), list) and not proposal.get('product'):
            content[proposal.get('key') or path.stem] = proposal['blocks']
    return census(parts=contract.get('parts') or {}, content=content, frame=contract.get('frame'))


def brief(share):
    """The census without its per-element items, for a report that links the ledger."""
    if not isinstance(share, dict):
        return None
    fallback = {k: v for k, v in (share.get('fallback') or {}).items() if k != 'items'}
    return {**{k: v for k, v in share.items() if k != 'fallback'}, 'fallback': fallback}


def summary_line(share):
    """One line: the core share, per surface, and the reasons elements stay."""
    if not isinstance(share, dict) or not share.get('total'):
        return 'native share: no blocks counted'
    surfaces = ', '.join(f"{name} {s['coreShare']}%" for name, s in (share.get('surfaces') or {}).items())
    fallback = share.get('fallback') or {}
    reasons = ', '.join(f'{reason} {n}' for reason, n in (fallback.get('reasons') or {}).items())
    kept = fallback.get('total', 0)
    return (f"native share: {share['coreShare']}% core blocks ({share['core']} of {share['total']}"
            + (f'; {surfaces}' if surfaces else '') + ')'
            + (f'; {kept} h2wp/element kept ({CODE}: {reasons})' if kept else '; no h2wp/element kept'))
