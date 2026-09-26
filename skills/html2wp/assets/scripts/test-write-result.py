#!/usr/bin/env python3
"""write-result.py writes the verdict a UI reads, and report-pdf.py prints it.

- A Flash run with gate A red and the rest green is delivered, its verdict is
  "Flash: not visually repaired", and gate A stays failed, marked reportOnly.
- The theme ZIP and the report are copied into the output directory and the
  ZIP's sha256 is the copy's.
- No secret travels: a password in the test-env state file, the service's
  message (it names local paths) and the job token stay out of result.json.
- A gate with no report is not_run; a run with no theme is stopped, with the
  stage and reason it was given.
- report-pdf.py prints a real PDF from the result and the report (skipped
  where Playwright is not installed).

  python3 test-write-result.py
"""
import hashlib
import importlib.util
import json
import io
import zipfile
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
WRITE = HERE / 'write-result.py'
PDF = HERE / 'report-pdf.py'


def write(root, files):
    for rel, value in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(value, bytes):
            path.write_bytes(value)
        else:
            path.write_text(value if isinstance(value, str) else json.dumps(value))


MANIFEST = {'schema': 'html2wp/1', 'site': {'name': 'Test Site', 'slug': 'test-site', 'version': '1.2.0'},
            'pages': [{'file': 'index.html', 'key': 'front-page'}],
            'nav': [{'selector': 'nav.main', 'zoneSelector': '[data-ve-nav="1"]'},
                    {'selector': 'ul.foot', 'unwired': 'menu not editable, static nav kept'}],
            'blog': {'present': True, 'articles': ['a.html', 'b.html']},
            'shop': {'present': False, 'reason': 'a portfolio'},
            'forms': [{'page': 'contact.html', 'selector': 'form.contact', 'purpose': 'contact'}]}


def theme_zip():
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w') as z:
        for name, content in {'style.css':'/* Theme Name: Test */', 'theme.json':'{}',
                              'templates/index.html':'<!-- wp:post-content /-->',
                              'functions.php':"<?php require_once __DIR__ . '/inc/content-import.php';",
                              'inc/content-import.php':'<?php // fixture importer',
                              'clara-content/sources/index.json':json.dumps([{'key':'front-page','file':'sources/front-page.html'}]),
                              'clara-content/manifest.json':json.dumps({'format':'clara-content/1','contains':{'sources':1}}),
                              'clara-content/sources/front-page.html':'<main>Real content</main>'}.items():
            z.writestr('test-site/' + name, content)
    return data.getvalue()


def workspace(root, zip_bytes=b'PK-theme'):
    ws = root / 'ws'
    files = {
        'conversion-manifest.json': MANIFEST,
        'CONVERSION-REPORT.md': '# Test report\n\nGate A is red on **one** page.\n',
        'detect.json': {'kind': 'static-html'},
        'verify-static/report.json': {'passed': False, 'scope': 'full', 'pages': {
            'index.html': {'desktop': {'diffRatio': 0.012, 'ok': False}, 'mobile': {'diffRatio': 0.001, 'ok': True}},
            'about.html': {'desktop': {'diffRatio': 0.0, 'ok': True}}}},
        'parity-report.json': {'passed': True, 'pages': {'index.html': {}}},
        'preflight-listings.json': {'passed': True, 'rows': []},
        '.h2wp-result.json': {'status': 'SUCCESS', 'code': 'OK', 'stage': 'done', 'edition': 'pro', 'jobId': 'job_1',
                              'message': 'theme unpacked into /Users/someone/secret/path', 'token': 'TOKEN-SECRET'},
        'install-theme/report.json': {'passed': True},
        'quick-check.json': {'schema': 'h2wp-quick-check/1', 'passed': True, 'problems': []},
        'verify-wp/report.json': {'passed': True, 'pages': {'index.html': {'desktop': {'diffRatio': 0.001, 'ok': True}}}},
        'smoke-editor/report.json': {'passed': True, 'failed': []},
        '.test-env-test-site.json': {'url': 'http://localhost:55123', 'password': 'WP-PASSWORD-SECRET'},
    }
    if zip_bytes is not None:
        files['test-site-1.2.0.zip'] = theme_zip() if zip_bytes == b'PK-theme' else zip_bytes
    write(ws, files)
    return ws


def run(*args):
    return subprocess.run([sys.executable, str(WRITE), *map(str, args)], capture_output=True, text=True, timeout=120)


class WriteResult(unittest.TestCase):
    def test_a_flash_delivery_with_a_red_visual_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws, out = workspace(Path(tmp)), Path(tmp) / 'out'
            done = run(ws, '--output', out, '--mode', 'flash', '--no-pdf')
            self.assertEqual(done.returncode, 0, done.stderr)
            raw = (out / 'result.json').read_text()
            doc = json.loads(raw)
            self.assertEqual((doc['schema'], doc['mode'], doc['target'], doc['status']),
                             ('h2wp-result/1', 'flash', 'html', 'delivered'))
            self.assertEqual(doc['verdict'], 'Flash: not visually repaired')
            gates = {g['id']: g for g in doc['gates']}
            self.assertEqual((gates['A']['status'], gates['A']['reportOnly']), ('failed', True))
            self.assertIn('1 of 3 measured page width(s)', gates['A']['detail'])
            self.assertEqual((gates['prerender']['status'], gates['B']['status'], gates['C']['status'], gates['woo']['status']),
                             ('skipped', 'passed', 'passed', 'skipped'))
            self.assertFalse(gates['quick']['reportOnly'])
            self.assertEqual(doc['theme']['sha256'], hashlib.sha256((ws / 'test-site-1.2.0.zip').read_bytes()).hexdigest())
            self.assertEqual(doc['theme']['file'], 'test-site-1.2.0.zip')
            self.assertTrue((out / 'test-site-1.2.0.zip').is_file())
            self.assertIn('Test report', (out / 'CONVERSION-REPORT.md').read_text())
            self.assertEqual(doc['wired'], {'menus': 2, 'menusWired': 1, 'blog': {'present': True, 'posts': 2},
                                            'shop': {'present': False, 'products': 0}, 'forms': 1, 'collections': 0})
            self.assertEqual(doc['preview']['url'], 'http://localhost:55123')
            self.assertEqual((doc['service']['edition'], doc['service']['jobId']), ('pro', 'job_1'))
            for secret in ('password', 'PASSWORD-SECRET', 'TOKEN-SECRET', '/Users/someone'):
                self.assertNotIn(secret, raw)
            self.assertEqual(json.loads((ws / 'result.json').read_text()), doc)

    def test_a_functional_failure_is_not_a_visual_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws, out = workspace(Path(tmp)), Path(tmp) / 'out'
            write(ws, {'quick-check.json': {'passed': False, 'problems': ['/about/: HTTP 500']}})
            doc = json.loads((out / 'result.json').read_text()) if run(ws, '--output', out, '--mode', 'flash', '--no-pdf').returncode == 0 else {}
            quick = next(g for g in doc['gates'] if g['id'] == 'quick')
            self.assertEqual((quick['status'], quick['reportOnly'], quick['detail']), ('failed', False, '/about/: HTTP 500'))
            self.assertEqual(doc['verdict'], 'Flash: failed checks')

    def test_gate_b_is_a_picture_and_gate_c_is_not(self):
        # verify-wp writes both into one report. A red page picture is B,
        # reported and not repaired; a menu that was not wired is C, and it
        # makes the verdict "failed checks", never "not visually repaired".
        with tempfile.TemporaryDirectory() as tmp:
            ws, out = workspace(Path(tmp)), Path(tmp) / 'out'
            write(ws, {'verify-static/report.json': {'passed': True, 'pages': {}},
                       'verify-wp/report.json': {'passed': False, 'pages': {
                           'about.html': {'desktop': {'diffRatio': 0.2, 'ok': False}},
                           'contact.html': {'desktop': {'diffRatio': 0.7, 'ok': False, 'status': 'stylesheet-missing'}}},
                           'checks': {'routing': {'ok': True}, 'menusWired': {'ok': False},
                                      'storedSources': {'checked': 5, 'empty': [], 'missing': []}}}})
            self.assertEqual(run(ws, '--output', out, '--mode', 'flash', '--no-pdf').returncode, 0)
            doc = json.loads((out / 'result.json').read_text())
            gates = {g['id']: g for g in doc['gates']}
            self.assertEqual((gates['B']['status'], gates['B']['reportOnly'], gates['B']['pages']), ('failed', True, ['about.html']))
            self.assertEqual((gates['C']['status'], gates['C']['reportOnly']), ('failed', False))
            self.assertIn('menusWired', gates['C']['detail'])
            self.assertIn('contact.html: stylesheet missing', gates['C']['detail'])
            self.assertEqual(doc['verdict'], 'Flash: failed checks')
            # Pictures alone: reported, the verdict names them as not repaired.
            write(ws, {'verify-wp/report.json': {'passed': False, 'pages': {'about.html': {'desktop': {'diffRatio': 0.2, 'ok': False}}},
                                                 'checks': {'menusWired': {'ok': True}}}})
            self.assertEqual(run(ws, '--output', out, '--mode', 'flash', '--no-pdf').returncode, 0)
            doc = json.loads((out / 'result.json').read_text())
            self.assertEqual({g['id']: g['status'] for g in doc['gates']}['C'], 'passed')
            self.assertEqual(doc['verdict'], 'Flash: not visually repaired')

    def test_exempt_cells_are_not_counted_as_measured_and_a2_is_fidelity(self):
        # A dynamic listing's cells (ok None) were exempted by the gate, not
        # measured; a red A2 is markup fidelity, reported like a picture.
        with tempfile.TemporaryDirectory() as tmp:
            ws, out = workspace(Path(tmp)), Path(tmp) / 'out'
            write(ws, {'verify-static/report.json': {'passed': True, 'pages': {}},
                       'parity-report.json': {'passed': False, 'pages': {'about.html': {}}},
                       'verify-wp/report.json': {'passed': False, 'pages': {
                           'index.html': {'desktop': {'diffRatio': 0.05, 'ok': False}, 'mobile': {'diffRatio': 0.0, 'ok': True}},
                           'journal.html': {'desktop': {'diffRatio': 0.3, 'ok': None, 'status': 'dynamic-listing'}}}}})
            self.assertEqual(run(ws, '--output', out, '--mode', 'flash', '--no-pdf').returncode, 0)
            doc = json.loads((out / 'result.json').read_text())
            gates = {g['id']: g for g in doc['gates']}
            self.assertIn('1 of 2 measured page width(s)', gates['B']['detail'])
            self.assertIn('1 exempt by the gate', gates['B']['detail'])
            self.assertEqual((gates['A2']['status'], gates['A2']['reportOnly']), ('failed', True))
            self.assertEqual(gates['listings']['stage'], '1')
            self.assertEqual(doc['verdict'], 'Flash: not visually repaired')

    def test_a_check_that_did_not_run_is_never_a_pass(self):
        # app-ui's repro: a manifest, a ZIP and a report, and no gate report at
        # all. Its checks never ran; the verdict must say so.
        with tempfile.TemporaryDirectory() as tmp:
            ws, out = Path(tmp) / 'ws', Path(tmp) / 'out'
            write(ws, {'conversion-manifest.json': {'site': {'name': 'Clara Hayes', 'slug': 'clara-hayes', 'version': '1.0.0'}, 'pages': []},
                       'clara-hayes-1.0.0.zip': theme_zip(), 'CONVERSION-REPORT.md': '# Report\n'})
            self.assertEqual(run(ws, '--output', out, '--no-pdf', '--mode', 'flash').returncode, 0)
            doc = json.loads((out / 'result.json').read_text())
            self.assertEqual((doc['status'], doc['verdict']), ('delivered', 'Flash: checks not run'))
            self.assertEqual(run(ws, '--output', out, '--no-pdf', '--mode', 'full').returncode, 0)
            self.assertEqual(json.loads((out / 'result.json').read_text())['verdict'], 'checks not run')

    def test_gate_minus_one_skipped_by_flash_is_not_a_missing_check(self):
        # Flash prerenders with --no-verify on purpose: that row is not_run
        # byDesign, and the rest green is "all checks passed". In Full the
        # same row is a check that did not run.
        with tempfile.TemporaryDirectory() as tmp:
            ws, out = workspace(Path(tmp)), Path(tmp) / 'out'
            write(ws, {'detect.json': {'kind': 'web-app'},
                       'prerender-report.json': {'passed': True, 'routes': ['/', '/about'], 'warnings': ['parity gate skipped (--no-verify)']},
                       'verify-static/report.json': {'passed': True, 'pages': {}}})
            self.assertEqual(run(ws, '--output', out, '--no-pdf', '--mode', 'flash').returncode, 0)
            doc = json.loads((out / 'result.json').read_text())
            row = next(g for g in doc['gates'] if g['id'] == 'prerender')
            self.assertEqual((row['status'], row.get('byDesign')), ('not_run', True))
            self.assertEqual(doc['verdict'], 'Flash: all checks passed')
            self.assertEqual(run(ws, '--output', out, '--no-pdf', '--mode', 'full').returncode, 0)
            self.assertEqual(json.loads((out / 'result.json').read_text())['verdict'], 'checks not run')

    def test_the_astro_run_delivers_the_astro_project_and_its_built_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws, out = workspace(Path(tmp), zip_bytes=None), Path(tmp) / 'out'
            write(ws, {'verify-static/report.json': {'passed': True, 'pages': {}},
                       'astro-project/package.json': '{}', 'astro-project/dist/index.html': '<h1>x</h1>'})
            self.assertEqual(run(ws, '--output', out, '--no-pdf', '--mode', 'astro').returncode, 0)
            doc = json.loads((out / 'result.json').read_text())
            self.assertEqual((doc['mode'], doc['target'], doc['status'], doc['theme']), ('astro', 'astro', 'delivered', None))
            self.assertEqual(doc['astro']['file'], 'test-site-astro-1.2.0.zip')
            self.assertEqual(doc['builtSite'], {'path': str((ws / 'astro-project' / 'dist').resolve()),
                                                'workspacePath': 'astro-project/dist', 'index': 'index.html'})
            self.assertEqual([g['id'] for g in doc['gates']], ['prerender', 'A', 'A2'])
            self.assertEqual(doc['verdict'], 'Astro: all checks passed')

    def test_a_run_that_stopped_before_a_theme(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws, out = workspace(Path(tmp), zip_bytes=None), Path(tmp) / 'out'
            for rel in ('verify-wp/report.json', 'install-theme/report.json', 'quick-check.json', 'smoke-editor/report.json'):
                (ws / rel).unlink()
            write(ws, {'.h2wp-result.json': {'status': 'FAILED', 'code': 'SITE_TOO_LARGE', 'stage': 'job'}})
            done = run(ws, '--output', out, '--mode', 'flash', '--no-pdf', '--stopped-stage', '3',
                       '--stopped-reason', 'the service refused the site: SITE_TOO_LARGE')
            self.assertEqual(done.returncode, 0, done.stderr)
            doc = json.loads((out / 'result.json').read_text())
            self.assertEqual((doc['status'], doc['theme'], doc['stopped']['stage'], doc['verdict']),
                             ('stopped', None, '3', 'Flash: stopped'))
            gates = {g['id']: g for g in doc['gates']}
            self.assertEqual((gates['convert']['status'], gates['convert']['detail']), ('failed', 'SITE_TOO_LARGE at job'))
            self.assertEqual((gates['install']['status'], gates['B']['status'], gates['C']['status']),
                             ('not_run', 'not_run', 'not_run'))

    def test_a_run_that_stopped_before_stage_0_has_a_result_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws, out = Path(tmp) / 'ws', Path(tmp) / 'out'
            ws.mkdir()
            self.assertEqual(run(ws, '--output', out, '--no-pdf').returncode, 2, 'a delivery needs a manifest')
            done = run(ws, '--output', out, '--no-pdf', '--mode', 'flash', '--status', 'stopped',
                       '--stopped-stage', '-4', '--stopped-reason', 'nothing convertible: no HTML pages')
            self.assertEqual(done.returncode, 0, done.stderr)
            doc = json.loads((out / 'result.json').read_text())
            self.assertEqual((doc['status'], doc['verdict'], doc['stopped']['stage']), ('stopped', 'Flash: stopped', '-4'))

    def test_a_full_run_all_green_is_verified_and_the_mode_file_is_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws, out = workspace(Path(tmp)), Path(tmp) / 'out'
            write(ws, {'verify-static/report.json': {'passed': True, 'pages': {}}, '.h2wp-mode': 'full\n'})
            self.assertEqual(run(ws, '--output', out, '--no-pdf').returncode, 0)
            self.assertEqual(json.loads((out / 'result.json').read_text())['verdict'], 'verified')

    def test_the_astro_project_is_zipped_without_its_packages(self):
        # The owner gets the Astro project too, as the desktop app packaged it,
        # with the converter's report inside so the ZIP can come back as input.
        import zipfile
        with tempfile.TemporaryDirectory() as tmp:
            ws, out = workspace(Path(tmp)), Path(tmp) / 'out'
            write(ws, {'astro-project/package.json': '{}', 'astro-project/src/pages/index.astro': '---\n---',
                       'astro-project/dist/index.html': '<h1>x</h1>', 'astro-project/node_modules/x/i.js': '',
                       'astro-project/.astro/types.d.ts': '', 'astro-report.json': '{"variants":[]}'})
            self.assertEqual(run(ws, '--output', out, '--no-pdf').returncode, 0)
            doc = json.loads((out / 'result.json').read_text())
            self.assertEqual(doc['astro']['file'], 'test-site-astro-1.2.0.zip')
            names = zipfile.ZipFile(out / 'test-site-astro-1.2.0.zip').namelist()
            self.assertIn('test-site-astro/src/pages/index.astro', names)
            self.assertIn('test-site-astro/dist/index.html', names)
            self.assertIn('test-site-astro/.html2wp/astro-report.json', names)
            self.assertFalse([n for n in names if 'node_modules' in n or '/.astro/' in n], names)

    @unittest.skipUnless(importlib.util.find_spec('playwright'), 'playwright is not installed')
    def test_the_pdf_is_printed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws, out = workspace(Path(tmp)), Path(tmp) / 'out'
            write(ws, {'CONVERSION-REPORT.md': '# Report\n\n| Page | Diff |\n|---|---|\n| index | 1.2% |\n\n'
                                              '- a <script>alert(1)</script> item\n\n```\ncode block\n```\n'})
            done = run(ws, '--output', out, '--mode', 'flash')
            self.assertEqual(done.returncode, 0, done.stderr)
            doc = json.loads((out / 'result.json').read_text())
            self.assertEqual(doc['report']['pdf'], 'conversion-report.pdf', doc['report'])
            self.assertEqual((out / 'conversion-report.pdf').read_bytes()[:4], b'%PDF')


class Markdown(unittest.TestCase):
    def test_nothing_passes_through_as_html(self):
        spec = importlib.util.spec_from_file_location('report_pdf', PDF)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        out = mod.markdown('# Title\n\nText with <b>tag</b> and `<code>` and **bold**.\n\n- one\n- two\n\n1. first\n')
        self.assertIn('<h2>Title</h2>', out)
        self.assertIn('&lt;b&gt;tag&lt;/b&gt;', out)
        self.assertIn('<code>&lt;code&gt;</code>', out)
        self.assertIn('<strong>bold</strong>', out)
        self.assertIn('<ul><li>one</li><li>two</li></ul>', out)
        self.assertIn('<ol><li>first</li></ol>', out)


if __name__ == '__main__':
    unittest.main()
