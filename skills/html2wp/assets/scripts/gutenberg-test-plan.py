#!/usr/bin/env python3
"""Behavioral checks for isolated proposals, stale reviews and exact upload scope."""
import importlib.util
import json
import os
import tarfile
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('gutenberg-plan.py')
spec = importlib.util.spec_from_file_location('planner', SCRIPT)
planner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(planner)


class PlanTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp.name)
        self.dist = self.ws / 'astro-project/dist'
        self.dist.mkdir(parents=True)
        (self.dist / 'index.html').write_text('<html><head><title>Home</title></head><body><main class="content"><h1>Hello <em>world</em></h1><p>See <a href="/about/">about</a>.</p><div style="color:red"><a href="/about/" class="nav">About</a><svg viewBox="0 0 10 10"><path d="M0 0" /></svg></div></main></body></html>')
        (self.dist / 'about').mkdir()
        (self.dist / 'about/index.html').write_text('<body><main class="content"><h1>About</h1></main></body>')
        self.manifest = self.ws / 'conversion-manifest.json'
        planner.write(self.manifest, {'pages': [{'key': 'home', 'file': 'index.html'}, {'key': 'about', 'file': 'about/index.html'}]})

    def tearDown(self):
        self.temp.cleanup()

    def run_cli(self, command='prepare', *extra, ok=True):
        result = subprocess.run(['python3', str(SCRIPT), command, '--manifest=' + str(self.manifest), *extra], text=True, capture_output=True)
        self.assertEqual(result.returncode == 0, ok, result.stderr + result.stdout)
        return json.loads(result.stdout) if result.stdout else result.stderr

    def review(self):
        self.run_cli('claim', '--task=family-1', '--owner=test')
        self.run_cli('complete', '--task=family-1', '--owner=test')

    def test_round_trip_tokens_and_styles(self):
        self.run_cli()
        manifest = planner.read(self.manifest)
        self.assertEqual(manifest['target'], 'gutenberg')
        home = planner.read(self.ws / 'block-plan/pages/home.json')
        data = json.dumps(home)
        self.assertIn('page:about', data)
        self.assertIn('viewBox', data)
        self.assertIn('h2wp-inline-', data)
        self.assertEqual(planner.read(self.ws / '.gutenberg/inventory.json')['pages'][0]['findings'], [])
        self.run_cli('finalize', ok=False)
        self.review()
        self.run_cli('finalize')
        self.assertIn('color:red', (self.dist / 'assets/gutenberg-inline.css').read_text())

    def test_stale_source_and_resume_preserves_worker_output(self):
        self.run_cli()
        self.review()
        path = self.ws / 'block-plan/pages/home.json'
        original = path.read_bytes()
        (self.dist / 'index.html').write_text('<body><h1>Changed</h1></body>')
        report = self.run_cli('prepare', ok=False)
        self.assertTrue(any('stale source' in f for f in report['findings']))
        self.assertEqual(path.read_bytes(), original)
        self.run_cli('freeze', ok=False)

    def test_output_changes_and_duplicate_coverage(self):
        self.run_cli()
        self.review()
        path = self.ws / 'block-plan/pages/home.json'
        proposal = planner.read(path)
        proposal['blocks'][0]['sourceIds'].append('source-0001')
        planner.write(path, proposal)
        report = self.run_cli('finalize', ok=False)
        self.assertTrue(any('duplicate' in f for f in report['findings']))
        self.assertTrue(any('after worker checkpoint' in f for f in report['findings']))

    def test_shared_style_change_invalidates_reviews(self):
        self.run_cli()
        self.review()
        (self.dist / 'assets/gutenberg-inline.css').write_text('body{color:blue}')
        self.run_cli('check', ok=False)
        self.run_cli('freeze')
        self.run_cli('check', ok=False)
        self.review()
        self.run_cli('check')

    def test_unsupported_markup_never_silently_passes(self):
        (self.dist / 'index.html').write_text('<body><iframe src="https://example.com"></iframe><script>hydrate()</script><p onclick="bad()">Hello</p></body>')
        self.run_cli()
        self.review()
        report = self.run_cli('check', ok=False)
        self.assertTrue(any('unresolved' in f for f in report['findings']))
        self.assertTrue(any('coverage' in f for f in report['findings']))

    def test_unrecorded_disclosure_and_mutated_style_are_blocking(self):
        (self.dist / 'index.html').write_text('<body><button aria-expanded="false">Buy a gift</button><div role="button" aria-expanded="true">Open</div><div data-spa-panel="t1" style="display:none">Answer</div></body>')
        self.run_cli()
        inventory=planner.read(self.ws / '.gutenberg/inventory.json')
        codes=[f['code'] for f in inventory['pages'][0]['findings']]
        self.assertEqual(codes.count('unrecorded-disclosure'),2)
        self.assertIn('runtime-inline-style',codes)
        self.review()
        report=self.run_cli('finalize',ok=False)
        self.assertTrue(any('unrecorded-disclosure' in f for f in report['findings']))

    def test_path_traversal_and_symlink_rejected(self):
        manifest = planner.read(self.manifest)
        manifest['pages'][0]['key'] = '../secret'
        planner.write(self.manifest, manifest)
        self.run_cli(ok=False)

    def test_client_packages_only_reviewed_manifest_page_files(self):
        self.run_cli()
        self.review()
        (self.ws / 'astro-report.json').write_text('{}')
        (self.ws / 'block-plan/private-notes.json').write_text('{"private":"must remain local"}')
        (self.ws / 'block-plan/pages/unlisted.json').write_text('{}')
        bin_dir = self.ws / 'fake-bin'
        bin_dir.mkdir()
        archive = self.ws / 'captured.tar.gz'
        (bin_dir / 'curl').write_text('#!/usr/bin/env python3\nimport sys,json,shutil\nargs=sys.argv[1:]\nmethod=args[args.index("-X")+1]\nif method=="POST":\n payload={"job":"test","token":"test","upload":{"url":"http://127.0.0.1:1/upload"}};code="201"\nelse:\n shutil.copyfile(args[args.index("--data-binary")+1][1:], ' + repr(str(archive)) + ')\n payload={"error":"local test ends after packaging","reason":"test"};code="400"\nopen(args[args.index("-o")+1],"w").write(json.dumps(payload))\nprint(code,end="")\n')
        for path in bin_dir.iterdir():
            path.chmod(0o755)
        env = dict(os.environ, PATH=str(bin_dir) + os.pathsep + os.environ['PATH'])
        client = SCRIPT.with_name('convert-remote.sh')
        result = subprocess.run(['bash', str(client), str(self.ws), '--api=http://127.0.0.1:1', '--key=local-test'], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        with tarfile.open(archive) as tar:
            members = tar.getnames()
        self.assertIn('block-plan/contract.json', members)
        self.assertIn('block-plan/pages/home.json', members)
        self.assertNotIn('block-plan/private-notes.json', members)
        self.assertNotIn('block-plan/pages/unlisted.json', members)
        self.assertFalse(any(name.startswith('.gutenberg') for name in members))
        # Old manifests continue to exclude the entire block plan.
        manifest = planner.read(self.manifest)
        manifest.pop('schema'); manifest.pop('target')
        planner.write(self.manifest, manifest)
        subprocess.run(['bash', str(client), str(self.ws), '--api=http://127.0.0.1:1', '--key=local-test'], env=env, capture_output=True, text=True)
        with tarfile.open(archive) as tar:
            self.assertFalse(any(name.startswith('block-plan') for name in tar.getnames()))

    def plan(self, pages, css=None):
        """Write pages {file: html}, run prepare, return (contract, proposals, findings by key)."""
        for name in ('index.html', 'about/index.html'):
            (self.dist / name).unlink()
        for name, source in pages.items():
            (self.dist / name).parent.mkdir(parents=True, exist_ok=True)
            (self.dist / name).write_text(source)
        if css is not None:
            (self.dist / 'assets').mkdir(exist_ok=True)
            (self.dist / 'assets/app.css').write_text(css)
        keys = [name.split('/')[0].replace('.html', '') for name in pages]
        planner.write(self.manifest, {'pages': [{'key': key, 'file': name} for key, name in zip(keys, pages)]})
        self.run_cli()
        contract = planner.read(self.ws / 'block-plan/contract.json')
        proposals = {key: planner.read(self.ws / 'block-plan/pages' / (key + '.json')) for key in keys}
        findings = {p['key']: p['findings'] for p in planner.read(self.ws / '.gutenberg/inventory.json')['pages']}
        return contract, proposals, findings

    def app(self, header='<a href="/">Brand</a>', body='<section class="hero"><h1>Train Swim Win</h1></section>'):
        return ('<html><body><div class="min-h-screen bg-background"><header class="top">' + header + '</header>'
                '<main class="flex-1">' + body + '<section class="cta"><p>Book</p></section></main>'
                '<footer class="foot"><p>© Novak</p></footer></div>'
                '<section aria-label="Notifications" tabindex="-1"></section></body></html>')

    def test_frame_unwrap_auto_parts_and_chrome_variant(self):
        contract, proposals, findings = self.plan({'index.html': self.app(), 'about.html': self.app(header='<a href="/">Brand</a><a href="#sale">Sale</a>'), 'plain.html': '<body><section><h2>Loose</h2></section><section><p>Two</p></section></body>'})
        self.assertEqual(contract['schema'], 'h2wp-blocks/2')
        self.assertEqual(contract['frame'], {'wrapper': {'tagName': 'div', 'className': 'min-h-screen bg-background'}, 'main': {'tagName': 'main', 'className': 'flex-1'}})
        self.assertEqual(contract['parts']['header'][0]['attributes']['tagName'], 'header')
        self.assertEqual(contract['parts']['footer'][0]['attributes']['tagName'], 'footer')
        blocks = proposals['index']['blocks']
        self.assertEqual([b['name'] for b in blocks], ['core/template-part', 'core/group', 'core/group', 'core/template-part', 'h2wp/element'])
        self.assertEqual([b['attributes'].get('slug') for b in blocks], ['header', None, None, 'footer', None])
        self.assertEqual(blocks[1]['attributes']['metadata'], {'name': 'Train Swim Win'})
        self.assertEqual(blocks[4]['attributes']['htmlAttributes']['aria-label'], 'Notifications')  # sibling stays a section
        self.assertEqual([b['sourceIds'] for b in blocks], [['source-000' + str(i)] for i in range(1, 6)])
        self.assertEqual([f['code'] for f in findings['index']], [])
        self.assertEqual([f['code'] for f in findings['about']], ['chrome-variant'])
        self.assertEqual(findings['about'][0]['section'], 'source-0001')
        self.assertEqual(sorted(f['code'] for f in findings['plain']), ['frame-variant'])
        # Wrapper-less pages keep today's top-level mapping.
        self.assertEqual([b['attributes']['tagName'] for b in proposals['plain']['blocks']], ['section', 'section'])

    def test_active_nav_state_is_not_a_chrome_variant(self):
        def nav(active, scroll):
            links = []
            for name in ('index', 'about', 'team'):
                target = 'team/index.html' if name == 'team' else name + '.html'
                href = os.path.relpath(target, 'team') if active == 'team' else target
                if name == active:
                    links.append('<a class="link text-primary active" href="' + href + '" data-status="active" aria-current="page">' + name + '</a>')
                else:
                    links.append('<a href="' + href + '" class="link text-muted-foreground">' + name + '</a>')
            return '<div data-spa-scroll=\'{"y":' + scroll + '}\'>' + ''.join(links) + '</div>'
        pages = {'index.html': self.app(header=nav('index', '81')), 'about.html': self.app(header=nav('about', '1')),
                 'team/index.html': self.app(header=nav('team', '1'))}
        # Class-only differences on an unmarked link count when they are not a symmetric swap.
        pages['odd.html'] = self.app(header=nav('index', '81').replace('class="link text-muted-foreground">about', 'class="link text-danger">about'))
        contract, proposals, findings = self.plan(pages)
        codes = {key: [f['code'] for f in items] for key, items in findings.items()}
        self.assertEqual(codes, {'index': ['chrome-active-state'], 'about': [], 'team': [], 'odd': ['chrome-variant']})
        self.assertEqual(findings['index'][0]['detail'], "Shared header keeps the first page's link classes; per-page active styling needs core/navigation or CSS")
        header = json.dumps(contract['parts']['header'])
        self.assertNotIn('aria-current', header)
        self.assertNotIn('data-status', header)
        self.assertIn('link text-primary active', header)  # first page's classes are kept
        self.assertEqual(planner.chrome_equivalent(
            planner.Parser('<a class="x on">A</a><a class="x off">B</a>').root, planner.Parser('<a class="x off">A</a><a class="x on">B</a>').root), True)

    def test_plain_static_frame_and_nav_fallback(self):
        contract, proposals, findings = self.plan({
            'index.html': '<body><header class="h"><a href="/">Brand</a></header><main><section><h1>Hi</h1></section></main><footer><p>F</p></footer></body>',
            'nav.html': '<body><nav class="n"><a href="/">Brand</a></nav><main><section><h1>Nav</h1></section></main><footer><p>F</p></footer></body>'})
        self.assertEqual(contract['frame'], {'wrapper': None, 'main': {'tagName': 'main'}})
        self.assertEqual(sorted(contract['parts']), ['footer', 'header'])
        self.assertEqual([b['attributes'].get('slug') for b in proposals['index']['blocks']], ['header', None, 'footer'])
        # A top-level <nav> stands in for a missing <header>.
        self.assertEqual([b['attributes'].get('slug') for b in proposals['nav']['blocks']], ['header', None, 'footer'])
        self.assertEqual([f['code'] for f in findings['index']], [])
        self.assertEqual([f['code'] for f in findings['nav']], ['chrome-variant'])

    def test_rich_text_lists_and_mixed_content(self):
        _, proposals, _ = self.plan({'index.html': '<body>'
            '<h1 class="big">Train <span class="font-script text-soft" style="opacity:1">Swim</span> <a href="/x" class="u">Win</a></h1>'
            '<ul class="list"><li class="i">One <strong>1</strong></li><li>Two<ol start="3"><li>Nested</li></ol></li></ul>'
            '<div class="mixed">Hello <strong>bold</strong><div>block</div></div>'
            '<a href="#top" class="btn">Top</a><div class="kicker">01</div>'
            '<ul><li><div>block item</div></li></ul>'
            '<blockquote><p>Quote</p><cite>Ann</cite></blockquote>'
            '<pre class="p"><code class="language-js">let a = 1 &lt; 2;\n</code></pre><pre>plain  text</pre></body>'})
        heading, lst, mixed, link, kicker, blocky, quote, code, pre = proposals['index']['blocks']
        self.assertEqual(heading['name'], 'core/heading')
        self.assertRegex(heading['attributes']['content'], r'^Train <span class="font-script text-soft h2wp-inline-[0-9a-f]{16}">Swim</span> <a href="/x" class="u">Win</a>$')
        self.assertEqual(lst['name'], 'core/list')
        self.assertEqual([i['attributes']['content'] for i in lst['innerBlocks']], ['One <strong>1</strong>', 'Two'])
        self.assertEqual(lst['innerBlocks'][0]['attributes']['className'], 'i')
        nested = lst['innerBlocks'][1]['innerBlocks'][0]
        self.assertEqual((nested['name'], nested['attributes']['ordered'], nested['attributes']['start']), ('core/list', True, 3))
        # Mixed element/text content keeps per-node order; text is never merged.
        self.assertEqual(mixed['name'], 'core/group')  # plain wrapper (spec 2 C)
        self.assertNotIn('text', mixed['attributes'])
        self.assertEqual([(b['attributes']['tagName'], b['attributes'].get('text')) for b in mixed['innerBlocks']], [('span', 'Hello '), ('strong', 'bold'), ('div', 'block')])
        self.assertEqual((link['attributes']['text'], link['attributes']['htmlAttributes']['href'], 'innerBlocks' in link), ('Top', '#top', False))
        self.assertEqual((kicker['attributes']['tagName'], kicker['attributes']['text']), ('div', '01'))
        self.assertEqual((blocky['name'], blocky['innerBlocks'][0]['attributes']['tagName']), ('h2wp/element', 'li'))
        self.assertEqual((quote['name'], quote['attributes']['citation'], quote['innerBlocks'][0]['attributes']['content']), ('core/quote', 'Ann', 'Quote'))
        self.assertEqual((code['name'], code['attributes']['content'], code['attributes']['className']), ('core/code', 'let a = 1 &lt; 2;\n', 'p'))
        self.assertEqual((pre['name'], pre['attributes']['content']), ('core/preformatted', 'plain  text'))

    def test_empty_inline_markup_is_not_rich_text(self):
        # The editor strips empty formats; a decorative dot would vanish on save.
        _, proposals, _ = self.plan({'index.html': '<body><ul class="l"><li class="f"><span class="dot"></span>Groups</li></ul>'
            '<p>Before <span class="icon"></span></p><h2>Kept <strong>bold</strong></h2></body>'})
        lst, para, heading = proposals['index']['blocks']
        self.assertEqual(lst['name'], 'h2wp/element')
        self.assertEqual([b['attributes']['tagName'] for b in lst['innerBlocks'][0]['innerBlocks']], ['span', 'span'])
        self.assertEqual(para['name'], 'h2wp/element')
        self.assertEqual((heading['name'], heading['attributes']['content']), ('core/heading', 'Kept <strong>bold</strong>'))

    def test_layout_list_items_stay_elements(self):
        # A flex/grid list item keeps its element tree: the editor would wrap its text.
        _, proposals, _ = self.plan({'index.html': '<body><ul><li class="flex items-center gap-3">A <b>b</b></li></ul><ul><li class="md:grid">B</li></ul><ul><li class="flexible">C</li></ul></body>'})
        self.assertEqual([b['name'] for b in proposals['index']['blocks']], ['h2wp/element', 'h2wp/element', 'core/list'])

    def test_runtime_panel_inline_style_is_scoped_to_closed_state(self):
        _, proposals, _ = self.plan({'index.html': '<body><button data-spa-toggle="t1" aria-label="Open">x</button>'
            '<div class="panel" data-spa-panel="t1" hidden style="display:none"><a href="/">A</a></div>'
            '<div class="plain" style="display:none">B</div></body>'})
        css = (self.dist / 'assets/gutenberg-inline.css').read_text()
        self.assertRegex(css, r'\.h2wp-inline-[0-9a-f]{16}\[hidden\]\{display:none\}')
        self.assertRegex(css, r'\.h2wp-inline-[0-9a-f]{16}\{display:none\}')

    def test_empty_wrappers_stay_elements(self):
        _, proposals, _ = self.plan({'index.html': self.app(body='<section class="s"><div class="mx-auto h-[40px] w-px bg-soft"></div><p>T</p></section>')})
        section = proposals['index']['blocks'][1]
        self.assertEqual(section['name'], 'core/group')
        self.assertEqual([b['name'] for b in section['innerBlocks']], ['h2wp/element', 'core/paragraph'])

    def test_rich_text_collapses_source_whitespace_except_pre(self):
        _, proposals, _ = self.plan({'index.html': self.app(body='<p class="x">Every   program\n      is built</p><pre>a\n   b</pre>')})
        para, pre = proposals['index']['blocks'][1]['innerBlocks'][:2] if proposals['index']['blocks'][1].get('innerBlocks') else proposals['index']['blocks'][1:3]
        self.assertEqual(para['attributes']['content'], 'Every program is built')
        self.assertEqual(pre['attributes']['content'], 'a\n   b')

    def test_capitalized_brand_binds_site_title_uppercase(self):
        leaf = {'name': 'h2wp/element', 'attributes': {'tagName': 'a', 'className': 'font-display', 'text': 'NOVAK SWIM', 'htmlAttributes': {'href': 'page:home'}}}
        styles = planner.site_title([{'name': 'h2wp/element', 'attributes': {'tagName': 'header'}, 'innerBlocks': [leaf]}], 'home', 'Novak Swim')
        self.assertEqual(leaf['attributes']['bind'], 'siteTitle')
        klass = leaf['attributes']['className'].split()[-1]
        self.assertEqual(styles, {klass: 'text-transform:uppercase'})
        other = {'name': 'h2wp/element', 'attributes': {'tagName': 'a', 'text': 'Other Brand', 'htmlAttributes': {'href': 'page:home'}}}
        self.assertEqual(planner.site_title([other], 'home', 'Novak Swim'), {})
        self.assertNotIn('bind', other['attributes'])

    def test_design_tokens_from_tailwind_v4_css(self):
        css = ('/*! tailwindcss v4 */@layer theme{:root,:host{--color-brand:var(--brand);--color-ink:var(--ink, #111);'
               '--color-ghost:var(--missing);--color-bad:url(x.png);--font-display:"Caudex", serif;--font-weight-bold:700;'
               '--text-lg:1.125rem;--text-lg--line-height:calc(1.75 / 1.125);--text-fluid:clamp(1rem, 2vw, 3rem);--text-bad:var(--nope)}}'
               '.\\[stroke\\=\\\'\\#ccc\\\'\\]{stroke:red}'
               ':root{--brand:#FF0000;--line:oklch(92% .008 60)}.dark{--color-dark:#111}'
               '@media (prefers-color-scheme:dark){:root{--color-night:#000}}')
        page = '<html><head><link rel="stylesheet" href="/assets/app.css"></head><body><p>x</p></body></html>'
        contract, _, _ = self.plan({'index.html': page}, css)
        settings = contract['themeJson']['settings']
        self.assertEqual(contract['themeJson']['version'], 3)
        self.assertEqual(settings['color']['palette'], [{'slug': 'brand', 'name': 'Brand', 'color': '#ff0000'}, {'slug': 'ink', 'name': 'Ink', 'color': '#111'}, {'slug': 'line', 'name': 'Line', 'color': 'oklch(92% .008 60)'}])
        self.assertEqual(settings['typography']['fontFamilies'], [{'slug': 'display', 'name': 'Display', 'fontFamily': '"Caudex", serif'}])
        self.assertEqual(settings['typography']['fontSizes'], [{'slug': 'lg', 'name': 'Lg', 'size': '1.125rem', 'fluid': False}, {'slug': 'fluid', 'name': 'Fluid', 'size': 'clamp(1rem, 2vw, 3rem)', 'fluid': False}])
        self.assertNotIn('blockGap', json.dumps(contract['themeJson']))
        # Bridge: literal holders declared only in :root; var() fallbacks are not bridged.
        self.assertEqual(contract['tokenBridge'], [
            {'preset': 'color', 'slug': 'brand', 'variable': '--brand'}, {'preset': 'color', 'slug': 'line', 'variable': '--line'},
            {'preset': 'font-family', 'slug': 'display', 'variable': '--font-display'},
            {'preset': 'font-size', 'slug': 'lg', 'variable': '--text-lg'}, {'preset': 'font-size', 'slug': 'fluid', 'variable': '--text-fluid'}])
        self.assertEqual(contract['styleVariations'], [{'slug': 'dark', 'title': 'Dark', 'variables': {'--color-dark': '#111', '--color-night': '#000'}}])

    def test_media_and_table_mappings(self):
        (self.dist / 'assets').mkdir()
        (self.dist / 'assets/clip.mp4').write_bytes(b'0')
        (self.dist / 'assets/a.jpg').write_bytes(b'0')
        _, proposals, findings = self.plan({'index.html': '<body>'
            '<iframe class="yt" src="https://www.youtube.com/embed/dQw4w9WgXcQ?rel=0" width="640" height="480"></iframe>'
            '<iframe src="https://player.vimeo.com/video/12345"></iframe>'
            '<iframe src="https://maps.example.com/embed"></iframe>'
            '<video autoplay muted loop playsinline poster="/assets/a.jpg"><source src="/assets/clip.mp4" type="video/mp4"></video>'
            '<table class="t"><caption>Rates</caption><thead><tr><th scope="col">Plan</th><th>Price</th></tr></thead>'
            '<tbody><tr><td>Single</td><td colspan="2"><strong>$80</strong></td></tr></tbody></table>'
            '<table><tr><td><div>block</div></td></tr></table>'
            '<picture class="pic"><source srcset="/assets/a.webp" type="image/webp"><img src="/assets/a.jpg" alt="A"></picture></body>'})
        youtube, vimeo, video, table, kept, picture = proposals['index']['blocks']
        self.assertEqual(youtube['name'], 'core/embed')
        self.assertEqual({k: youtube['attributes'][k] for k in ('url', 'type', 'providerNameSlug', 'responsive', 'className')}, {'url': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ', 'type': 'video', 'providerNameSlug': 'youtube', 'responsive': True, 'className': 'wp-embed-aspect-4-3 wp-has-aspect-ratio yt'})
        self.assertEqual((vimeo['attributes']['url'], vimeo['attributes']['providerNameSlug']), ('https://vimeo.com/12345', 'vimeo'))
        self.assertEqual(video['name'], 'core/video')
        self.assertEqual({k: video['attributes'][k] for k in ('src', 'poster', 'autoplay', 'muted', 'loop', 'playsInline', 'controls')}, {'src': 'asset:assets/clip.mp4', 'poster': 'asset:assets/a.jpg', 'autoplay': True, 'muted': True, 'loop': True, 'playsInline': True, 'controls': False})
        self.assertEqual(table['name'], 'core/table')
        self.assertEqual(table['attributes']['caption'], 'Rates')
        self.assertEqual(table['attributes']['head'], [{'cells': [{'content': 'Plan', 'tag': 'th', 'scope': 'col'}, {'content': 'Price', 'tag': 'th'}]}])
        self.assertEqual(table['attributes']['body'], [{'cells': [{'content': 'Single', 'tag': 'td'}, {'content': '<strong>$80</strong>', 'tag': 'td', 'colspan': '2'}]}])
        self.assertEqual((kept['name'], kept['attributes']['tagName']), ('h2wp/element', 'table'))  # reviewed markup, never dropped
        self.assertEqual((picture['name'], picture['attributes']['url'], picture['attributes']['alt']), ('core/image', 'asset:assets/a.jpg', 'A'))
        codes = [f['code'] + ':' + f['detail'][:14] for f in findings['index']]
        self.assertIn('unmapped-element:iframe https:/', codes)
        self.assertIn('unmapped-element:table with non', codes)
        self.assertTrue(any(c.startswith('picture-sources') for c in codes))
        self.assertEqual(len(proposals['index']['sections']), 7)  # the unmapped iframe stays a visible coverage gap

    def test_plain_wrappers_become_groups(self):
        contract, proposals, _ = self.plan({'index.html': self.app(body='<section id="s" class="hero"><div class="wrap"><h2>T</h2><div class="x" data-k="1"><p>P</p></div>'
                                                                   '<div class="kicker">01</div><aside><span>a</span></aside><div></div></div></section>')})
        self.assertEqual(contract['frame']['wrapper']['tagName'], 'div')  # frame wrapper/main stay frame elements
        section = proposals['index']['blocks'][1]
        self.assertEqual((section['name'], section['attributes']['tagName'], section['attributes']['className'], section['attributes']['anchor'], section['attributes']['layout']),
                         ('core/group', 'section', 'hero', 's', {'type': 'default'}))
        wrap = section['innerBlocks'][0]
        self.assertEqual([(b['name'], b['attributes'].get('tagName')) for b in wrap['innerBlocks']],
                         [('core/heading', None), ('h2wp/element', 'div'), ('h2wp/element', 'div'), ('core/group', 'aside'), ('h2wp/element', 'div')])  # empty wrappers stay elements
        self.assertEqual(wrap['innerBlocks'][1]['attributes']['htmlAttributes'], {'data-k': '1'})  # attributes keep the element
        self.assertEqual(wrap['innerBlocks'][2]['attributes']['text'], '01')  # text leaves keep the element

    def test_svg_icons(self):
        icon = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" class="lucide lucide-menu" aria-hidden="true">'
                '<path d="M4 6h16"/><g stroke-width="2"><circle cx="12" cy="12" r="3"></circle></g></svg>')
        _, proposals, findings = self.plan({'index.html': '<body><button type="button" class="b">' + icon + '</button>'
                                            '<svg viewBox="0 0 8 8"><title>Logo</title><path d="M0 0"/></svg>'
                                            '<svg viewBox="0 0 8 8"><path mask-type="alpha" d="M0 0"/></svg>'
                                            '<svg viewBox="0 0 8 8"><path fill="url(#g)" d="M0 0"/></svg>'
                                            '<svg viewBox="0 0 8 8"><path fill-rule="evenodd" transform="rotate(45)" d="M0 0"/></svg></body>'})
        button, titled, ruled, painted, presentational = proposals['index']['blocks']
        block = button['innerBlocks'][0]
        self.assertEqual(block, {'name': 'h2wp/icon', 'attributes': {'className': 'lucide lucide-menu',
            'htmlAttributes': {'xmlns': 'http://www.w3.org/2000/svg', 'viewBox': '0 0 24 24', 'fill': 'none', 'stroke': 'currentColor', 'aria-hidden': 'true'},
            'nodes': [{'tagName': 'path', 'htmlAttributes': {'d': 'M4 6h16'}},
                      {'tagName': 'g', 'htmlAttributes': {'stroke-width': '2'}, 'children': [{'tagName': 'circle', 'htmlAttributes': {'cx': '12', 'cy': '12', 'r': '3'}}]}]}})
        # Text content, non-allowlisted attributes and url() keep the element tree.
        self.assertEqual((titled['name'], titled['attributes']['tagName']), ('h2wp/element', 'svg'))
        self.assertEqual((ruled['name'], ruled['attributes']['tagName']), ('h2wp/element', 'svg'))
        self.assertEqual((painted['name'], painted['attributes']['tagName']), ('h2wp/element', 'svg'))
        self.assertEqual(sorted(f['code'] for f in findings['index']), ['unmapped-attribute', 'unsafe-attribute'])
        self.assertEqual(presentational['attributes']['nodes'][0]['htmlAttributes'], {'fill-rule': 'evenodd', 'transform': 'rotate(45)', 'd': 'M0 0'})

    def test_attribute_mirror_matches_shared_allowlist(self):
        allowlist = SCRIPT.resolve().parents[5] / 'server/core/templates/gutenberg/element-allowlist.json'
        if not allowlist.is_file():
            self.skipTest('shared allowlist not in this checkout')
        data = json.loads(allowlist.read_text())
        self.assertEqual(planner.ELEMENT_ATTRIBUTES, {name.lower() for name in data['attributes']})
        self.assertEqual(planner.ELEMENT_TAGS, set(data['tags']))
        self.assertEqual(planner.SVG_TAGS, set(data['svgTags']))

    def test_block_styles_from_repeated_buttons(self):
        cta = '<a href="/" class="inline-flex rounded-full bg-deep px-9 py-3">Book now</a>'
        other = '<a href="/" class="border px-4 [&>svg]:size-4">Odd</a><a href="/" class="border px-4 [&>svg]:size-4">Odd</a>'
        contract, proposals, _ = self.plan({'index.html': '<body><p>Intro</p><section>' + cta + other + '<a href="/" class="px-2 text-sm">Text</a></section></body>',
                                            'about.html': '<body><p>Intro</p><section><div>' + cta + '</div><button class="rounded bg-x px-2">Once</button></section></body>'})
        self.assertEqual(contract['blockStyles'], [{'name': 'button-1', 'label': 'Button — Book now', 'classes': 'inline-flex rounded-full bg-deep px-9 py-3'}])
        links = [b['attributes']['className'] for b in proposals['index']['blocks'][1]['innerBlocks']]
        self.assertEqual(links, ['is-style-button-1', 'border px-4 [&>svg]:size-4', 'border px-4 [&>svg]:size-4', 'px-2 text-sm'])
        self.assertEqual(proposals['about']['blocks'][1]['innerBlocks'][0]['innerBlocks'][0]['attributes']['className'], 'is-style-button-1')

    def test_parse_date_formats(self):
        self.assertEqual(planner.parse_date('May 11, 2026'), ('2026-05-11 12:00:00', 'F j, Y'))
        self.assertEqual(planner.parse_date('Jul 4, 2026'), ('2026-07-04 12:00:00', 'M j, Y'))
        self.assertEqual(planner.parse_date('04 July 2026'), ('2026-07-04 12:00:00', 'd F Y'))
        self.assertEqual(planner.parse_date('2026-05-11'), ('2026-05-11 12:00:00', 'Y-m-d'))
        self.assertEqual(planner.parse_date('May 2026'), ('2026-05-01 12:00:00', 'F Y'))
        self.assertIsNone(planner.parse_date('7 min read'))

    def blog_site(self):
        posts = [('a', 'The catch is where freestyle is won', 'Technique', 'July 14, 2026', '6 min read', 'Catch excerpt that is long enough to read.'),
                 ('b', 'First month for an adult beginner', 'Adult Lessons', 'June 2, 2026', '5 min read', 'Beginner excerpt that is long enough too.'),
                 ('c', 'How to taper for a race', 'Competitive', 'May 11, 2026', '7 min read', 'Taper excerpt that is long enough as well.')]

        def page(body, prefix='', description=''):
            return ('<html><head><title>T</title><meta name="description" content="' + description + '"></head><body><div class="min-h-screen">'
                    '<header class="top"><a href="' + prefix + 'index.html" class="brand uppercase">brand</a><nav><a href="' + prefix + 'blog.html">Journal</a></nav></header>'
                    '<main>' + body + '</main><footer class="foot"><p>© Brand</p></footer></div>'
                    '<section aria-label="Notifications" tabindex="-1"></section></body></html>')

        def card(post, prefix, heading='h3'):
            key, title, category, date, read, excerpt = post
            return ('<a href="' + prefix + 'blog/' + key + '.html" class="group block"><span class="cat">' + category + '</span>'
                    '<p class="meta">' + date + '<!-- --> · <!-- -->' + read + '</p><' + heading + ' class="t">' + title + '</' + heading + '>'
                    '<p class="ex">' + excerpt + '</p><span class="more">Read article →</span></a>')
        files = {'index.html': page('<section class="hero"><h1>Home</h1></section><section class="latest"><div class="grid">' + ''.join(card(p, '') for p in posts[:2]) + '</div></section>'),
                 'blog.html': page('<section class="intro"><h1>Journal</h1></section><section class="list"><div class="rows">' + ''.join('<div class="row" style="opacity:1">' + card(p, '', 'h2') + '</div>' for p in posts) + '</div></section>')}
        for post in posts:
            key, title, category, date, read, excerpt = post
            related = ''.join(card(p, '../') for p in posts if p is not post)
            body = ''.join('<div class="mb-10"><h2 class="h">Part ' + str(i) + ' of ' + title + '</h2><p class="p">' + ('Body text for ' + key + ' ') * 12 + '</p></div>' for i in range(1, 3 + len(key)))
            files['blog/' + key + '.html'] = page(
                '<section class="hero"><div class="wrap"><a href="../blog.html" class="back">← Journal</a>'
                '<p class="meta">' + category + '<!-- --> · <!-- -->' + date + '<!-- --> · <!-- -->' + read + '</p><h1 class="title">' + title + '</h1></div></section>'
                '<section class="body"><div class="wrap"><article class="max-w-[660px]">' + body + '</article></div></section>'
                '<section class="related"><div class="wrap"><p class="label">Keep reading</p><div class="grid gap-10">' + related + '</div></div></section>'
                '<section id="contact" class="cta"><div class="wrap"><h2>Ready?</h2><a href="../index.html" class="rounded-full bg-x px-9">Book</a></div></section>', '../', excerpt)
        for name, source in files.items():
            (self.dist / name).parent.mkdir(parents=True, exist_ok=True)
            (self.dist / name).write_text(source)
        (self.dist / 'about/index.html').unlink()
        kinds = [('home', 'index.html', 'front'), ('blog', 'blog.html', 'blog')] + [('blog-' + p[0], 'blog/' + p[0] + '.html', 'post') for p in posts]
        planner.write(self.manifest, {'site': {'name': 'Brand'}, 'pages': [{'key': k, 'file': f, 'kind': kind} for k, f, kind in kinds]})
        self.run_cli()
        contract = planner.read(self.ws / 'block-plan/contract.json')
        proposals = {k: planner.read(self.ws / 'block-plan/pages' / (k + '.json')) for k, _, _ in kinds}
        findings = {p['key']: p['findings'] for p in planner.read(self.ws / '.gutenberg/inventory.json')['pages']}
        return contract, proposals, findings

    @staticmethod
    def find(blocks, name):
        for block in blocks:
            if block['name'] == name:
                yield block
            yield from PlanTest.find(block.get('innerBlocks', []), name)

    def test_article_template_post_metadata_and_queries(self):
        contract, proposals, findings = self.blog_site()
        self.assertEqual({k: [f['code'] for f in v] for k, v in findings.items()}, {k: [] for k in proposals})
        single = contract['templates']['single']
        self.assertEqual(len(single), 1)
        wrapper = single[0]
        self.assertEqual([(b['name'], b['attributes'].get('slug') or b['attributes'].get('tagName')) for b in wrapper['innerBlocks']],
                         [('core/template-part', 'header'), ('h2wp/element', 'main'), ('core/template-part', 'footer')])
        hero, body, related, cta = wrapper['innerBlocks'][1]['innerBlocks']
        self.assertEqual([b['attributes']['className'] for b in (hero, body, related, cta)], ['hero', 'body', 'related', 'cta'])
        meta, title = hero['innerBlocks'][0]['innerBlocks'][1:]
        self.assertEqual([(s['attributes']['text'], s['attributes'].get('bind'), s['attributes'].get('bindFormat')) for s in meta['innerBlocks']],
                         [('Technique', 'postTerms', None), (' · ', None, None), ('July 14, 2026', 'postDate', 'F j, Y'), (' · ', None, None), ('6 min read', 'postReadTime', None)])
        self.assertEqual(title, {'name': 'core/post-title', 'attributes': {'level': 1, 'className': 'title'}})
        article = body['innerBlocks'][0]['innerBlocks'][0]
        self.assertEqual((article['name'], article['attributes']['tagName'], article['attributes']['className']), ('core/group', 'article', 'max-w-[660px]'))
        self.assertEqual(article['innerBlocks'], [{'name': 'core/post-content', 'attributes': {'layout': {'type': 'default'}}}])
        query = related['innerBlocks'][0]['innerBlocks'][1]
        self.assertEqual(query['attributes']['namespace'], 'h2wp/related-posts')
        self.assertEqual(query['attributes']['query'], {'perPage': 2, 'pages': 0, 'offset': 0, 'postType': 'post', 'order': 'desc', 'orderBy': 'date', 'inherit': False})
        template = query['innerBlocks'][0]
        self.assertEqual((template['name'], template['attributes']['className']), ('core/post-template', 'grid gap-10'))
        card = template['innerBlocks'][0]
        self.assertEqual((card['attributes']['tagName'], card['attributes']['bind'], card['attributes']['htmlAttributes']['href']), ('a', 'postLink', 'page:blog-b'))
        binds = [(b['attributes'].get('tagName'), b['attributes'].get('bind')) for b in card['innerBlocks']]
        self.assertEqual(binds, [('span', 'postTerms'), ('p', None), ('h3', 'postTitle'), ('p', 'postExcerpt'), ('span', None)])
        self.assertEqual([s['attributes'].get('bind') for s in card['innerBlocks'][1]['innerBlocks']], ['postDate', None, 'postReadTime'])
        self.assertEqual(cta['innerBlocks'][0]['innerBlocks'][1]['attributes']['className'], 'is-style-button-1')
        self.assertEqual(contract['blockStyles'][0]['classes'], 'rounded-full bg-x px-9')
        # Post content: the body only; template-owned section ids ride on the first/last body block.
        post = proposals['blog-c']
        self.assertEqual([b['name'] for b in post['blocks']], ['core/template-part', 'core/group', 'core/group', 'core/group', 'core/template-part', 'h2wp/element'])
        self.assertEqual([b.get('sourceIds') for b in post['blocks']], [['source-0001'], ['source-0002', 'source-0003'], None, ['source-0004', 'source-0005'], ['source-0006'], ['source-0007']])
        self.assertEqual(post['post'], {'date': '2026-05-11 12:00:00', 'categories': ['Competitive'], 'readTime': '7 min read', 'excerpt': 'Taper excerpt that is long enough as well.'})
        self.assertNotIn('core/post-title', json.dumps(post['blocks']))
        # Listing: page content keeps a bounded query, home/archive inherit the main query.
        listing = next(self.find(proposals['blog']['blocks'], 'core/query'))
        self.assertEqual((listing['attributes']['query']['perPage'], listing['attributes']['query']['inherit']), (3, False))
        row = listing['innerBlocks'][0]['innerBlocks'][0]
        self.assertEqual((row['attributes']['tagName'], row['attributes']['className'].split()[0]), ('div', 'row'))
        self.assertEqual(row['innerBlocks'][0]['attributes']['bind'], 'postLink')
        home = contract['templates']['home']
        self.assertEqual(home, contract['templates']['archive'])
        self.assertEqual(home[-1]['attributes']['htmlAttributes']['aria-label'], 'Notifications')  # sibling after the wrapper
        inherited = next(self.find(home, 'core/query'))['attributes']['query']
        self.assertEqual((inherited['inherit'], 'perPage' in inherited), (True, False))
        self.assertNotIn('sourceIds', json.dumps(home))
        front = next(self.find(proposals['home']['blocks'], 'core/query'))
        self.assertEqual((front['attributes']['query']['perPage'], 'namespace' in front['attributes']), (2, False))
        # Header brand: uppercase class allows a case-insensitive site name match.
        brand = contract['parts']['header'][0]['innerBlocks'][0]['attributes']
        self.assertEqual((brand['text'], brand['bind'], brand['htmlAttributes']['href']), ('brand', 'siteTitle', 'page:home'))
        for task in planner.read(self.ws / '.gutenberg/tasks.json')['tasks']:
            self.run_cli('claim', '--task=' + task['id'], '--owner=test')
            self.run_cli('complete', '--task=' + task['id'], '--owner=test')
        self.run_cli('finalize')  # coverage stays complete and ordered

    def test_article_without_shared_layout_is_a_finding(self):
        page = lambda body: '<body><div class="app"><header><a href="/">B</a></header><main>' + body + '</main></div></body>'
        (self.dist / 'about/index.html').unlink()
        (self.dist / 'x.html').write_text(page('<section class="a"><h1>X</h1><p>One</p></section>'))
        (self.dist / 'y.html').write_text(page('<section class="b"><h1>Y</h1></section><section><p>Two</p></section>'))
        planner.write(self.manifest, {'pages': [{'key': 'home', 'file': 'index.html'}, {'key': 'x', 'file': 'x.html', 'kind': 'post'}, {'key': 'y', 'file': 'y.html', 'kind': 'post'}]})
        self.run_cli()
        contract = planner.read(self.ws / 'block-plan/contract.json')
        self.assertNotIn('single', contract['templates'])
        codes = {p['key']: [f['code'] for f in p['findings']] for p in planner.read(self.ws / '.gutenberg/inventory.json')['pages']}
        self.assertIn('article-template-variant', codes['x'])

    def test_task_duplicate_assignment_rejected(self):
        self.run_cli()
        self.review()
        path = self.ws / '.gutenberg/tasks.json'
        tasks = planner.read(path)
        tasks['tasks'][0]['pages'].append('home')
        planner.write(path, tasks)
        self.assertIn('worker task coverage must contain every page exactly once', self.run_cli('check', ok=False)['findings'])


if __name__ == '__main__':
    unittest.main()
