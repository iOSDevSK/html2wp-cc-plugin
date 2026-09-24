#!/usr/bin/env python3
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
"""The manifest's pre-flight: two known destructive layouts, and in a Flash
run, every decision stage 0 owes.

    check-manifest.py --manifest {workspace}/conversion-manifest.json
                      [--flash [--candidates {workspace}/flash-candidates.json]]

Never rewrites anything. Prints {"ok": bool, "errors": [...], "warnings": [...]}
and exits 0 (ok) or 1.

Always (carried over from the desktop app's manifest-check.py, which guarded
every manifest before the app let it reach a conversion):

- a CONSENSUS front page whose header and content share a viewport-height
  wrapper: splitting the header into a shared part leaves an empty
  full-height block above the content. The page must stay self-contained,
  with chrome.frontOwnsFooter true;
- blog.cardContainer must match exactly ONE element on the listing: gate C3
  counts the children of every match, so a shared utility class doubles the
  count on WordPress.

With --flash, the decisions Flash's stage 0 must make and write, because no
repair round comes after it (SKILL.md, "Flash mode"):

- `nav` is a list — every navigation group that is a menu, or empty with
  `navReason` saying why the site has none;
- `blog` and `shop` are decided: present with their listing, articles or
  products and card selectors, or `present: false` with a `reason` that is not
  a count;
- `forms` holds EVERY form flash-manifest.py found (by page and selector), each
  with a purpose, so each can be connected in Visual Edit Lite;
- every page has a key, a kind and a chrome mode.
"""
import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from html_layout import layout, parse_selector, select  # noqa: E402

FULL_HEIGHT = re.compile(r'(?:min-)?h-(?:screen|dvh|svh|lvh|\[100(?:d|s|l)?vh\])')
KINDS = {'front', 'page', 'listing', 'article', 'shop', 'product', 'utility', 'fragment'}
COUNT_REASON = re.compile(r'\b(only|just|few|single|one|two|three|\d+)\s+(article|post|product|item|entr)', re.I)


def needs_inline_shell(html):
    """A styled full-height shell holding both the header and the content."""
    nodes = layout(html)
    for header in (n for n in nodes if n['tag'] == 'header'):
        for shell in header['ancestors']:
            if shell['tag'] in ('html', 'body'):
                continue
            style = shell['attrs'].get('style') or ''
            full = any(FULL_HEIGHT.fullmatch(c) for c in (shell['attrs'].get('class') or '').split()) \
                or bool(re.search(r'(?:^|;)\s*(?:min-)?height\s*:\s*100(?:d|s|l)?vh\b', style, re.I))
            if not full:
                continue
            for content in nodes:
                if content['tag'] in ('main', 'section', 'article') and any(a is shell for a in content['ancestors']) \
                        and not any(a is header for a in content['ancestors']):
                    return True
    return False


def input_dir(manifest, path):
    root = Path((manifest.get('input') or {}).get('dir') or '')
    if not root.is_absolute():
        root = (path.parent / root)
    return root.resolve()


def layout_checks(manifest, root, errors):
    for page in manifest.get('pages') or []:
        if page.get('key') != 'front-page' or page.get('chrome') == 'self-contained':
            continue
        path = (root / page.get('file', '')).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            continue
        if path.stat().st_size <= 20 * 1024 * 1024 and needs_inline_shell(path.read_text(errors='replace')):
            errors.append('The front-page header and content share a viewport-height wrapper: consensus chrome would '
                          'leave an empty full-height block above the content. Keep this page self-contained '
                          '(pages[].chrome "self-contained", chrome.frontOwnsFooter true); do not add a wrapper '
                          'or CSS to hide the difference.')
    blog = manifest.get('blog') or {}
    container, listing = blog.get('cardContainer'), blog.get('listing')
    if blog.get('present') and container and listing:
        steps = parse_selector(container)
        path = (root / listing).resolve()
        if steps and path.is_relative_to(root) and path.is_file():
            found = len(select(layout(path.read_text(errors='replace')), steps))
            if found != 1:
                errors.append(f'blog.cardContainer "{container}" matches {found} elements on the listing {listing}; '
                              'it must match exactly the one element whose direct children are the article cards '
                              '(gate C3 counts the cards of every match).')


def decided(section, name, errors, required):
    value = section if isinstance(section, dict) else None
    if value is None or 'present' not in value:
        errors.append(f'{name}: not decided — write {name}.present (true with its fields, or false with a reason)')
        return
    if value.get('present') is True:
        missing = [f for f in required if not value.get(f)]
        if missing:
            errors.append(f'{name}: present but missing {", ".join(missing)}')
    elif not str(value.get('reason') or '').strip():
        errors.append(f'{name}: present false needs a reason saying why this is not a {name}')
    elif COUNT_REASON.search(str(value['reason'])):
        errors.append(f'{name}.reason argues from a count ("{value["reason"]}"): say why it is not a {name}, '
                      'never how many items it has')


def flash_checks(manifest, candidates, errors, warnings):
    nav = manifest.get('nav')
    if not isinstance(nav, list):
        errors.append('nav: not decided — write the list of menus (each navigation group that is one), '
                      'or [] with navReason')
    elif not nav and not str(manifest.get('navReason') or '').strip():
        errors.append('nav: empty with no navReason — a site with no menu says why')
    decided(manifest.get('blog'), 'blog', errors, ('listing', 'articles', 'cardContainer', 'cardSelector'))
    decided(manifest.get('shop'), 'shop', errors, ('listing', 'products', 'cardContainer'))
    forms = manifest.get('forms')
    if not isinstance(forms, list):
        errors.append('forms: not decided — list every form (page, selector, purpose)')
        forms = []
    for f in forms:
        if not isinstance(f, dict) or not f.get('page') or not f.get('selector') or not f.get('purpose'):
            errors.append(f'forms: {json.dumps(f)[:120]} needs page, selector and purpose')
    have = {(f.get('page'), f.get('selector')) for f in forms if isinstance(f, dict)}
    pages_with = {f.get('page') for f in forms if isinstance(f, dict)}
    for c in (candidates or {}).get('forms') or []:
        if (c.get('page'), c.get('selector')) in have:
            continue
        if c.get('page') in pages_with:
            warnings.append(f"forms: {c.get('page')} {c.get('selector')} is not listed by that selector — "
                            'check the manifest entry on that page is this form')
        else:
            errors.append(f"forms: {c.get('page')} has a form ({c.get('selector')}) the manifest does not list; "
                          'every form goes into forms[] so it can be connected in Visual Edit Lite')
    for page in manifest.get('pages') or []:
        if not page.get('key') or page.get('kind') not in KINDS or page.get('chrome') not in ('consensus', 'self-contained'):
            errors.append(f"pages: {page.get('file')} needs a key, a kind ({', '.join(sorted(KINDS))}) and a chrome mode")
    blog = manifest.get('blog') or {}
    if blog.get('present'):
        kinds = {p.get('file'): p.get('kind') for p in manifest.get('pages') or []}
        wrong = [a for a in blog.get('articles') or [] if kinds.get(a) != 'article']
        if wrong:
            errors.append(f'blog.articles must be pages of kind "article": {", ".join(wrong[:5])}')
        if kinds.get(blog.get('listing')) != 'listing':
            errors.append(f'blog.listing {blog.get("listing")} must be a page of kind "listing"')


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--manifest', required=True)
    ap.add_argument('--flash', action='store_true')
    ap.add_argument('--candidates', default='', help='default: flash-candidates.json beside the manifest')
    args = ap.parse_args(argv)
    path = Path(args.manifest).resolve()
    manifest = json.loads(path.read_text())
    errors, warnings = [], []
    layout_checks(manifest, input_dir(manifest, path), errors)
    if args.flash:
        cand_path = Path(args.candidates) if args.candidates else path.parent / 'flash-candidates.json'
        try:
            candidates = json.loads(cand_path.read_text())
        except (OSError, ValueError):
            candidates = None
            warnings.append(f'no {cand_path.name}: the forms cannot be checked against what flash-manifest.py found')
        flash_checks(manifest, candidates, errors, warnings)
    result = {'ok': not errors, 'errors': errors, 'warnings': warnings}
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if not errors else 1


if __name__ == '__main__':
    sys.exit(main())
