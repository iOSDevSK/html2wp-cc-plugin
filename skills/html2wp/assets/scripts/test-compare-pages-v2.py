#!/usr/bin/env python3
"""compare-pages.py on a Gutenberg (html2wp/2) manifest: every page is
captured at ITS OWN WordPress permalink.

WordPress REST filters pages by the last slug segment only, so /a/about/ and
/b/about/ both answer `?slug=about`. Taking the first answer captured one of
them twice and the other never. Pinned against a stand-in WordPress (no
Docker): nested pages sharing a leaf, a flat page, a post, a nested page REST
does not know, and the front page — plus the permalink choice on its own,
including the answers that must keep resolving exactly as before.

  python3 test-compare-pages-v2.py
"""
import ast
import functools
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import warnings
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

SCRIPT = Path(__file__).with_name('compare-pages.py')


def permalink_for(found, slug, wp, others=()):
    """compare-pages.py's own permalink_for, lifted out without running the
    script (it has no __main__ guard)."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', SyntaxWarning)
        tree = ast.parse(SCRIPT.read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'permalink_for')
    ns = {'urlsplit': urlsplit}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(SCRIPT), 'exec'), ns)
    return ns['permalink_for'](found, slug, wp, others)


class PermalinkFor(unittest.TestCase):
    WP = 'http://wp.test'

    def test_two_parents_share_a_leaf(self):
        found = [{'link': 'http://wp.test/b/about/'}, {'link': 'http://wp.test/a/about/'}]
        self.assertEqual(permalink_for(found, 'a/about', self.WP), 'http://wp.test/a/about/')
        self.assertEqual(permalink_for(found, 'b/about', self.WP), 'http://wp.test/b/about/')

    def test_flat_page_beats_a_nested_namesake(self):
        found = [{'link': 'http://wp.test/a/about/'}, {'link': 'http://wp.test/about/'}]
        self.assertEqual(permalink_for(found, 'about', self.WP), 'http://wp.test/about/')

    def test_wordpress_in_a_subdirectory(self):
        found = [{'link': 'http://wp.test/site/b/team/'}, {'link': 'http://wp.test/site/a/team/'}]
        self.assertEqual(permalink_for(found, 'a/team', 'http://wp.test/site'), 'http://wp.test/site/a/team/')

    def test_unchanged_where_nothing_collides(self):
        # One answer whose path is not the slug (a post under a date
        # permalink): still that answer, as before.
        self.assertEqual(permalink_for([{'link': 'http://wp.test/2026/09/x/'}], 'blog/x', self.WP),
                         'http://wp.test/2026/09/x/')
        # No answer, or not a list: the slug rule, as before.
        self.assertEqual(permalink_for([], 'a/b', self.WP), 'http://wp.test/a/b/')
        self.assertEqual(permalink_for({'code': 'rest_no_route'}, 'x', self.WP), 'http://wp.test/x/')
        # Several answers, none exactly the slug: the first, as before.
        found = [{'link': 'http://wp.test/p/about/'}, {'link': 'http://wp.test/q/about/'}]
        self.assertEqual(permalink_for(found, 'r/about', self.WP), 'http://wp.test/p/about/')

    def test_an_answer_that_is_another_pages_address_is_not_taken(self):
        # The import flattened e/team to team-2: REST answers ?slug=team with
        # d/team's address alone. That is d-team's page, so e-team is looked
        # for at its own address, where the review then shows what is there.
        found = [{'link': 'http://wp.test/d/team/'}]
        self.assertEqual(permalink_for(found, 'e/team', self.WP, ['d/team', 'e/team', 'contact']),
                         'http://wp.test/e/team/')
        self.assertEqual(permalink_for(found, 'd/team', self.WP, ['d/team', 'e/team', 'contact']),
                         'http://wp.test/d/team/')
        # Without that knowledge the old answer stands.
        self.assertEqual(permalink_for(found, 'e/team', self.WP), 'http://wp.test/d/team/')
        # Another claimed answer is skipped for an unclaimed one.
        found = [{'link': 'http://wp.test/d/team/'}, {'link': 'http://wp.test/2026/team/'}]
        self.assertEqual(permalink_for(found, 'e/team', self.WP, ['d/team']), 'http://wp.test/2026/team/')

    def test_malformed_rows_are_skipped(self):
        found = [{'id': 3}, 'junk', {'link': 'http://wp.test/a/about/'}]
        self.assertEqual(permalink_for(found, 'z/about', self.WP), 'http://wp.test/a/about/')


PAGE = ('<!doctype html><html><head><title>{t}</title><style>body{{margin:0;font:18px sans-serif}}'
        '.b{{height:{h}px;background:{c}}}</style></head><body><h1>{t}</h1><div class=b></div></body></html>')

# key, file, kind, slug, wp route (None: REST does not know it), height, colour
# c-ghost is served at its address but unknown to REST; e-team was flattened
# by an import (REST knows only d/team for ?slug=team) and is NOT served at
# /e/team/ — its capture must be that address, showing the 404.
PAGES = [
    ('front-page', 'index.html', 'front', '', '/', 700, '#2a6'),
    ('a-about', 'a/about.html', 'page', 'a/about', '/a/about/', 900, '#a62'),
    ('b-about', 'b/about.html', 'page', 'b/about', '/b/about/', 1300, '#26a'),
    ('contact', 'contact.html', 'page', 'contact', '/contact/', 500, '#666'),
    ('blog-first', 'blog/first.html', 'post', 'blog/first', '/blog/first/', 1100, '#a2a'),
    ('c-ghost', 'c/ghost.html', 'page', 'c/ghost', None, 600, '#aa2'),
    ('d-team', 'd/team.html', 'page', 'd/team', '/d/team/', 1500, '#2aa'),
    ('e-team', 'e/team.html', 'page', 'e/team', None, 1800, '#a22'),
]


class V2Captures(unittest.TestCase):
    def test_every_page_at_its_own_permalink(self):
        tmp = Path(tempfile.mkdtemp())
        site = tmp / 'site'
        for key, f, kind, slug, route, h, c in PAGES:
            (site / f).parent.mkdir(parents=True, exist_ok=True)
            (site / f).write_text(PAGE.format(t=key, h=h, c=c))
        (tmp / 'manifest.json').write_text(json.dumps({
            'schema': 'html2wp/2', 'target': 'gutenberg', 'workspace': str(tmp), 'input': {'dir': str(site)},
            'pages': [{'key': k, 'file': f, 'kind': kind, 'slug': s} for k, f, kind, s, *_ in PAGES]
            + [{'key': 'nav', 'file': 'nav.html', 'kind': 'fragment'}]}))

        routes = {r: f for _, f, _, _, r, *_ in PAGES if r}
        routes['/c/ghost/'] = 'c/ghost.html'  # served, but unknown to REST
        base = {}

        class WP(SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                u = urlsplit(self.path)
                if u.path.startswith('/wp-json/wp/v2/'):
                    rest = u.path.rsplit('/', 1)[-1]
                    leaf = parse_qs(u.query).get('slug', [''])[0]
                    kinds = {'pages': ('front', 'page'), 'posts': ('post',)}.get(rest, ())
                    # Deliberately b before a: the first answer is the wrong one for a/about.
                    rows = [{'link': base['url'] + r} for _, _, kind, s, r, *_ in sorted(PAGES, key=lambda p: p[3], reverse=True)
                            if r and kind in kinds and s.rsplit('/', 1)[-1] == leaf]
                    body = json.dumps(rows).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if u.path in routes:
                    self.path = '/' + routes[u.path]
                    return super().do_GET()
                self.send_error(404)

        httpd = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(WP, directory=str(site)))
        base['url'] = f'http://127.0.0.1:{httpd.server_port}'
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            run = subprocess.run([sys.executable, '-W', 'ignore', str(SCRIPT), '--manifest', str(tmp / 'manifest.json'),
                                  '--wp', base['url'], '--out', str(tmp / 'out')], capture_output=True, text=True, timeout=300)
        finally:
            httpd.shutdown()
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        pairs = {p['key']: p for p in json.loads((tmp / 'out' / 'review-manifest.json').read_text())['pairs']}
        self.assertEqual(set(pairs), {p[0] for p in PAGES}, 'fragments are not pages')
        want = {k: base['url'] + '/' + s + '/' if not r else base['url'] + r for k, _, _, s, r, *_ in PAGES}
        self.assertEqual({k: p['wpUrl'] for k, p in pairs.items()}, want)
        # The capture is the right page, not merely the right address: the
        # pages differ in height by hundreds of pixels, so a page captured
        # from its namesake's address cannot match its own original.
        for key, p in pairs.items():
            if key != 'e-team':
                self.assertLessEqual(abs(p['origHeight'] - p['wpHeight']), 2, key)
        # The flattened page is captured at its address, where there is
        # nothing: the reviewer sees the missing page, not d-team twice.
        self.assertGreater(abs(pairs['e-team']['origHeight'] - pairs['e-team']['wpHeight']), 200)
        # One composite per key; the two abouts do not overwrite each other.
        self.assertEqual(sorted(x.name for x in (tmp / 'out').glob('*.side-by-side.png')),
                         sorted(f'{k}.side-by-side.png' for k in want))


if __name__ == '__main__':
    unittest.main(verbosity=2)
