#!/usr/bin/env python3
"""compare-pages.py --from-captures: pages gutenberg-verify-local.py has
just captured are composed from its PNGs, not captured again.

Against a stand-in WordPress (static files at each route; no Docker) and a
stand-in screenshots/ directory holding what the gate's visual phase writes
(<width>-<target stem>-source.png / -wp.png and captures.json):
  - a page whose pair is indexed, of this site, with the same bytes, is
    composed from it (the composite is exactly those two PNGs), the PNGs stay
  - a page without a route, without a pair, whose PNG changed since the
    index was written, whose two PNGs differ in size, or that is not in the
    original, is captured (or refused) as without the flag
  - an index of another site, or none, means every page is captured
  - review-manifest.json has the same shape either way, and --jobs 2 agrees
  - the index gutenberg-verify-local.py itself writes is the one read here
Needs Chromium (Playwright).

  python3 test-compare-pages-captures.py
"""
import functools
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

SCRIPT = Path(__file__).with_name('compare-pages.py')
PAGE = ('<!doctype html><html><head><title>{t}</title><style>body{{margin:0;font:18px/1.5 sans-serif}}'
        '.band{{height:{h}px;background:#468}}</style></head><body><h1>{t}</h1><div class="band"></div></body></html>')


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


class FromCapturesTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='h2wp-compare-')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.ws, self.site, self.wp, self.shots = root / 'ws', root / 'ws/static', root / 'wp', root / 'ws/screenshots'
        for d in (self.site, self.wp, self.shots):
            d.mkdir(parents=True)
        pages = [('index.html', 'front-page', 'front', '/', 600), ('about.html', 'about', 'page', '/about/', 900),
                 ('work.html', 'work', 'page', '/work/', 700), ('team.html', 'team', 'page', '/team/', 800),
                 ('gone.html', 'gone', 'page', '/gone/', 400), ('contact.html', 'contact', 'page', '/contact/', 500)]
        for file, key, kind, target, height in pages:
            if file != 'gone.html':  # in the manifest and captured, not in the original
                (self.site / file).write_text(PAGE.format(t=key, h=height))
            (self.wp / target.strip('/') / 'index.html').parent.mkdir(parents=True, exist_ok=True)
            (self.wp / target.strip('/') / 'index.html').write_text(PAGE.format(t=key, h=height))
        httpd = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Quiet, directory=str(self.wp)))
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close()))
        self.base = f'http://127.0.0.1:{httpd.server_port}'
        self.manifest = self.ws / 'conversion-manifest.json'
        self.manifest.write_text(json.dumps({'schema': 'html2wp/2', 'target': 'gutenberg', 'workspace': str(self.ws), 'input': {'dir': str(self.site)},
                                             'pages': [{'file': f, 'key': k, 'kind': kind, 'slug': t.strip('/') or None} for f, k, kind, t, _ in pages]}))
        # contact.html has no route; the others were captured by the gate.
        self.routes = self.ws / 'gutenberg-routes.json'
        self.routes.write_text(json.dumps([{'source': '/' + f, 'target': t} for f, _, _, t, _ in pages if f != 'contact.html']))
        self.captured = {}
        for f, key, _, target, _ in pages[:5]:
            stem = '1440-' + (target.strip('/').replace('/', '-') or 'home')
            for side, colour, height in (('source', (200, 30, 30), 1111), ('wp', (30, 30, 200), 1099 if key == 'team' else 1111)):
                Image.new('RGB', (1440, height), colour).save(self.shots / f'{stem}-{side}.png')
            self.captured[f] = stem
        self.index(self.base)

    def index(self, site):
        digest = lambda name: hashlib.sha256((self.shots / name).read_bytes()).hexdigest()
        (self.shots / 'captures.json').write_text(json.dumps({'schema': 'h2wp-captures/1', 'site': site, 'source': 'http://127.0.0.1:1', 'pairs': [
            {'source': '/' + f, 'target': '/' if stem == '1440-home' else '/' + stem[5:] + '/', 'width': 1440, 'sourcePng': stem + '-source.png',
             'wpPng': stem + '-wp.png', 'sha256': {'source': digest(stem + '-source.png'), 'wp': digest(stem + '-wp.png')}}
            for f, stem in self.captured.items()]}))

    def compare(self, *extra, out='review'):
        run = subprocess.run([sys.executable, '-W', 'ignore', str(SCRIPT), '--manifest', str(self.manifest), '--wp', self.base,
                              '--out', str(self.ws / out), *extra], capture_output=True, text=True, timeout=600)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        return json.loads((self.ws / out / 'review-manifest.json').read_text()), run.stdout

    def test_captured_pages_are_composed_from_the_gate_pngs(self):
        plain, _ = self.compare()
        (self.shots / '1440-work-wp.png').write_bytes((self.shots / '1440-work-wp.png').read_bytes() + b'\0')  # changed since indexed
        used, stdout = self.compare('--from-captures', str(self.shots), '--routes', str(self.routes), out='review-captures')
        self.assertIn('2 page(s) composed from', stdout)
        by_key = {p.get('key'): p for p in used['pairs']}
        for key in ('front-page', 'about'):
            self.assertEqual((by_key[key]['origHeight'], by_key[key]['wpHeight'], by_key[key]['fromCaptures']), (1111, 1111, True))
            board = Image.open(self.ws / 'review-captures' / by_key[key]['composite']).convert('RGB')
            self.assertEqual(board.getpixel((10, 100)), (200, 30, 30))
            self.assertEqual(board.getpixel((1440 + 24 + 10, 100)), (30, 30, 200))
        for key in ('work', 'team', 'contact'):  # changed PNG; two heights; no route
            self.assertNotIn('fromCaptures', by_key[key])
            self.assertEqual(by_key[key]['origHeight'], {p.get('key'): p for p in plain['pairs']}[key]['origHeight'])
        gone = [p for p in used['pairs'] if p['page'] == 'gone.html']
        self.assertEqual(gone, [p for p in plain['pairs'] if p['page'] == 'gone.html'])
        self.assertEqual(gone, [{'page': 'gone.html', 'error': 'missing in original'}])
        self.assertTrue(all((self.shots / name).is_file() for stem in self.captured.values() for name in (stem + '-source.png', stem + '-wp.png')))
        # Same shape: the same keys per pair (the provenance mark aside), the
        # same files beside the manifest.
        strip = lambda pair: sorted(k for k in pair if k != 'fromCaptures')
        self.assertEqual([strip(p) for p in used['pairs']], [strip(p) for p in plain['pairs']])
        self.assertEqual({k for k in used if k != 'pairs'}, {k for k in plain if k != 'pairs'})
        self.assertEqual(sorted(p.name.split('.')[0] for p in (self.ws / 'review-captures').glob('*.side-by-side.png')),
                         sorted(p.name.split('.')[0] for p in (self.ws / 'review').glob('*.side-by-side.png')))
        jobs, _ = self.compare('--from-captures', str(self.shots), '--routes', str(self.routes), '--jobs', '2', out='review-jobs')
        self.assertEqual([{k: v for k, v in p.items() if k != 'recaptured'} for p in jobs['pairs']],
                         [{k: v for k, v in p.items() if k != 'recaptured'} for p in used['pairs']])

    def test_the_gate_writes_the_index_read_here(self):
        # The real gutenberg-verify-local.py (visual phase only) against the
        # same stand-ins; its own index is what --from-captures reads.
        source = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Quiet, directory=str(self.site)))
        threading.Thread(target=source.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (source.shutdown(), source.server_close()))
        routes = self.ws / 'gate-routes.json'
        routes.write_text(json.dumps([{'source': '/' + p.name, 'target': '/' if p.stem == 'index' else f'/{p.stem}/'}
                                      for p in sorted(self.site.glob('*.html'))]))
        gate = subprocess.run([sys.executable, str(SCRIPT.with_name('gutenberg-verify-local.py')), '--site=' + self.base,
                               f'--source=http://127.0.0.1:{source.server_port}', '--routes=' + str(routes), '--password=x',
                               '--theme-slug=t', '--skip-editor', '--scope=smoke', '--out=' + str(self.ws / 'gate/report.json')],
                              capture_output=True, text=True, timeout=600)
        shots = self.ws / 'gate/screenshots'
        self.assertTrue((shots / 'captures.json').is_file(), gate.stdout + gate.stderr)
        used, stdout = self.compare('--from-captures', str(shots), '--routes', str(routes))
        self.assertIn('5 page(s) composed from', stdout)
        for pair in used['pairs']:
            if pair['page'] == 'gone.html':
                self.assertEqual(pair, {'page': 'gone.html', 'error': 'missing in original'})
                continue
            self.assertTrue(pair['fromCaptures'], pair)
            stem = '1440-' + ('home' if pair['key'] == 'front-page' else pair['key'])
            board = Image.open(self.ws / 'review' / pair['composite']).convert('RGB')
            left = Image.open(shots / f'{stem}-source.png').convert('RGB')
            self.assertEqual(board.crop((0, 44, left.width, 44 + left.height)).tobytes(), left.tobytes())

    def test_captures_of_another_site_or_no_index_are_not_used(self):
        self.index('http://127.0.0.1:9')
        used, stdout = self.compare('--from-captures', str(self.shots), '--routes', str(self.routes))
        self.assertIn('not of this --wp site', stdout)
        self.assertFalse(any(p.get('fromCaptures') for p in used['pairs']))
        (self.shots / 'captures.json').unlink()
        used, stdout = self.compare('--from-captures', str(self.shots), '--routes', str(self.routes))
        self.assertIn('no usable capture index', stdout)
        self.assertFalse(any(p.get('fromCaptures') for p in used['pairs']))

    def test_the_two_flags_go_together(self):
        run = subprocess.run([sys.executable, '-W', 'ignore', str(SCRIPT), '--manifest', str(self.manifest), '--wp', self.base,
                              '--from-captures', str(self.shots)], capture_output=True, text=True)
        self.assertEqual(run.returncode, 2)
        self.assertIn('go together', run.stderr)


if __name__ == '__main__':
    unittest.main()
