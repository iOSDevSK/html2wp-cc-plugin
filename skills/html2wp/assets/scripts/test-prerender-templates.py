#!/usr/bin/env python3
"""prerender-spa.py records a group of pages' controls once, and captures in
parallel — for a React Router table and for a TanStack Start site alike.

A shop's /product/:id is one template filled with twenty products' data. It
used to be driven page by page — every control, at two widths, with a reload
per navigating control — which on a real 45-page shop ran for over an hour.
Here:

- Flash: the first product page is recorded; the other nineteen are not
  (their timing row names the template) — /product/:id is the app's own
  route, so every page of it shares the recording, even one whose control
  says something else — and each gets the disclosure with ITS OWN panel,
  opened on that page at capture, never the first product's words;
- a TanStack-style site (its pages named by the build output's paths): the
  route file blog/$slug.tsx makes /blog/<slug> one group, recorded once;
  /docs/<page> pages alike by shape share one recording too; /about/story
  and /about/care share a prefix and not a template, so each is recorded;
- the capture runs in --jobs browsers, and every page is written;
- inside a run (H2WP_WORKSPACE with a progress.json), the stage's progress
  says "N of M";
- the whole capture stays bounded.

  python3 test-prerender-templates.py
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / 'prerender-spa.py'

APP = """import { Routes, Route } from "react-router-dom";
export default () => (<Routes>
  <Route path="/" element={<Index />} />
  <Route path="/shop" element={<Shop />} />
  <Route path="/product/:id" element={<Product />} />
</Routes>);
"""

INDEX = """<!doctype html><html><head><title>Shop</title></head><body><div id="root"></div>
<script>
const ids = Array.from({length: 20}, (_, i) => 'p' + (i + 1));
const root = document.getElementById('root');
const path = location.pathname.replace(/\\/$/, '') || '/';
function product(id) {
  const label = id === 'p7' ? 'Specs' : 'Details';
  return '<h1>Product ' + id + '</h1><p>Price ' + (10 + ids.indexOf(id)) + '</p>'
    + '<div class="more"><button type="button" aria-expanded="false" id="toggle">' + label + '</button></div>'
    + '<a href="/shop">Back to the shop</a>';
}
const pages = {
  '/': '<h1>Home</h1><a href="/shop">Shop</a>',
  '/shop': '<h1>Shop</h1>' + ids.map((id) => '<a href="/product/' + id + '">' + id + '</a>').join(' '),
};
if (path.startsWith('/product/')) root.innerHTML = product(path.split('/')[2]);
else root.innerHTML = pages[path] || '<h1>404</h1>';
document.addEventListener('click', (e) => {
  const b = e.target.closest('#toggle');
  if (!b) return;
  const open = b.getAttribute('aria-expanded') === 'true';
  b.setAttribute('aria-expanded', open ? 'false' : 'true');
  const wrap = b.parentElement;
  if (open) wrap.querySelector('.panel').remove();
  else { const p = document.createElement('div'); p.className = 'panel'; p.textContent = 'Made for ' + location.pathname.split('/')[2] + '.'; wrap.appendChild(p); }
});
</script></body></html>"""


class Templates(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        base = Path(cls.tmp.name)
        root = base / 'site'
        (root / 'src').mkdir(parents=True)
        (root / 'dist').mkdir()
        (root / 'package.json').write_text('{"dependencies":{"react-router-dom":"6"}}')
        (root / 'src' / 'App.tsx').write_text(APP)
        (root / 'dist' / 'index.html').write_text(INDEX)
        cls.ws = base / 'work space'
        cls.ws.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith('H2WP_')}
        env.update({'H2WP_WORKSPACE': str(cls.ws), 'H2WP_MODE': 'flash'})
        subprocess.run(['bash', str(HERE / 'progress.sh'), 'mode', 'flash'], env=env, capture_output=True, check=True)
        subprocess.run(['bash', str(HERE / 'progress.sh'), 'start', '-1'], env=env, capture_output=True, check=True)
        cls.out = cls.ws / 'static-src'
        started = time.monotonic()
        cls.proc = subprocess.run([sys.executable, str(SCRIPT), '--project', str(root), '--out', str(cls.out),
                                   '--skip-build', '--dist', str(root / 'dist'), '--no-verify', '--flash', '--jobs', '3'],
                                  capture_output=True, text=True, timeout=900, env=env)
        cls.seconds = time.monotonic() - started
        cls.report = json.loads((cls.ws / 'prerender-report.json').read_text())
        cls.progress = json.loads((cls.ws / 'progress.json').read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_run_succeeds(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stdout[-3000:] + self.proc.stderr[-3000:])

    def test_the_pattern_is_recorded_once(self):
        routes = self.report['timing']['routes']
        products = sorted(r for r in routes if r.startswith('/product/'))
        self.assertEqual(len(products), 20)
        recorded = sorted(r for r in products if 'interactionsMs' in routes[r])
        self.assertEqual(recorded, ['/product/p1'], 'the app declares the route: one recording for all')
        self.assertEqual(sum(1 for r in products if routes[r].get('inheritedFrom') == '/product/p1'), 19)

    def test_each_page_replays_its_own_panel(self):
        for n in (1, 2, 20):
            page = markup((self.out / 'product' / f'p{n}.html').read_text())
            self.assertIn('data-spa-toggle', page, f'p{n}')
            self.assertIn(f'Made for p{n}.', page, f'p{n}: its own panel')
            if n != 1:
                self.assertNotIn('Made for p1.', page, f"p{n}: never the first product's words")

    def test_a_control_with_other_words_is_the_same_kind_of_control(self):
        page = markup((self.out / 'product' / 'p7.html').read_text())
        self.assertIn('data-spa-toggle', page, 'the "Specs" button sits where "Details" does')
        self.assertIn('Made for p7.', page)
        self.assertEqual(self.report['pages']['product/p7.html'].get('inheritedFrom'), '/product/p1')

    def test_every_page_is_captured_in_parallel(self):
        self.assertEqual(len(list((self.out / 'product').glob('*.html'))), 20)
        self.assertEqual(self.report['timing']['captureJobs'], 3)
        self.assertTrue(all('captureMs' in row for row in self.report['timing']['routes'].values()))

    def test_the_stage_says_how_far_it_is(self):
        self.assertIn('(12 of 22)', self.proc.stdout)
        self.assertRegex(self.progress.get('note', ''), r'(recording|capturing) the pages')
        self.assertEqual(self.progress['percent'], 0, 'a note never moves the bar')

    def test_bounded(self):
        # 22 pages, one product recorded, three browsers capturing.
        self.assertLess(self.seconds, 240, f'{self.seconds:.0f}s')


def markup(html):
    """The page without its scripts: the words the markup itself carries."""
    import re
    return re.sub(r'<script[\s\S]*?</script>', '', html)


def page(title, body):
    return f"""<!doctype html><html><head><title>{title}</title></head><body><main>{body}</main>
<script>
document.addEventListener('click', (e) => {{
  const b = e.target.closest('[data-toggle]');
  if (!b) return;
  const open = b.getAttribute('aria-expanded') === 'true';
  b.setAttribute('aria-expanded', open ? 'false' : 'true');
  const wrap = b.parentElement;
  if (open) wrap.querySelector('.panel').remove();
  else {{ const p = document.createElement('div'); p.className = 'panel'; p.textContent = b.dataset.toggle; wrap.appendChild(p); }}
}});
</script></body></html>"""


class TanStackStyle(unittest.TestCase):
    """No route table: the framework wrote one HTML file per page, so the pages
    are named only by their paths."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name) / 'site'
        dist = root / 'dist'
        slugs = [f'post-{n}' for n in range(1, 13)]
        (root / 'src' / 'routes' / 'blog').mkdir(parents=True)
        (root / 'src' / 'routes' / 'blog' / '$slug.tsx').write_text('export const Route = createFileRoute("/blog/$slug")({});\n')
        (root / 'src' / 'routes' / '__root.tsx').write_text('export const Route = createRootRoute({});\n')
        files = {
            'index.html': page('Home', '<h1>Home</h1><a href="/blog">Blog</a> <a href="/about/story">Story</a>'),
            'blog/index.html': page('Blog', '<h1>Blog</h1>' + ' '.join(f'<a href="/blog/{s}">{s}</a>' for s in slugs)),
            'about/story/index.html': page('Story', '<h1>Our story</h1><div><button type="button" aria-expanded="false" '
                                                    'data-toggle="Founded in a garage.">How it began</button></div>'),
            'about/care/index.html': page('Care', '<h1>Care</h1><section><div><button type="button" aria-expanded="false" '
                                                  'data-toggle="Polish gently.">Cleaning</button></div></section>'),
        }
        for s in slugs:
            files[f'blog/{s}/index.html'] = page(s, f'<article><h1>{s}</h1><p>Words of {s}.</p><div>'
                                                    '<button type="button" aria-expanded="false" '
                                                    f'data-toggle="Notes on {s}.">Notes</button></div></article>')
        for d in ('one', 'two', 'three'):
            files[f'docs/{d}/index.html'] = page(d, f'<h1>{d}</h1><div><button type="button" aria-expanded="false" '
                                                    f'data-toggle="More about {d}.">More</button></div>')
        files['index.html'] = files['index.html'].replace('</h1>', '</h1><a href="/docs/one">Docs</a>', 1)
        for rel, html in files.items():
            (dist / rel).parent.mkdir(parents=True, exist_ok=True)
            (dist / rel).write_text(html)
        (root / 'package.json').write_text('{"dependencies":{"@tanstack/react-router":"1"}}')
        cls.out = Path(cls.tmp.name) / 'static-src'
        env = {k: v for k, v in os.environ.items() if not k.startswith('H2WP_')}
        cls.proc = subprocess.run([sys.executable, str(SCRIPT), '--project', str(root), '--out', str(cls.out),
                                   '--skip-build', '--dist', str(dist), '--no-verify', '--flash', '--jobs', '3'],
                                  capture_output=True, text=True, timeout=900, env=env)
        cls.report = json.loads((Path(cls.tmp.name) / 'prerender-report.json').read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_the_blog_posts_are_one_group_recorded_once(self):
        self.assertEqual(self.proc.returncode, 0, self.proc.stdout[-3000:] + self.proc.stderr[-3000:])
        routes = self.report['timing']['routes']
        posts = sorted(r for r in routes if r.startswith('/blog/'))
        self.assertEqual(len(posts), 12)
        recorded = [r for r in posts if 'interactionsMs' in routes[r]]
        self.assertEqual(len(recorded), 1, recorded)
        for n in (1, 7, 12):
            html = markup((self.out / 'blog' / f'post-{n}.html').read_text())
            self.assertIn('data-spa-toggle', html, f'post-{n}')
            self.assertIn(f'Notes on post-{n}.', html, f'post-{n}: its own panel')

    def test_pages_alike_by_shape_share_a_recording(self):
        routes = self.report['timing']['routes']
        docs = sorted(r for r in routes if r.startswith('/docs/'))
        self.assertEqual(len(docs), 3)
        self.assertEqual(sum(1 for r in docs if 'interactionsMs' in routes[r]), 1)
        for d in ('one', 'two', 'three'):
            self.assertIn(f'More about {d}.', markup((self.out / 'docs' / f'{d}.html').read_text()), d)

    def test_a_shared_prefix_is_not_a_shared_template(self):
        routes = self.report['timing']['routes']
        self.assertIn('interactionsMs', routes['/about/story'])
        self.assertIn('interactionsMs', routes['/about/care'])
        for name, text in (('story', 'Founded in a garage.'), ('care', 'Polish gently.')):
            html = (self.out / 'about' / f'{name}.html').read_text()
            self.assertIn(text, markup(html), name)


class Groups(unittest.TestCase):
    """The grouping rules alone, on the script imported."""

    @classmethod
    def setUpClass(cls):
        import importlib.util
        argv = list(sys.argv)
        cls.tmp = tempfile.TemporaryDirectory()
        sys.argv = [str(SCRIPT), '--project', cls.tmp.name, '--out', str(Path(cls.tmp.name) / 'out'), '--flash']
        spec = importlib.util.spec_from_file_location('prerender_spa_groups', SCRIPT)
        cls.spa = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.spa)
        sys.argv = argv

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_tanstack_route_files_name_their_parameters(self):
        root = Path(self.tmp.name) / 'app'
        for rel in ('blog/$slug.tsx', 'shop.$category.$id.tsx', '_layout/docs/$page.tsx', 'about.tsx',
                    '(marketing)/news/$slug/index.tsx', 'files/$.tsx', '__root.tsx'):
            (root / 'src' / 'routes' / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / 'src' / 'routes' / rel).write_text('')
        self.assertEqual(sorted(self.spa.tanstack_param_routes(root)),
                         ['/blog/:slug', '/docs/:page', '/news/:slug', '/shop/:category/:id'])

    def test_a_declared_route_wins_over_the_shape(self):
        declared = self.spa.param_route_patterns(['/blog/:slug'])
        routes = ['/', '/blog/a', '/blog/b', '/about/x', '/about/y', '/solo/z']
        group = lambda r: self.spa.route_group(r, routes, {}, declared)
        self.assertEqual(group('/blog/a'), ('/blog/:slug', True))
        self.assertEqual(group('/about/x'), ('/about/*', False))
        self.assertEqual(group('/solo/z'), (None, False), 'no sibling, no group')
        self.assertEqual(group('/'), (None, False))


if __name__ == '__main__':
    unittest.main()
