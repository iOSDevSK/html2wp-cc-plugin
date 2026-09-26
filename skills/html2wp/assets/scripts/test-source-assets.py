#!/usr/bin/env python3
"""Missing exported images: restoration, guarded fetch, reports and input isolation."""
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'lib'))
import source_assets as sa


def png():
    out = io.BytesIO()
    Image.new('RGB', (20, 10), '#65ab45').save(out, 'PNG')
    return out.getvalue()


class SourceAssets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = self.root / 'project'
        self.dist = self.project / 'dist'
        self.dist.mkdir(parents=True)
        self.meta = self.project / 'src/hero.png.asset.json'
        self.meta.parent.mkdir()
        self.url = '/__platform/assets/id/hero.png'
        self.meta.write_text(json.dumps({'url': self.url, 'original_filename': 'hero-original.png'}))
        (self.dist / 'index.html').write_text('<main><img src="' + self.url + '"></main>')

    def run_recovery(self, **kwargs):
        return sa.recover(self.dist, self.project, **kwargs)

    def test_exact_local_metadata_recovery_and_repeat(self):
        data = png()
        self.meta.with_name('hero.png').write_bytes(data)
        r = self.run_recovery()
        self.assertTrue(r['passed'])
        self.assertEqual((self.dist / self.url.lstrip('/')).read_bytes(), data)
        self.assertEqual(r['recovered'][0]['sha256'], hashlib.sha256(data).hexdigest())
        self.assertEqual(self.run_recovery()['recovered'], [])

    def test_missing_origin_does_not_guess_or_fetch(self):
        with patch.object(sa, 'fetch_image') as fetch:
            r = self.run_recovery()
        fetch.assert_not_called()
        self.assertFalse(r['passed'])
        self.assertIn('source origin', r['unresolved'][0]['reason'])

    def test_explicit_origin_fetches_exact_path(self):
        with patch.object(sa, 'fetch_image', return_value=png()) as fetch:
            r = self.run_recovery(origin='https://original.example')
        self.assertTrue(r['passed'])
        self.assertEqual(fetch.call_args.args[0], 'https://original.example' + self.url)

    def test_html_response_is_never_written_as_png(self):
        with patch.object(sa, 'fetch_image', return_value=b'<html>fallback</html>'):
            r = self.run_recovery(origin='https://original.example')
        self.assertFalse(r['passed'])
        self.assertFalse((self.dist / self.url.lstrip('/')).exists())

    def test_ambiguous_local_names_refused(self):
        for folder in ('one', 'two'):
            p = self.project / folder / 'hero-original.png'
            p.parent.mkdir()
            p.write_bytes(png())
        r = self.run_recovery()
        self.assertIn('ambiguous', r['unresolved'][0]['reason'])

    def test_unused_metadata_does_not_fail(self):
        (self.dist / 'index.html').write_text('<main>no images</main>')
        self.assertTrue(self.run_recovery()['passed'])

    def test_metadata_used_only_in_js_is_recovered(self):
        (self.dist / 'index.html').write_text('<main></main><script src="/app.js"></script>')
        (self.dist / 'app.js').write_text('const img=' + json.dumps(self.url) + ';')
        self.meta.with_name('hero.png').write_bytes(png())
        self.assertEqual(len(self.run_recovery()['recovered']), 1)

    def test_nested_html_and_css_paths(self):
        (self.dist / 'index.html').unlink()
        (self.dist / 'nested').mkdir()
        (self.dist / 'nested/page.html').write_text('<img src="../images/photo.png"><img srcset="../images/photo.png 2x">')
        p = self.project / 'public/images/photo.png'
        p.parent.mkdir(parents=True)
        p.write_bytes(png())
        r = self.run_recovery()
        self.assertEqual([x['file'] for x in r['recovered']], ['images/photo.png'])

    def test_working_build_keeps_owner_unchanged(self):
        self.meta.with_name('hero.png').write_bytes(png())
        out = self.root / 'workspace/static-src'
        out.parent.mkdir()
        prepared, r = sa.prepare_build(self.dist, self.project, out)
        self.assertTrue(r['passed'])
        self.assertFalse((self.dist / self.url.lstrip('/')).exists())
        self.assertTrue((prepared / self.url.lstrip('/')).is_file())

    def test_traversal_symlink_private_and_credential_paths_refused(self):
        for ref in ('/../../secret.png', '/%2e%2e/secret.png', '/.private/secret.png', '/x\\secret.png'):
            with self.assertRaises(ValueError):
                sa.safe_target(self.dist, ref)
        (self.dist / 'escape').symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(ValueError):
            sa.safe_target(self.dist, '/escape/secret.png')
        with self.assertRaises(ValueError):
            self.run_recovery(origin='https://user:pass@example.com')
        with self.assertRaises(ValueError):
            sa.fetch_image('https://127.0.0.1/x.png', time.monotonic() + 2)

    def test_fetch_validates_type_and_size(self):
        class Response(io.BytesIO):
            headers = {'Content-Type': 'image/png'}
        opener = unittest.mock.Mock()
        opener.open.return_value = Response(png())
        with patch.object(sa.urllib.request, 'build_opener', return_value=opener), \
             patch.object(sa, 'address_verdict', return_value=None):
            self.assertEqual(sa.fetch_image('https://example.com/a.png', time.monotonic() + 2), png())
            opener.open.return_value = Response(b'oversized')
            with patch.object(sa, 'MAX_BYTES', 3), self.assertRaisesRegex(ValueError, '25 MB'):
                sa.fetch_image('https://example.com/a.png', time.monotonic() + 2)
            response = Response(b'<html>not an image</html>')
            response.headers = {'Content-Type': 'text/html'}
            opener.open.return_value = response
            with self.assertRaisesRegex(ValueError, 'raster image'):
                sa.fetch_image('https://example.com/a.png', time.monotonic() + 2)

    def test_existing_asset_and_unowned_directory_not_overwritten(self):
        target = self.dist / self.url.lstrip('/')
        target.parent.mkdir(parents=True)
        target.write_bytes(png())
        self.assertEqual(self.run_recovery()['recovered'], [])
        output = self.root / 'workspace/static-src'
        prepared = output.parent / '.static-src-source-build'
        prepared.mkdir(parents=True)
        (prepared / 'owner.txt').write_text('keep')
        with self.assertRaisesRegex(ValueError, 'not owned'):
            sa.prepare_build(self.dist, self.project, output)
        self.assertEqual((prepared / 'owner.txt').read_text(), 'keep')

    def test_query_variants_do_not_silently_share_one_image(self):
        (self.dist / 'index.html').write_text('<img src="/hero.png?id=A"><img src="/hero.png?id=B">')
        with patch.object(sa, 'fetch_image') as fetch:
            r = self.run_recovery(origin='https://original.example')
        fetch.assert_not_called()
        self.assertIn('conflicting query', r['unresolved'][0]['reason'])

    def test_overlapping_build_and_failed_copy_preserve_baseline(self):
        with self.assertRaisesRegex(ValueError, 'overlapping'):
            sa.prepare_build(self.dist, self.project, self.dist / 'static-src')
        self.meta.with_name('hero.png').write_bytes(png())
        out = self.root / 'workspace/static-src'
        prepared, _ = sa.prepare_build(self.dist, self.project, out)
        before = (prepared / 'index.html').read_bytes()
        (self.dist / 'unsafe').symlink_to(self.project, target_is_directory=True)
        with self.assertRaises(ValueError):
            sa.prepare_build(self.dist, self.project, out)
        self.assertEqual((prepared / 'index.html').read_bytes(), before)

    def test_corrupt_png_and_incomplete_http_become_unresolved(self):
        import http.client
        data = bytearray(png())
        at = data.index(b'IDAT')
        length = int.from_bytes(data[at - 4:at], 'big')
        data[at + 4 + length] ^= 0xff  # corrupt the IDAT CRC, keep structure intact
        with patch.object(sa, 'fetch_image', return_value=bytes(data)):
            r = self.run_recovery(origin='https://original.example')
        self.assertFalse(r['passed'])
        self.assertIn('invalid image data', r['unresolved'][0]['reason'])
        with patch.object(sa, 'fetch_image', side_effect=http.client.IncompleteRead(b'x', 10)):
            r = self.run_recovery(origin='https://original.example')
        self.assertFalse(r['passed'])
        self.assertEqual(r['unresolved'][0]['reason'], 'IncompleteRead')

    def test_encoded_filename_is_decoded_only_once(self):
        (self.dist / 'index.html').write_text('<img src="/literal%2520name.png"><img src="/literal%23name.png">')
        with patch.object(sa, 'fetch_image', return_value=png()) as fetch:
            r = self.run_recovery(origin='https://original.example')
        self.assertTrue(r['passed'])
        for entry in r['recovered']:
            self.assertEqual((self.dist / entry['file']).read_bytes(), png())
        self.assertTrue((self.dist / 'literal%20name.png').is_file())
        self.assertTrue((self.dist / 'literal#name.png').is_file())
        self.assertEqual({c.args[0] for c in fetch.call_args_list},
                         {'https://original.example/literal%2520name.png', 'https://original.example/literal%23name.png'})

    def test_private_redirect_rechecked(self):
        import urllib.error
        from email.message import Message
        headers = Message()
        headers['Location'] = 'https://127.0.0.1/secret.png'
        opener = unittest.mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError('https://example.com/a.png', 302, '', headers, None)
        with patch.object(sa.urllib.request, 'build_opener', return_value=opener), \
             patch.object(sa, 'address_verdict', side_effect=[None, 'private address']):
            with self.assertRaisesRegex(ValueError, 'private'):
                sa.fetch_image('https://example.com/a.png', time.monotonic() + 2)
        self.assertEqual(opener.open.call_count, 1)

    def test_exhausted_budget_is_reported(self):
        with patch.object(sa, 'MAX_ASSETS', 0):
            r = self.run_recovery()
        self.assertIn('budget', r['unresolved'][0]['reason'])

    def test_result_row_cannot_be_hidden_by_pixel_pass(self):
        spec = importlib.util.spec_from_file_location('writer', HERE / 'write-result.py')
        writer = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(writer)
        r = self.run_recovery(report_path=self.root / 'source-assets-report.json')
        rows = writer.gates_of(self.root, {}, 'html', 'flash')
        row = next(x for x in rows if x['id'] == 'source-assets')
        self.assertEqual(row['status'], 'failed')
        self.assertEqual(writer.verdict_of('flash', [row]), 'Flash: failed checks')

    def test_static_site_cli_restores_before_flattening(self):
        self.meta.with_name('hero.png').write_bytes(png())
        out = self.root / 'workspace/static-src'
        out.parent.mkdir()
        p = subprocess.run([sys.executable, str(HERE / 'static-site.py'), '--project', str(self.project),
                            '--out', str(out), '--skip-build'], capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertTrue((out / self.url.lstrip('/')).is_file())
        self.assertFalse((self.dist / self.url.lstrip('/')).exists())


    def test_imported_astro_keeps_asset_for_next_build(self):
        spec = importlib.util.spec_from_file_location('detect', HERE / 'detect-project.py')
        detect = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(detect)
        self.meta.with_name('hero.png').write_bytes(png())
        report = self.project / '.html2wp/astro-report.json'
        report.parent.mkdir()
        report.write_text('{}')
        ws = self.root / 'imported'
        detect.prepare_astro_export(self.project, ws)
        for folder in ('static-src', 'astro-project/public', 'astro-project/dist'):
            self.assertEqual((ws / folder / self.url.lstrip('/')).read_bytes(), png())
        self.assertFalse((self.dist / self.url.lstrip('/')).exists())

    def test_recovered_image_survives_real_assembly_and_zip(self):
        import os
        import shutil
        import zipfile
        self.meta.with_name('hero.png').write_bytes(png())
        self.assertTrue(self.run_recovery()['passed'])
        fragment = (self.dist / 'index.html').read_text()
        (self.dist / 'index.html').write_text('<html><head><title>Test</title></head><body>' + fragment + '</body></html>')
        ws = self.root / 'assembly'
        ws.mkdir()
        manifest = {'schema': 'html2wp/1', 'workspace': str(ws),
                    'site': {'name': 'Test', 'slug': 'test-site', 'prefix': 'test_site', 'version': '1.0.0'},
                    'input': {'dir': str(self.dist), 'type': 'built-dist'},
                    'design': {'palette': [], 'fonts': []}, 'chrome': {'frontOwnsFooter': True}, 'nav': [],
                    'pages': [{'file': 'index.html', 'key': 'front-page', 'kind': 'front', 'chrome': 'self-contained'}],
                    'blog': {'present': False, 'reason': 'Single page'},
                    'shop': {'present': False, 'reason': 'No products'}}
        (ws / 'conversion-manifest.json').write_text(json.dumps(manifest))
        core = HERE.parents[4] / 'server/core/scripts'
        commands = [
            ['node', str(HERE / 'html-to-astro.mjs'), f'--manifest={ws}/conversion-manifest.json'],
            ['node', str(core / 'make-theme.mjs'), f'--manifest={ws}/conversion-manifest.json'],
            ['node', str(core / 'dist-to-bundle.mjs'), str(ws / 'astro-project/dist'), str(ws / 'bundle-out'),
             '--theme-slug=test-site', '--theme-name=Test', '--theme-version=1.0.0',
             f'--theme-dir={ws}/theme/test-site', f'--manifest={ws}/conversion-manifest.json', '--forbid-in-front=']]
        for i, command in enumerate(commands):
            done = subprocess.run(command, cwd=ws, capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
            if i == 0:
                # Self-contained HTML fixture needs no Astro compiler or dependency download.
                shutil.copytree(ws / 'astro-project/public', ws / 'astro-project/dist')
                astro_env = {**os.environ, 'H2WP_MODE': 'astro', 'H2WP_OUTPUT_DIR': str(ws / 'out')}
                delivered = subprocess.run([sys.executable, str(HERE / 'write-result.py'), str(ws),
                                            '--mode', 'astro', '--no-pdf'], env=astro_env,
                                           capture_output=True, text=True)
                self.assertEqual(delivered.returncode, 0, delivered.stdout + delivered.stderr)
                result = json.loads((ws / 'out/result.json').read_text())
                self.assertEqual(result['status'], 'delivered')
                with zipfile.ZipFile(ws / 'out' / result['astro']['file']) as archive:
                    for part in ('public', 'dist'):
                        self.assertEqual(archive.read('test-site-astro/' + part + self.url), png())
        env = {**os.environ, 'H2WP_WORKSPACE': str(ws), 'MAKE_ZIP_BEST_EFFORT': '1',
               'MAKE_ZIP_MANIFEST': str(ws / 'conversion-manifest.json')}
        done = subprocess.run(['bash', str(HERE / 'make-zip.sh'), str(ws / 'theme/test-site'), str(ws / 'theme.zip')],
                              env=env, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        with zipfile.ZipFile(ws / 'theme.zip') as archive:
            images = [name for name in archive.namelist() if name.endswith('/hero.png')]
            self.assertTrue(images)
            self.assertTrue(any(archive.read(name) == png() for name in images))
            self.assertNotIn('test-site/source-assets-report.json', archive.namelist())

    def test_browser_full_flash_and_gates_only_keep_restored_image(self):
        from functools import partial
        from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
        import threading
        from playwright.sync_api import sync_playwright
        self.meta.with_name('hero.png').write_bytes(png())
        for mode in ('flash', 'full'):
            ws = self.root / mode
            ws.mkdir()
            out = ws / 'static-src'
            cmd = [sys.executable, str(HERE / 'prerender-spa.py'), '--project', str(self.project),
                   '--out', str(out), '--skip-build', '--routes', '/', '--jobs', '1']
            if mode == 'flash':
                cmd += ['--flash', '--no-verify']
            done = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            self.assertEqual(done.returncode, 0, done.stdout[-3000:] + done.stderr[-3000:])
            report = json.loads((ws / 'prerender-report.json').read_text())
            self.assertEqual(len(report['sourceAssets']['recovered']), 1)
            prepared = Path(report['dist'])
            for root in (prepared, out):
                handler = partial(SimpleHTTPRequestHandler, directory=str(root))
                server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                try:
                    with sync_playwright() as pw:
                        browser = pw.chromium.launch()
                        page = browser.new_page()
                        page.goto(f'http://127.0.0.1:{server.server_port}/')
                        self.assertEqual(page.locator('img').evaluate('(el) => el.naturalWidth'), 20)
                        browser.close()
                finally:
                    server.shutdown()
                    server.server_close()
            # Gate-only must keep the prepared reference and recovery receipt byte-identical.
            if mode == 'full':
                asset_report = (ws / 'source-assets-report.json').read_bytes()
                asset = prepared / self.url.lstrip('/')
                before = asset.stat().st_mtime_ns
                check = subprocess.run(cmd + ['--gates-only'], capture_output=True, text=True, timeout=180)
                self.assertEqual(check.returncode, 0, check.stdout[-2000:] + check.stderr[-2000:])
                self.assertEqual(asset.stat().st_mtime_ns, before)
                self.assertEqual((ws / 'source-assets-report.json').read_bytes(), asset_report)
                self.assertEqual(json.loads((ws / 'prerender-report.json').read_text())['dist'], str(prepared))
            self.assertFalse((self.dist / self.url.lstrip('/')).exists())


if __name__ == '__main__':
    unittest.main()
