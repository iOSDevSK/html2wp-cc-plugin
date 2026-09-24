#!/usr/bin/env python3
"""static-site.py takes a static-site generator's own pages, without a browser.

An Astro build writes every page as complete HTML. They are written in the
shape prerender-spa.py produces — `about/index.html` becomes `about.html`,
page links point at the flat files relative to the linking page, resources
are rebased to the page's new place — and nothing else in the page changes.
A build that is not a plain static site (a hydrating framework root, an app
shell) writes nothing and exits 3: the browser capture applies instead.

  python3 test-static-site.py
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name('static-site.py')
BODY = '<p>' + 'Words that make this a real page and not an app shell. ' * 3 + '</p>'


def build(root, pages, extra=None):
    dist = root / 'project' / 'dist'
    for rel, html in {**pages, **(extra or {})}.items():
        path = dist / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html)
    (root / 'project' / 'package.json').write_text('{"scripts":{"build":"astro build"},"devDependencies":{"astro":"5"}}')
    return root / 'project'


def run(root, project):
    out = root / 'ws' / 'static-src'
    out.parent.mkdir(exist_ok=True)
    result = subprocess.run([sys.executable, str(SCRIPT), '--project', str(project), '--out', str(out), '--skip-build'],
                            capture_output=True, text=True, timeout=120)
    report = out.parent / 'static-site-report.json'
    return result, json.loads(report.read_text()) if report.is_file() else {}, out


class StaticSite(unittest.TestCase):
    def test_pages_are_flat_and_links_follow_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = build(Path(tmp), {
                'index.html': f'<html><head><link rel="stylesheet" href="/_astro/site.css"></head><body><a href="/about/">About</a>{BODY}</body></html>',
                'about/index.html': f'<html><head><link rel="stylesheet" href="/_astro/site.css"></head><body><a href="/">Home</a><img src="../img/a.png">{BODY}</body></html>',
            }, {'_astro/site.css': 'body{}', 'img/a.png': 'png'})
            result, report, out = run(Path(tmp), project)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertTrue(report['ok'])
            about = (out / 'about.html').read_text()
            self.assertIn('href="index.html"', about)
            self.assertIn('src="img/a.png"', about)
            self.assertIn('href="about.html"', (out / 'index.html').read_text())
            self.assertTrue((out / '_astro/site.css').is_file())
            self.assertFalse((out / 'about').exists())

    def test_a_hydrating_build_asks_for_the_browser_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            project = build(Path(tmp), {'index.html': f'<html><body><div id="__next">{BODY}</div><script id="__NEXT_DATA__"></script></body></html>'})
            result, report, out = run(Path(tmp), project)
            self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
            self.assertEqual(report.get('fallback'), 'prerender')
            self.assertIn('Next.js', report.get('reason', ''))
            self.assertFalse(out.exists())


if __name__ == '__main__':
    unittest.main()
