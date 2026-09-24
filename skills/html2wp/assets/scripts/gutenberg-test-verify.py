#!/usr/bin/env python3
"""gutenberg-verify-local.py's scopes and what accepts its report.

A smoke run (--scope smoke) measures 1440 only and skips the save/reload
gates. It is a diagnosis: packaging refuses its report even when it sits at
the verification path, and a report without the field is a full run. The
visual phase runs against two stand-in origins (static servers; no
WordPress): a smoke run captures 1440 only, a full run all three widths.

  python3 gutenberg-test-verify.py
"""
import contextlib
import functools
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
VERIFY = HERE / 'gutenberg-verify-local.py'
spec = importlib.util.spec_from_file_location('h2wp_package_under_test', HERE / 'gutenberg-package.py')
PACKAGE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(PACKAGE)


class ScopeEvidenceTest(unittest.TestCase):
    """A complete passing full report packages; the same report with any
    other scope, copied to the verification path, is refused."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='h2wp-scope-')
        self.addCleanup(self.temp.cleanup)
        self.ws = Path(self.temp.name).resolve()
        self.theme = self.ws / 'theme' / 'scoped'
        pages = [{'key': 'home', 'kind': 'front', 'slug': 'home'}, {'key': 'about', 'kind': 'page', 'slug': 'about'}]
        for name, value in (('style.css', '/* Theme Name: Scoped */'), ('theme.json', '{"version":3}'), ('functions.php', '<?php\n'),
                            ('templates/index.html', '<!-- wp:post-content /-->'), ('templates/front-page.html', '<!-- wp:post-content /-->'),
                            ('content/content.json', json.dumps({'schema': 'h2wp-content/1', 'pages': pages})),
                            ('content/config.json', json.dumps({'frontPage': 'home'})), ('content/assets.json', '[]')):
            (self.theme / name).parent.mkdir(parents=True, exist_ok=True)
            (self.theme / name).write_text(value)
        image = Image.new('RGB', (1200, 900), 'white')
        ImageDraw.Draw(image).rectangle((0, 0, 1199, 200), fill='navy')
        image.save(self.theme / 'screenshot.png')
        digest = PACKAGE.theme_digest(self.theme)
        editor, visual, editor_visual = [], [], []
        for number, page in enumerate(pages, start=1):
            path = '/' if page['kind'] == 'front' else '/' + page['slug'] + '/'
            editor.append({'id': number, 'kind': 'page', 'path': path, 'count': 2, 'invalid': [], 'unknown': [],
                           'roundtrip': {'count': 2, 'invalid': [], 'unknown': [], 'textPersisted': True}})
            for width in (1440, 820, 390):
                visual.append({'path': path, 'width': width, 'diff': .002, 'passed': True})
                editor_visual.append(self.canvas('page', path, width, 'content'))
        editor += [{'id': 'scoped//' + slug, 'kind': 'templates', 'count': 1, 'invalid': [], 'unknown': [], 'unresolvedTokens': False} for slug in ('index', 'front-page')]
        editor_visual += [{**self.canvas('templates', '/', width, 'document'), 'id': 'scoped//front-page'} for width in (1440, 820, 390)]
        preview = PACKAGE.screenshot_info(self.theme / 'screenshot.png')
        preview.update(passed=True, installedSha256=preview['sha256'], sourceUrl='http://localhost:8080/',
                       remoteUrl='http://localhost:8080/wp-content/themes/scoped/screenshot.png')
        counts = {'pages': 2, 'posts': 0, 'products': 0, 'media': 0, 'menus': 0}
        imported = {'schema': 'h2wp-import-status/1', 'passed': True, 'complete': True, 'phase': 'done', 'pending': 0, 'errors': [],
                    'stylesheet': 'scoped', 'bundleDigest': 'a' * 64, 'stateDigest': 'a' * 64, 'counts': counts, 'importedCounts': dict(counts),
                    'bundleSha256': hashlib.sha256((self.theme / 'content/content.json').read_bytes()).hexdigest()}
        serialization = [{'kind': row['kind'], 'id': row['id'], 'blocks': row['count'], 'byteIdentical': True, 'attributeLoss': [], 'passed': True} for row in editor]
        self.report = {'schema': 'h2wp-local-verification/2', 'scope': 'full', 'passed': True, 'themeDigest': digest, 'installedThemeDigest': digest,
                       'editor': editor, 'serialization': serialization, 'visual': visual, 'editorVisual': editor_visual, 'preview': preview, 'import': imported, 'threshold': .01}
        self.verification = self.ws / 'gutenberg-verification.json'

    @staticmethod
    def canvas(kind, path, width, region):
        return {'kind': kind, 'path': path, 'width': width, 'actualWidth': width, 'region': region, 'diff': .004, 'passed': True,
                'frontendScreenshot': 'f.png', 'editorScreenshot': 'e.png', 'canvas': {'selector': '.is-root-container', 'iframe': True, 'width': width, 'height': 600}}

    def package(self, report):
        self.verification.write_text(json.dumps(report))
        out = self.ws / 'scoped.zip'
        out.unlink(missing_ok=True)
        argv = ['gutenberg-package.py', '--theme', str(self.theme), '--report', str(self.verification), '--out', str(out)]
        stdout, stderr = io.StringIO(), io.StringIO()
        # PHP linting is its own gate; evidence acceptance is under test.
        with patch.object(sys, 'argv', argv), patch.object(PACKAGE.subprocess, 'run'), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                PACKAGE.main()
            except SystemExit as error:
                return error.code, stderr.getvalue(), out.exists()
        return 0, stdout.getvalue(), out.exists()

    def test_full_report_packages(self):
        code, output, made = self.package(self.report)
        self.assertEqual((code, made), (0, True), output)

    def test_every_surface_needs_the_editor_to_write_its_blocks_back(self):
        # A page whose stored blocks the editor would rewrite on its first save
        # (a deprecated save, an attribute that does not read back) is refused,
        # as is one the gate did not check.
        report = json.loads(json.dumps(self.report))
        report['serialization'][1].update(byteIdentical=False, passed=False)
        code, message, packaged = self.package(report)
        self.assertNotEqual(code, 0)
        self.assertFalse(packaged)
        self.assertIn('Missing passing serialization gate for page', message)
        report = json.loads(json.dumps(self.report))
        report['serialization'][2]['attributeLoss'] = [{'path': '/0:core/paragraph', 'block': 'core/paragraph', 'phase': 'parse', 'key': 'foo'}]
        self.assertIn('serialization gate for templates scoped//index', self.package(report)[1])
        report = json.loads(json.dumps(self.report))
        del report['serialization']
        self.assertNotEqual(self.package(report)[0], 0)
        self.assertEqual(self.package(self.report)[0], 0)

    def test_a_report_from_before_the_field_is_a_full_run(self):
        self.report.pop('scope')
        code, output, made = self.package(self.report)
        self.assertEqual((code, made), (0, True), output)

    def test_a_smoke_report_at_the_verification_path_is_refused(self):
        # Everything else in it passes: the scope alone refuses it.
        for scope in ('smoke', 'partial', None, ''):
            with self.subTest(scope=scope):
                code, output, made = self.package({**self.report, 'scope': scope})
                self.assertNotEqual(code, 0)
                self.assertFalse(made, 'a refused report must not produce a ZIP')
                self.assertIn('is not packaging evidence', output)
        with self.assertRaisesRegex(ValueError, 'not packaging evidence'):
            PACKAGE.validate_evidence(self.theme, {**self.report, 'scope': 'smoke'})


spec = importlib.util.spec_from_file_location('h2wp_verify_under_test', VERIFY)
VERIFY_MODULE = importlib.util.module_from_spec(spec)
spec.loader.exec_module(VERIFY_MODULE)


class PoolTest(unittest.TestCase):
    """pool_map: one Chromium per worker thread for all its items, results
    in item order, the first failure in item order raised after every item
    ran, and a browser that died replaced for the next item."""

    def test_one_browser_per_worker_in_item_order(self):
        seen = []
        def fn(browser, item):
            context = browser.new_context()
            try:
                page = context.new_page()
                page.set_content(f'<p>{item}</p>')
                seen.append((threading.get_ident(), id(browser)))
                return (item, page.inner_text('p'))
            finally:
                context.close()
        self.assertEqual(VERIFY_MODULE.pool_map(fn, range(7), 3), [(i, str(i)) for i in range(7)])
        self.assertLessEqual(len({b for _, b in seen}), 3)
        self.assertEqual(len({t for t, _ in seen}), len({b for _, b in seen}))

    def test_first_failure_in_item_order_after_all_ran(self):
        ran = []
        def fn(browser, item):
            ran.append(item)
            if item in (5, 2):
                raise RuntimeError(f'case {item}')
            return item
        with self.assertRaisesRegex(RuntimeError, 'case 2'):
            VERIFY_MODULE.pool_map(fn, range(6), 2)
        self.assertEqual(sorted(ran), list(range(6)))

    def test_a_dead_browser_is_replaced(self):
        browsers = []
        def fn(browser, item):
            browsers.append(browser)
            if item == 0:
                browser.close()
            return browser.is_connected()
        self.assertEqual(VERIFY_MODULE.pool_map(fn, range(3), 1), [False, True, True])
        self.assertIsNot(browsers[0], browsers[1])
        self.assertIs(browsers[1], browsers[2])

    def fake_playwright(self, failures):
        """A Playwright whose Chromium dies starting up `failures` times, as
        the runtime's did once (SIGSEGV: 'Target page, context or browser
        has been closed')."""
        calls = []
        def launch():
            calls.append(1)
            if len(calls) <= failures:
                raise RuntimeError('BrowserType.launch: Target page, context or browser has been closed')
            return 'browser'
        return type('PW', (), {'chromium': type('Chromium', (), {'launch': staticmethod(launch)})()})(), calls

    def test_a_browser_that_dies_starting_up_is_launched_again(self):
        with patch.object(VERIFY_MODULE, 'LAUNCH_RETRY_SECONDS', 0):
            pw, calls = self.fake_playwright(2)
            self.assertEqual(VERIFY_MODULE.launch_chromium(pw), 'browser')
            self.assertEqual(len(calls), 3)
            pw, calls = self.fake_playwright(3)
            with self.assertRaisesRegex(RuntimeError, 'has been closed'):
                VERIFY_MODULE.launch_chromium(pw)
            self.assertEqual(len(calls), VERIFY_MODULE.LAUNCH_ATTEMPTS)

    def test_workers_start_their_browsers_through_the_retry(self):
        launched = []
        real = VERIFY_MODULE.launch_chromium
        def counted(pw):
            launched.append(1)
            return real(pw)
        with patch.object(VERIFY_MODULE, 'launch_chromium', counted):
            self.assertEqual(VERIFY_MODULE.pool_map(lambda browser, item: item, range(4), 2), [0, 1, 2, 3])
        self.assertEqual(len(launched), 2)


class EditorReadingTest(unittest.TestCase):
    """The editor phase's own reading of stored blocks, against a stand-in
    block API (the live check: tools/gutenberg-serialization-live-test.py)."""

    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def page(self, api):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.evaluate('api => { window.wp = (new Function("return " + api))(); }', api)
        return page

    def test_an_unregistered_block_is_unknown_though_core_missing_is_registered(self):
        page = self.page('{blocks:{getBlockType:n=>["core/missing","core/paragraph"].includes(n)?{}:undefined}}')
        result = VERIFY_MODULE.inspect_tree(page, '[{name:"core/paragraph",attributes:{},innerBlocks:[{name:"core/missing",attributes:{originalName:"acme/thing"},innerBlocks:[]}]},{name:"acme/raw",attributes:{}}]')
        self.assertEqual(result, {'count': 3, 'invalid': [], 'unknown': ['acme/thing', 'acme/raw']})

    def test_stored_blocks_compare_with_what_the_editor_writes_back(self):
        # The stand-in editor reads `id` (appended last by the importer) and a
        # URL written with `\/`; it writes the same attributes in its own order.
        stored = ('<!-- wp:image {"className":"h2wp-source-image","id":5} -->\n<figure class="wp-block-image"><img src="x" class="wp-image-5"/></figure>\n<!-- /wp:image -->\n\n'
                  '<!-- wp:h2wp/element {"tagName":"a","htmlAttributes":{"href":"http:\\/\\/localhost\\/"}} /-->')
        saved = ('<!-- wp:image {"id":5,"className":"h2wp-source-image"} -->\n<figure class="wp-block-image"><img src="x" class="wp-image-5"/></figure>\n<!-- /wp:image -->\n\n'
                 '<!-- wp:h2wp/element {"tagName":"a","htmlAttributes":{"href":"http://localhost/"}} /-->')
        image = {'name': 'core/image', 'attributes': {'id': 5, 'className': 'h2wp-source-image'}, 'innerBlocks': []}
        link = {'name': 'h2wp/element', 'attributes': {'tagName': 'a', 'htmlAttributes': {'href': 'http://localhost/'}}, 'innerBlocks': []}
        raw = [{'blockName': 'core/image', 'attrs': {'className': 'h2wp-source-image', 'id': 5}, 'innerBlocks': [], 'innerHTML': ''},
               {'blockName': None, 'attrs': {}, 'innerBlocks': [], 'innerHTML': '\n\n'},
               {'blockName': 'h2wp/element', 'attrs': {'tagName': 'a', 'htmlAttributes': {'href': 'http://localhost/'}}, 'innerBlocks': [], 'innerHTML': ''}]
        def api(parsed, written, reread=None):
            return ('{blocks:{parse:s=>JSON.parse(JSON.stringify(s===' + json.dumps(written) + '?' + json.dumps(reread or parsed) + ':' + json.dumps(parsed) + ')),'
                    'serialize:()=>' + json.dumps(written) + '},blockSerializationDefaultParser:{parse:()=>' + json.dumps(raw) + '}}')
        clean = VERIFY_MODULE.serialization_row(self.page(api([image, link], saved)), stored, kind='page', id=7)
        self.assertEqual((clean['kind'], clean['id'], clean['blocks'], clean['byteIdentical'], clean['attributeLoss'], clean['passed']), ('page', 7, 2, True, [], True))
        # The editor writes other markup (a deprecated save migrated): refused, with where.
        rewritten = VERIFY_MODULE.serialization_row(self.page(api([image, link], saved.replace('<figure class="wp-block-image">', '<figure class="wp-block-image size-full">'))), stored)
        self.assertFalse(rewritten['passed'])
        self.assertIn('size-full', rewritten['firstDifference']['saved'])
        # A stored attribute the editor does not read (undeclared, dropped).
        lost = VERIFY_MODULE.serialization_row(self.page(api([{**image, 'attributes': {'className': 'h2wp-source-image'}}, link], saved)), stored)
        self.assertEqual([(l['phase'], l['key'], l['stored']) for l in lost['attributeLoss']], [('parse', 'id', 5)])
        self.assertFalse(lost['passed'])
        # One that changes between the editor's reading and a reading of what it saves.
        drift = VERIFY_MODULE.serialization_row(self.page(api([image, link], saved, [image, {**link, 'attributes': {'tagName': 'a'}}])), stored)
        self.assertEqual([(l['phase'], l['key']) for l in drift['attributeLoss']], [('save', 'htmlAttributes')])


def serve(root):
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Quiet, directory=str(root)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f'http://127.0.0.1:{httpd.server_port}'


class CaptureTest(unittest.TestCase):
    """The frontend capture: reveals at rest before the scroll-through, and
    instant scrolling."""

    def test_reveals_are_at_rest_and_scrolling_is_instant(self):
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory() as root:
            # A fade that only a script would start: none runs here, so a
            # capture that waits for the scroll-through leaves it invisible.
            (Path(root) / 'index.html').write_text('<!doctype html><style>html{scroll-behavior:smooth}body{margin:0}'
                '.reveal{height:300px;background:#c30;opacity:0;transition:opacity 2s}.reveal.in{opacity:1}.tall{height:3000px}</style>'
                '<div class="reveal"></div><div class="tall"></div>')
            httpd, origin = serve(Path(root))
            self.addCleanup(lambda: (httpd.shutdown(), httpd.server_close()))
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                page = browser.new_page(viewport={'width': 400, 'height': 600})
                shot = Image.open(io.BytesIO(VERIFY_MODULE.capture(page, origin + '/'))).convert('RGB')
                self.assertEqual(shot.getpixel((200, 150)), (204, 51, 0))
                self.assertEqual(page.evaluate('getComputedStyle(document.documentElement).scrollBehavior'), 'auto')
                browser.close()


class VisualScopeTest(unittest.TestCase):
    """The visual phase against two static stand-ins: the source site and a
    'WordPress' that serves the same pages at their routes. No editor:
    --skip-editor."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='h2wp-visual-')
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.source, self.site, self.ws = root / 'source', root / 'site', root / 'ws'
        page = ('<!doctype html><html><head><style>body{margin:0;font:18px/1.5 sans-serif}.band{height:%dpx;background:%s}'
                '@media (max-width:500px){.band{background:#963}}</style></head><body><h1>%s</h1><div class="band"></div>'
                '<img src="/pic.png" alt="pic" width="40" height="40"></body></html>')
        for tree, colour in ((self.source, '#369'), (self.site, '#369')):
            (tree / 'about').mkdir(parents=True)
            Image.new('RGB', (40, 40), 'orange').save(tree / 'pic.png')
            (tree / 'index.html').write_text(page % (900, colour, 'Home'))
        (self.source / 'about.html').write_text(page % (1300, '#393', 'About'))
        (self.site / 'about' / 'index.html').write_text(page % (1300, '#393', 'About'))
        self.ws.mkdir()
        (self.ws / 'routes.json').write_text(json.dumps([{'source': '/index.html', 'target': '/'}, {'source': '/about.html', 'target': '/about/'}]))
        self.servers = [serve(self.source), serve(self.site)]
        self.addCleanup(lambda: [(httpd.shutdown(), httpd.server_close()) for httpd, _ in self.servers])

    def verify(self, *extra, out='gutenberg-verification.json', env=None):
        out = self.ws / out
        run = subprocess.run([sys.executable, str(VERIFY), '--site=' + self.servers[1][1], '--source=' + self.servers[0][1],
                              '--routes=' + str(self.ws / 'routes.json'), '--password=x', '--theme-slug=t', '--skip-editor',
                              '--out=' + str(out), *extra], capture_output=True, text=True, timeout=600, env=env)
        self.assertTrue(out.is_file(), run.stdout + run.stderr)
        return json.loads(out.read_text()), run

    def test_smoke_captures_1440_only_and_full_all_three(self):
        report, _ = self.verify('--scope=smoke', '--workers=2', out='smoke/gutenberg-smoke.json')
        self.assertEqual(report['scope'], 'smoke')
        self.assertEqual(sorted((r['path'], r['width']) for r in report['visual']), [('/', 1440), ('/about/', 1440)])
        self.assertTrue(all(r['diff'] == 0 and r['passed'] for r in report['visual']), report['visual'])
        shots = sorted(p.name for p in (self.ws / 'smoke/screenshots').iterdir())
        self.assertEqual(shots, ['1440-about-source.png', '1440-about-wp.png', '1440-home-source.png', '1440-home-wp.png', 'captures.json'])
        # Not evidence: no import, no editor. The phase itself is what ran.
        self.assertFalse(report['passed'])
        # Never signed in to wp-admin: the WordPress version is recorded as unknown.
        self.assertEqual(report['wordpress'], {'version': None})
        report, _ = self.verify()
        self.assertEqual(report['scope'], 'full')
        self.assertEqual(sorted((r['path'], r['width']) for r in report['visual']),
                         sorted((p, w) for p in ('/', '/about/') for w in (1440, 820, 390)))
        self.assertEqual(len(list((self.ws / 'screenshots').glob('*.png'))), 12)

    def test_the_capture_index_names_every_pair_and_goes_with_the_next_run(self):
        # screenshots/captures.json (compare-pages.py --from-captures): each
        # visual row's two PNGs by name and sha256, written once the phase
        # finished; the next run removes it first, so one that stops before
        # its visual phase has finished leaves none.
        report, _ = self.verify('--scope=smoke')
        shots = self.ws / 'screenshots'
        index = json.loads((shots / 'captures.json').read_text())
        self.assertEqual((index['schema'], index['site'], index['source'], index['scope']),
                         ('h2wp-captures/1', self.servers[1][1], self.servers[0][1], 'smoke'))
        self.assertEqual([(p['source'], p['target'], p['width']) for p in index['pairs']],
                         [('/index.html', '/', 1440), ('/about.html', '/about/', 1440)])
        for pair, row in zip(index['pairs'], report['visual']):
            self.assertEqual((str(shots / pair['sourcePng']), str(shots / pair['wpPng'])), (row['sourceScreenshot'], row['wpScreenshot']))
            self.assertEqual(pair['sha256'], {side: hashlib.sha256((shots / pair[side + 'Png']).read_bytes()).hexdigest() for side in ('source', 'wp')})
        (self.ws / 'routes.json').write_text('[]')
        stopped, _ = self.verify('--scope=smoke')
        self.assertIn('No visual routes', stopped['error'])
        self.assertFalse((shots / 'captures.json').exists())

    def test_source_captures_are_reused_until_the_source_changes(self):
        # --source-dir: the second run takes every source capture from the
        # cache, and measures exactly what the first run measured.
        first, _ = self.verify('--source-dir=' + str(self.source), '--scope=smoke')
        self.assertEqual(first['sourceCache']['misses'], 2)
        self.assertEqual({r['sourceCapture'] for r in first['visual']}, {'fresh'})
        shots = {p.name: p.read_bytes() for p in (self.ws / 'screenshots').iterdir()}
        second, _ = self.verify('--source-dir=' + str(self.source), '--scope=smoke')
        self.assertEqual((second['sourceCache']['hits'], second['sourceCache']['misses']), (2, 0))
        self.assertEqual([(r['path'], r['diff'], r['passed']) for r in second['visual']], [(r['path'], r['diff'], r['passed']) for r in first['visual']])
        self.assertEqual({p.name: p.read_bytes() for p in (self.ws / 'screenshots').iterdir() if p.name.endswith('-source.png')},
                         {k: v for k, v in shots.items() if k.endswith('-source.png')})
        # Another width is another entry.
        full, _ = self.verify('--source-dir=' + str(self.source))
        self.assertEqual((full['sourceCache']['hits'], full['sourceCache']['misses']), (2, 4))
        # Any source file changes: every entry is stale, and the old ones go.
        (self.source / 'pic.png').write_bytes((self.source / 'pic.png').read_bytes() + b'\0')
        (self.site / 'pic.png').write_bytes((self.source / 'pic.png').read_bytes())
        changed, _ = self.verify('--source-dir=' + str(self.source), '--scope=smoke')
        self.assertEqual((changed['sourceCache']['hits'], changed['sourceCache']['misses']), (0, 2))
        self.assertEqual(len([d for d in (self.ws / '.h2wp-capture-cache').iterdir()]), 1)
        self.assertNotEqual(changed['sourceCache']['sourceDigest'], first['sourceCache']['sourceDigest'])

    def test_no_cache_when_source_does_not_serve_source_dir(self):
        other = Path(self.temp.name) / 'other'
        shutil.copytree(self.source, other)
        (other / 'about.html').write_text((other / 'about.html').read_text().replace('About', 'Other'))
        report, _ = self.verify('--source-dir=' + str(other), '--scope=smoke')
        self.assertIn('/about.html differs', report['sourceCache']['disabled'])
        self.assertNotIn('sourceCapture', report['visual'][0])
        self.assertFalse((self.ws / '.h2wp-capture-cache').exists())

    def test_workers_must_be_positive(self):
        run = subprocess.run([sys.executable, str(VERIFY), '--site=http://localhost:1', '--password=x', '--theme-slug=t',
                              '--out=' + str(self.ws / 'x.json'), '--workers=0'], capture_output=True, text=True)
        self.assertEqual(run.returncode, 2)
        self.assertIn('--workers', run.stderr)


class WordPressVersionTest(unittest.TestCase):
    """The report records the WordPress version the admin screen names
    (its `version-7-1-2` body class), and null when it cannot tell."""

    def test_the_admin_body_class_names_the_version(self):
        from playwright.sync_api import sync_playwright
        spec = importlib.util.spec_from_file_location('h2wp_verify_under_test', VERIFY)
        verify = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(verify)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            for body, version in (('wp-admin wp-core-ui js is-fullscreen-mode branch-7-1 version-7-1-2 admin-color-modern', '7.1.2'),
                                  ('wp-admin branch-7 version-7-0-2 locale-en-us', '7.0.2'),
                                  ('wp-admin version-7-2', '7.2'),
                                  ('wp-admin my-version-7-1-2 version-x', None), ('', None)):
                page.set_content(f'<body class="{body}"></body>')
                self.assertEqual(verify.wordpress_version(page), version, body)
            browser.close()

        class Gone:
            def wait_for_load_state(self, *a):
                raise RuntimeError('Target page, context or browser has been closed')
        self.assertIsNone(verify.wordpress_version(Gone()))


if __name__ == '__main__':
    unittest.main()
