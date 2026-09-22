#!/usr/bin/env python3
"""prerender-spa.py captures the pages of parameterised routes.

A React Router table declares /blog/:slug, and that route has no data of its
own, so the capture used to skip it and every article was lost. The app links
to its articles, though: the listing to two of them, and one article to a
third that nothing else links to. All three are captured, gated like any
other page, and their links written as the flat files stage 0 reads. A slug
no page links to is not invented.

With --routes (and on TanStack, whose routes come from its own output) the
caller has named the pages and no link is followed.

Takes about two minutes: every route is recorded at two widths.

  python3 test-prerender-linked-routes.py
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name('prerender-spa.py')
TMP = tempfile.TemporaryDirectory()
ARGV = list(sys.argv)
sys.argv = [str(SCRIPT), '--project', TMP.name, '--out', str(Path(TMP.name) / 'out')]
spec = importlib.util.spec_from_file_location('prerender_spa', SCRIPT)
spa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(spa)
sys.argv = ARGV

APP = """import { Routes, Route } from "react-router-dom";
export default () => (<Routes>
  <Route path="/" element={<Index />} />
  <Route path="/blog" element={<Blog />} />
  <Route path="/blog/:slug" element={<Post />} />
  <Route path="*" element={<NotFound />} />
</Routes>);
"""

# A client-side router in a few lines: every address is served index.html,
# and the script renders the page for location.pathname.
INDEX = """<!doctype html><html><head><title>Journal</title></head><body><div id="root"></div>
<script>
const pages = {
  '/': '<h1>Home</h1><a href="/blog">Journal</a>',
  '/blog': '<h1>Journal</h1><a href="/blog/first-light">First light</a> <a href="/blog/second-look?ref=list#top">Second look</a>',
  '/blog/first-light': '<h1>First light</h1><a href="/blog/third-roll/">Next: Third roll</a> <a href="/blog">Back</a>',
  '/blog/second-look': '<h1>Second look</h1><a href="/blog">Back</a>',
  '/blog/third-roll': '<h1>Third roll</h1><a href="/">Home</a>',
};
const path = location.pathname.replace(/\\/$/, '') || '/';
document.getElementById('root').innerHTML = pages[path] || '<h1>404</h1><a href="/">Home</a>';
</script></body></html>"""


def make_site():
    root = Path(TMP.name) / 'site'
    if not root.exists():
        (root / 'src').mkdir(parents=True)
        (root / 'dist').mkdir()
        (root / 'package.json').write_text('{"dependencies":{"react-router-dom":"6"}}')
        (root / 'src' / 'App.tsx').write_text(APP)
        (root / 'dist' / 'index.html').write_text(INDEX)
    return root


class Helpers(unittest.TestCase):
    def test_patterns_skip_catch_alls(self):
        pats = spa.param_route_patterns(['/blog/:slug', '/*', '/shop/:cat/:id'])
        self.assertEqual([r for r, _ in pats], ['/blog/:slug', '/shop/:cat/:id'])

    def test_only_linked_paths_that_match_a_family(self):
        pats = spa.param_route_patterns(['/blog/:slug'])
        hrefs = ['/blog/a', '/blog/a/', '/blog/b?x=1#y', '/blog', '/blog/a/b', 'blog/c', '//cdn/blog/d',
                 'https://x.test/blog/e', '/other/f', None, '/blog/g,h']
        self.assertEqual(spa.linked_route_instances(hrefs, pats, {'/', '/blog'}),
                         [('/blog/a', '/blog/:slug'), ('/blog/b', '/blog/:slug')])
        self.assertEqual(spa.linked_route_instances(['/blog/a'], pats, {'/blog/a'}), [])
        self.assertEqual(spa.linked_route_instances(['/blog/a'], [], set()), [])


class Capture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = make_site()
        cls.out = Path(TMP.name) / 'static-src'
        cls.proc = subprocess.run([sys.executable, str(SCRIPT), '--project', str(root), '--out', str(cls.out),
                                  '--skip-build', '--dist', str(root / 'dist'), '--no-verify'],
                                 capture_output=True, text=True, timeout=600)
        cls.report = json.loads((Path(TMP.name) / 'prerender-report.json').read_text())

    # Gate -1 is left out (--no-verify): an unstyled fixture's text raster
    # drifts past 0.6% at 390px on its own. The gates walk report["routes"],
    # which this test asserts includes the linked pages.
    def test_run_succeeds(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stdout[-3000:] + self.proc.stderr[-3000:])
        self.assertTrue(self.report['passed'])

    def test_every_linked_article_is_captured_including_one_only_an_article_links_to(self):
        for slug in ('first-light', 'second-look', 'third-roll'):
            self.assertTrue((self.out / 'blog' / f'{slug}.html').is_file(), slug)
        self.assertEqual(sorted(r['route'] for r in self.report['linkedRoutes']),
                         ['/blog/first-light', '/blog/second-look', '/blog/third-roll'])
        self.assertIn('/blog/third-roll', self.report['routes'])

    def test_links_to_articles_are_rewritten_not_left_unmapped(self):
        self.assertFalse([w for w in self.report['warnings'] if 'unmapped internal link: /blog/' in w])
        self.assertFalse([w for w in self.report['warnings'] if 'parameterised' in w])
        listing = (self.out / 'blog.html').read_text()
        self.assertIn('href="blog/first-light.html"', listing)
        self.assertIn('href="blog/second-look.html?ref=list#top"', listing)

    def test_nothing_is_invented(self):
        self.assertEqual(sorted(p.name for p in (self.out / 'blog').glob('*.html')),
                         ['first-light.html', 'second-look.html', 'third-roll.html'])


class ExplicitRoutes(unittest.TestCase):
    def test_named_routes_follow_no_links(self):
        root = make_site()
        out = Path(TMP.name) / 'named' / 'static-src'
        proc = subprocess.run([sys.executable, str(SCRIPT), '--project', str(root), '--out', str(out),
                               '--routes', '/,/blog', '--skip-build', '--dist', str(root / 'dist'), '--no-verify'],
                              capture_output=True, text=True, timeout=600)
        self.assertEqual(proc.returncode, 0, proc.stdout[-2000:] + proc.stderr[-2000:])
        report = json.loads((out.parent / 'prerender-report.json').read_text())
        self.assertEqual(report.get('linkedRoutes', []), [])
        self.assertFalse((out / 'blog').exists())


if __name__ == '__main__':
    unittest.main()
