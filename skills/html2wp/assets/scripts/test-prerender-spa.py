#!/usr/bin/env python3
"""Recorder + runtime regression test for prerender-spa.py disclosures.

A fixture application renders a single-open accordion whose FIRST item is
open at rest and whose closed panels are unmounted (framer AnimatePresence,
Radix), plus a hamburger drawer. The recorder must record every item as a
closed->open transition, put all six in one group, mark the first as
open at rest (`startsOpen`, its panel adopted from the at-rest markup and
its trigger stamped `data-spa-starts-open`), and the emitted runtime must then behave exactly like the
application: first item open at load, one item open at a time, a second
click closes, and the drawer still toggles.

  python3 test-prerender-spa.py
"""
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name('prerender-spa.py')
TMP = tempfile.TemporaryDirectory()
ARGV = list(sys.argv)
sys.argv = [str(SCRIPT), '--project', TMP.name, '--out', str(Path(TMP.name) / 'out')]
spec = importlib.util.spec_from_file_location('prerender_spa', SCRIPT)
spa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(spa)
sys.argv = ARGV

QUESTIONS = [
    'How does the free trial work?',
    'What age groups do you coach?',
    'Where do sessions take place?',
    'Do I need my own equipment?',
    'How often should I train?',
    # "buy" is on the recorder's destructive-verb list; a declared
    # disclosure must still be recorded.
    'Can I buy sessions as a gift?',
]

APP = r"""<!doctype html><html><head><title>FAQ</title></head><body>
<header><button id="menu" aria-label="Open menu">=</button></header>
<main><section><div id="faq"></div></section></main>
<script id="app">
const QS = %s;
let open = 0, drawer = false;
// Reconciles in place, as React does: attributes update on the same nodes,
// and only the open panel is mounted — closed panels do not exist.
const faq = document.getElementById('faq');
const items = QS.map((q, i) => {
  const item = document.createElement('div');
  item.className = 'item';
  const b = document.createElement('button');
  b.innerHTML = '<span>' + q + '</span><i class="chev"></i>';
  b.onclick = () => { open = open === i ? null : i; render(); };
  item.appendChild(b);
  faq.appendChild(item);
  return item;
});
function render() {
  items.forEach((item, i) => {
    const b = item.firstElementChild, on = open === i;
    b.setAttribute('aria-expanded', String(on));
    b.lastElementChild.className = 'chev' + (on ? ' rotate-180' : '');
    const p = item.children[1];
    if (on && !p) {
      const n = document.createElement('div');
      n.className = 'overflow-hidden';
      n.setAttribute('style', 'height:auto;opacity:1');
      n.innerHTML = '<p>Answer ' + (i + 1) + ' text.</p>';
      item.appendChild(n);
    } else if (!on && p) p.remove();
  });
}
render();
const menu = document.getElementById('menu');
menu.onclick = () => {
  drawer = !drawer;
  menu.setAttribute('aria-label', drawer ? 'Close menu' : 'Open menu');
  const d = document.getElementById('drawer');
  if (drawer && !d) {
    const n = document.createElement('nav');
    n.id = 'drawer';
    n.textContent = 'Programs Pricing';
    document.querySelector('header').appendChild(n);
  } else if (!drawer && d) d.remove();
};
</script></body></html>""" % repr(QUESTIONS)

STATE = """() => [...document.querySelectorAll('#faq > .item')].map(item => {
  const b = item.querySelector('button');
  let h = 0;
  for (const k of item.children) if (k !== b) h = Math.max(h, k.getBoundingClientRect().height);
  return (b.getAttribute('aria-expanded') === 'true' ? 'E' : '-') + (h > 0 ? 'V' : '-');
}).join(' ')"""


def expected(open_index):
    return ' '.join('EV' if i == open_index else '--' for i in range(len(QUESTIONS)))


class DisclosureRecordingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = Path(TMP.name) / 'site'
        cls.site.mkdir()
        (cls.site / 'index.html').write_text(APP)
        cls.httpd, cls.url = spa.serve(cls.site, spa_fallback=False)
        cls.pw = spa.sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        ctx = cls.browser.new_context(viewport={'width': 390, 'height': 900})
        ctx.add_init_script(spa.HELPERS)
        page = ctx.new_page()
        url = cls.url + '/index.html'
        cls.records, cls.links = spa.record_interactions(page, url)
        cls.groups = spa.detect_single_select(page, url, cls.records)
        # As main() does: each group member keeps only its own changes.
        spa.scope_group_changes(cls.records)
        # The capture step: a fresh at-rest page, records applied to it, the
        # application's own script removed, the emitted runtime put in.
        page.goto(url, wait_until='networkidle')
        cls.notes = page.evaluate(spa.APPLY_JS, {'records': cls.records, 'scroll': [], 'groups': {}, 'entrance': None})
        page.evaluate("() => document.getElementById('app').remove()")
        cls.static = '<!doctype html>\n' + page.evaluate('() => document.documentElement.outerHTML')
        ctx.close()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.httpd.shutdown()

    def replay(self):
        page = self.browser.new_page(viewport={'width': 390, 'height': 900})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        html = self.static.replace('</body>', '<script>' + spa.RUNTIME + '</script></body>')
        page.set_content(html)
        page.wait_for_timeout(200)
        self.addCleanup(page.close)
        self.addCleanup(lambda: self.assertEqual(errors, []))
        return page

    def faq_records(self):
        return [r for r in self.records if r['label'] in QUESTIONS]

    def test_every_item_recorded_including_destructive_sounding_question(self):
        self.assertEqual([r['label'] for r in self.faq_records()], QUESTIONS)
        self.assertEqual(self.notes, [])

    def test_initially_open_item_recorded_closed_to_open(self):
        # Open at rest: "on" is the resting state and the panel is the one
        # already in the markup, so the runtime closes it rather than adding it.
        first = self.faq_records()[0]
        self.assertTrue(first.get('startsOpen'))
        self.assertEqual(first['panels'], [])
        self.assertEqual(len(first['openPanels']), 1)
        aria = [c for c in first['attrChanges'] if c['attr'] == 'aria-expanded']
        self.assertEqual([(c['off'], c['on']) for c in aria], [('false', 'true')])
        self.assertFalse(any(r.get('startsOpen') for r in self.faq_records()[1:]))

    def test_no_item_carries_another_items_deltas(self):
        for r in self.faq_records():
            own = r['trigger']
            for c in r['attrChanges']:
                self.assertTrue(c['path'] == own or c['path'].startswith(own + '.'),
                                f"{r['label']} records a foreign change at {c['path']}")

    def test_all_siblings_share_one_group(self):
        self.assertEqual(len(self.groups), 1)
        members = next(iter(self.groups.values()))
        self.assertEqual(members, [r['trigger'] for r in self.faq_records()])

    def test_captured_markup_keeps_first_answer_open_once(self):
        self.assertEqual(self.static.count('Answer 1 text.'), 1)
        self.assertEqual(self.static.count('data-spa-starts-open="1"'), 1)

    def test_runtime_matches_application_click_sequence(self):
        page = self.replay()
        self.assertEqual(page.evaluate(STATE), expected(0))
        buttons = page.locator('#faq button')
        for step, open_after in [(1, None), (1, 0), (2, 1), (3, 2), (6, 5), (6, None), (5, 4)]:
            buttons.nth(step - 1).click()
            page.wait_for_timeout(50)
            self.assertEqual(page.evaluate(STATE), expected(open_after), f'after clicking item {step}')

    def test_drawer_still_toggles(self):
        page = self.replay()
        drawer = page.locator('#drawer')
        self.assertFalse(drawer.is_visible())
        page.locator('#menu').click()
        self.assertTrue(drawer.is_visible())
        self.assertEqual(page.locator('#menu').get_attribute('aria-label'), 'Close menu')
        page.locator('#menu').click()
        self.assertFalse(drawer.is_visible())

    def test_hostile_records_in_saved_markup_do_not_run(self):
        # What an Author can save: wp_kses_post keeps data-* attributes, so a
        # record can be rewritten — an event handler added to the drawer's
        # recorded attributes, an <img onerror> as its closed inner.
        pwn = 'window.pwned=(window.pwned||0)+1'
        page = self.browser.new_page(viewport={'width': 390, 'height': 900})
        self.addCleanup(page.close)
        page.set_content(self.static)
        page.evaluate("""(pwn) => {
          const t = document.getElementById('menu');
          const attrs = JSON.parse(t.getAttribute('data-spa-attrs') || '[]');
          document.body.setAttribute('data-spa-id', 'body');
          attrs.push({id: 'body', attr: 'onmouseover', off: pwn, on: pwn});
          t.setAttribute('data-spa-attrs', JSON.stringify(attrs));
          t.setAttribute('data-spa-inner', JSON.stringify({off: '<img src=x onerror="' + pwn + '">Menu', on: 'Close'}));
        }""", pwn)
        page.add_script_tag(content=spa.RUNTIME)
        page.wait_for_timeout(300)
        page.mouse.move(100, 100)
        page.locator('#menu').click()
        page.wait_for_timeout(100)
        page.mouse.move(200, 300)
        self.assertEqual(page.evaluate('window.pwned || 0'), 0)
        self.assertIsNone(page.get_attribute('body', 'onmouseover'))
        self.assertTrue(page.locator('#drawer').is_visible(), 'the drawer still opens')
        self.assertEqual(page.locator('#menu').get_attribute('aria-label'), 'Close menu')


REVEAL_APP = r"""<!doctype html><html><head><title>Reveal</title><style>body{margin:0}.gap{height:1600px}</style></head><body>
<main><section id="top" class="r" style="opacity:0;transform:translateY(24px);transition:opacity .4s,transform .4s"><h1>Above the fold</h1></section>
<div class="gap"></div>
<section id="low" class="r" style="opacity:0;transform:translateY(24px);transition:opacity .4s,transform .4s"><p>Below the fold</p></section>
<section id="plain"><p>Always visible</p></section></main>
<script id="app">
// whileInView-style reveal: shown once it enters the viewport.
const io = new IntersectionObserver(es => es.forEach(e => { if (e.isIntersecting) { e.target.style.opacity = 1; e.target.style.transform = 'none'; } }));
document.querySelectorAll('.r').forEach(e => io.observe(e));
</script></body></html>"""


class RevealRecordingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.site = Path(TMP.name) / 'reveal'
        cls.site.mkdir()
        (cls.site / 'index.html').write_text(REVEAL_APP)
        cls.httpd, cls.url = spa.serve(cls.site, spa_fallback=False)
        cls.pw = spa.sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        ctx = cls.browser.new_context(viewport={'width': 1024, 'height': 700})
        ctx.add_init_script(spa.HELPERS)
        page = ctx.new_page()
        page.goto(cls.url + '/index.html', wait_until='commit')
        page.wait_for_selector('#low', state='attached')
        page.evaluate(spa.REVEAL_WATCH_JS)
        spa.settle(page)
        cls.reveals = page.evaluate(spa.REVEAL_COLLECT_JS, None)
        page.evaluate("() => document.getElementById('app').remove()")
        cls.static = '<!doctype html>\n' + page.evaluate('() => document.documentElement.outerHTML')
        ctx.close()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.httpd.shutdown()

    def load(self, runtime=True, reduced=False):
        ctx = self.browser.new_context(viewport={'width': 1024, 'height': 700}, reduced_motion='reduce' if reduced else 'no-preference')
        self.addCleanup(ctx.close)
        page = ctx.new_page()
        html = self.static.replace('</head>', spa.reveal_css(self.reveals) + '</head>')
        if runtime:
            html = html.replace('</body>', '<script>' + spa.RUNTIME + '</script></body>')
        page.set_content(html)
        page.wait_for_timeout(100)
        return page

    def opacity(self, page, sel):
        return page.evaluate("s => +getComputedStyle(document.querySelector(s)).opacity", sel)

    def test_both_reveals_recorded_with_start_state_and_duration(self):
        self.assertEqual(len(self.reveals), 2)
        self.assertEqual({r['o'] for r in self.reveals}, {0})
        self.assertTrue(all(r['t'].startswith('matrix(') and 300 <= r['ms'] <= 700 for r in self.reveals), self.reveals)
        self.assertIn('data-spa-reveal=', self.static)
        self.assertNotIn('data-spa-reveal', self.static.split('id="plain"')[1].split('</section>')[0])

    def test_runtime_reveals_on_scroll(self):
        page = self.load()
        page.wait_for_timeout(700)
        self.assertEqual(self.opacity(page, '#top'), 1)   # on screen at load: plays at once
        self.assertEqual(self.opacity(page, '#low'), 0)   # below the fold: held
        page.evaluate("document.getElementById('low').scrollIntoView()")
        page.wait_for_timeout(900)
        self.assertEqual(self.opacity(page, '#low'), 1)

    def test_visible_without_runtime_or_with_reduced_motion(self):
        # The head boot hides before first paint and gives up when no runtime
        # started within 4s, so a page without the script ends up visible.
        page = self.load(runtime=False)
        page.wait_for_timeout(4500)
        self.assertEqual(self.opacity(page, '#low'), 1)
        self.assertEqual(self.opacity(self.load(reduced=True), '#low'), 1)


MARGIN_APP = REVEAL_APP.replace('<div class="gap"></div>', '<div style="height:540px"></div><section id="near" class="r" style="opacity:0;transition:opacity .3s"><p>Near the fold</p></section><div class="gap"></div>').replace(
    "es.forEach(e => { if (e.isIntersecting) { e.target.style.opacity = 1; e.target.style.transform = 'none'; } }));", "es.forEach(e => { if (e.isIntersecting) { e.target.style.opacity = 1; e.target.style.transform = 'none'; } }), { rootMargin: '0px 0px -150px 0px' });")


class RevealMarginTest(unittest.TestCase):
    """A reveal on screen at load that the app still holds until it is 150px
    into the viewport keeps that trigger depth."""
    @classmethod
    def setUpClass(cls):
        site = Path(TMP.name) / 'margin'
        site.mkdir()
        (site / 'index.html').write_text(MARGIN_APP)
        cls.httpd, url = spa.serve(site, spa_fallback=False)
        cls.pw = spa.sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        ctx = cls.browser.new_context(viewport={'width': 1024, 'height': 700})
        ctx.add_init_script(spa.HELPERS)
        page = ctx.new_page()
        page.goto(url + '/index.html', wait_until='commit')
        page.wait_for_selector('#low', state='attached')
        page.evaluate(spa.REVEAL_WATCH_JS)
        spa.settle(page)
        cls.reveals = page.evaluate(spa.REVEAL_COLLECT_JS, None)
        page.evaluate("() => document.getElementById('app').remove()")
        cls.static = '<!doctype html>\n' + page.evaluate('() => document.documentElement.outerHTML')
        ctx.close()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.httpd.shutdown()

    def test_trigger_depth_recorded_and_replayed(self):
        at = int(self.static.split('id="near"')[1].split('data-spa-reveal-at="')[1].split('"')[0])
        self.assertTrue(40 < at <= 150, at)
        page = self.browser.new_page(viewport={'width': 1024, 'height': 700})
        self.addCleanup(page.close)
        page.set_content(self.static.replace('</head>', spa.reveal_css(self.reveals) + '</head>').replace('</body>', '<script>' + spa.RUNTIME + '</script></body>'))
        page.wait_for_timeout(700)
        opacity = lambda: page.evaluate("+getComputedStyle(document.getElementById('near')).opacity")
        self.assertEqual(opacity(), 0)          # on screen, but not deep enough yet
        page.evaluate('scrollTo(0, 300)')
        page.wait_for_timeout(700)
        self.assertEqual(opacity(), 1)


if __name__ == '__main__':
    unittest.main()
