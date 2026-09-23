#!/usr/bin/env python3
"""Behavioral checks for isolated proposals, stale reviews and exact upload scope."""
import importlib.util
import json
import os
import shutil
import tarfile
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).with_name('gutenberg-plan.py')
spec = importlib.util.spec_from_file_location('planner', SCRIPT)
planner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(planner)


def walk_blocks(blocks):
    for block in blocks:
        yield block
        yield from walk_blocks(block.get('innerBlocks', []))


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

    def test_a_block_schema_beside_the_planner_gates_check(self):
        # gutenberg_block_schema.py, when it ships beside the planner, judges
        # every proposal tree: each violation fails check/finalize, and so does
        # a module that cannot answer. A copy of the planner runs here, so no
        # stub ever sits beside the real script.
        self.run_cli()
        self.review()
        self.run_cli('finalize')
        beside = Path(self.temp.name) / 'bin'
        beside.mkdir()
        shutil.copy(SCRIPT, beside / SCRIPT.name)
        def check(body):
            (beside / 'gutenberg_block_schema.py').write_text('def validate_trees(plan_dir):\n' + body)
            result = subprocess.run(['python3', str(beside / SCRIPT.name), 'check', '--manifest=' + str(self.manifest)], text=True, capture_output=True)
            return result.returncode, json.loads(result.stdout)['findings']
        # It is handed the block-plan directory itself.
        where = '    assert plan_dir.name == "block-plan" and (plan_dir / "contract.json").is_file(), plan_dir\n'
        self.assertEqual(check(where + '    return []\n'), (0, []))
        code, findings = check(where + '    return [{"at": "home:/blocks/0", "message": "core/heading lacks its level"}, {"at": "about:/blocks/1", "message": "unknown attribute"}]\n')
        self.assertEqual((code, findings), (1, ['block schema: home:/blocks/0: core/heading lacks its level', 'block schema: about:/blocks/1: unknown attribute']))
        code, findings = check('    raise RuntimeError("schema file unreadable")\n')
        self.assertEqual(code, 1)
        self.assertEqual(findings, ['block schema: cannot validate the plan: RuntimeError: schema file unreadable'])
        code, findings = check('    return None\n')
        self.assertEqual(code, 1)
        self.assertIn('not a list', findings[0])
        # Without the module the check is what it was.
        (beside / 'gutenberg_block_schema.py').unlink()
        self.assertEqual(subprocess.run(['python3', str(beside / SCRIPT.name), 'check', '--manifest=' + str(self.manifest)], capture_output=True).returncode, 0)

    def test_a_code_raised_many_times_on_a_page_is_one_finding(self):
        # Three unresolved links over two sections and one unsafe URL: two
        # findings. The repeated code lists every item in source order and is
        # resolved once; the single one keeps its own shape and id.
        (self.dist / 'index.html').write_text('<body><section><h1>Home</h1><p><a href="/gone-a/">A</a> <a href="/gone-b/">B</a></p></section>'
                                              '<section><p><a href="/gone-c/">C</a> <a href="javascript:alert(1)">X</a></p></section></body>')
        self.run_cli()
        findings = planner.read(self.ws / '.gutenberg/inventory.json')['pages'][0]['findings']
        self.assertEqual([f['code'] for f in findings], ['unresolved-link', 'unsafe-url'])
        links, unsafe = findings
        self.assertEqual(links['items'], [{'section': 'source-0001', 'detail': '/gone-a/'}, {'section': 'source-0001', 'detail': '/gone-b/'},
                                          {'section': 'source-0002', 'detail': '/gone-c/'}])
        self.assertEqual(links['id'], planner.digest({'code': 'unresolved-link', 'items': links['items']})[:20])
        self.assertNotIn('items', unsafe)
        self.assertEqual(unsafe['id'], planner.digest({k: unsafe[k] for k in ('code', 'section', 'detail')})[:20])
        self.review()
        report = self.run_cli('finalize', ok=False)
        self.assertIn('home: unresolved unresolved-link [' + links['id'] + '] (3 items)', report['findings'])
        checkpoint = planner.read(self.ws / '.gutenberg/checkpoint.json')
        for page in planner.read(self.ws / '.gutenberg/inventory.json')['pages']:
            for f in page['findings']:
                checkpoint['resolutions'][page['key'] + ':' + f['id']] = 'reviewed and accepted for the test'
        checkpoint['resolutions']['home:' + links['id']] = 'too short'
        planner.write(self.ws / '.gutenberg/checkpoint.json', checkpoint)
        self.assertEqual(self.run_cli('finalize', ok=False)['findings'], ['home: unresolved unresolved-link [' + links['id'] + '] (3 items)'])
        checkpoint['resolutions']['home:' + links['id']] = 'the source links three pages it never shipped; kept as written'
        planner.write(self.ws / '.gutenberg/checkpoint.json', checkpoint)
        self.run_cli('finalize')

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
        # Both disclosures, in one finding that lists each of them.
        self.assertEqual(codes.count('unrecorded-disclosure'),1)
        self.assertEqual(len(next(f for f in inventory['pages'][0]['findings'] if f['code']=='unrecorded-disclosure')['items']),2)
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

    def plan(self, pages, css=None, kinds=None):
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
        planner.write(self.manifest, {'pages': [{'key': key, 'file': name, **({'kind': kinds[key]} if kinds and key in kinds else {})} for key, name in zip(keys, pages)]})
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
        # A responsive picture on a nested page names the same files through ../ paths.
        top = '<a href="index.html"><img src="./assets/a.webp" srcset="./assets/a-80.webp 80w, ./assets/a.webp 1000w"></a>'
        nested = '<a href="../index.html"><img src="../assets/a.webp" srcset="../assets/a-80.webp 80w, ../assets/a.webp 1000w"></a>'
        self.assertTrue(planner.chrome_equivalent(planner.Parser(top).root, planner.Parser(nested).root, 'blog.html', 'blog/post.html'))
        self.assertFalse(planner.chrome_equivalent(planner.Parser(top).root, planner.Parser(nested.replace('a-80', 'b-80')).root, 'blog.html', 'blog/post.html'))

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
        # A drawer after the footer is furniture after the frame, not content
        # between the parts (else every article's content splits in two).
        self.assertEqual(split(nomain), (None, False, [('header', 'header', 'head'), ('section', None, 'main'), ('section', None, 'main'), ('footer', 'footer', 'tail'), ('aside', None, 'after')]))
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

    def test_class_only_current_links_and_header_variants(self):
        def page(header_class, link_class, current=None, extra=''):
            links = ''.join('<a class="' + (link_class + ' is-on' if name == current else link_class) + '" href="' + name + '.html">' + name.title() + '</a>'
                            for name in ('shop', 'about', 'journal'))
            return ('<html><body><div class="min-h-screen"><header class="' + header_class + '"><a class="brand" href="index.html">Brand</a>'
                    '<nav class="links">' + links + '</nav>' + extra + '</header><main class="flex-1"><section><h1>' + (current or 'x') + '</h1></section></main>'
                    '<footer class="foot"><p>F</p></footer></div></body></html>')
        solid, light = ('fixed bg-background', 'nav-link text-muted'), ('fixed bg-transparent', 'nav-link text-white')
        pages = {'index.html': page(light[0], light[1]), 'about.html': page(*solid, current='about'), 'shop.html': page(*solid, current='shop'),
                 'cart.html': page(*solid), 'checkout.html': page(*solid), 'journal-post.html': page(*solid, current='journal')}
        kinds = {'index': 'front', 'about': 'page', 'shop': 'page', 'cart': 'page', 'checkout': 'page', 'journal-post': 'article'}
        contract, proposals, findings = self.plan(pages, kinds=kinds)
        codes = {key: sorted(f['code'] for f in items if f['code'] != 'chrome-active-state') for key, items in findings.items()}
        # A class-only current link is page state, not a design variant.
        self.assertEqual({k: v for k, v in codes.items() if 'chrome-variant' in v}, {})
        # The majority (solid) header is shared and owned by a page without a current link.
        header = json.dumps(contract['parts']['header'])
        self.assertIn('bg-background', header)
        self.assertNotIn('"className": "nav-link text-muted is-on"', header)
        # The front page's transparent header is its own part, rendered by its own template.
        self.assertIn('bg-transparent', json.dumps(contract['parts']['header-2']))
        self.assertIn({'name': 'header-2', 'title': 'Header 2', 'area': 'header'}, contract['themeJson']['templateParts'])
        self.assertEqual([b['attributes'].get('slug') for b in proposals['index']['blocks'] if b['name'] == 'core/template-part'], ['header-2', 'footer'])
        self.assertEqual([b['attributes'].get('slug') for b in proposals['about']['blocks'] if b['name'] == 'core/template-part'], ['header', 'footer'])
        front = json.dumps(contract['templates']['front-page'])
        self.assertIn('"slug": "header-2"', front)
        self.assertIn('core/post-content', front)
        self.assertNotIn('template', proposals['about'])
        # The current link's own classes become the menu's current-page classes.
        def blocks(tree):
            for b in tree:
                yield b
                yield from blocks(b.get('innerBlocks') or [])
        navs = [b['attributes'] for b in blocks(contract['parts']['header']) if b['name'] == 'h2wp/navigation']
        self.assertEqual([(n['linkClassName'], n.get('currentClassName')) for n in navs], [('nav-link text-muted', 'nav-link text-muted is-on')])

    def test_variant_used_by_shared_templates_is_replaced(self):
        solid = '<header class="bar"><a href="index.html">B</a></header>'
        other = '<header class="bar dark"><a href="index.html">B</a></header>'
        wrap = lambda head: '<html><body><div class="app">' + head + '<main class="m"><section><h1>T</h1></section></main></div></body></html>'
        pages = {'index.html': wrap(solid), 'a.html': wrap(solid), 'p1.html': wrap(other), 'p2.html': wrap(other), 'extra.html': wrap(other.replace('dark', 'x'))}
        contract, proposals, findings = self.plan(pages, kinds={'index': 'front', 'a': 'page', 'p1': 'article', 'p2': 'article', 'extra': 'page'})
        codes = {key: [f['code'] for f in items if f['code'] == 'chrome-variant'] for key, items in findings.items()}
        # Posts share the single template, so their variant falls back to the shared part (and says so).
        self.assertEqual(codes, {'index': [], 'a': [], 'p1': ['chrome-variant'], 'p2': ['chrome-variant'], 'extra': []})
        self.assertEqual(sorted(contract['parts']), ['header', 'header-2'])
        self.assertIn('bar x', json.dumps(contract['parts']['header-2']))
        self.assertEqual(proposals['extra']['template'], 'page-header-2')
        self.assertIn('page-header-2', contract['templates'])

    def test_odd_links_and_variant_owner(self):
        root = lambda html: planner.Parser(html).root
        def odd_texts(html):
            tree = root(html); ids = planner.odd_links(tree)
            return [n.children[0] for n in planner.walk(tree) if isinstance(n, planner.Node) and id(n) in ids]
        self.assertEqual(odd_texts('<nav><a class="l">A</a><a class="l on">B</a><a class="l">C</a></nav>'), ['B'])
        self.assertEqual(odd_texts('<ul><li><a class="l">A</a></li><li><a class="l">B</a></li><li><a class="l x">C</a></li></ul>'), ['C'])
        self.assertEqual(odd_texts('<nav><a class="l">A</a><a class="l on">B</a></nav>'), [])  # two links: no majority
        self.assertEqual(odd_texts('<nav><a class="a">A</a><a class="b">B</a><a class="c">C</a></nav>'), [])  # mixed list
        self.assertEqual(odd_texts('<nav><a class="l">A</a><a class="l">B</a><a class="l">C</a></nav>'), [])
        groups = [[('p1', 'cur'), ('p2', 'plain')], [('front', 'x')], [('a', 'plain'), ('b', 'plain')]]
        got = planner.variant_parts('header', groups, {'p1': 'page', 'p2': 'page', 'front': 'front', 'a': 'article', 'b': 'article'}, lambda src: src == 'cur')
        # Tie between the first and third group goes to the first seen; its owner is the member without a current link.
        self.assertEqual(got, {'parts': [('header', 'p2', ['p1', 'p2']), ('header-2', 'front', ['front'])], 'replaced': ['a', 'b']})

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
        contract, proposals, findings = self.plan({'index.html': page.replace('{}', ''), 'about.html': page.replace('{}', '<style>.only{color:red}</style>').replace('400ms', '550ms').replace('"400"', '"550"')})
        self.assertEqual(contract['scripts'], ['assets/spa-runtime.js'])
        # The reveal boot runs in the head, before first paint.
        self.assertEqual(contract['headScripts'], ['assets/gutenberg-reveal-boot.js'])
        self.assertIn('spa-reveal', (self.dist / 'assets/gutenberg-reveal-boot.js').read_text())
        self.assertIn('assets/gutenberg-head.css', contract['styles'])
        css = (self.dist / 'assets/gutenberg-head.css').read_text()
        # Each page's recorded entrance keeps its own duration.
        self.assertIn('[data-spa-enter="400"]{animation:spa-enter 400ms', css)
        self.assertIn('[data-spa-enter="550"]{animation:spa-enter 550ms', css)
        # A font service stylesheet rides along; any other remote stylesheet
        # and the source application script stay findings. Source inline CSS
        # is the page's own stylesheet, never merged into the shared one.
        self.assertEqual(contract['fontStyles'], ['https://fonts.googleapis.com/css2?family=Jost:wght@300;400&display=swap'])
        # Different font links per page: shared prefix global, the rest per page.
        self.assertNotIn('fontStyles', proposals['index'])
        self.assertNotIn('.only', css)
        self.assertEqual(sorted(f['code'] for f in findings['index']), ['external-stylesheet', 'source-runtime'])
        self.assertEqual(sorted(f['code'] for f in findings['about']), ['external-stylesheet', 'source-runtime'])
        own = proposals['about']['styles']
        self.assertEqual(len(own), 1)
        self.assertEqual((self.dist / own[0]).read_text(), '.only{color:red}\n')
        self.assertNotIn('styles', proposals['index'])

    def test_self_contained_page_keeps_its_shell_and_skips_the_parts(self):
        sub = '<html><body><header class="nav"><a href="index.html">Home</a><a href="about.html">About</a></header><section><h1>{}</h1></section><footer class="f"><p>Shared</p></footer></body></html>'
        home = '<html><body><section class="hero"><header class="nav"><a href="about.html">About</a></header><h1>Home</h1></section><footer class="own"><p>Own footer</p></footer></body></html>'
        for name, source in {'index.html': home, 'about.html': sub.format('About'), 'contact.html': sub.format('Contact')}.items():
            (self.dist / name).parent.mkdir(parents=True, exist_ok=True)
            (self.dist / name).write_text(source)
        (self.dist / 'about/index.html').unlink()
        planner.write(self.manifest, {'chrome': {'header': {'selector': 'header.nav'}}, 'pages': [
            {'key': 'front-page', 'file': 'index.html', 'kind': 'front', 'chrome': 'self-contained'},
            {'key': 'about', 'file': 'about.html', 'chrome': 'consensus'}, {'key': 'contact', 'file': 'contact.html', 'chrome': 'self-contained'}]})
        self.run_cli()
        contract = planner.read(self.ws / 'block-plan/contract.json')
        front = planner.read(self.ws / 'block-plan/pages/front-page.json')
        about = planner.read(self.ws / 'block-plan/pages/about.json')
        # WordPress renders the front page through front-page.html whatever it
        # selects: its self-contained shell is that template, not a selection.
        self.assertNotIn('template', front)
        self.assertEqual(contract['templates']['front-page'], [{'name': 'core/post-content', 'attributes': {'layout': {'type': 'default'}}}])
        self.assertFalse(any(b['name'] == 'core/template-part' for b in front['blocks']))
        self.assertIn('Own footer', json.dumps(front['blocks']))
        # Any other self-contained page selects the custom template.
        self.assertEqual(planner.read(self.ws / 'block-plan/pages/contact.json')['template'], 'page-self-contained')
        self.assertEqual(contract['templates']['page-self-contained'][0]['name'], 'core/post-content')
        self.assertIn({'name': 'page-self-contained', 'title': 'Self-contained page', 'postTypes': ['page']}, contract['themeJson']['customTemplates'])
        self.assertIn('footer', contract['parts'])
        self.assertIn('Shared', json.dumps(contract['parts']['footer']))
        self.assertTrue(any(b['name'] == 'core/template-part' for b in about['blocks']))

    def test_article_host_holding_the_headline_and_card_summaries(self):
        # No <main>; header/footer at body level; a drawer after the footer;
        # each post's prose host also holds its eyebrow and <h1>.
        shell = lambda body: ('<html><head><title>{t} | Journal | Site</title></head><body><header class="nav"><a href="index.html">Home</a><a href="journal.html">Journal</a></header>'
                              + body + '<footer class="f"><p>Foot</p></footer><aside class="drawer"><a href="index.html">Home</a></aside></body></html>')
        posts = [('hook', 'The Hook', 'Strategy', 'Why hooks.'), ('reach', 'The Reach', 'Case Notes', 'Plan behind it.'), ('aff', 'Affiliate', 'Campaigns', 'Thank you for it.')]
        files = {'index.html': shell('<section class="hero"><h1>Home</h1></section><section><div class="grid">' + ''.join(
                     '<article class="card"><img src="assets/missing-' + k + '.jpg" alt=""><div><h3>' + t + '</h3><p>' + b + '</p><a href="' + k + '.html">Read</a></div></article>'
                     for k, t, c, b in posts) + '</div></section>').replace('{t}', 'Home'),
                 'journal.html': shell('<section><div class="grid" data-equal-grid>' + ''.join(
                     '<article class="card"><img src="assets/' + k + '.png" alt=""><div><span class="label">' + c + '</span><h3>' + t + '</h3><p>' + b + '</p><a href="' + k + '.html">Read</a></div></article>'
                     for k, t, c, b in posts) + '</div></section>').replace('{t}', 'Journal')}
        for k, t, c, b in posts:
            files[k + '.html'] = shell('<section style="padding-top:40px"><div class="wrap"><div class="article"><span class="eyebrow">' + c + ' &middot; 4 min read</span><h1>' + t + '</h1>'
                                       '<img class="hero" src="assets/' + k + '.png" alt=""><p class="lede">Lede of ' + t + ', the opening line.</p>'
                                       + ''.join('<h2>Part ' + str(i) + '</h2><p>' + ('Body ' + k + ' ') * 6 + '</p>' for i in range(4)) + '</div></div></section>').replace('{t}', t)
        (self.dist / 'assets').mkdir(exist_ok=True)
        for k, *_ in posts:
            (self.dist / ('assets/' + k + '.png')).write_bytes(b'png')
        for name in ('index.html', 'about/index.html'):
            (self.dist / name).unlink()
        for name, source in files.items():
            (self.dist / name).write_text(source)
        pages = [{'key': 'front-page', 'file': 'index.html', 'kind': 'front'}, {'key': 'journal', 'file': 'journal.html', 'kind': 'listing'}]
        pages += [{'key': k, 'file': k + '.html', 'kind': 'article', 'title': t + ' | Journal | Site'} for k, t, *_ in posts]
        planner.write(self.manifest, {'chrome': {'header': {'selector': 'header.nav'}, 'trailing': [{'role': 'footer', 'selectors': ['footer.f']}, {'role': 'drawer', 'selectors': ['aside.drawer']}]},
                                      'blog': {'present': True, 'listing': 'journal.html'}, 'pages': pages})
        self.run_cli()
        contract = planner.read(self.ws / 'block-plan/contract.json')
        manifest = planner.read(self.manifest)
        # One single template: the head (eyebrow + native title) around the post content.
        single = json.dumps(contract['templates']['single'])
        self.assertIn('core/post-title', single)
        self.assertIn('core/post-content', single)
        hook = planner.read(self.ws / 'block-plan/pages/hook.json')
        body = json.dumps(hook['blocks'])
        self.assertNotIn('The Hook<', body)
        self.assertIn('Lede of The Hook', body)
        # Each post: its headline as title, the category the design printed, and
        # the summary its listing card prints as excerpt.
        self.assertEqual({p['key']: p['title'] for p in manifest['pages'] if p['kind'] in ('article', 'post')}, {k: t for k, t, *_ in posts})
        reach = planner.read(self.ws / 'block-plan/pages/reach.json')['post']
        listing = json.dumps(planner.read(self.ws / 'block-plan/pages/journal.json')['blocks'])
        self.assertIn('h2wp-query-contents', listing)
        self.assertEqual((reach['categories'], reach['excerpt']), (['Case Notes'], 'Plan behind it.'))
        # A card list whose pictures the source never shipped keeps them unbound.
        home = json.dumps(planner.read(self.ws / 'block-plan/pages/front-page.json')['blocks'])
        self.assertNotIn('h2wp/post-image', home)
        # ...its card picture as featured image, and its place in the listing.
        self.assertEqual((reach['featuredImage'], reach['listingOrder']), ('asset:assets/reach.png', 1))

    def test_moved_inline_style_outranks_source_selectors(self):
        self.plan({'index.html': '<html><body><main><section class="split"><img src="assets/a.png" style="aspect-ratio:1/1" alt=""></section></main></body></html>'}, css='.split img{aspect-ratio:3/4}')
        css = (self.dist / 'assets/gutenberg-inline.css').read_text()
        self.assertRegex(css, r'^(\.h2wp-inline-[0-9a-f]{16}){3}\{aspect-ratio:1/1\}$')

    def test_source_body_classes_ride_with_the_page(self):
        _, proposals, _ = self.plan({'index.html': '<html><body class="v8 lg:flex a<b"><main><section><h1>A</h1></section></main></body></html>',
                                     'about.html': '<html><body><main><section><h1>B</h1></section></main></body></html>'})
        self.assertEqual(proposals['index']['bodyClass'], 'v8 lg:flex')
        self.assertNotIn('bodyClass', proposals['about'])

    def test_font_links_follow_their_page(self):
        a, b = 'https://fonts.googleapis.com/css2?family=A', 'https://fonts.googleapis.com/css2?family=B'
        page = lambda links: '<html><head>' + ''.join('<link rel="stylesheet" href="' + u + '">' for u in links) + '</head><body><main><section><h1>T</h1></section></main></body></html>'
        contract, proposals, _ = self.plan({'index.html': page([a, b]), 'about.html': page([a])})
        self.assertEqual(contract['fontStyles'], [a])
        self.assertEqual(proposals['index']['fontStyles'], [b])
        self.assertNotIn('fontStyles', proposals['about'])

    def test_submit_button_keeps_its_style_attribute(self):
        _, proposals, _ = self.plan({'index.html': '<html><body><main><section><form><input type="email" name="email" aria-label="Email"><button class="btn" type="submit" style="border:0;cursor:pointer">Go</button></form></section></main></body></html>'})
        submit = next(b for b in walk_blocks(proposals['index']['blocks']) if b['name'] == 'h2wp/submit')
        self.assertRegex(submit['attributes']['className'], r'^btn h2wp-inline-[0-9a-f]{16}$')
        self.assertIn('{border:0;cursor:pointer}', (self.dist / 'assets/gutenberg-inline.css').read_text())

    def test_page_styles_keep_each_page_head_order(self):
        (self.dist / 'assets').mkdir(exist_ok=True)
        (self.dist / 'assets/base.css').write_text('body{margin:0}')
        (self.dist / 'assets/type.css').write_text('h1{font-family:serif}')
        head = lambda inner: '<html><head>' + inner + '</head><body><main><section><h1>T</h1></section></main></body></html>'
        base, typ = '<link rel="stylesheet" href="assets/base.css">', '<link href="assets/type.css" rel="stylesheet">'
        contract, proposals, _ = self.plan({
            'index.html': head(base + '<style>.hero{color:red}</style>' + typ),
            'about.html': head(base + '<style>.page{color:blue}</style>' + typ),
            'variant.html': head(base + '<style>:root{--brand:#00aa00}.hero{color:var(--brand)}</style>'),
        })
        # Only the common prefix is global; everything after keeps its place.
        self.assertEqual(contract['styles'][:1], ['assets/base.css'])
        self.assertNotIn('assets/type.css', contract['styles'])
        idx, about, variant = proposals['index']['styles'], proposals['about']['styles'], proposals['variant']['styles']
        self.assertEqual(idx[1], 'assets/type.css')
        self.assertEqual(about[1], 'assets/type.css')
        self.assertEqual(len(variant), 1)
        self.assertEqual((self.dist / idx[0]).read_text(), '.hero{color:red}\n')
        self.assertEqual((self.dist / variant[0]).read_text(), ':root{--brand:#00aa00}.hero{color:var(--brand)}\n')
        self.assertNotEqual(idx[0], about[0])
        # Presets and the token bridge come only from what every page loads.
        self.assertNotIn('tokenBridge', contract)

    def test_recorded_form_success_feedback(self):
        success = {'kind': 'toast', 'html': '<li class="toast">Thanks!</li>', 'text': 'Thanks!', 'list': '<ol class="toasts"></ol>', 'region': '<section role="region" aria-label="Notifications">', 'ms': 4000}
        page = "<html><body><main><section><form id=\"news\" data-spa-success='" + json.dumps(success).replace("'", '&#39;') + "'><input type=\"email\" name=\"email\" placeholder=\"Email\"><button type=\"submit\">Join</button></form></section></main></body></html>"
        _, proposals, _ = self.plan({'index.html': page})
        form = next(self.find(proposals['index']['blocks'], 'h2wp/form'))['attributes']
        self.assertEqual(form['success'], {k: success[k] for k in ('kind', 'html', 'list', 'region', 'ms')})

    def test_a_lone_unnamed_email_tel_or_url_field_is_named_by_its_type(self):
        page = ('<html><body><main><section>'
                '<form id="a"><input type="email" placeholder="Email"><input type="tel"><input placeholder="Name"><button type="submit">Go</button></form>'
                '<form id="b"><input type="email"><input type="email"><button type="submit">Go</button></form>'
                '<form id="c"><input type="email"><input name="email" type="text"><button type="submit">Go</button></form>'
                '</section></main></body></html>')
        _, proposals, findings = self.plan({'index.html': page})
        names = [[f['attributes']['name'] for f in self.find(form.get('innerBlocks', []), 'h2wp/field')] for form in self.find(proposals['index']['blocks'], 'h2wp/form')]
        self.assertEqual(names[0][:2], ['email', 'tel'])
        self.assertTrue(names[0][2].startswith('field-'), 'a text field without a name is not guessed')
        self.assertTrue(all(n.startswith('field-') for n in names[1]), 'two unnamed email fields: neither is guessed')
        self.assertTrue(names[2][0].startswith('field-'), 'the name is taken by another field')
        self.assertTrue(any(f['code'] == 'field-name' for f in findings['index']))

    def test_a_form_whose_unnamed_field_is_named_by_type_raises_nothing(self):
        _, _, findings = self.plan({'index.html': '<html><body><main><section><form><input type="email" placeholder="Email"><button type="submit">Join</button></form></section></main></body></html>'})
        self.assertFalse([f for f in findings['index'] if f['code'] == 'field-name'])

    def test_overlay_before_the_header_and_hero_after_the_headline(self):
        # bruce-banner's shape: a decorative overlay ahead of the shared
        # header, and each article's own picture right after its headline.
        posts = [('a', 'Alpha story'), ('b', 'Beta story'), ('c', 'Gamma story')]
        page = lambda body, pre='': ('<html><body><div class="grain" aria-hidden="true"></div><header class="top"><a href="' + pre + 'index.html">Home</a>'
                                     '<a href="' + pre + 'blog.html">Blog</a></header><main>' + body + '</main><footer class="foot">F</footer></body></html>')
        pages = {'index.html': page('<h1>Home</h1>'), 'blog.html': page('<h1>Blog</h1><div class="list">' + ''.join(
            '<a class="card" href="blog/' + k + '.html"><h2>' + t + '</h2></a>' for k, t in posts) + '</div>')}
        for k, t in posts:
            pages['blog/' + k + '.html'] = page('<article class="post"><h1>' + t + '</h1><img src="../assets/' + k + '.png" alt="' + t + '">'
                                                + ''.join('<p>Paragraph ' + str(i) + ' of ' + k + ' ' + 'words ' * 15 + '</p>' for i in range(2 + len(k))) + '</article>', '../')
        for name in ('index.html', 'about/index.html'):
            (self.dist / name).unlink()
        (self.dist / 'assets').mkdir(exist_ok=True)
        for k, _ in posts:
            (self.dist / 'assets' / (k + '.png')).write_bytes(b'png' + k.encode())
        for name, source in pages.items():
            (self.dist / name).parent.mkdir(parents=True, exist_ok=True)
            (self.dist / name).write_text(source)
        planner.write(self.manifest, {'pages': [{'key': 'index', 'file': 'index.html', 'kind': 'front'}, {'key': 'blog', 'file': 'blog.html', 'kind': 'blog'}]
                                      + [{'key': 'blog-' + k, 'file': 'blog/' + k + '.html', 'kind': 'post'} for k, _ in posts]})
        self.run_cli()
        contract = planner.read(self.ws / 'block-plan/contract.json')
        single = json.dumps(contract['templates']['single'])
        self.assertIn('"core/post-title"', single)
        self.assertIn('"core/post-content"', single)
        self.assertIn('h2wp/post-image', single)
        post = planner.read(self.ws / 'block-plan/pages/blog-a.json')
        self.assertEqual(post['post'].get('featuredImage'), 'asset:assets/a.png')
        self.assertNotIn('Alpha story', json.dumps(post['blocks']))
        checkpoint = planner.read(self.ws / '.gutenberg/checkpoint.json')
        for p in planner.read(self.ws / '.gutenberg/inventory.json')['pages']:
            for f in p['findings']:
                checkpoint['resolutions'][p['key'] + ':' + f['id']] = 'reviewed'
        planner.write(self.ws / '.gutenberg/checkpoint.json', checkpoint)
        for task in planner.read(self.ws / '.gutenberg/tasks.json')['tasks']:
            self.run_cli('claim', '--task=' + task['id'], '--owner=test')
            self.run_cli('complete', '--task=' + task['id'], '--owner=test')
        self.run_cli('finalize')  # coverage complete and in order, the overlay included

    def test_newsletter_email_on_every_page_is_named_without_a_finding(self):
        # A shop's newsletter strip (one unnamed email input in its own form)
        # repeated on the front page, about, journal and an article.
        strip = ('<section class="news"><form class="flex"><input type="email" required placeholder="Email Address" class="flex-1">'
                 '<button type="submit">Subscribe</button></form></section>')
        page = lambda h: '<html><body><main><section><h1>' + h + '</h1></section>' + strip + '</main></body></html>'
        _, proposals, findings = self.plan({'index.html': page('Home'), 'about.html': page('About'), 'journal.html': page('Journal'), 'story.html': page('Story')})
        for key in ('index', 'about', 'journal', 'story'):
            self.assertEqual([f['attributes']['name'] for f in self.find(proposals[key]['blocks'], 'h2wp/field')], ['email'], key)
            self.assertFalse([f for f in findings[key] if f['code'] == 'field-name'], key)

    def test_an_image_recorder_id_is_dropped_unless_an_interaction_targets_it(self):
        page = ('<html><body><main><section><img src="/assets/a.png" alt="" data-spa-id="e1.0">'
                '<img src="/assets/b.png" alt="" data-spa-id="e1.1"><button data-spa-toggle="t1" data-spa-attrs=\'[{"id":"e1.1","attr":"src","off":"/assets/b.png","on":"/assets/a.png"}]\'>x</button></section></main></body></html>')
        _, _, findings = self.plan({'index.html': page})
        flagged = [f['detail'] for f in findings['index'] if f['code'] == 'image-attributes']
        self.assertEqual(len(flagged), 1)
        self.assertIn('e1.1', flagged[0])

    def shop(self, card_as_link=True, sort=True, quantity=True, thumbs=True, notes=None):
        """A small shop: listing + three product pages, written with manifest shop.* hints."""
        products = [('cap', 'Wool Cap', 'Hats', '$20.00', ''), ('mitt', 'Warm Mitts', 'Gloves', '$30.00', '$40.00'), ('scarf', 'Long Scarf', 'Scarves', '$50.00', '')]
        def card(slug, name, cat, price, was):
            amounts = '<span class="now">' + price + '</span>' + ('<span class="was line-through">' + was + '</span>' if was else '')
            inner = ('<div class="frame"><img class="pic" src="/assets/' + slug + '.png" alt=""></div><div class="meta"><p class="chip">' + cat + '</p>'
                     '<h3 class="name">' + name + '</h3><div class="row">' + amounts + '</div></div>')
            if card_as_link:
                return '<a class="card" href="/product/' + slug + '">' + inner + '</a>'
            return ('<div class="card"><a class="pl" href="/product/' + slug + '"><img class="pic" src="/assets/' + slug + '.png" alt=""></a><div class="meta"><p class="chip">' + cat + '</p>'
                    '<h3 class="name"><a href="/product/' + slug + '">' + name + '</a></h3><div class="row">' + amounts + '</div></div></div>')
        def grid(skip=None):
            return '<div class="grid">' + ''.join(card(*p) for p in products if p[0] != skip) + '</div>'
        tabs = '<div class="tabs"><button class="tab on">All</button><button class="tab">Hats</button><button class="tab">Gloves</button><button class="tab">Scarves</button></div>'
        listing = ('<html><body><main><section class="hero"><h1>All pieces</h1></section><section class="bar">' + tabs
                   + ('<label class="lbl">Sort</label><select class="sel"><option>Featured</option><option>Price: low to high</option></select>' if sort else '') + '</section>'
                   '<section class="list">' + grid() + '</section></main></body></html>')
        pages = {'shop.html': listing}
        for slug, name, cat, price, was in products:
            amounts = '<span class="big">' + price + '</span>' + ('<span class="old line-through">' + was + '</span>' if was else '')
            gallery = ('<div class="gal"><div class="box"><img class="main" src="/assets/' + slug + '.png" alt=""></div><div class="thumbs"><button class="t on"><img src="/assets/' + slug + '.png" alt=""></button><button class="t"><img src="/assets/b.png" alt=""></button></div></div>'
                       if thumbs else '<div class="gal"><img class="main" src="/assets/' + slug + '.png" alt=""></div>')
            buy = (('<div class="opt"><p class="lbl">Qty</p><div class="step"><button type="button" aria-label="Decrease">-</button><input type="number" value="1"><button type="button" aria-label="Increase">+</button></div></div>' if quantity else '')
                   + '<button class="buy">Add to cart</button>')
            page_notes = (notes or {}).get(slug, 'Free shipping over $200.')
            pages['product/' + slug + '.html'] = ('<html><body><main><nav class="crumb"><a href="/shop">Shop</a><span>/</span><span class="c">' + cat + '</span></nav>'
                '<section class="top">' + gallery + '<div class="info"><p class="cat">' + cat + '</p><h1 class="title">' + name + '</h1><div class="price">' + amounts + '</div>'
                '<p class="desc">A ' + name.lower() + ' made by hand.</p><p><span class="k">Materials:</span> wool</p>' + buy + '<div class="notes"><p>' + page_notes + '</p></div></div></section>'
                '<section class="more"><div class="head"><h2>You may also like</h2></div>' + grid(skip=slug) + '</section></main></body></html>')
        for name, html_text in pages.items():
            (self.dist / name).parent.mkdir(parents=True, exist_ok=True)
            (self.dist / name).write_text(html_text)
        for name in ('index.html', 'about/index.html'):
            (self.dist / name).unlink()
        entries = [{'key': 'shop', 'file': 'shop.html', 'kind': 'shop'}] + [{'key': 'product-' + p[0], 'file': 'product/' + p[0] + '.html', 'kind': 'product'} for p in products]
        planner.write(self.manifest, {'pages': entries, 'shop': {'present': True, 'productMain': 'main', 'productPrice': 'div.price', 'productBody': 'p.desc', 'productCategory': 'p.cat',
                                                                  'products': ['product/' + p[0] + '.html' for p in products]}})
        self.run_cli()
        contract = planner.read(self.ws / 'block-plan/contract.json')
        findings = {p['key']: p['findings'] for p in planner.read(self.ws / '.gutenberg/inventory.json')['pages']}
        return contract, findings

    def names(self, tree):
        return [b['name'] for b in self.find(tree, lambda b: True)] if False else [b['name'] for b in planner.walk_blocks(tree)]

    def test_shop_templates_come_from_the_listing_and_product_pages(self):
        contract, findings = self.shop()
        catalog, product = contract['templates']['archive-product'], contract['templates']['single-product']
        collection = next(b for b in planner.walk_blocks(catalog) if b['name'] == 'woocommerce/product-collection')
        self.assertTrue(collection['attributes']['query']['inherit'])
        template = collection['innerBlocks'][0]
        self.assertEqual(template['attributes']['className'], 'h2wp-source-layout grid')
        card = self.names(template['innerBlocks'])
        for name in ('core/post-featured-image', 'core/post-terms', 'core/post-title', 'woocommerce/product-price'):
            self.assertIn(name, card)
        self.assertNotIn('h2wp/element', [b['name'] for b in planner.walk_blocks(template['innerBlocks']) if b['attributes'].get('tagName') == 'a'], 'the card link is WooCommerce\'s title and picture links')
        row = next(b for b in planner.walk_blocks(template['innerBlocks']) if b['name'] == 'core/group' and b['attributes'].get('className') == 'row')
        self.assertEqual(row['innerBlocks'][0]['attributes']['className'], 'now', 'the price keeps its row and the amount its classes')
        sort = next(b for b in planner.walk_blocks(catalog) if b['name'] == 'woocommerce/catalog-sorting')
        self.assertEqual(sort['attributes']['className'], 'h2wp-source-control sel')
        menu = next(m for m in contract['menus'] if m['key'] == 'shop-categories')
        self.assertEqual([i['url'] for i in menu['items']], ['page:shop', 'category:Hats', 'category:Gloves', 'category:Scarves'])
        nav = next(b for b in planner.walk_blocks(catalog) if b['name'] == 'h2wp/navigation')
        self.assertEqual((nav['attributes']['linkClassName'], nav['attributes']['currentClassName']), ('tab', 'tab on'))
        names = self.names(product)
        for name in ('core/post-title', 'core/post-content', 'woocommerce/add-to-cart-form', 'woocommerce/product-image-gallery', 'woocommerce/product-price', 'woocommerce/product-reviews'):
            self.assertIn(name, names)
        self.assertEqual(names.count('core/post-terms'), 3, 'breadcrumb, chip, and every card')
        crumb = next(b for b in planner.walk_blocks(product) if b['name'] == 'core/post-terms' and 'c' in b['attributes'].get('className', '').split())
        self.assertIn('h2wp-inline', crumb['attributes']['className'].split(), 'a category printed inline stays inline')
        upsells = next(b for b in planner.walk_blocks(product) if b['attributes'].get('collection') == 'woocommerce/product-collection/upsells')
        self.assertEqual((upsells['attributes']['tagName'], upsells['attributes']['className'], upsells['attributes']['query']['orderBy']), ('section', 'more', 'post__in'))
        self.assertIn('core/heading', self.names(upsells['innerBlocks']), 'the heading is inside the collection')
        text = json.dumps(product)
        self.assertNotIn('Materials', text, 'the spec line is the description\'s')
        self.assertIn('Free shipping', text)
        self.assertNotIn('Add to cart', text)
        price = next(b for b in planner.walk_blocks(product) if b['name'] == 'woocommerce/product-price')
        self.assertEqual(price['attributes']['className'], 'big h2wp-price-in-row')
        for key, page in findings.items():
            self.assertFalse([f for f in page if f['code'] in ('unmapped-element', 'product-template-variant')], key)

    def test_shop_templates_with_linked_parts_and_no_sort_quantity_or_thumbnails(self):
        contract, findings = self.shop(card_as_link=False, sort=False, quantity=False, thumbs=False)
        catalog, product = contract['templates']['archive-product'], contract['templates']['single-product']
        self.assertNotIn('woocommerce/catalog-sorting', self.names(catalog))
        template = next(b for b in planner.walk_blocks(catalog) if b['name'] == 'woocommerce/product-template')
        card = self.names(template['innerBlocks'])
        self.assertEqual(card.count('core/post-featured-image'), 1)
        self.assertEqual(card.count('core/post-title'), 1)
        self.assertIn('woocommerce/add-to-cart-form', self.names(product))
        self.assertIn('woocommerce/product-image-gallery', self.names(product))

    def test_product_pages_that_differ_outside_woocommerce_regions_get_no_template(self):
        contract, findings = self.shop(notes={'mitt': 'Ships next week.'})
        self.assertNotIn('single-product', contract['templates'])
        self.assertIn('archive-product', contract['templates'])
        self.assertTrue([f for f in findings['product-mitt'] if f['code'] == 'product-template-variant'])

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

    def test_element_text_collapses_source_whitespace(self):
        # Indented markup: the editor's rich text would show each newline as a
        # line break (<br>) the frontend never draws.
        _, proposals, _ = self.plan({'index.html': '<body>'
            '<blockquote class="q">\n      Brands need\n      stories.\n      <cite>Ann</cite>\n    </blockquote>'
            '<div class="kicker">\n  01\t\n</div>'
            '<a href="#top" class="btn">Top   of\n page</a>'
            '<pre>keep\n  this</pre></body>'})
        quote, kicker, link, pre = proposals['index']['blocks']
        texts = [b['attributes'].get('text') for b in walk_blocks([quote]) if b['name'] == 'h2wp/element' and 'text' in b['attributes']]
        self.assertEqual(texts, [' Brands need stories. ', 'Ann'])
        self.assertEqual(kicker['attributes']['text'], ' 01 ')
        self.assertEqual(link['attributes']['text'], 'Top of page')
        self.assertIn('keep\n  this', json.dumps(pre['attributes']).replace('\\n', '\n'))

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
        self.assertEqual((code['name'], code['attributes']['content'], code['attributes']['className']), ('core/code', 'let a = 1 &lt; 2;', 'p h2wp-source-pre'))
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
        self.assertRegex(css, r'(?:\.h2wp-inline-[0-9a-f]{16})+\[hidden\]\{display:none\}')
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
        codes = [f['code'] + ':' + item['detail'][:14] for f in findings['index'] for item in planner.finding_items(f)]
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

    def test_excerpt_cut_reads_a_listing_s_own_truncation(self):
        self.assertEqual(planner.excerpt_cut(['Explore advanced CSS tak...', 'Next.js is a powerful Re...']), 'cut:24:...')
        self.assertEqual(planner.excerpt_cut(['Twelve chars…', 'Other twelve…']), 'cut:12:…')
        self.assertIsNone(planner.excerpt_cut(['Short...', 'A longer one...']), 'different lengths are no one rule')
        self.assertIsNone(planner.excerpt_cut(['Whole sentence.', 'Another one.']))

    def test_parse_date_formats(self):
        self.assertEqual(planner.parse_date('May 11, 2026'), ('2026-05-11 12:00:00', 'F j, Y'))
        self.assertEqual(planner.parse_date('Jul 4, 2026'), ('2026-07-04 12:00:00', 'M j, Y'))
        self.assertEqual(planner.parse_date('04 July 2026'), ('2026-07-04 12:00:00', 'd F Y'))
        self.assertEqual(planner.parse_date('2026-05-11'), ('2026-05-11 12:00:00', 'Y-m-d'))
        self.assertEqual(planner.parse_date('May 2026'), ('2026-05-01 12:00:00', 'F Y'))
        self.assertIsNone(planner.parse_date('7 min read'))
        self.assertEqual(planner.parse_date('Thursday, Feb 15, 2024'), ('2024-02-15 12:00:00', 'l, M j, Y'))
        self.assertEqual(planner.parse_date('Friday, September 15, 2023'), ('2023-09-15 12:00:00', 'l, F j, Y'))
        self.assertEqual(planner.parse_date('Thu, Feb 15, 2024'), ('2024-02-15 12:00:00', 'D, M j, Y'))

    def blog_site(self, images=False, shared_description=False, lead=False, dash=False, section_current=False, odd_title=False, stated=None, hand_ordered=False, css=None):
        posts = [('a', 'The catch is where freestyle is won', 'Technique', 'July 14, 2026', '6 min read', 'Catch excerpt that is long enough — and a dash to read.' if dash else 'Catch excerpt that is long enough to read.'),
                 ('b', 'First month for an adult beginner', 'Adult Lessons', 'June 2, 2026', '5 min read', 'Beginner excerpt that is long enough too.'),
                 ('c', 'How to taper for a race', 'Competitive', 'May 11, 2026', '7 min read', 'Taper excerpt that is long enough as well.')]

        def page(body, prefix='', description=''):
            current = ' aria-current="page"' if section_current and prefix else ''
            sheet = '<link rel="stylesheet" href="' + prefix + 'assets/app.css">' if css else ''
            return ('<html><head><title>T</title>' + sheet + '<meta name="description" content="' + description + '"></head><body><div class="min-h-screen">'
                    '<header class="top"><a href="' + prefix + 'index.html" class="brand uppercase">brand</a><nav><a href="' + prefix + 'blog.html"' + current + '>Journal</a></nav></header>'
                    '<main>' + body + '</main><footer class="foot"><p>© Brand</p></footer></div>'
                    '<section aria-label="Notifications" tabindex="-1"></section></body></html>')

        def card(post, prefix, heading='h3'):
            key, title, category, date, read, excerpt = post
            return ('<a href="' + prefix + 'blog/' + key + '.html" class="group block"><span class="cat">' + category + '</span>'
                    '<p class="meta">' + date + '<!-- --> · <!-- -->' + read + '</p><' + heading + ' class="t">' + title + '</' + heading + '>'
                    '<p class="ex">' + excerpt + '</p><span class="more">Read article →</span></a>')
        files = {'index.html': page('<section class="hero"><h1>Home</h1></section><section class="latest"><div class="grid">' + ''.join(card(p, '') for p in (posts[::-1] if hand_ordered else posts)[:2]) + '</div></section>'),
                 'blog.html': page('<section class="intro"><h1>Journal</h1></section><section class="list"><div class="rows">' + ''.join('<div class="row" style="opacity:1">' + card(p if not (odd_title and p[0] == 'c') else (p[0], 'A different card headline', *p[2:]), '', 'h2') + '</div>' for p in (posts[::-1] if hand_ordered else posts)) + '</div></section>')}
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
        if css:
            (self.dist / 'assets').mkdir(exist_ok=True)
            (self.dist / 'assets/app.css').write_text(css)
        (self.dist / 'about/index.html').unlink()
        kinds = [('home', 'index.html', 'front'), ('blog', 'blog.html', 'blog')] + [('blog-' + p[0], 'blog/' + p[0] + '.html', 'post') for p in posts]
        planner.write(self.manifest, {'site': {'name': 'Brand'}, 'pages': [{'key': k, 'file': f, 'kind': kind, **({'post': {'excerpt': stated[k]}} if stated and k in stated else {})} for k, f, kind in kinds]})
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

    def test_a_card_heading_that_differs_from_one_post_title_is_kept_per_post(self):
        # The odd card's words are that post's card title: the listing binds
        # postCardTitle (the title unless a post keeps its own), the article
        # keeps its real title, and the slip is reported as information.
        contract, proposals, findings = self.blog_site(odd_title=True)
        query = next(self.find(proposals['blog']['blocks'], 'core/query'))
        binds = [b['attributes'].get('bind') for b in planner.walk_blocks([query]) if b['name'] == 'h2wp/element']
        self.assertIn('postCardTitle', binds)
        self.assertNotIn('postTitle', binds)
        titles = {k: (p.get('post') or {}).get('cardTitle') for k, p in proposals.items() if k.startswith('blog-')}
        odd = [k for k, v in titles.items() if v]
        self.assertEqual(len(odd), 1)
        self.assertEqual(titles[odd[0]], 'A different card headline')
        self.assertIn('core/post-title', json.dumps(contract['templates']['single']))
        self.assertTrue([f for f in findings['blog'] if f['code'] == 'query-card-title' and 'A different card headline' in f['detail'] and 'kept per post' in f['detail']])

    def test_a_stated_excerpt_cut_short_yields_to_the_card_s_whole_words(self):
        _, proposals, _ = self.blog_site(stated={'blog-a': 'Catch excerpt that is...', 'blog-b': 'A stated excerpt of its own.'})
        self.assertEqual(proposals['blog-a']['post']['excerpt'], 'Catch excerpt that is long enough to read.')
        self.assertNotEqual((proposals['blog-b'].get('post') or {}).get('excerpt'), 'Beginner excerpt that is long enough too.', 'a whole stated excerpt is kept')

    def test_a_hand_ordered_listing_keeps_its_order_and_its_printed_dates(self):
        contract, proposals, _ = self.blog_site(hand_ordered=True)
        self.assertEqual(contract.get('postsOrder'), 'listing')
        self.assertEqual([(proposals['blog-' + k]['post']['date'][:10], proposals['blog-' + k]['post']['listingOrder']) for k in 'cba'], [('2026-05-11', 0), ('2026-06-02', 1), ('2026-07-14', 2)])

    def test_a_newest_first_listing_needs_no_order_of_its_own(self):
        contract, _, _ = self.blog_site()
        self.assertNotIn('postsOrder', contract)

    def test_cards_styled_by_their_place_keep_their_container(self):
        _, proposals, _ = self.blog_site(css='.row:nth-child(2){height:120px}.group{display:block}')
        query = next(self.find(proposals['blog']['blocks'], 'core/query'))
        self.assertEqual(query['attributes'].get('className'), 'h2wp-query-contents')
        holder = next(b for b in planner.walk_blocks(proposals['blog']['blocks']) if query in b.get('innerBlocks', []))
        self.assertEqual((holder['name'], holder['attributes']['className']), ('h2wp/element', 'rows'))

    def test_cards_without_structural_rules_are_the_post_template(self):
        _, proposals, _ = self.blog_site(css='.group{display:block}')
        self.assertNotIn('className', next(self.find(proposals['blog']['blocks'], 'core/query'))['attributes'])

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

    def test_articles_that_mark_their_listing_current_carry_it_to_single_posts(self):
        contract, _, _ = self.blog_site(section_current=True)
        self.assertIs(contract.get('postsCurrent'), True)

    def test_articles_that_mark_nothing_current_leave_the_listing_link_alone(self):
        contract, _, _ = self.blog_site()
        self.assertNotIn('postsCurrent', contract)

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
        self.assertEqual(article['innerBlocks'], [{'name': 'core/post-content', 'attributes': {'className': 'h2wp-contents', 'layout': {'type': 'default'}}}], 'the body takes no box of its own')
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


REFRESH_CSS = '.wrap{max-width:70rem;margin:0 auto}.hero{padding:4rem}.card{border:1px solid #ddd}'


class RefreshTest(unittest.TestCase):
    """`refresh`: a reviewed plan carried over an edit of its source dist."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp.name) / 'ws'
        self.dist = self.ws / 'astro-project/dist'
        self.previous = Path(self.temp.name) / 'dist.prev'
        self.manifest = self.ws / 'conversion-manifest.json'

    def tearDown(self):
        self.temp.cleanup()

    def page(self, main, nav='<a href="/">Home</a><a href="/about/">About</a>', footer='<p>© Studio</p>'):
        return ('<html><head><title>Studio</title><link rel="stylesheet" href="/assets/site.css"></head><body>'
                '<header class="site-head"><nav class="nav">' + nav + '</nav></header><main>' + main + '</main>'
                '<footer class="site-foot">' + footer + '</footer></body></html>')

    def build(self, shape):
        files = {'assets/site.css': REFRESH_CSS}
        if shape == 'pages':
            files['index.html'] = self.page('<section class="hero wrap"><h1>Swim faster</h1><p>Coaching for <strong>every</strong> level.</p><a class="btn" href="/about/">Meet us</a></section>'
                                            '<section class="wrap"><img src="/assets/pool.jpg" alt="Pool"><p>Train with us.</p></section>')
            files['about/index.html'] = self.page('<section class="wrap"><h1>About</h1><p>Since 2009.</p></section>')
            pages = [{'key': 'home', 'file': 'index.html', 'kind': 'front'}, {'key': 'about', 'file': 'about/index.html'}]
        elif shape == 'blog':
            card = '<article class="card"><a href="/{0}/"><h2>{1}</h2></a><p>{2}</p><time>March {3}, 2024</time></article>'
            files['index.html'] = self.page('<section class="wrap"><h1>Journal</h1>' + card.format('one', 'First post', 'The first summary.', 3) + card.format('two', 'Second post', 'The second summary.', 1) + '</section>')
            for slug, title, day in (('one', 'First post', 3), ('two', 'Second post', 1)):
                files[slug + '/index.html'] = self.page('<article class="wrap"><h1>' + title + '</h1><time>March ' + str(day) + ', 2024</time><p>Body of ' + title + '.</p><p>More words.</p></article>')
            pages = [{'key': 'blog', 'file': 'index.html', 'kind': 'blog'}, {'key': 'one', 'file': 'one/index.html', 'kind': 'post'}, {'key': 'two', 'file': 'two/index.html', 'kind': 'post'}]
        elif shape == 'links':
            # Two links to pages the source never shipped: one coalesced finding.
            files['index.html'] = self.page('<section class="wrap"><h1>Links</h1><p><a href="/gone-a/">A</a> <a href="/gone-b/">B</a> <a href="/about/">About</a></p></section>')
            files['about/index.html'] = self.page('<section class="wrap"><h1>About</h1><p>Since 2009.</p></section>')
            pages = [{'key': 'home', 'file': 'index.html', 'kind': 'front'}, {'key': 'about', 'file': 'about/index.html'}]
        else:
            # No shared chrome: body-level sections only, one of them self-contained.
            files['index.html'] = '<html><body><section class="hero"><h1>Plain</h1><p>Text <a href="/contact/">here</a>.</p></section></body></html>'
            files['contact/index.html'] = '<html><body><div class="card"><h1>Contact</h1><p>Mail us.</p></div></body></html>'
            pages = [{'key': 'home', 'file': 'index.html'}, {'key': 'contact', 'file': 'contact/index.html', 'kind': 'page', 'family': 'contact'}]
        for name, text in files.items():
            (self.dist / name).parent.mkdir(parents=True, exist_ok=True)
            (self.dist / name).write_text(text)
        (self.dist / 'assets/pool.jpg').write_bytes(b'\\xff\\xd8\\xff\\xd9')
        planner.write(self.manifest, {'pages': pages})
        self.cli('prepare')
        # Coordinator: resolve every finding and review every family task.
        checkpoint = planner.read(self.ws / '.gutenberg/checkpoint.json')
        for page in planner.read(self.ws / '.gutenberg/inventory.json')['pages']:
            for finding in page['findings']:
                checkpoint['resolutions'][page['key'] + ':' + finding['id']] = 'reviewed and accepted for the test'
        planner.write(self.ws / '.gutenberg/checkpoint.json', checkpoint)
        for task in planner.read(self.ws / '.gutenberg/tasks.json')['tasks']:
            self.cli('claim', '--task=' + task['id'], '--owner=w')
            # A worker edit the refresh must keep.
            if task['id'] == 'family-1':
                path = self.ws / 'block-plan/pages' / (task['representative'] + '.json')
                proposal = planner.read(path)
                proposal['blocks'][0].setdefault('attributes', {}).setdefault('metadata', {})['name'] = 'Reviewed name'
                planner.write(path, proposal)
            self.cli('complete', '--task=' + task['id'], '--owner=w')
        self.cli('finalize')
        # The pre-edit dist, as the runner keeps it before rebuilding.
        shutil.copytree(self.dist, self.previous)

    def cli(self, command, *extra, ok=True):
        result = subprocess.run(['python3', str(SCRIPT), command, '--manifest=' + str(self.manifest), *extra], text=True, capture_output=True)
        self.assertEqual(result.returncode == 0, ok, result.stderr + result.stdout)
        return json.loads(result.stdout) if result.stdout.strip() else result.stderr

    def refresh(self, ok=True):
        return self.cli('refresh', '--previous-dist=' + str(self.previous), ok=ok)

    def edit(self, name, old, new):
        path = self.dist / name
        text = path.read_text()
        self.assertIn(old, text)
        path.write_text(text.replace(old, new))

    def snapshot(self):
        return {str(p.relative_to(self.ws)): p.read_bytes() for p in sorted(self.ws.rglob('*')) if p.is_file()}

    def test_text_edit_costs_no_review_in_every_shape(self):
        for shape, name, old, new, key in (('pages', 'index.html', 'Swim faster', 'Swim further', 'home'),
                                           ('blog', 'one/index.html', 'Body of First post.', 'A rewritten opening.', 'one'),
                                           ('plain', 'index.html', 'Text ', 'Some text ', 'home')):
            with self.subTest(shape=shape):
                shutil.rmtree(self.ws, ignore_errors=True)
                shutil.rmtree(self.previous, ignore_errors=True)
                self.build(shape)
                tasks = planner.read(self.ws / '.gutenberg/tasks.json')
                self.edit(name, old, new)
                self.assertTrue(any('stale source' in f for f in self.cli('check', ok=False)['findings']))
                report = self.refresh()
                self.assertEqual(report['reopened'], [])
                self.assertEqual(report['tasksReopened'], [])
                self.assertIn(key, report['refreshed'])
                self.assertEqual(report['unresolved'], [])
                proposal = json.dumps(planner.read(self.ws / 'block-plan/pages' / (key + '.json')))
                self.assertIn(new.strip(), proposal)
                self.assertNotIn(old.strip(), proposal)
                reviewed = planner.read(self.ws / 'block-plan/pages' / (tasks['tasks'][0]['representative'] + '.json'))
                self.assertEqual(reviewed['blocks'][0]['attributes']['metadata']['name'], 'Reviewed name')
                self.cli('finalize')

    def test_a_coalesced_finding_keeps_its_resolution_until_it_gains_an_item(self):
        # The same text edit leaves the item set as it was; dropping an item
        # leaves only items already reviewed; re-wording one is the old
        # per-finding carry-over. Gaining an item needs a new resolution.
        for label, old, new, carried in (('unchanged', 'Links</h1>', 'Our links</h1>', True),
                                         ('dropped', 'href="/gone-b/">B', 'href="/about/">B', True),
                                         ('re-worded', 'href="/gone-a/">A', 'href="/gone-z/">A', True),
                                         ('gained', 'href="/about/">About</a></p>', 'href="/gone-c/">About</a></p>', False)):
            with self.subTest(label):
                shutil.rmtree(self.ws, ignore_errors=True)
                shutil.rmtree(self.previous, ignore_errors=True)
                self.build('links')
                before = next(f for p in planner.read(self.ws / '.gutenberg/inventory.json')['pages'] for f in p['findings'] if f['code'] == 'unresolved-link')
                self.assertEqual(len(before['items']), 2)
                self.edit('index.html', old, new)
                report = self.refresh()
                self.assertEqual(report['reopened'], [])
                after = next(f for p in planner.read(self.ws / '.gutenberg/inventory.json')['pages'] for f in p['findings'] if f['code'] == 'unresolved-link')
                self.assertEqual(after['id'] == before['id'], label == 'unchanged')
                if carried:
                    self.assertEqual(report['unresolved'], [])
                    self.cli('finalize')
                else:
                    self.assertEqual(report['unresolved'], ['home:' + after['id']])
                    self.assertEqual(len(after['items']), 3)
                    self.assertIn('home: unresolved unresolved-link [' + after['id'] + '] (3 items)', self.cli('finalize', ok=False)['findings'])

    def test_link_and_image_values_are_not_structure(self):
        self.build('pages')
        self.edit('index.html', 'href="/about/">Meet us', 'href="https://example.com/team">Meet us')
        self.edit('index.html', 'src="/assets/pool.jpg" alt="Pool"', 'src="/assets/lane.jpg" alt="Lanes"')
        (self.dist / 'assets/lane.jpg').write_bytes(b'\\xff\\xd8\\xff\\xd9')
        report = self.refresh()
        self.assertEqual((report['refreshed'], report['reopened'], report['contract']), (['home'], [], 'refreshed'))
        data = json.dumps(planner.read(self.ws / 'block-plan/pages/home.json'))
        self.assertIn('https://example.com/team', data)
        self.assertIn('lane.jpg', data)
        self.cli('finalize')

    def test_phrasing_inside_text_is_content(self):
        for label, old, new, shown in (('bold', 'Text ', '<strong>Text</strong> ', '<strong>Text</strong>'),
                                       ('link', '<p>Mail us.</p>', '<p>Mail <a href="/">us</a>.</p>', 'Mail <a href=\\"page:home\\">us</a>.'),
                                       ('line break', 'Mail us.', 'Mail<br>us.', '"tagName": "br"'),
                                       ('recorded span', '<p>Mail us.</p>', '<p>Mail <span data-spa-toggle="menu">us</span>.</p>', None)):
            with self.subTest(label):
                shutil.rmtree(self.ws, ignore_errors=True)
                shutil.rmtree(self.previous, ignore_errors=True)
                self.build('plain')
                self.edit('index.html' if label == 'bold' else 'contact/index.html', old, new)
                report = self.refresh()
                key = 'home' if label == 'bold' else 'contact'
                if shown is None:
                    self.assertEqual((report['reopened'], report['reopenedWhy']), ([key], {key: 'structure'}))
                    continue
                self.assertEqual((report['reopened'], report['refreshed'], report['tasksReopened']), ([], [key], []))
                self.assertIn(shown, json.dumps(planner.read(self.ws / 'block-plan/pages' / (key + '.json'))))
                self.cli('finalize')

    def test_class_only_edit_is_a_restyle(self):
        # A Tailwind class swap on an element that stays, in the shared
        # header and in a page: merged like text, nothing reopened; the
        # reviewed edit and the new classes both survive.
        self.build('pages')
        for name in ('index.html', 'about/index.html'):
            self.edit(name, '<nav class="nav">', '<nav class="nav bg-deep">')
        self.edit('index.html', '<a class="btn" href="/about/">', '<a class="btn btn-deep" href="/about/">')
        self.edit('index.html', '<section class="wrap"><img', '<section><img')
        report = self.refresh()
        self.assertEqual((report['reopened'], report['tasksReopened'], report['contract']), ([], [], 'refreshed'))
        self.assertIn('home', report['refreshed'])
        home = json.dumps(planner.read(self.ws / 'block-plan/pages/home.json'))
        self.assertIn('btn btn-deep', home)
        self.assertIn('bg-deep', json.dumps(planner.read(self.ws / 'block-plan/contract.json')['parts']))
        tasks = planner.read(self.ws / '.gutenberg/tasks.json')
        reviewed = planner.read(self.ws / 'block-plan/pages' / (tasks['tasks'][0]['representative'] + '.json'))
        self.assertEqual(reviewed['blocks'][0]['attributes']['metadata']['name'], 'Reviewed name')
        self.cli('finalize')

    def test_added_element_or_recorder_id_stays_structural(self):
        for label, old, new in (('element', '<p>Since 2009.</p>', '<p>Since 2009.</p><div class="badge"></div>'),
                                ('recorder id', '<section class="wrap"><h1>About</h1>', '<section class="wrap" data-spa-id="e9"><h1>About</h1>')):
            with self.subTest(label):
                shutil.rmtree(self.ws, ignore_errors=True)
                shutil.rmtree(self.previous, ignore_errors=True)
                self.build('pages')
                self.edit('about/index.html', old, new)
                report = self.refresh()
                self.assertEqual((report['reopened'], report['reopenedWhy']), (['about'], {'about': 'structure'}))

    def test_coordinator_files_a_rebuilt_dist_lacks_are_carried(self):
        # The coordinator self-hosted the fonts: a sheet in contract.styles and
        # the woff2 it loads, placed in the dist. A rebuild drops both.
        self.build('pages')
        (self.dist / 'assets/fonts').mkdir(parents=True)
        (self.dist / 'assets/fonts/inter.woff2').write_bytes(b'wOF2')
        (self.dist / 'assets/gutenberg-fonts.css').write_text('@font-face{font-family:Inter;src:url("fonts/inter.woff2") format("woff2")}\n')
        contract = planner.read(self.ws / 'block-plan/contract.json')
        contract['styles'].append('assets/gutenberg-fonts.css')
        planner.write(self.ws / 'block-plan/contract.json', contract)
        self.cli('freeze')
        for task in planner.read(self.ws / '.gutenberg/tasks.json')['tasks']:
            self.cli('claim', '--task=' + task['id'], '--owner=w')
            self.cli('complete', '--task=' + task['id'], '--owner=w')
        self.cli('finalize')
        shutil.rmtree(self.previous)
        shutil.copytree(self.dist, self.previous)
        (self.dist / 'assets/gutenberg-fonts.css').unlink()
        shutil.rmtree(self.dist / 'assets/fonts')
        self.edit('index.html', 'Swim faster', 'Swim further')
        report = self.refresh()
        self.assertEqual(report['carried'], ['assets/gutenberg-fonts.css', 'assets/fonts/inter.woff2'])
        self.assertEqual((report['reopened'], report['tasksReopened']), ([], []))
        self.assertEqual((self.dist / 'assets/fonts/inter.woff2').read_bytes(), b'wOF2')
        self.assertIn('assets/gutenberg-fonts.css', planner.read(self.ws / 'block-plan/contract.json')['styles'])
        self.cli('finalize')
        # Nothing to carry when the rebuilt dist has them.
        shutil.rmtree(self.previous)
        shutil.copytree(self.dist, self.previous)
        self.edit('index.html', 'Swim further', 'Swim fastest')
        self.assertEqual(self.refresh()['carried'], [])

    def test_stylesheet_edit_keeps_reviewed_contract(self):
        self.build('pages')
        contract = planner.read(self.ws / 'block-plan/contract.json')
        contract['themeJson']['reviewed'] = True
        planner.write(self.ws / 'block-plan/contract.json', contract)
        self.cli('freeze')
        for task in planner.read(self.ws / '.gutenberg/tasks.json')['tasks']:
            self.cli('claim', '--task=' + task['id'], '--owner=w')
            self.cli('complete', '--task=' + task['id'], '--owner=w')
        self.cli('finalize')
        shutil.rmtree(self.previous)
        shutil.copytree(self.dist, self.previous)
        self.edit('assets/site.css', 'padding:4rem', 'padding:5rem')
        self.assertIn('shared CSS/JS changed: coordinator must freeze and re-review workers', self.cli('check', ok=False)['findings'])
        report = self.refresh()
        self.assertEqual((report['refreshed'], report['reopened'], report['unchanged'], report['assets']), ([], [], ['home', 'about'], 'refreshed'))
        self.assertTrue(planner.read(self.ws / 'block-plan/contract.json')['themeJson']['reviewed'])
        self.cli('finalize')

    def test_structural_edit_reopens_only_that_page(self):
        self.build('plain')
        self.edit('contact/index.html', '<p>Mail us.</p>', '<ul class="list"><li>Mail us.</li></ul>')
        report = self.refresh()
        self.assertEqual((report['reopened'], report['refreshed'], report['unchanged'], report['contract']), (['contact'], [], ['home'], 'refreshed'))
        self.assertEqual(report['reopenedWhy'], {'contact': 'structure'})
        tasks = {t['id']: t for t in planner.read(self.ws / '.gutenberg/tasks.json')['tasks']}
        self.assertEqual(report['tasksReopened'], ['family-2'])
        self.assertEqual((tasks['family-1']['status'], tasks['family-2']['status']), ('complete', 'pending'))
        findings = self.cli('finalize', ok=False)['findings']
        self.assertEqual(findings, ['family-2: worker review incomplete or stale'])
        self.cli('claim', '--task=family-2', '--owner=w')
        self.cli('complete', '--task=family-2', '--owner=w')
        self.cli('finalize')

    def test_a_reshaped_review_is_kept_aside_when_it_cannot_merge(self):
        self.build('plain')
        path = self.ws / 'block-plan/pages/home.json'
        proposal = planner.read(path)
        # The worker adds a block of its own: the block list no longer aligns.
        proposal['blocks'].append({'name': 'core/separator', 'attributes': {}})
        planner.write(path, proposal)
        self.cli('freeze')
        for task in planner.read(self.ws / '.gutenberg/tasks.json')['tasks']:
            self.cli('claim', '--task=' + task['id'], '--owner=w')
            self.cli('complete', '--task=' + task['id'], '--owner=w')
        self.cli('finalize')
        shutil.rmtree(self.previous)
        shutil.copytree(self.dist, self.previous)
        self.edit('index.html', 'Plain', 'Planar')
        report = self.refresh()
        self.assertEqual((report['reopened'], report['refreshed']), (['home'], []))
        self.assertTrue(report['reopenedWhy']['home'].startswith('unmerged '))
        self.assertEqual(planner.read(self.ws / '.gutenberg/displaced/home.json'), proposal)
        self.assertIn('Planar', json.dumps(planner.read(path)))

    def test_chrome_structural_edit_reopens_the_contract(self):
        self.build('pages')
        for name in ('index.html', 'about/index.html'):
            self.edit(name, '<a href="/about/">About</a></nav>', '<a href="/about/">About</a><a class="cta" href="/join/">Join</a></nav>')
        report = self.refresh()
        self.assertEqual(report['contract'], 'reopened')
        self.assertIn('contract changed: coordinator must run freeze after reviewing shared changes', self.cli('check', ok=False)['findings'])
        self.assertIn('Join', json.dumps(planner.read(self.ws / 'block-plan/contract.json')['parts']))
        self.cli('freeze')

    def test_chrome_text_edit_stays_frozen(self):
        self.build('blog')
        for name in ('index.html', 'one/index.html', 'two/index.html'):
            self.edit(name, '© Studio', '© Studio 2026')
        report = self.refresh()
        self.assertEqual((report['contract'], report['reopened']), ('refreshed', []))
        self.assertIn('Studio 2026', json.dumps(planner.read(self.ws / 'block-plan/contract.json')['parts']))
        self.cli('finalize')

    def test_unchanged_dist_is_a_no_op(self):
        self.build('blog')
        before = self.snapshot()
        report = self.refresh()
        self.assertEqual((report['refreshed'], report['reopened'], report['contract'], report['assets']), ([], [], 'unchanged', 'unchanged'))
        self.assertEqual(sorted(report['unchanged']), ['blog', 'one', 'two'])
        self.assertEqual(self.snapshot(), before)

    def test_refuses_a_foreign_previous_dist_and_running_workers(self):
        self.build('plain')
        self.edit('index.html', 'Plain', 'Other')
        shutil.rmtree(self.previous)
        shutil.copytree(self.dist, self.previous)
        self.assertIn('not the dist this plan was prepared from', self.refresh(ok=False))
        self.assertIn('--previous-dist', self.cli('refresh', ok=False))
        shutil.rmtree(self.previous)
        shutil.copytree(self.dist, self.previous)
        self.edit('index.html', 'Other', 'Plain')
        self.cli('freeze')
        self.cli('claim', '--task=family-1', '--owner=w')
        self.edit('index.html', 'Plain', 'Other')
        self.assertIn('running workers', self.refresh(ok=False))


if __name__ == '__main__':
    unittest.main()
