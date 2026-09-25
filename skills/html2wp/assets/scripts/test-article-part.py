#!/usr/bin/env python3
"""The article layout from the site's own article (Flash stage 3.5).

Run B replayed: an article page with no <article> whose headline sits in its
own <header> inside <main> — the service read that header as the site's
chrome, shipped the generic layout, and make-zip refused it, so the run
stopped with no theme. Here:

- make-zip refuses the generic part and names the failure
  (`h2wp-signature: article-part-foreign`);
- article-part.py derives the part from the article page — title, date,
  image and body as [wp-article] fields, the article's own <header> kept, the
  specimen appended, the image pointed at the theme's copy, the back link at
  the listing's permalink — and make-zip passes;
- a <main> that wraps the site's header and footer (the site chrome inside
  the region) derives without them;
- theme-zip.sh (stage 3.5) derives it and packs <slug>-<version>.zip;
- a layout the service derived is kept; --force replaces it;
- a page with no headline cannot be derived: exit 1 with the signature.

  python3 test-article-part.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent

CSS = ('.page{max-width:60rem}.post{padding:2rem}.post-header{margin:0}.kicker{font-size:.8rem}'
       '.post-title{font-size:3rem}.post-date{color:#666}.post-hero{width:100%}.post-body{line-height:1.7}'
       '.post-back{display:block}.site-header{height:4rem}.site-footer{padding:2rem}')
GENERIC = ('<!-- wp:html -->\n<!-- clara-ve-key: article -->\n<article class="clara-article">'
           '<h1 class="clara-article__title">[wp-article field="title"]T[/wp-article]</h1>'
           '<div class="clara-article__body">[wp-article field="content"]<p>x</p>[/wp-article]</div></article>\n'
           '<div data-cve-specimen hidden aria-hidden="true"></div>\n<!-- /wp:html -->\n')
SITE_HEADER = ('<header class="site-header"><nav><a href="../index.html">Home</a><a href="../blog.html">Journal</a>'
               '<a href="../contact.html">Contact</a></nav></header>')
SITE_FOOTER = '<footer class="site-footer"><a href="../index.html">Home</a><a href="../contact.html">Contact</a></footer>'
ARTICLE = ('<div class="post"><header class="post-header"><p class="kicker">Notes</p>'
           '<h1 class="post-title">How the label tripled its reach</h1>'
           '<time class="post-date" datetime="2026-07-01">July 1, 2026</time></header>'
           '<img class="post-hero" src="../images/hero.jpg" alt="">'
           '<div class="post-body"><p>First paragraph of the story.</p><p>Second paragraph.</p></div>'
           '<a class="post-back" href="../blog.html">Back to the journal</a></div>')


def run(*argv, env=None):
    return subprocess.run([str(a) for a in argv], capture_output=True, text=True, timeout=300,
                          env={**{k: v for k, v in os.environ.items() if not k.startswith('H2WP_')}, **(env or {})})


def workspace(root, page_html, derived=False, part=GENERIC):
    ws = root / 'work space'
    theme = ws / 'theme' / 'label'
    for d in ('clara-content/sources', 'templates', 'parts', 'images'):
        (theme / d).mkdir(parents=True, exist_ok=True)
    (theme / 'style.css').write_text('/*\nTheme Name: Label\nVersion: 1.0.0\n*/\n' + CSS)
    (theme / 'theme.json').write_text('{"version": 3}')
    (theme / 'functions.php').write_text('<?php\n')
    (theme / 'templates' / 'index.html').write_text('<!-- wp:post-content /-->')
    (theme / 'images' / 'hero.jpg').write_bytes(b'\xff\xd8jpeg')
    (theme / 'parts' / 'header.html').write_text('<!-- wp:html -->\n' + SITE_HEADER.replace('../', '/') + '\n<!-- /wp:html -->\n')
    (theme / 'parts' / 'footer.html').write_text('<!-- wp:html -->\n' + SITE_FOOTER.replace('../', '/') + '\n<!-- /wp:html -->\n')
    (theme / 'parts' / 'article.html').write_text(part)
    (theme / 'clara-content' / 'sources' / 'front-page.html').write_text('<h1>Label</h1>')
    (theme / 'clara-content' / 'sources' / 'blog.html').write_text('<section>[wp-posts count="6"]<a href="{url}">{title}</a>[/wp-posts]</section>')
    (theme / 'clara-content' / 'posts.json').write_text(json.dumps([{'title': 'How the label tripled its reach', 'slug': 'reach'}]))
    from PIL import Image
    Image.new('RGB', (1200, 900), (240, 240, 240)).save(theme / 'screenshot.png')
    dist = ws / 'astro-project' / 'dist'
    (dist / 'blog').mkdir(parents=True)
    (dist / 'blog' / 'reach.html').write_text(page_html)
    (ws / 'theme-report.json').write_text(json.dumps({'articlePart': {'derived': derived, 'derivedFrom': None}}))
    (ws / 'conversion-manifest.json').write_text(json.dumps({
        'schema': 'html2wp/1', 'site': {'name': 'Label', 'slug': 'label', 'version': '1.0.0'}, 'workspace': str(ws),
        'pages': [{'file': 'index.html', 'key': 'front-page', 'kind': 'front', 'title': 'Label', 'chrome': 'consensus'},
                  {'file': 'blog.html', 'key': 'blog', 'kind': 'listing', 'title': 'Journal', 'chrome': 'consensus'},
                  {'file': 'blog/reach.html', 'key': 'blog-reach', 'kind': 'article', 'title': 'Reach', 'chrome': 'consensus'}],
        'chrome': {'header': {'selector': 'header'}, 'footer': {'selector': 'footer'}},
        'blog': {'present': True, 'listing': 'blog.html', 'articles': ['blog/reach.html'],
                 'cardContainer': 'section', 'cardSelector': 'a'}}))
    return ws, theme


def make_zip(ws, theme):
    return run('bash', HERE / 'make-zip.sh', theme, ws / 'label-1.0.0.zip',
               env={'MAKE_ZIP_MANIFEST': str(ws / 'conversion-manifest.json')})


class ArticlePart(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def derive(self, *args):
        return run(sys.executable, HERE / 'article-part.py', self.ws, *args)

    def test_run_b_the_nested_header_article_packages(self):
        page = f'<html><body>{SITE_HEADER}<main class="page">{ARTICLE}</main>{SITE_FOOTER}</body></html>'
        self.ws, theme = workspace(Path(self.tmp.name), page)
        refused = make_zip(self.ws, theme)
        self.assertNotEqual(refused.returncode, 0, 'the generic layout is refused')
        self.assertIn('h2wp-signature: article-part-foreign', refused.stderr)
        done = self.derive()
        self.assertEqual(done.returncode, 0, done.stderr)
        part = (theme / 'parts' / 'article.html').read_text()
        self.assertTrue(part.startswith('<!-- wp:html -->\n<!-- clara-ve-key: article -->\n'))
        self.assertIn('<h1 class="post-title">[wp-article field="title"]How the label tripled its reach[/wp-article]</h1>', part)
        self.assertIn('<time class="post-date" datetime="2026-07-01">[wp-article field="date"]July 1, 2026[/wp-article]</time>', part)
        self.assertIn('[wp-article field="image"]<img class="post-hero" src="__CLARA_THEME_URI__/images/hero.jpg" alt="">[/wp-article]', part)
        self.assertIn('<div class="post-body">[wp-article field="content"]<p>First paragraph', part)
        self.assertIn('<header class="post-header">', part, "the article's own header stays")
        self.assertIn('href="/blog/"', part, 'the back link is the listing\'s permalink')
        self.assertIn('data-cve-specimen', part)
        self.assertNotIn('site-header', part)
        self.assertNotIn('<main', part)
        self.assertNotRegex(part, r'\[wp-article[^\]]*\][^\[]*\[wp-article', 'no nested tokens')
        report = json.loads((self.ws / 'article-part.json').read_text())
        self.assertEqual(report['placed'], ['content', 'title', 'date', 'image'])
        built = make_zip(self.ws, theme)
        self.assertEqual(built.returncode, 0, built.stderr)

    def test_stage_3_5_is_one_command_and_packs_what_stage_5_installs(self):
        page = f'<html><body>{SITE_HEADER}<main class="page">{ARTICLE}</main>{SITE_FOOTER}</body></html>'
        self.ws, theme = workspace(Path(self.tmp.name), page)
        done = run('bash', HERE / 'theme-zip.sh', self.ws)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue((self.ws / 'label-1.0.0.zip').is_file())
        self.assertEqual(done.stdout.strip().splitlines()[-1], str(self.ws / 'label-1.0.0.zip'))

    def test_a_main_that_wraps_the_site_chrome_derives_without_it(self):
        page = f'<html><body><main class="page">{SITE_HEADER}{ARTICLE}{SITE_FOOTER}</main></body></html>'
        self.ws, theme = workspace(Path(self.tmp.name), page)
        done = self.derive()
        self.assertEqual(done.returncode, 0, done.stderr)
        part = (theme / 'parts' / 'article.html').read_text()
        self.assertNotIn('site-header', part)
        self.assertNotIn('site-footer', part)
        self.assertIn('[wp-article field="title"]', part)
        self.assertEqual(make_zip(self.ws, theme).returncode, 0)

    def test_the_services_own_layout_is_kept_unless_forced(self):
        derived = ('<!-- wp:html -->\n<!-- clara-ve-key: article -->\n<div class="post"><h1 class="post-title">'
                   '[wp-article field="title"]x[/wp-article]</h1><div class="post-body">[wp-article field="content"]'
                   '<p>x</p>[/wp-article]</div></div>\n<div data-cve-specimen hidden></div>\n<!-- /wp:html -->\n')
        page = f'<html><body>{SITE_HEADER}<main class="page">{ARTICLE}</main></body></html>'
        self.ws, theme = workspace(Path(self.tmp.name), page, derived=True, part=derived)
        kept = self.derive()
        self.assertEqual(kept.returncode, 0)
        self.assertIn('kept', kept.stdout)
        self.assertEqual((theme / 'parts' / 'article.html').read_text(), derived)
        self.assertEqual(self.derive('--force').returncode, 0)
        self.assertIn('post-header', (theme / 'parts' / 'article.html').read_text())

    def test_no_headline_no_layout(self):
        page = f'<html><body><main class="page"><div class="post-body"><p>a</p><p>b</p></div></main></body></html>'
        self.ws, theme = workspace(Path(self.tmp.name), page)
        done = self.derive()
        self.assertEqual(done.returncode, 1)
        self.assertIn('h2wp-signature: article-part-foreign', done.stderr)
        self.assertEqual((theme / 'parts' / 'article.html').read_text(), GENERIC, 'nothing written')


if __name__ == '__main__':
    unittest.main()
