#!/usr/bin/env python3
"""prerender-spa.py takes the pages a build wrote when there is no route table.

An app without React Router (a Vite multi-page build, a static export, an
Astro build that hydrates) has no `<Route path>` to read, and the capture
used to refuse it: "no routes discovered — pass --routes". The desktop app
worked around that by passing the built pages as --routes itself; the plugin
now does it: when no route is declared, the build's own HTML files are the
routes (`about/index.html` → /about, `blog/post.html` → /blog/post), and the
report says `routesFrom: build output`. A build that wrote nothing but the
app shell still has no routes and is still refused.

  python3 test-prerender-build-routes.py
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name('prerender-spa.py')

PAGE = """<!doctype html><html><head><title>{title}</title></head><body>
<header><a href="/">Home</a> <a href="/about/">About</a> <a href="/blog/post.html">Post</a></header>
<main><h1>{title}</h1><p>Plain page {title}, no router.</p></main></body></html>"""


def site(root, pages):
    (root / 'dist').mkdir(parents=True)
    (root / 'package.json').write_text('{"scripts":{"build":"vite build"},"dependencies":{"vite":"5"}}')
    for rel, title in pages.items():
        path = root / 'dist' / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(PAGE.format(title=title))
    return root


def run(root):
    out = root / 'out'
    result = subprocess.run([sys.executable, str(SCRIPT), '--project', str(root), '--out', str(out),
                             '--skip-build', '--no-verify'], capture_output=True, text=True, timeout=600)
    report = root / 'prerender-report.json'
    return result, (json.loads(report.read_text()) if report.is_file() else {}), out


class BuildRoutes(unittest.TestCase):
    def test_the_pages_a_build_wrote_are_the_routes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = site(Path(tmp), {'index.html': 'Home', 'about/index.html': 'About', 'blog/post.html': 'Post',
                                    '404.html': 'Missing', 'assets/x.html': 'Asset'})
            result, report, out = run(root)
            self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-2000:])
            self.assertEqual(report.get('routesFrom'), 'build output')
            self.assertEqual(sorted(report['routes']), ['/', '/about', '/blog/post'])
            for rel in ('index.html', 'about.html', 'blog/post.html'):
                self.assertTrue((out / rel).is_file(), rel)

    def test_an_app_shell_alone_is_still_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = site(Path(tmp), {'index.html': 'Shell'})
            result, _, _ = run(root)
            self.assertEqual(result.returncode, 2)
            self.assertIn('no routes discovered', result.stderr)


if __name__ == '__main__':
    unittest.main()
