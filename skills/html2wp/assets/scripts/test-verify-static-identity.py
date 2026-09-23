#!/usr/bin/env python3
"""verify-static.py's proof by identity (gate A).

A page whose bytes, and the bytes of every same-origin file it can load, are
identical on both sides is proven, not photographed: `identicalByHash` with
each file's sha256, widths marked `identical-by-hash`, its console still read
on the original side. Anything that could render differently is rasterised:
  - one byte of a linked stylesheet differs (a comment: the pixels match, the
    raster still runs, and nothing is marked proven)
  - a file the page loads exists on one side only (an <img>, a url() in its
    CSS, a path only its script names)
  - an --original-remote run (those images are fetched live, twice)
Needs Chromium (Playwright); no network.

  python3 test-verify-static-identity.py
"""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import io

from PIL import Image

SCRIPT = Path(__file__).with_name('verify-static.py')
CSS = 'body{margin:0;font:16px/1.4 sans-serif;background:#fff}.hero{height:300px;background:url(../img/bg.png) #cde}\n'
PAGE = ('<!doctype html><html><head><title>{t}</title><link rel="stylesheet" href="/css/site.css"></head><body>'
        '<div class="hero"></div><h1>{t}</h1><p>Text of {t}.</p><a href="{link}">next</a>{extra}</body></html>')


class IdentityTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='h2wp-identity-')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        png = io.BytesIO()
        Image.new('RGB', (8, 8), (200, 120, 40)).save(png, 'PNG')
        self.orig, self.dist, self.out = root / 'orig', root / 'dist', root / 'report'
        for tree in (self.orig, self.dist):
            (tree / 'css').mkdir(parents=True)
            (tree / 'img').mkdir()
            (tree / 'css/site.css').write_text(CSS)
            (tree / 'img/bg.png').write_bytes(png.getvalue())
            (tree / 'index.html').write_text(PAGE.format(t='Home', link='about.html', extra=''))
            (tree / 'about.html').write_text(PAGE.format(t='About', link='index.html', extra='<img src="img/bg.png" alt="" width="10" height="10">'))

    def gate(self, *extra):
        run = subprocess.run([sys.executable, '-W', 'ignore', str(SCRIPT), '--original', str(self.orig), '--dist', str(self.dist),
                              '--out', str(self.out), *extra], capture_output=True, text=True, timeout=600)
        self.assertTrue((self.out / 'report.json').is_file(), run.stdout + run.stderr)
        return json.loads((self.out / 'report.json').read_text()), run

    def proven(self, report):
        return sorted(f for f, entry in report['pages'].items() if entry.get('identicalByHash'))

    def test_identical_build_is_proven_page_by_page(self):
        # A file only the build has, which nothing loads, changes nothing.
        (self.dist / 'img/unused.png').write_bytes((self.orig / 'img/bg.png').read_bytes())
        report, run = self.gate()
        self.assertTrue(report['passed'], run.stdout)
        self.assertEqual(self.proven(report), ['about.html', 'index.html'])
        self.assertEqual(report['identity'], {'pages': 2, 'distOnlyFiles': 1})
        about = report['pages']['about.html']
        self.assertEqual(sorted(about['sha256']), ['about.html', 'css/site.css', 'img/bg.png'])
        self.assertEqual(sorted(report['pages']['index.html']['sha256']), ['css/site.css', 'img/bg.png', 'index.html'])  # via the CSS
        for width in ('desktop', 'tablet', 'mobile'):
            self.assertEqual(about[width], {'diffRatio': 0.0, 'ok': True, 'status': 'identical-by-hash'})
        self.assertIn('proven identical by hash', run.stdout)
        self.assertEqual(list(self.out.glob('*.png')), [])

    def test_one_stylesheet_byte_differs_so_the_raster_runs(self):
        (self.dist / 'css/site.css').write_text(CSS + '/* rebuilt */\n')
        report, _ = self.gate()
        self.assertEqual(self.proven(report), [])
        self.assertIn('css/site.css', report['identity']['disabled'])
        for f in ('index.html', 'about.html'):
            for width in ('desktop', 'tablet', 'mobile'):
                row = report['pages'][f][width]
                self.assertNotIn('status', row)
                self.assertEqual(row['diffRatio'], 0.0)  # measured: a comment paints nothing
        self.assertTrue(report['passed'])

    def test_a_file_the_page_loads_on_one_side_only_is_rasterised(self):
        # The build lost the hero's url() image: nothing is proven, and the
        # raster sees the loss.
        (self.dist / 'img/bg.png').unlink()
        report, _ = self.gate()
        self.assertEqual(self.proven(report), [])
        self.assertFalse(report['passed'])

    def test_a_path_only_a_script_names_counts(self):
        # A data file only the original ships, named only inside a script:
        # that page is rasterised, the other one is still proven.
        (self.orig / 'data').mkdir()
        (self.orig / 'data/menu.json').write_text('{"items": ["img/bg.png"]}')
        script = "<script>fetch('data/menu.json').then(r => r.json())</script>"
        for tree in (self.orig, self.dist):
            (tree / 'index.html').write_text(PAGE.format(t='Home', link='about.html', extra=script))
        report, _ = self.gate()
        self.assertEqual(self.proven(report), ['about.html'])
        self.assertNotIn('identicalByHash', report['pages']['index.html'])
        self.assertIn('diffRatio', report['pages']['index.html']['desktop'])

    def test_an_attribute_value_longer_than_a_file_name_is_no_file(self):
        # Every whole attribute value is a candidate path. An SVG path or a
        # style-like data-* value over 255 bytes is a name no file system
        # holds: a 404 on both sides alike, not a crash of the whole gate.
        d = 'M0,0' + 'L1.25,2.5' * 60
        offsets = '; '.join(f'--offset-{side}: 24px' for side in ('top', 'right', 'bottom', 'left') * 6)
        extra = f'<svg width="10" height="10"><path d="{d}"/></svg><section data-offsets="{offsets};"></section>'
        for tree in (self.orig, self.dist):
            (tree / 'index.html').write_text(PAGE.format(t='Home', link='about.html', extra=extra))
        report, run = self.gate()
        self.assertTrue(report['passed'], run.stdout + run.stderr)
        self.assertEqual(self.proven(report), ['about.html', 'index.html'])

    def test_a_file_linked_out_of_the_tree_disables_the_proof(self):
        # The gate's server refuses a link out of the site, so nothing is
        # known about its bytes: every page is rasterised, nothing crashes.
        outside = Path(self.temp.name) / 'outside.css'
        outside.write_text(CSS)
        for tree in (self.orig, self.dist):
            (tree / 'css/linked.css').symlink_to(outside)
        report, run = self.gate()
        self.assertEqual(self.proven(report), [], run.stdout + run.stderr)
        self.assertIn('css/linked.css', report['identity']['disabled'])
        self.assertTrue(report['passed'])

    def test_an_original_remote_run_is_never_proven(self):
        (self.out.parent / 'remote.json').write_text(json.dumps({'localized': [{'url': 'https://img.example/a.png', 'finalUrl': 'https://img.example/a.png'}]}))
        report, _ = self.gate('--original-remote', str(self.out.parent / 'remote.json'))
        self.assertEqual(self.proven(report), [])
        self.assertIn('--original-remote', report['identity']['disabled'])

    def test_jobs_run_writes_the_same_proof(self):
        (self.dist / 'about.html').write_text(PAGE.format(t='About', link='index.html', extra='<img src="img/bg.png" alt="" width="10" height="10"> '))
        report, _ = self.gate('--jobs', '3')
        self.assertEqual(self.proven(report), ['index.html'])
        self.assertEqual(report['pages']['index.html']['desktop']['status'], 'identical-by-hash')
        self.assertNotIn('status', report['pages']['about.html']['desktop'])
        self.assertTrue(report['passed'])


if __name__ == '__main__':
    unittest.main()
