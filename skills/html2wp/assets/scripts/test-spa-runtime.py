#!/usr/bin/env python3
"""spa-runtime.js and the swap-group pass, against hand-built captures.

Two behaviours the one-at-a-time recording cannot see, both found on a
converted shop:

  1. A mobile-menu toggle that makes a transparent-over-the-hero header solid
     was undone by the header's own scroll record: opening the menu at the top
     of the page left the header transparent.
  2. A gallery's thumbnail that is ACTIVE at rest changes nothing when
     clicked, so it was never recorded — once another thumbnail had been
     clicked, the first photograph could not be brought back — and each
     recorded thumbnail, recorded against the resting page, left a sibling's
     highlight on when it was clicked after that sibling.

  python3 test-spa-runtime.py
"""
import html
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

from playwright.sync_api import sync_playwright

SCRIPT = Path(__file__).with_name('prerender-spa.py')
TMP = tempfile.TemporaryDirectory()
ARGV = list(sys.argv)
sys.argv = [str(SCRIPT), '--project', TMP.name, '--out', str(Path(TMP.name) / 'out')]
spec = importlib.util.spec_from_file_location('prerender_spa', SCRIPT)
spa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(spa)
sys.argv = ARGV


def attr(value):
    return json.dumps(value).replace('"', '&quot;')


HEADER = f"""<!doctype html><html><body style="margin:0">
<header data-spa-id="h" class="hdr fixed bg-transparent"
  data-spa-scroll="{attr({'y': 61, 'add': ['bg-solid'], 'remove': ['bg-transparent']})}">
  <a data-spa-id="logo" class="logo text-light"
     data-spa-scroll="{attr({'y': 61, 'add': ['text-dark'], 'remove': ['text-light']})}">Brand</a>
  <button id="menu" data-spa-toggle="t1"
    data-spa-attrs="{attr([{'id': 'h', 'attr': 'class', 'off': 'hdr fixed bg-transparent', 'on': 'hdr fixed bg-solid'},
                           {'id': 'logo', 'attr': 'class', 'off': 'logo text-light', 'on': 'logo text-dark'}])}"
    data-spa-inner="{attr({'off': '<i>=</i>', 'on': '<i>x</i>'})}"><i>=</i></button>
  <nav data-spa-panel="t1" hidden style="display:none">Shop About</nav>
</header>
<div style="height:4000px"></div>
</body></html>"""


def thumb_attrs(n, count=3):
    """Thumbnail n's record, taken against the resting page (thumb 1 active)."""
    changes = [{'id': 'main', 'attr': 'src', 'off': 'a.jpg', 'on': f'{"abc"[n - 1]}.jpg'},
               {'id': 'th1', 'attr': 'class', 'off': 'th active', 'on': 'th'},
               {'id': f'th{n}', 'attr': 'class', 'off': 'th', 'on': 'th active'}]
    return changes


GALLERY = f"""<!doctype html><html><body>
<img data-spa-id="main" src="a.jpg">
<div class="strip">
  <button data-spa-id="th1" class="th active">1</button>
  <button data-spa-id="th2" class="th" data-spa-toggle="t2" data-spa-attrs="{attr(thumb_attrs(2))}">2</button>
  <button data-spa-id="th3" class="th" data-spa-toggle="t3" data-spa-attrs="{attr(thumb_attrs(3))}">3</button>
</div>
<button id="theme" data-spa-toggle="t9" data-spa-attrs="{attr([{'id': 'root', 'attr': 'class', 'off': 'light', 'on': 'dark'}])}">theme</button>
<div data-spa-id="root" class="light"></div>
</body></html>"""


def stepper(minus, plus, typ='number', bounds='min="1" max="5"', value='1'):
    """A quantity field between two recorded buttons, as the recorder writes them."""
    rec = lambda off, on: attr([{'id': 'q', 'attr': 'value', 'off': off, 'on': on}])
    return f"""<!doctype html><html><body><form>
<button type="button" id="minus" data-spa-toggle="t1" data-spa-attrs="{rec(*minus)}">-</button>
<input data-spa-id="q" name="quantity" type="{typ}" {bounds} value="{value}">
<button type="button" id="plus" data-spa-toggle="t2" data-spa-attrs="{rec(*plus)}">+</button>
</form></body></html>"""


class RuntimeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def page(self, html, swap_groups=False):
        page = self.browser.new_page(viewport={'width': 390, 'height': 800})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.set_content(html)
        if swap_groups:
            page.evaluate(spa.SWAP_GROUPS_JS)
        page.add_script_tag(content=spa.RUNTIME)
        page.wait_for_timeout(100)
        self.addCleanup(page.close)
        self.addCleanup(lambda: self.assertEqual(errors, []))
        return page

    def classes(self, page, sel):
        return set(page.eval_on_selector(sel, 'e => e.className').split())

    def scroll(self, page, y):
        page.evaluate(f'window.scrollTo(0, {y})')
        page.wait_for_timeout(100)

    def test_open_menu_keeps_the_header_solid_at_the_top(self):
        page = self.page(HEADER)
        self.assertIn('bg-transparent', self.classes(page, 'header'))
        page.click('#menu')
        page.wait_for_timeout(50)
        self.assertIn('bg-solid', self.classes(page, 'header'))
        self.assertNotIn('bg-transparent', self.classes(page, 'header'))
        self.assertIn('text-dark', self.classes(page, 'a'))
        page.click('#menu')
        page.wait_for_timeout(50)
        self.assertIn('bg-transparent', self.classes(page, 'header'))
        self.assertNotIn('bg-solid', self.classes(page, 'header'))

    def test_scroll_still_drives_the_header_and_yields_to_an_open_menu(self):
        page = self.page(HEADER)
        self.scroll(page, 300)
        self.assertIn('bg-solid', self.classes(page, 'header'))
        page.click('#menu')
        self.scroll(page, 0)
        self.assertIn('bg-solid', self.classes(page, 'header'), 'scrolling back up with the menu open')
        self.assertNotIn('bg-transparent', self.classes(page, 'header'))
        page.click('#menu')
        page.wait_for_timeout(50)
        self.assertIn('bg-transparent', self.classes(page, 'header'))

    def test_gallery_first_thumbnail_is_a_trigger_and_brings_image_one_back(self):
        page = self.page(GALLERY, swap_groups=True)
        self.assertEqual(page.get_attribute('button:has-text("1")', 'data-spa-swap'),
                         page.get_attribute('button:has-text("2")', 'data-spa-swap'))
        seq = []
        for n in ('2', '3', '1', '3', '3'):
            page.click(f'.strip button:has-text("{n}")')
            page.wait_for_timeout(30)
            active = [b for b in ('th1', 'th2', 'th3') if 'active' in self.classes(page, f'[data-spa-id="{b}"]')]
            seq.append((page.get_attribute('[data-spa-id="main"]', 'src'), active))
        self.assertEqual(seq, [('b.jpg', ['th2']), ('c.jpg', ['th3']), ('a.jpg', ['th1']),
                               ('c.jpg', ['th3']), ('c.jpg', ['th3'])])

    def presses(self, page, seq):
        out = []
        for b in seq:
            page.click('#' + b)
            out.append(page.eval_on_selector('[data-spa-id="q"]', 'e => e.value'))
        return out

    def test_quantity_stepper_counts_instead_of_toggling(self):
        # "-" recorded at the minimum (1 -> 1), "+" as 1 -> 2; grouped like the capture groups them.
        page = self.page(stepper(('1', '1'), ('1', '2')), swap_groups=True)
        self.assertEqual(self.presses(page, ['plus', 'plus', 'minus', 'plus', 'plus', 'plus', 'plus']),
                         ['2', '3', '2', '3', '4', '5', '5'])
        self.assertEqual(self.presses(page, ['minus'] * 6), ['4', '3', '2', '1', '1', '1'])

    def test_stepper_shapes(self):
        # A "-" recorded above the minimum, a text field, no bounds, a step of 5.
        page = self.page(stepper(('10', '5'), ('10', '15'), typ='text', bounds='', value='10'))
        self.assertEqual(self.presses(page, ['plus', 'minus', 'minus', 'minus', 'minus']),
                         ['15', '10', '5', '0', '-5'])
        # The change reaches whatever listens (a price total, a cart form).
        page = self.page(stepper(('1', '1'), ('1', '2')))
        page.evaluate("window.seen = 0; document.querySelector('input').addEventListener('change', () => seen++)")
        page.click('#plus')
        self.assertEqual(page.evaluate('seen'), 1)

    def test_a_value_toggle_on_a_non_number_is_still_a_toggle(self):
        html = f"""<!doctype html><html><body>
<button id="t" data-spa-toggle="t1" data-spa-attrs="{attr([{'id': 'f', 'attr': 'value', 'off': 'monthly', 'on': 'yearly'}])}">plan</button>
<input data-spa-id="f" value="monthly"></body></html>"""
        page = self.page(html)
        page.click('#t')
        self.assertEqual(page.get_attribute('[data-spa-id="f"]', 'value'), 'yearly')
        page.click('#t')
        self.assertEqual(page.get_attribute('[data-spa-id="f"]', 'value'), 'monthly')

    def test_a_lone_panel_less_trigger_still_toggles(self):
        page = self.page(GALLERY, swap_groups=True)
        self.assertIsNone(page.get_attribute('#theme', 'data-spa-swap'))
        page.click('#theme')
        self.assertEqual(page.get_attribute('[data-spa-id="root"]', 'class'), 'dark')
        page.click('#theme')
        self.assertEqual(page.get_attribute('[data-spa-id="root"]', 'class'), 'light')


class RevealBootTest(unittest.TestCase):
    """The head boot hides reveals before first paint; the runtime takes over,
    or — absent, or told to skip — the page is handed back visible."""
    PAGE = ('<!doctype html><html><head>' + '__CSS__' + '</head><body style="margin:0">'
            '<div style="height:1500px"></div><p id="r" data-spa-reveal="r0-400-n">below</p></body></html>')

    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def load(self, runtime=True, reduced=False):
        ctx = self.browser.new_context(reduced_motion='reduce' if reduced else 'no-preference', viewport={'width': 800, 'height': 600})
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        css = spa.reveal_css([{'key': 'r0-400-n', 'o': 0, 't': 'none', 'ms': 400}])
        page.set_content(self.PAGE.replace('__CSS__', css))
        if runtime:
            page.add_script_tag(content=spa.RUNTIME)
        page.wait_for_timeout(100)
        return page

    def opacity(self, page):
        return page.eval_on_selector('#r', 'e => getComputedStyle(e).opacity')

    def test_hidden_from_the_start_then_revealed_on_scroll(self):
        page = self.load()
        self.assertEqual(self.opacity(page), '0')
        self.assertIsNotNone(page.get_attribute('html', 'data-spa-reveal-live'))
        page.evaluate('window.scrollTo(0, 1200)')
        page.wait_for_timeout(700)
        self.assertEqual(self.opacity(page), '1')

    def test_jumped_past_still_plays(self):
        # A jump from the top to far below: the observer never saw it cross.
        page = self.load()
        page.evaluate("document.body.insertAdjacentHTML('beforeend', '<div style=\"height:3000px\"></div>')")
        page.evaluate('window.scrollTo(0, 2600)')
        page.wait_for_timeout(700)
        self.assertEqual(self.opacity(page), '1')

    def test_unreachable_depth_stays_held_at_the_documents_end(self):
        # Recorded 200 px deep, but the page ends 120 px under it: the app
        # never shows it there, and neither does the page.
        ctx = self.browser.new_context(viewport={'width': 800, 'height': 600})
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        css = spa.reveal_css([{'key': 'r0-400-n', 'o': 0, 't': 'none', 'ms': 400}])
        html = self.PAGE.replace('data-spa-reveal="r0-400-n"', 'data-spa-reveal="r0-400-n" data-spa-reveal-at="200"').replace('</body>', '<div style="height:100px"></div></body>')
        page.set_content(html.replace('__CSS__', css))
        page.add_script_tag(content=spa.RUNTIME)
        page.wait_for_timeout(100)
        page.evaluate('window.scrollTo(0, 980)')
        page.wait_for_timeout(300)
        self.assertEqual(self.opacity(page), '0')
        page.evaluate('window.scrollTo(0, document.documentElement.scrollHeight)')
        page.wait_for_timeout(700)
        self.assertEqual(self.opacity(page), '0')

    def test_reduced_motion_never_hides(self):
        page = self.load(reduced=True)
        self.assertNotIn('spa-reveal', page.get_attribute('html', 'class') or '')
        self.assertEqual(self.opacity(page), '1')

    def test_no_runtime_hands_the_page_back_visible(self):
        page = self.load(runtime=False)
        self.assertEqual(self.opacity(page), '0')
        page.wait_for_timeout(4200)
        self.assertEqual(self.opacity(page), '1')


class CaptureWalkTest(unittest.TestCase):
    """The gates' scroll-through reaches the bottom of a page that scrolls
    smoothly, and a reveal it carries past plays: the two captures of a
    comparison see every reveal in its end state, not whichever the walk
    happened to reach."""
    def walk(self, name):
        src = SCRIPT.with_name(name).read_text()
        start = src.rindex('"""async () => {', 0, src.index('y += 700')) + 3
        return src[start:src.index('}"""', start) + 1]

    def test_walk_reaches_the_end_and_plays_reveals(self):
        css = spa.reveal_css([{'key': 'r0-400-n', 'o': 0, 't': 'translateY(40px)', 'ms': 400}])
        page_html = ('<!doctype html><html style="scroll-behavior:smooth"><head>' + css + '</head><body style="margin:0">'
                     + ''.join('<div style="height:900px"></div><p class="r" data-spa-reveal="r0-400-n" data-spa-reveal-at="100">%d</p>' % i for i in range(10))
                     + '<div style="height:900px"></div></body></html>')
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for name in ('verify-wp.py', 'verify-static.py', 'compare-pages.py'):
                page = browser.new_page(viewport={'width': 390, 'height': 844})
                page.set_content(page_html)
                page.add_script_tag(content=spa.RUNTIME)
                page.wait_for_timeout(100)
                page.evaluate(self.walk(name))
                page.wait_for_timeout(600)
                shown = page.evaluate("() => document.querySelectorAll('.r.spa-in').length")
                self.assertEqual(shown, 10, name)
                self.assertEqual(page.evaluate('getComputedStyle(document.documentElement).scrollBehavior'), 'smooth', name)
                page.close()
            browser.close()


class EditorWalkTest(unittest.TestCase):
    """smoke-editor's image walk reaches the bottom of a smooth-scrolling page."""
    def test_walk_reaches_the_end(self):
        src = SCRIPT.with_name('smoke-editor.py').read_text()
        at = src.index('def _images_loaded')
        start = src.index('"""async (limit) => {', at) + 3
        walk = src[start:src.index('}"""', start) + 1]
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={'width': 390, 'height': 844})
            page.set_content('<!doctype html><html style="scroll-behavior:smooth"><body style="margin:0"><div style="height:10000px"></div></body></html>')
            page.evaluate("() => { window.__maxY = 0; addEventListener('scroll', () => { window.__maxY = Math.max(window.__maxY, scrollY); }); }")
            page.evaluate(walk, 8000)
            max_y = page.evaluate('window.__maxY')
            smooth = page.evaluate('getComputedStyle(document.documentElement).scrollBehavior')
            browser.close()
        self.assertGreater(max_y, 8500)
        self.assertEqual(smooth, 'smooth')


# What an Author can store: wp_kses_post keeps every data-* attribute, and an
# entity-encoded tag inside one is only a tag once the runtime parses it.
PWN = "window.pwned = (window.pwned || 0) + 1"
ICON = ('<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" '
        'stroke="currentColor" stroke-width="2" class="lucide" aria-hidden="true"><line x1="4" x2="20" y1="12" y2="12"></line>'
        '<path d="M4 6h16"></path></svg>')


def stored(value):
    """A record as it sits in saved markup: JSON, attribute-escaped."""
    return html.escape(json.dumps(value), quote=True)


class HostileRecordTest(unittest.TestCase):
    """Records in stored content are data: nothing in them runs, while the
    legitimate record beside a hostile one still replays."""

    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def page(self, body):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.set_content('<!doctype html><html><body>' + body + '</body></html>')
        page.add_script_tag(content=spa.RUNTIME)
        page.wait_for_timeout(100)
        return page

    def pwned(self, page):
        return page.evaluate('window.pwned || 0')

    def test_event_handler_and_unsafe_url_attributes_are_not_written(self):
        changes = [{'id': 'x', 'attr': 'onmouseover', 'off': PWN, 'on': PWN},
                   {'id': 'x', 'attr': 'ONCLICK', 'off': PWN, 'on': PWN},
                   {'id': 'x', 'attr': 'class', 'off': 'box', 'on': 'box open'},
                   {'id': 'a', 'attr': 'href', 'off': '/', 'on': 'javascript:' + PWN},
                   {'id': 'a', 'attr': 'xlink:href', 'off': None, 'on': 'javascript:' + PWN},
                   {'id': 'f', 'attr': 'src', 'off': None, 'on': 'javascript:' + PWN},
                   {'id': 'f', 'attr': 'srcdoc', 'off': None, 'on': '<script>' + PWN + '</script>'},
                   {'id': 'img', 'attr': 'src', 'off': 'a.png', 'on': 'javascript:' + PWN},
                   {'id': 'btn', 'attr': 'formaction', 'off': None, 'on': 'javascript:' + PWN},
                   {'id': 'x', 'attr': 'style', 'off': None, 'on': 'color:red;background:url(javascript:alert(1));width:expression(alert(1))'},
                   {'id': '"],*,[x="', 'attr': 'class', 'off': 'a', 'on': 'b'}]
        page = self.page(f"""
<button id="t" data-spa-toggle="t1" data-spa-attrs="{stored(changes)}">go</button>
<div data-spa-id="x" class="box">box</div><a data-spa-id="a" href="/">link</a>
<iframe data-spa-id="f"></iframe><img data-spa-id="img" src="a.png"><button data-spa-id="btn">b</button>""")
        page.click('#t')
        page.hover('[data-spa-id=x]')
        page.click('[data-spa-id=x]')
        self.assertEqual(page.get_attribute('[data-spa-id=x]', 'class'), 'box open', 'the legitimate change beside them replays')
        self.assertEqual(page.eval_on_selector('[data-spa-id=x]', 'e => e.getAttributeNames().filter(n => /^on/i.test(n))'), [])
        self.assertEqual(page.get_attribute('[data-spa-id=x]', 'style'), 'color:red')
        self.assertEqual(page.get_attribute('[data-spa-id=a]', 'href'), '/')
        self.assertIsNone(page.get_attribute('[data-spa-id=a]', 'xlink:href'))
        self.assertIsNone(page.get_attribute('[data-spa-id=f]', 'src'))
        self.assertIsNone(page.get_attribute('[data-spa-id=f]', 'srcdoc'))
        self.assertEqual(page.get_attribute('[data-spa-id=img]', 'src'), 'a.png')
        self.assertIsNone(page.get_attribute('[data-spa-id=btn]', 'formaction'))
        page.click('[data-spa-id=a]')
        self.assertEqual(self.pwned(page), 0)

    def test_hostile_inner_never_runs_at_load_or_on_click(self):
        inner = {'off': '<img src=x onerror="' + PWN + '"><script>' + PWN + '</script>Menu'
                        '<svg><animate attributeName="href" to="javascript:alert(1)"/></svg>'
                        '<iframe srcdoc="<script>parent.pwned=1</script>"></iframe>',
                 'on': '<a href="javascript:' + PWN + '" onclick="' + PWN + '">Close</a>'
                       '<details open ontoggle="' + PWN + '"></details><form><button formaction="javascript:' + PWN + '">x</button></form>'}
        page = self.page(f"""
<button id="t" data-spa-toggle="t1" data-spa-inner="{stored(inner)}">Menu</button>
<nav data-spa-panel="t1">Shop</nav>""")
        page.wait_for_timeout(300)
        self.assertEqual(self.pwned(page), 0, 'the closed inner is applied at load')
        self.assertEqual(page.inner_text('#t'), 'Menu')
        self.assertEqual(page.locator('#t img').count(), 1, 'a plain image survives, minus its handler')
        self.assertIsNone(page.get_attribute('#t img', 'onerror'))
        self.assertEqual(page.locator('#t script, #t iframe, #t animate').count(), 0)
        page.click('#t')
        page.wait_for_timeout(200)
        self.assertEqual(page.inner_text('#t'), 'Close')
        self.assertIsNone(page.get_attribute('#t a', 'href'))
        self.assertIsNone(page.get_attribute('#t a', 'onclick'))
        self.assertEqual(page.locator('#t form, #t [formaction], #t [ontoggle]').count(), 0)
        page.click('#t a')
        self.assertEqual(self.pwned(page), 0)

    def test_legitimate_icon_and_label_swaps_are_byte_identical(self):
        inner = {'off': ICON + '<span class="sr-only" data-state="closed" data-spa-id="e0.1">Open menu</span>',
                 'on': '<span class="x">Close menu</span>'}
        page = self.page(f"""
<button id="t" data-spa-toggle="t1" data-spa-inner="{stored(inner)}">{ICON}</button>
<nav data-spa-panel="t1">Shop</nav>""")
        self.assertEqual(page.eval_on_selector('#t', 'e => e.innerHTML'), inner['off'])
        self.assertEqual(page.eval_on_selector('#t svg', 'e => e.namespaceURI'), 'http://www.w3.org/2000/svg')
        page.click('#t')
        self.assertEqual(page.eval_on_selector('#t', 'e => e.innerHTML'), inner['on'])
        page.click('#t')
        self.assertEqual(page.eval_on_selector('#t', 'e => e.innerHTML'), inner['off'])

    def test_sprite_icon_swaps_stay_in_the_document(self):
        sprite = lambda ref: f'<svg class="i"><use href="{ref}"></use></svg>'
        inner = {'off': sprite('#menu'), 'on': sprite('#close')}
        changes = [{'id': 'u', 'attr': 'href', 'off': '#menu', 'on': '#close'},
                   {'id': 'u2', 'attr': 'xlink:href', 'off': '#a', 'on': 'https://evil.test/s.svg#x'}]
        page = self.page(f"""
<button id="t" data-spa-toggle="t1" data-spa-inner="{stored(inner)}">{sprite('#menu')}</button><nav data-spa-panel="t1">Shop</nav>
<button id="s" data-spa-toggle="t2" data-spa-attrs="{stored(changes)}">s</button>
<svg><use data-spa-id="u" href="#menu"></use><use data-spa-id="u2" href="#a"></use></svg>""")
        page.click('#t')
        self.assertEqual(page.eval_on_selector('#t', 'e => e.innerHTML'), inner['on'])
        page.click('#s')
        self.assertEqual(page.get_attribute('[data-spa-id=u]', 'href'), '#close')
        self.assertIsNone(page.get_attribute('[data-spa-id=u2]', 'xlink:href'))
        hostile = {'off': sprite('https://evil.test/s.svg#x') + sprite('data:image/svg+xml,<svg/>#x'), 'on': 'x'}
        page = self.page(f'<button id="t" data-spa-toggle="t1" data-spa-inner="{stored(hostile)}">m</button><nav data-spa-panel="t1">n</nav>')
        self.assertEqual(page.eval_on_selector_all('#t use', 'us => us.map(u => u.getAttribute("href"))'), [None, None])

    def test_a_malformed_record_does_not_stop_the_other_triggers(self):
        page = self.page("""
<button id="bad" data-spa-toggle="t1" data-spa-attrs="{}">bad</button><div data-spa-panel="t1" hidden>p1</div>
<button id="ok" data-spa-toggle="t2">ok</button><div data-spa-panel="t2" hidden>p2</div>""")
        page.click('#ok')
        self.assertTrue(page.is_visible('[data-spa-panel=t2]'))

    def test_recorded_panel_and_message_styles_keep_only_css(self):
        style = '--radix-h:var(--x);height:auto;background:url(javascript:alert(1));width:expression(alert(1))'
        page = self.page(f"""
<button id="t" data-spa-toggle="t1">q</button>
<div data-spa-panel="t1" data-spa-style="{style}" hidden>a</div>
<form data-spa-validate="v1"><input data-spa-vfield="v1-1"><button>go</button></form>
<p data-spa-invalid="v1" data-spa-for="v1-1" data-spa-style="{style}" hidden>Required</p>""")
        page.click('#t')
        self.assertEqual(page.get_attribute('[data-spa-panel]', 'style'), '--radix-h:var(--x);height:auto')
        page.click('form button')
        self.assertEqual(page.get_attribute('[data-spa-invalid]', 'style'), '--radix-h:var(--x);height:auto')


class NamedRoutesTest(unittest.TestCase):
    """--routes keeps the 404 probe when the app declares a catch-all."""

    def write_app(self, body):
        src = Path(TMP.name) / 'src'
        src.mkdir(exist_ok=True)
        (src / 'App.tsx').write_text(body)

    def test_catch_all_adds_the_probe(self):
        self.write_app('<Routes><Route path="/" element={<A/>} /><Route path="/product/:slug" element={<P/>} />'
                       '<Route path="*" element={<NotFound/>} /></Routes>')
        routes, catchall = spa.named_routes('/, /product/a')
        self.assertEqual(routes, ['/', '/product/a'])
        self.assertTrue(catchall)

    def test_no_catch_all_no_probe(self):
        self.write_app('<Routes><Route path="/" element={<A/>} /></Routes>')
        self.assertEqual(spa.named_routes('/,/about'), (['/', '/about'], False))

    def test_probe_already_named(self):
        self.write_app('<Routes><Route path="*" element={<N/>} /></Routes>')
        self.assertFalse(spa.named_routes('/,' + spa.CATCHALL_PROBE)[1])


LOAD_MORE_APP = """<!doctype html><html><body><main>
<div id="grid"><a class="card">P1</a><a class="card">P2</a></div>
<button id="more">Load more</button>
<script id="app">
let shown = 2;
document.getElementById('more').onclick = () => {
  const grid = document.getElementById('grid');
  for (let i = shown + 1; i <= 5; i++) { const a = document.createElement('a'); a.className = 'card'; a.textContent = 'P' + i; grid.appendChild(a); }
  shown = 5;
};
</script></main></body></html>"""


class AppendedPanelsOrderTest(unittest.TestCase):
    """A "Load more" appends several cards after the same resting card; the
    capture must keep them in the application's order (it reversed them)."""

    def test_captured_panels_keep_document_order(self):
        site = Path(TMP.name) / 'lm'
        site.mkdir(exist_ok=True)
        (site / 'index.html').write_text(LOAD_MORE_APP)
        httpd, url = spa.serve(site, spa_fallback=False)
        self.addCleanup(httpd.shutdown)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context(viewport={'width': 1440, 'height': 900})
            ctx.add_init_script(spa.HELPERS)
            page = ctx.new_page()
            records, _ = spa.record_interactions(page, url + '/index.html')
            more = [r for r in records if r['label'].lower().startswith('load more')]
            self.assertEqual(len(more), 1)
            page.goto(url + '/index.html', wait_until='networkidle')
            page.evaluate(spa.APPLY_JS, {'records': records, 'scroll': [], 'groups': {}, 'entrance': None})
            order = page.eval_on_selector_all('#grid > a', 'els => els.map(e => e.textContent)')
            browser.close()
        self.assertEqual(order, ['P1', 'P2', 'P3', 'P4', 'P5'])


if __name__ == '__main__':
    unittest.main()
