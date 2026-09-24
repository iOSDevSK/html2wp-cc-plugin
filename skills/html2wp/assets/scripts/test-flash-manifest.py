#!/usr/bin/env python3
"""Flash's stage 0: flash-manifest.py drafts, check-manifest.py refuses what
is left undecided.

A small site — a front page, an about page, a blog listing and two articles
under blog/, a contact form whose fields have no name, a header menu, and a
`main.wrap` rule in the stylesheet — is analysed by the real
analyze-input.mjs, then:

- the draft shares the header and footer across the pages (the desktop app's
  Flash chrome rule) and sets design.templateMainClass "wrap";
- the candidates hold the blog family with its listing, the card grid
  (`div.cards`) and the card (`a.card`), the header menu, and the form with
  its three unnamed fields;
- check-manifest.py --flash refuses the draft (nav, blog, shop and forms
  undecided, the form unlisted), accepts it once they are written, refuses a
  reason that argues from a count, and refuses a card container that matches
  twice on the listing;
- a consensus front page inside a full-height shell is refused in any mode.

  python3 test-flash-manifest.py
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent

HEAD = '<head><title>{t} | Studio North</title><link rel="stylesheet" href="assets/site.css"></head>'
TITLES = {'index.html': 'Design that lasts', 'about.html': 'About', 'journal.html': 'Journal',
          'blog/first.html': 'First', 'blog/second.html': 'Second', 'contact.html': 'Contact'}
HEADER = ('<header class="top"><nav class="menu"><a href="index.html">Home</a> <a href="about.html">About</a> '
          '<a href="journal.html">Journal</a></nav></header>')
FOOTER = '<footer class="foot"><p>Studio North</p></footer>'
PAGES = {
    'index.html': '<main class="wrap"><h1>Studio North</h1><p>We design things.</p></main>',
    'about.html': '<main class="wrap"><h1>About</h1><p>Since 2001.</p></main>',
    'journal.html': ('<main class="wrap"><h1>Journal</h1><div class="cards">'
                     '<a class="card" href="blog/first.html"><h2>First</h2><p>One</p></a>'
                     '<a class="card" href="blog/second.html"><h2>Second</h2><p>Two</p></a></div></main>'),
    'blog/first.html': '<main class="wrap"><article><h1>First</h1><p>Body one.</p></article></main>',
    'blog/second.html': '<main class="wrap"><article><h1>Second</h1><p>Body two.</p></article></main>',
    'contact.html': ('<main class="wrap"><h1>Contact</h1><form class="contact-form">'
                     '<label for="n">Your name</label><input id="n" type="text">'
                     '<input id="e" type="email" placeholder="Email"><textarea id="m"></textarea>'
                     '<button type="submit">Send</button></form></main>'),
}


def build_site(root):
    site = root / 'site'
    for rel, main in PAGES.items():
        depth = '../' * rel.count('/')
        html = ('<!doctype html><html>' + HEAD.format(t=TITLES[rel]).replace('assets/', depth + 'assets/')
                + '<body>' + HEADER.replace('href="', 'href="' + depth) + main + FOOTER + '</body></html>')
        (site / rel).parent.mkdir(parents=True, exist_ok=True)
        (site / rel).write_text(html)
    (site / 'assets').mkdir(exist_ok=True)
    (site / 'assets/site.css').write_text(':root{--ink:#111111;--paper:#fafafa}main.wrap{padding-top:56px}.card{display:block}')
    return site


def run(*argv):
    return subprocess.run([str(a) for a in argv], capture_output=True, text=True, timeout=120)


class FlashManifest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        cls.site, cls.ws = build_site(root), root / 'ws'
        cls.ws.mkdir()
        analysed = run('node', HERE / 'analyze-input.mjs', cls.site, f'--out={cls.ws / "analysis.json"}')
        assert analysed.returncode == 0, analysed.stdout + analysed.stderr
        drafted = run(sys.executable, HERE / 'flash-manifest.py', '--analysis', cls.ws / 'analysis.json',
                      '--input', cls.site, '--workspace', cls.ws)
        assert drafted.returncode == 0, drafted.stdout + drafted.stderr
        cls.manifest = json.loads((cls.ws / 'conversion-manifest.json').read_text())
        cls.candidates = json.loads((cls.ws / 'flash-candidates.json').read_text())

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def check(self, manifest, *flags):
        path = self.ws / 'm-under-test.json'
        path.write_text(json.dumps(manifest))
        done = run(sys.executable, HERE / 'check-manifest.py', '--manifest', path,
                   '--candidates', self.ws / 'flash-candidates.json', *flags)
        return done.returncode, json.loads(done.stdout)

    def decided(self):
        m = json.loads(json.dumps(self.manifest))
        kinds = {'journal.html': 'listing', 'blog/first.html': 'article', 'blog/second.html': 'article'}
        for page in m['pages']:
            page['kind'] = kinds.get(page['file'], page['kind'])
        blog = self.candidates['blog'][0]
        m['blog'] = {'present': True, 'listing': blog['listing'], 'articles': blog['articles'],
                     'cardContainer': blog['cardContainer'], 'cardSelector': blog['cardSelector']}
        m['shop'] = {'present': False, 'reason': 'a studio site: nothing is sold here'}
        nav = next(n for n in self.candidates['nav'] if n['suggest'])
        m['nav'] = [{'selector': nav['selector'], 'region': nav['region'], 'label': 'Main menu', 'links': nav['links']}]
        m['forms'] = [{'page': f['page'], 'selector': f['selector'], 'purpose': f['purpose']} for f in self.candidates['forms']]
        return m

    def test_the_draft(self):
        self.assertEqual({p['chrome'] for p in self.manifest['pages']}, {'consensus'})
        self.assertEqual(self.manifest['design']['templateMainClass'], 'wrap')
        self.assertEqual((self.manifest['site']['name'], self.manifest['site']['slug']), ('Studio North', 'studio-north'))
        for key in ('nav', 'blog', 'shop', 'forms'):
            self.assertNotIn(key, self.manifest, 'the draft decides none of these')

    def test_the_candidates(self):
        blog = self.candidates['blog']
        self.assertEqual(len(blog), 1)
        self.assertEqual((blog[0]['listing'], blog[0]['cardContainer'], blog[0]['cardSelector'], blog[0]['cards']),
                         ('journal.html', 'div.cards', 'a.card', 2))
        self.assertEqual(blog[0]['articles'], ['blog/first.html', 'blog/second.html'])
        self.assertTrue(any(n['suggest'] and n['region'] == 'header' for n in self.candidates['nav']))
        form = self.candidates['forms'][0]
        self.assertEqual((form['page'], form['selector'], form['purpose'], form['unnamed']),
                         ('contact.html', 'form.contact-form', 'contact', 3))
        self.assertEqual([f['label'] for f in form['fields']], ['Your name', 'Email', ''])

    def test_undecided_is_refused_and_decided_passes(self):
        code, out = self.check(self.manifest, '--flash')
        self.assertEqual(code, 1)
        text = ' '.join(out['errors'])
        for word in ('nav: not decided', 'blog: not decided', 'shop: not decided', 'forms: not decided', 'contact.html has a form'):
            self.assertIn(word, text)
        code, out = self.check(self.decided(), '--flash')
        self.assertEqual((code, out['errors']), (0, []))

    def test_a_form_purpose_is_one_visual_edit_lite_knows(self):
        m = self.decided()
        m['forms'][0]['purpose'] = 'newsletter'
        code, out = self.check(m, '--flash')
        self.assertEqual(code, 1)
        self.assertIn("purpose 'newsletter'", ' '.join(out['errors']))

    def test_a_count_is_not_a_reason(self):
        m = self.decided()
        m['blog'] = {'present': False, 'reason': 'only two articles'}
        code, out = self.check(m, '--flash')
        self.assertEqual(code, 1)
        self.assertIn('argues from a count', ' '.join(out['errors']))

    def test_a_card_container_that_matches_twice(self):
        m = self.decided()
        m['blog']['cardContainer'] = 'main.wrap > div'
        (self.site / 'journal.html').write_text((self.site / 'journal.html').read_text().replace(
            '</main>', '<div class="extra"></div></main>'))
        try:
            code, out = self.check(m)
            self.assertEqual(code, 1)
            self.assertIn('matches 2 elements', ' '.join(out['errors']))
        finally:
            (self.site / 'journal.html').write_text((self.site / 'journal.html').read_text().replace(
                '<div class="extra"></div>', ''))

    def test_a_consensus_front_page_in_a_full_height_shell(self):
        front = self.site / 'index.html'
        before = front.read_text()
        front.write_text(before.replace('<body>', '<body><div class="min-h-screen">').replace('</body>', '</div></body>'))
        try:
            code, out = self.check(self.decided())
            self.assertEqual(code, 1)
            self.assertIn('viewport-height wrapper', ' '.join(out['errors']))
        finally:
            front.write_text(before)


if __name__ == '__main__':
    unittest.main()
