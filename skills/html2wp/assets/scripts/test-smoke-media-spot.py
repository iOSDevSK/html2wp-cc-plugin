#!/usr/bin/env python3
"""Where smoke-editor.py's mediaReachable step clicks (_MEDIA_SPOT_JS).

The step must click the main image where no editable content lies over it,
because selecting the words you clicked is correct: a left-aligned poster
headline (one display:block span per word, each as wide as the column)
covered both fixed probes on a real hero and the step blamed a "decorative
element" for a photograph that selects fine. It must still click THROUGH
kind-less layers (the tint over every hero photograph), because a kind-less
layer answering the click is the defect the step exists for.

Fixtures stamp data-cve-kind the way the editor bridge does. No WordPress.

  python3 test-smoke-media-spot.py
"""
import ast
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

SCRIPT = Path(__file__).with_name('smoke-editor.py')
SPOT_JS = next(
    ast.literal_eval(node.value)
    for node in ast.parse(SCRIPT.read_text()).body
    if isinstance(node, ast.Assign) and any(getattr(t, 'id', '') == '_MEDIA_SPOT_JS' for t in node.targets)
)

# A 1200x600 photograph in a hero; BODY is laid over it.
HERO = """<!doctype html><html><head><style>
body{margin:0;font-family:sans-serif}
.hero{position:relative;width:1200px;height:600px;overflow:hidden}
.hero img{display:block;width:1200px;height:600px}
.layer{position:absolute;inset:0}
%s
</style></head><body><section class="hero">
<img data-cve-kind="image" data-cve-path="p1" src="data:image/svg+xml,%%3Csvg xmlns='http://www.w3.org/2000/svg' width='1200' height='600'/%%3E">
%s
</section><p data-cve-kind="text" data-cve-path="p9">after</p></body></html>"""

TINT = '<div class="layer" data-cve-path="p2" style="background:rgba(0,0,0,.35)"></div>'


class MediaSpot(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        cls.page = cls.browser.new_page(viewport={'width': 1400, 'height': 900})

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def spot(self, css, body):
        self.page.set_content(HERO % (css, body))
        return self.page.evaluate(SPOT_JS)

    def assert_clear(self, spot):
        """The point is on the image and no kinded element lies above it there."""
        above = self.page.evaluate("""(s) => {
          const img = document.querySelector('[data-cve-smoke-media]');
          const r = img.getBoundingClientRect();
          const stack = document.elementsFromPoint(r.left + s.x, r.top + s.y);
          const at = stack.indexOf(img);
          return at < 0 ? 'image not under point' : stack.slice(0, at).filter((e) => e.hasAttribute('data-cve-kind')).map((e) => e.tagName);
        }""", spot)
        self.assertEqual(above, [])

    def test_tint_only_keeps_the_fixed_probe(self):
        # The decorative tint is kind-less: it never moves the click, so the
        # step still clicks through it and still fails if the tint answers.
        s = self.spot('', TINT)
        self.assertEqual((s['x'], s['y']), (216, 210))
        self.assertTrue(s['clear'])
        self.assertNotIn('covered', s)

    def test_centred_title_away_from_the_probe_keeps_the_fixed_probe(self):
        s = self.spot('.t{position:absolute;left:400px;top:260px;width:400px;font-size:40px;text-align:center}',
                      TINT + '<h1 class="t" data-cve-kind="text" data-cve-path="p3">Name</h1>')
        self.assertEqual((s['x'], s['y']), (216, 210))

    def test_poster_headline_moves_the_click_off_the_words(self):
        # One block span per word, each as wide as the 1000px column: the
        # text boxes, not the glyphs, cover the probe and most of the photo.
        s = self.spot('.c{position:absolute;left:48px;top:40px;width:1000px}'
                      '.c span{display:block;font-size:130px;line-height:1}'
                      '.c p{width:380px;margin:20px 0 0}',
                      TINT + '<div class="c" data-cve-path="p4"><h1 data-cve-kind="text" data-cve-path="p5" style="margin:0">'
                      '<span data-cve-kind="text" data-cve-path="p6">BRUCE</span>'
                      '<span data-cve-kind="text" data-cve-path="p7">BANNER</span>'
                      '<span data-cve-kind="text" data-cve-path="p8">STUDIO</span></h1>'
                      '<p data-cve-kind="text" data-cve-path="p10">A lede that sits under the headline.</p></div>')
        self.assertNotEqual((s['x'], s['y']), (216, 210))
        self.assertIn('span', s['movedFrom'])
        self.assertGreater(s['clear'], 0)
        self.assertLess(s['clear'], s['measured'])
        self.assert_clear(s)

    def test_right_aligned_copy_block(self):
        s = self.spot('.c{position:absolute;right:0;top:0;bottom:0;width:70%}',
                      TINT + '<div class="c" data-cve-kind="text" data-cve-path="p3">copy</div>')
        self.assertEqual((s['x'], s['y']), (216, 210))
        s = self.spot('.c{position:absolute;left:0;top:0;bottom:0;width:70%}',
                      TINT + '<div class="c" data-cve-kind="text" data-cve-path="p3">copy</div>')
        self.assertGreaterEqual(s['x'], 0.7 * 1200)
        self.assert_clear(s)

    def test_a_menu_over_the_image_moves_the_click(self):
        # A header nav laid over the hero is un-stamped while a page is edited
        # (no kind), but the editor routes a click on it to the menu first.
        nav = ('<nav data-ve-nav="1" style="position:absolute;left:150px;top:150px;width:200px">'
               '<a href="#" style="display:block;height:120px">Work</a></nav>')
        self.page.set_content(HERO % ('', TINT + nav))
        self.page.evaluate("window.claraVeBridgeConfig = {menuManaged: true, menuZones: [{selector: '[data-ve-nav=\"1\"]', location: 'x'}]}")
        s = self.page.evaluate(SPOT_JS)
        self.assertIn('<nav', s['movedFrom'])
        self.assertNotEqual((s['x'], s['y']), (216, 210))
        self.assertFalse(150 <= s['x'] < 350 and 150 <= s['y'] < 270)
        # Without menu management the nav is not a zone and the probe stands.
        self.page.evaluate("window.claraVeBridgeConfig = {menuManaged: false, menuZones: [{selector: '[data-ve-nav=\"1\"]'}]}")
        self.assertEqual(self.page.evaluate(SPOT_JS), {'x': 216, 'y': 210, 'clear': True})

    def test_a_wordpress_managed_zone_over_the_image_moves_the_click(self):
        s = self.spot('.z{position:absolute;left:100px;top:100px;width:400px;height:300px}',
                      TINT + '<div class="z" data-cve-skip="1" data-cve-zone="posts" data-cve-path="p3"><p>post</p></div>')
        self.assertIn('class="z"', s['movedFrom'])
        self.assertFalse(100 <= s['x'] < 500 and 100 <= s['y'] < 400)

    def test_probe_outside_the_viewport_is_left_to_the_click(self):
        # No hit stack exists there to read; the click scrolls to it as before.
        s = self.spot('.hero{margin-top:1000px}.c{position:absolute;inset:0}',
                      '<div class="c" data-cve-kind="text" data-cve-path="p3">copy</div>')
        self.assertEqual(s, {'x': 216, 'y': 210})

    def test_content_over_every_point_is_reported_not_clicked(self):
        s = self.spot('', '<a class="layer" href="#" data-cve-kind="link" data-cve-path="p3"></a>')
        self.assertIn('<a class="layer">', s['covered'])
        self.assertEqual(s['clear'], 0)
        self.assertGreater(s['measured'], 300)

    def test_candidates_a_visitor_cannot_click_are_skipped(self):
        # Behind its section (negative z-index) or bled off the left edge.
        self.page.set_content("""<!doctype html><body style="margin:0">
          <img id="deco" style="position:absolute;left:-300px;top:0;width:900px;height:900px" src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">
          <img id="back" style="position:relative;z-index:-1;display:block;width:1300px;height:700px" src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">
          <img id="main" style="display:block;width:800px;height:400px" src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">
        </body>""")
        s = self.page.evaluate(SPOT_JS)
        self.assertEqual(self.page.evaluate("document.querySelector('[data-cve-smoke-media]').id"), 'main')
        self.assertEqual((s['x'], s['y']), (144, 140))

    def test_a_post_card_picture_is_no_candidate(self):
        # A featured lead card from [wp-posts] (a WordPress-managed zone) is the
        # largest image; the page's own image below it is the one to click.
        self.page.set_content("""<!doctype html><body style="margin:0">
          <div data-cve-zone="posts" data-cve-skip style="display:contents"><a href="/p/"><img id="card" style="display:block;width:1300px;height:700px" src="data:image/gif;base64,R0lGODlhAQABAAAAACw="></a></div>
          <img id="own" style="display:block;width:800px;height:400px" src="data:image/gif;base64,R0lGODlhAQABAAAAACw=">
        </body>""")
        self.page.evaluate(SPOT_JS)
        self.assertEqual(self.page.evaluate("document.querySelector('[data-cve-smoke-media]').id"), 'own')
        # Only managed pictures: nothing to test on this page.
        self.page.set_content('<body><div data-cve-skip><img style="width:900px;height:500px" src="data:image/gif;base64,R0lGODlhAQABAAAAACw="></div></body>')
        self.assertIsNone(self.page.evaluate(SPOT_JS))

    def test_no_prominent_image(self):
        self.page.set_content('<body><img style="width:120px;height:80px" src="data:image/gif;base64,R0lGODlhAQABAAAAACw="></body>')
        self.assertIsNone(self.page.evaluate(SPOT_JS))


if __name__ == '__main__':
    unittest.main(verbosity=2)
