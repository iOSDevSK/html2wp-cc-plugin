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

    def test_shared_source_title_is_not_a_page_title(self):
        # An SPA prints the site's name as every page's <title>: the front page
        # keeps it, other pages and products fall back to WordPress's own title
        # and are named after their <h1>. A page's own <title> is kept.
        shared = '<html><head><title>Wool Shop | Knitwear</title></head><body><main><h1>{}</h1><p>x</p></main></body></html>'
        (self.dist / 'index.html').write_text(shared.format('Welcome'))
        (self.dist / 'about/index.html').write_text(shared.format('Our story'))
        (self.dist / 'scarf.html').write_text(shared.format('Merino scarf'))
        (self.dist / 'contact.html').write_text('<html><head><title>Contact us</title></head><body><main><h1>Write</h1></main></body></html>')
        planner.write(self.manifest, {'pages': [{'key': 'home', 'file': 'index.html', 'kind': 'front'}, {'key': 'about', 'file': 'about/index.html'},
                                                {'key': 'scarf', 'file': 'scarf.html', 'kind': 'product'}, {'key': 'contact', 'file': 'contact.html'}]})
        self.run_cli()
        seo = {k: planner.read(self.ws / f'block-plan/pages/{k}.json')['seo'] for k in ('home', 'about', 'scarf', 'contact')}
        self.assertEqual(seo['home'].get('title'), 'Wool Shop | Knitwear')
        self.assertNotIn('title', seo['about'])
        self.assertNotIn('title', seo['scarf'])
        self.assertEqual(seo['contact'].get('title'), 'Contact us')
        titles = {p['key']: p['title'] for p in planner.read(self.manifest)['pages']}
        self.assertEqual(titles['scarf'], 'Merino scarf')

    def test_page_container_for_woocommerce_pages(self):
        # The most common bounded content wrapper (max-width, auto inline
        # margins, horizontal padding) on ordinary pages, plus the vertical
        # padding of the source cart page's own; layout-only classes only.
        (self.dist / 'assets').mkdir()
        (self.dist / 'assets/site.css').write_text('@layer utilities{.wrap{max-width:80rem}.mx-auto{margin-left:auto;margin-right:auto}.px-6{padding-left:1.5rem;padding-right:1.5rem}.py-32{padding-top:8rem;padding-bottom:8rem}.grid{display:grid}}'
                                                   '@media (min-width:768px){.md\\:wide{max-width:90rem}}.text-center{text-align:center}')
        page = lambda inner: '<html><head><link rel="stylesheet" href="/assets/site.css"></head><body><main>' + inner + '</main></body></html>'
        (self.dist / 'index.html').write_text(page('<section><div class="wrap mx-auto px-6 grid"><h1>Home</h1></div></section><section class="wrap mx-auto"><p>x</p></section>'))
        (self.dist / 'about/index.html').write_text(page('<section><div class="wrap mx-auto px-6 md:wide"><h1>About</h1></div></section>'))
        (self.dist / 'cart.html').write_text(page('<section class="wrap mx-auto px-6 py-32 text-center"><h1>Your cart is empty</h1></section>'))
        planner.write(self.manifest, {'shop': {'present': True}, 'pages': [{'key': 'home', 'file': 'index.html', 'kind': 'front'}, {'key': 'about', 'file': 'about/index.html'}, {'key': 'cart', 'file': 'cart.html', 'kind': 'cart'}]})
        self.run_cli()
        self.assertEqual(planner.read(self.ws / 'block-plan/contract.json')['pageContainer'], {'tagName': 'div', 'className': 'wrap mx-auto px-6 py-32'})
        rules = planner.class_rules((self.dist / 'assets/site.css').read_text())
        self.assertNotIn('md:wide', rules)
        self.assertEqual(rules['mx-auto'], {'margin-left': 'auto', 'margin-right': 'auto'})

    def test_no_page_container_without_a_shop(self):
        self.run_cli()
        self.assertNotIn('pageContainer', planner.read(self.ws / 'block-plan/contract.json'))

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
        # The empty notifications portal beside the app wrapper is no content.
        self.assertEqual([b['name'] for b in blocks], ['core/template-part', 'core/group', 'core/group', 'core/template-part'])
        self.assertEqual([b['attributes'].get('slug') for b in blocks], ['header', None, None, 'footer'])
        self.assertEqual(blocks[1]['attributes']['metadata'], {'name': 'Train Swim Win'})
        self.assertEqual([b['sourceIds'] for b in blocks], [['source-000' + str(i)] for i in range(1, 5)])
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
        # The first page's own active link joins the group; nested pages' relative links resolve alike.
        self.assertEqual([(m['key'], [i['url'] for i in m['items']]) for m in contract['menus']], [('header-menu', ['page:index', 'page:about', 'page:team'])])
        # No source CSS resets lists here, so the container stays the source
        # element (with its recorded attribute) and the menu sits inside it.
        container = contract['parts']['header'][0]['innerBlocks'][0]
        nav = container['innerBlocks'][0]['attributes']
        self.assertEqual((nav['linkClassName'], nav['currentClassName']), ('link text-muted-foreground', 'link text-primary active'))
        self.assertNotIn('listClassName', nav)
        self.assertEqual(list(container['attributes']['htmlAttributes']), ['data-spa-scroll'])
        self.assertEqual(findings['index'][0]['detail'], "Shared header keeps the first page's link classes; per-page active styling needs core/navigation or CSS")
        header = json.dumps(contract['parts']['header'])
        self.assertNotIn('aria-current', header)
        self.assertNotIn('data-status', header)
        self.assertIn('link text-primary active', header)  # first page's classes are kept
        self.assertEqual(planner.chrome_equivalent(
            planner.Parser('<a class="x on">A</a><a class="x off">B</a>').root, planner.Parser('<a class="x off">A</a><a class="x on">B</a>').root), True)

    def test_frame_descends_through_app_wrappers(self):
        chrome = '<header class="h"><a href="/">B</a></header><main class="m"><section><h1>Hi</h1></section></main><footer class="f"><p>F</p></footer>'
        portals = '<div role="region"><ol></ol></div><section aria-label="Notifications"></section>'
        def split(html):
            body = next(n for n in planner.walk(planner.Parser(html).root) if isinstance(n, planner.Node) and n.tag == 'body')
            wrapper, main, items = planner.split_frame(body, None)
            return (wrapper.attrs.get('class') if wrapper is not None else None, main is not None,
                    [(c.tag, part, place) for c, part, place in items])
        framed = [('header', 'header', 'head'), ('section', None, 'main'), ('footer', 'footer', 'tail')]
        shapes = {
            'none': ('<body>' + chrome + '</body>', None),
            'one': ('<body><div class="w1">' + chrome + '</div></body>', 'w1'),
            'two+portals': ('<body><div id="root">' + portals + '<div class="w2 min-h-screen">' + chrome + '</div></div></body>', 'w2 min-h-screen'),
            'three+portals': ('<body><div id="root"><div class="w2">' + portals + '<div class="w3">' + chrome + '</div><div class="modal"></div></div></div><script></script></body>', 'w3'),
            'body portals': ('<body>' + portals + '<div class="app"><div class="layout">' + chrome + '</div></div></body>', 'layout'),
        }
        for name, (html, wrapper) in shapes.items():
            got_wrapper, has_main, items = split(html)
            self.assertEqual(got_wrapper, wrapper, name)
            self.assertTrue(has_main, name)
            self.assertEqual([i for i in items if i[2] not in ('before', 'after')], framed, name)
        # The trailing body <script> renders nothing and is no source section.
        # Empty portals (and the trailing body <script>) are no source sections;
        # a portal with content stays an ordinary section.
        self.assertEqual([(i[0], i[2]) for i in split(shapes['three+portals'][0])[2]], [('header', 'head'), ('section', 'main'), ('footer', 'tail')])
        self.assertEqual([(i[0], i[2]) for i in split(shapes['three+portals'][0].replace('<div class="modal"></div>', '<div class="modal"><p>Hi</p></div>'))[2]][-1], ('div', 'after'))
        # No <main> and no wrapper: a leading header and a trailing footer at
        # body level still frame the page (a drawer after the footer stays content).
        nomain = '<body><header class="nav"><nav><a href="a.html">A</a></nav></header><section class="s1"><p>x</p></section><section class="s2"><p>y</p></section><footer class="f"><p>F</p></footer><script></script><aside class="drawer"><nav><a href="a.html">A</a></nav></aside></body>'
        self.assertEqual(split(nomain), (None, False, [('header', 'header', 'head'), ('section', None, 'main'), ('section', None, 'main'), ('footer', 'footer', 'tail'), ('aside', None, 'main')]))
        # A header/footer tag in the middle of content is not page chrome.
        self.assertEqual(split('<body><section><p>x</p></section><header><h2>t</h2></header><section><p>y</p></section></body>')[2], [('section', None, 'main'), ('header', None, 'main'), ('section', None, 'main')])
        # No <main> behind a wrapper.
        self.assertEqual(split('<body><div class="w"><header class="nav"></header><section><p>x</p></section><footer><p>F</p></footer></div></body>')[:2], ('w', False))
        # An article's own header is not page chrome, and two chrome holders are ambiguous.
        self.assertEqual(split('<body><div class="a"><div class="b"><article><header><h1>T</h1></header></article></div><div class="c"><p>x</p></div></div></body>')[0], 'a')
        self.assertEqual(split('<body><div class="a"><div class="b">' + chrome + '</div><div class="c">' + chrome + '</div></div></body>')[0], 'a')
        # Through prepare: parts and menus appear behind two wrappers.
        page = ('<html><head><link rel="stylesheet" href="/assets/app.css"></head><body><div id="root">' + portals + '<div class="min-h-screen flex flex-col">'
                '<header><nav class="flex gap-4"><a href="index.html" class="l">Home</a><a href="about.html" class="l">About</a></nav></header>'
                '<main><section><h1>{}</h1></section></main><footer><p>F</p></footer></div></div></body></html>')
        contract, proposals, findings = self.plan({'index.html': page.format('Home'), 'about.html': page.format('About')}, css='@layer utilities{.l{color:red}}')
        self.assertEqual(sorted(contract['parts']), ['footer', 'header'])
        self.assertEqual(contract['frame']['wrapper']['className'], 'min-h-screen flex flex-col')
        self.assertEqual([m['key'] for m in contract['menus']], ['header-menu'])

    def test_link_groups_become_navigation_menus(self):
        def page(active):
            def link(name, href, normal, current):
                on = name == active
                return '<a href="' + href + '" class="' + (current if on else normal) + '"' + (' aria-current="page" data-status="active"' if on else '') + '>' + name + '</a>'
            pages = (('Home', 'index.html'), ('About', 'about.html'), ('Journal', 'blog.html'))
            desktop = ''.join(link(n, h, 'nav-link text-muted', 'nav-link text-ink') for n, h in pages)
            drawer = ''.join(link(n, h, 'text-sm', 'text-sm active') for n, h in pages)
            info = ''.join('<li>' + link(n, h, 'text-xs', 'text-xs') + '</li>' for n, h in pages[1:])
            return ('<html><head><link rel="stylesheet" href="/assets/app.css"></head><body><div class="app">'
                    '<header><a href="index.html" class="brand">Brand</a><nav class="hidden md:flex gap-8">' + desktop + '</nav>'
                    '<a href="contact.html" class="cta">Book now</a><div class="drawer flex flex-col">' + drawer + '<a href="contact.html" class="cta-sm">Book now</a></div></header>'
                    '<main><section><h1>' + active + '</h1></section></main>'
                    '<footer><div class="col"><div class="label">Info</div><ul class="flex flex-col gap-3">' + info + '</ul></div>'
                    '<div class="col"><a href="mailto:a@b.c" class="m">a@b.c</a><a href="tel:+1" class="m">+1</a></div>'
                    '<div class="space-y-2"><a href="about.html" class="s">About</a><a href="blog.html" class="s">Journal</a></div></footer></div></body></html>')
        css = '@layer theme, base, components, utilities;@layer base{*,::before,::after{box-sizing:border-box;margin:0;padding:0}}@layer utilities{.text-ink{color:#111}}'
        contract, proposals, findings = self.plan({'index.html': page('Home'), 'about.html': page('About'), 'blog.html': page('Journal'), 'contact.html': page('Contact')}, css=css)
        links = [{'label': 'Home', 'url': 'page:index'}, {'label': 'About', 'url': 'page:about'}, {'label': 'Journal', 'url': 'page:blog'}]
        self.assertEqual(contract['menus'], [
            {'key': 'header-menu', 'name': 'Header menu', 'items': links},
            {'key': 'footer-info', 'name': 'Footer: Info', 'items': links[1:]}])

        def navs(blocks):
            for block in blocks:
                if block['name'] == 'h2wp/navigation':
                    yield block['attributes']
                yield from navs(block.get('innerBlocks') or [])
        header = contract['parts']['header'][0]['innerBlocks']
        # The whole <nav> becomes the menu's list; the first page's own active
        # link does not split the group; the current page swaps the classes.
        self.assertEqual(header[1], {'name': 'h2wp/navigation', 'attributes': {'menu': 'header-menu', 'linkClassName': 'nav-link text-muted', 'listClassName': 'hidden md:flex gap-8', 'currentClassName': 'nav-link text-ink'}})
        self.assertEqual(header[2]['attributes']['className'], 'cta')  # a lone call to action stays an element
        # The drawer shares the menu and keeps its trailing button beside it.
        drawer = header[3]['innerBlocks']
        self.assertEqual(drawer[0]['attributes'], {'menu': 'header-menu', 'linkClassName': 'text-sm', 'currentClassName': 'text-sm active'})
        self.assertEqual(drawer[1]['attributes']['className'], 'cta-sm')
        footer = list(navs(contract['parts']['footer']))
        # A container spacing its children (space-y-2) keeps its classes on the
        # list, and item boxes (h2wp-nav-wrap) take the margins.
        self.assertEqual(footer, [{'menu': 'footer-info', 'linkClassName': 'text-xs', 'itemClassName': '', 'listClassName': 'flex flex-col gap-3'},
                                  {'menu': 'footer-info', 'linkClassName': 's', 'itemClassName': 'h2wp-nav-wrap', 'listClassName': 'space-y-2'}])
        # mailto/tel links stay elements.
        self.assertIn('mailto:a@b.c', json.dumps(contract['parts']['footer']))
        self.assertFalse(any(f['code'] == 'navigation-static' for f in findings['index']))

    def test_dropdown_submenus_stay_source_markup(self):
        # A nested list is a submenu (WordPress's list items hold only lists):
        # it stays as source markup; a flat sibling group still becomes a menu,
        # with any source CSS (no @layer here).
        page = ('<html><head><link rel="stylesheet" href="/assets/app.css"></head><body><header><ul class="menu">'
                '<li><a href="index.html" class="top">Home</a></li><li><span class="top">Company</span><ul class="sub">'
                '<li><a href="about.html" class="s">About</a></li><li><a href="team.html" class="s">Team</a></li></ul></li></ul>'
                '<div class="drawer"><a href="index.html" class="m">Home</a><a href="about.html" class="m">About</a><a href="team.html" class="m">Team</a></div></header>'
                '<main><section><h1>{}</h1></section></main></body></html>')
        contract, proposals, findings = self.plan({'index.html': page.format('Home'), 'about.html': page.format('About'), 'team.html': page.format('Team')}, css='.m{color:red}')
        self.assertEqual([(m['key'], [i['label'] for i in m['items']]) for m in contract['menus']], [('header-menu', ['Home', 'About', 'Team'])])
        data = json.dumps(contract['parts']['header'])
        self.assertIn('Company', data)  # the dropdown and its label stay elements
        self.assertEqual(data.count('h2wp/navigation'), 1)

    def test_drawer_classes_are_not_taken_from_desktop_links(self):
        # Desktop links carry a recorded attribute (not plain links, stay
        # elements); the drawer group with the same labels keeps its own
        # classes, and its spaced container becomes the list with item boxes.
        page = ('<html><head><link rel="stylesheet" href="/assets/app.css"></head><body><header><nav class="flex gap-8">'
                '<a href="index.html" class="d" data-x="1">Home</a><a href="about.html" class="d" data-x="1">About</a></nav>'
                '<nav class="space-y-4" data-spa-panel="t1" hidden><a href="index.html" class="block m">Home</a><a href="about.html" class="block m">About</a></nav></header>'
                '<main><section><h1>{}</h1></section></main></body></html>')
        contract, _, _ = self.plan({'index.html': page.format('Home'), 'about.html': page.format('About')}, css='ul{margin:0;padding:0}.m{color:red}')
        nav = next(b for b in contract['parts']['header'][0]['innerBlocks'] if b['name'] == 'h2wp/navigation')['attributes']
        self.assertEqual((nav['linkClassName'], nav['listClassName'], nav['itemClassName']), ('block m', 'space-y-4', 'h2wp-nav-wrap'))
        self.assertEqual(nav['listAttributes'], {'data-spa-panel': 't1', 'hidden': True})

    def test_recorder_runtime_and_inline_styles_ride_along(self):
        (self.dist / 'assets').mkdir(exist_ok=True)
        (self.dist / 'assets/spa-runtime.js').write_text('/* spa-runtime.js \u2014 generated by html2wp-sub prerender-spa.py.\n */')
        (self.dist / 'assets/app.js').write_text('hydrate()')
        page = ('<html><head><link href="https://fonts.googleapis.com/css2?family=Jost:wght@300;400&amp;display=swap" rel="stylesheet"><link href="https://cdn.example.com/x.css" rel="stylesheet"><style>@keyframes spa-enter{from{opacity:0}}[data-spa-enter]{animation:spa-enter 400ms ease-in-out}</style><script data-spa-reveals>document.documentElement.classList.add("spa-reveal")</script>{}'
                '<script src="assets/spa-runtime.js" defer></script><script src="assets/app.js"></script></head>'
                '<body><main><section data-spa-enter="400"><h1>Hi</h1></section></main></body></html>')
        contract, _, findings = self.plan({'index.html': page.replace('{}', ''), 'about.html': page.replace('{}', '<style>.only{color:red}</style>').replace('400ms', '550ms').replace('"400"', '"550"')})
        self.assertEqual(contract['scripts'], ['assets/spa-runtime.js'])
        # The reveal boot runs in the head, before first paint.
        self.assertEqual(contract['headScripts'], ['assets/gutenberg-reveal-boot.js'])
        self.assertIn('spa-reveal', (self.dist / 'assets/gutenberg-reveal-boot.js').read_text())
        self.assertIn('assets/gutenberg-head.css', contract['styles'])
        css = (self.dist / 'assets/gutenberg-head.css').read_text()
        # Each page's recorded entrance keeps its own duration.
        self.assertIn('[data-spa-enter="400"]{animation:spa-enter 400ms', css)
        self.assertIn('[data-spa-enter="550"]{animation:spa-enter 550ms', css)
        # A font service stylesheet rides along; any other remote stylesheet,
        # source inline CSS and the source application script stay findings.
        self.assertEqual(contract['fontStyles'], ['https://fonts.googleapis.com/css2?family=Jost:wght@300;400&display=swap'])
        self.assertNotIn('.only', css)
        self.assertEqual(sorted(f['code'] for f in findings['index']), ['external-stylesheet', 'source-runtime'])
        self.assertEqual(sorted(f['code'] for f in findings['about']), ['external-stylesheet', 'inline-stylesheet', 'source-runtime'])

    def test_recorded_form_success_feedback(self):
        success = {'kind': 'toast', 'html': '<li class="toast">Thanks!</li>', 'text': 'Thanks!', 'list': '<ol class="toasts"></ol>', 'region': '<section role="region" aria-label="Notifications">', 'ms': 4000}
        page = "<html><body><main><section><form id=\"news\" data-spa-success='" + json.dumps(success).replace("'", '&#39;') + "'><input type=\"email\" name=\"email\" placeholder=\"Email\"><button type=\"submit\">Join</button></form></section></main></body></html>"
        _, proposals, _ = self.plan({'index.html': page})
        form = next(self.find(proposals['index']['blocks'], 'h2wp/form'))['attributes']
        self.assertEqual(form['success'], {k: success[k] for k in ('kind', 'html', 'list', 'region', 'ms')})

    def test_recorded_empty_submit_messages_ride_on_fields_and_form(self):
        # What prerender-spa.py stamps: the form's token, each field's id, the
        # hidden messages (under a field, and a toast outside the form).
        page = ('<html><body><main><section><form data-spa-validate="v1">'
                '<div><label>Name<input name="name" data-spa-vfield="v1-1"></label>'
                '<p class="err" data-spa-invalid="v1" data-spa-for="v1-1" hidden style="display:none">Please enter your name</p></div>'
                '<div><label>Email<input type="email" name="email" data-spa-vfield="v1-2"></label></div>'
                '<button type="submit">Send</button></form></section>'
                '<ol><li data-spa-invalid="v1" hidden style="display:none">Please fill in the form.</li></ol>'
                '<form data-spa-validate="v2"><label>Search<input name="q" data-spa-vfield="v2-1"></label><button type="submit">Go</button></form></main></body></html>')
        _, proposals, findings = self.plan({'index.html': page})
        forms = [b['attributes'] for b in self.find(proposals['index']['blocks'], 'h2wp/form')]
        fields = [b['attributes'] for b in self.find(proposals['index']['blocks'], 'h2wp/field')]
        self.assertEqual([f['acceptSubmissions'] for f in forms], [False, False], 'forms ship disconnected')
        self.assertEqual(forms[0]['invalidMessage'], 'Please fill in the form.')
        self.assertNotIn('invalidMessage', forms[1], "another form's token does not borrow this one's message")
        self.assertEqual([(f['name'], f.get('invalidMessage')) for f in fields], [('name', 'Please enter your name'), ('email', None), ('q', None)])
        text = json.dumps(proposals['index']['blocks'])
        self.assertNotIn('Please enter your name</', text.replace('"invalidMessage": "Please enter your name"', ''))
        self.assertEqual(text.count('Please fill in the form.'), 1, 'the toast is not also kept as an element')
        # The recorder's own stamps are not reported as source attributes to review.
        self.assertFalse([f for f in findings['index'] if f['code'] in ('form-attributes', 'field-attributes')])

    def test_unlabelled_field_keeps_a_hidden_label(self):
        page = '<html><body><main><section><form class="flex"><input type="email" name="email" placeholder="Email Address" class="flex-1"><button type="submit">Join</button></form><form><label for="n">Name</label><input id="n" name="name" class="i"></form></section></main></body></html>'
        _, proposals, _ = self.plan({'index.html': page})
        fields = [b['attributes'] for b in self.find(proposals['index']['blocks'], 'h2wp/field')]
        self.assertEqual([(f['label'], f['labelClassName']) for f in fields], [('Email Address', 'h2wp-label-hidden'), ('Name', '')])

    def test_shop_category_query_links(self):
        for name in ('index.html', 'about/index.html'):
            (self.dist / name).unlink()
        foot = '<footer><ul class="f"><li><a href="shop.html?category=Knit%20Wear" class="l">Knitwear</a></li><li><a href="shop.html?category=Scarves" class="l">Scarves</a></li><li><a href="shop.html?sort=new" class="l">New</a></li></ul></footer>'
        for name in ('index.html', 'shop.html'):
            (self.dist / name).write_text('<html><head><link rel="stylesheet" href="/assets/app.css"></head><body><header><a href="index.html">B</a></header><main><section><h1>' + name + '</h1></section></main>' + foot + '</body></html>')
        (self.dist / 'assets').mkdir(exist_ok=True)
        (self.dist / 'assets/app.css').write_text('ul{margin:0;padding:0}')
        planner.write(self.manifest, {'shop': {'present': True, 'categoryQueryParam': 'category'}, 'pages': [{'key': 'home', 'file': 'index.html', 'kind': 'front'}, {'key': 'shop', 'file': 'shop.html', 'kind': 'shop'}]})
        self.run_cli()
        contract = planner.read(self.ws / 'block-plan/contract.json')
        # The category filter becomes WooCommerce's category archive; another query string is kept.
        self.assertEqual([i['url'] for i in contract['menus'][0]['items']], ['category:Knit%20Wear', 'category:Scarves', 'page:shop?sort=new'])

    def test_v1_utility_pages(self):
        for name in ('index.html', 'about/index.html'):
            (self.dist / name).unlink()
        for name in ('index.html', '404.html', 'thanks.html'):
            (self.dist / name).write_text('<html><body><main><section><h1>' + name + '</h1></section></main></body></html>')
        planner.write(self.manifest, {'utilityPages': {'404': '404.html'}, 'pages': [{'key': 'home', 'file': 'index.html', 'kind': 'front'}, {'key': 'nf', 'file': '404.html', 'kind': 'utility'}, {'key': 'thanks', 'file': 'thanks.html', 'kind': 'utility'}]})
        self.run_cli()
        self.assertEqual([p['kind'] for p in planner.read(self.manifest)['pages']], ['front', '404', 'page'])

    def test_sibling_dependent_link_classes_stay_elements(self):
        page = ('<html><head><link rel="stylesheet" href="/assets/app.css"></head><body><header><nav class="flex">'
                '<a href="index.html" class="m first:pl-0">Home</a><a href="about.html" class="m first:pl-0">About</a></nav></header>'
                '<main><section><h1>{}</h1></section></main></body></html>')
        contract, _, findings = self.plan({'index.html': page.format('Home'), 'about.html': page.format('About')}, css='.m{color:red}')
        self.assertEqual(contract['menus'], [])
        self.assertIn('Home / About', next(f['detail'] for f in findings['index'] if f['code'] == 'navigation-static'))

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

    def blog_site(self, images=False, shared_description=False, lead=False, dash=False):
        posts = [('a', 'The catch is where freestyle is won', 'Technique', 'July 14, 2026', '6 min read', 'Catch excerpt that is long enough — and a dash to read.' if dash else 'Catch excerpt that is long enough to read.'),
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
        if lead:
            files['blog.html'] = page('<section class="intro"><h1>Journal</h1></section><section class="lead">' + card(posts[0], '', 'h2') + '</section><section class="list"><div class="grid">' + ''.join(card(p, '') for p in posts[1:]) + '</div></section>')
        for post in posts:
            key, title, category, date, read, excerpt = post
            related = ''.join(card(p, '../') for p in posts if p is not post)
            body = ''.join('<div class="mb-10"><h2 class="h">Part ' + str(i) + ' of ' + title + '</h2><p class="p">' + ('Body text for ' + key + ' ') * 12 + '</p></div>' for i in range(1, 3 + len(key)))
            hero_image = '<img src="../assets/' + key + '.png" alt="' + title + '" class="w-full aspect-video">' if images else ''
            files['blog/' + key + '.html'] = page(
                '<section class="hero"><div class="wrap">' + hero_image + '<a href="../blog.html" class="back">← Journal</a>'
                '<p class="meta">' + category + '<!-- --> · <!-- -->' + date + '<!-- --> · <!-- -->' + read + '</p><h1 class="title">' + title + '</h1></div></section>'
                '<section class="body"><div class="wrap"><article class="max-w-[660px]">' + body + '</article></div></section>'
                '<section class="related"><div class="wrap"><p class="label">Keep reading</p><div class="grid gap-10">' + related + '</div></div></section>'
                '<section id="contact" class="cta"><div class="wrap"><h2>Ready?</h2><a href="../index.html" class="rounded-full bg-x px-9">Book</a></div></section>', '../', 'One site-wide description.' if shared_description else excerpt)
            if images:
                (self.dist / 'assets').mkdir(exist_ok=True)
                (self.dist / 'assets' / (key + '.png')).write_bytes(b'png' + key.encode())
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

    def test_listing_lead_article_and_offset_grid(self):
        # A lone card for the newest post is a one-post query; the grid below
        # it skips that post, in the page and in the listing templates.
        contract, proposals, _ = self.blog_site(lead=True)
        queries = [b['attributes']['query'] for b in self.find(proposals['blog']['blocks'], 'core/query')]
        self.assertEqual([(q['perPage'], q['offset'], q['inherit']) for q in queries], [(1, 0, False), (2, 1, False)])
        home = [b['attributes']['query'] for b in self.find(contract['templates']['home'], 'core/query')]
        self.assertEqual([(q.get('perPage'), q['offset'], q['inherit']) for q in home], [(1, 0, False), (2, 1, False)])

    def test_lead_card_excerpt_with_a_dash_stays_one_value(self):
        # An em dash inside the excerpt is no separator: the lead card binds
        # the whole excerpt once instead of repeating its tail as static text.
        _, proposals, _ = self.blog_site(lead=True, dash=True)
        lead = next(self.find(proposals['blog']['blocks'], 'core/query'))
        texts = [b['attributes'] for b in self.find([lead], 'h2wp/element') if b['attributes'].get('text') or b['attributes'].get('bind') == 'postExcerpt']
        self.assertFalse(any('a dash to read' in (a.get('text') or '') and a.get('bind') != 'postExcerpt' for a in texts))
        self.assertTrue(any(a.get('bind') == 'postExcerpt' for a in texts))

    def test_article_images_become_featured_images(self):
        # Each post's own hero image is its featured image; the template binds it.
        # A description every page shares is no excerpt.
        contract, proposals, _ = self.blog_site(images=True, shared_description=True)
        images = list(self.find(contract['templates']['single'], 'core/image'))
        self.assertEqual(len(images), 1)
        self.assertEqual(images[0]['attributes']['metadata'], {'bindings': {'url': {'source': 'h2wp/post-image'}, 'alt': {'source': 'h2wp/post-image', 'args': {'key': 'alt'}}}})
        self.assertEqual({k: p['post'].get('featuredImage') for k, p in proposals.items() if 'post' in p}, {'blog-a': 'asset:assets/a.png', 'blog-b': 'asset:assets/b.png', 'blog-c': 'asset:assets/c.png'})
        self.assertFalse(any(p['post'].get('excerpt') == 'One site-wide description.' for p in proposals.values() if 'post' in p))

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
        self.assertEqual([b['name'] for b in post['blocks']], ['core/template-part', 'core/group', 'core/group', 'core/group', 'core/template-part'])
        self.assertEqual([b.get('sourceIds') for b in post['blocks']], [['source-0001'], ['source-0002', 'source-0003'], None, ['source-0004', 'source-0005'], ['source-0006']])
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
        self.assertNotIn('Notifications', json.dumps(home))  # the empty portal is not part of the template
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
