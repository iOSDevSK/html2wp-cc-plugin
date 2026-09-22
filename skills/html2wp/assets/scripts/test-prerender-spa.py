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
import json
import re
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
        # As capture() does: the watch goes in at the page's first content,
        # before the on-screen reveal can start (a later look races it).
        ctx.add_init_script("try { sessionStorage.setItem('__spaWatch', '1'); } catch (e) {}")
        ctx.add_init_script(spa.REVEAL_WATCH_BOOT)
        page = ctx.new_page()
        page.goto(cls.url + '/index.html', wait_until='commit')
        page.wait_for_function('window.__spaWatched !== undefined')
        page.evaluate(spa.REVEAL_PROBE_JS)
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
    "es.forEach(e => { if (e.isIntersecting) { e.target.style.opacity = 1; e.target.style.transform = 'none'; } }));", "es.forEach(e => { if (e.isIntersecting) { e.target.style.opacity = 1; e.target.style.transform = 'none'; } }), { rootMargin: '0px 0px -150px 0px' });").replace(
    '</main>', '<section id="late" style="opacity:0;transition:opacity .3s"><p>On a timer</p></section><div style="height:800px"></div></main>').replace(
    '</script>', "setTimeout(() => { document.getElementById('late').style.opacity = 1; }, 500);\n</script>")


MIXED_APP = r"""<!doctype html><html><head><title>Mixed</title><style>body{margin:0}</style></head><body>
<main><div style="height:560px"></div>
<section id="held" style="opacity:0;transition:opacity .3s"><p>Held until deep</p></section>
<section id="eager" style="opacity:0;transform:translateY(24px);transition:opacity .3s,transform .3s"><p>Shown at once</p></section>
<div style="height:1400px"></div>
<section id="held2" style="opacity:0;transition:opacity .3s"><p>Held too</p></section>
<section id="eager2" style="opacity:0;transform:translateY(24px);transition:opacity .3s,transform .3s"><p>Eager too</p></section>
<div style="height:1400px"></div></main>
<script id="app">
const show = (e) => { e.target.style.opacity = 1; e.target.style.transform = 'none'; };
const deep = new IntersectionObserver(es => es.forEach(e => e.isIntersecting && show(e)), { rootMargin: '0px 0px -120px 0px' });
const eager = new IntersectionObserver(es => es.forEach(e => e.isIntersecting && show(e)));
['held', 'held2'].forEach(i => deep.observe(document.getElementById(i)));
['eager', 'eager2'].forEach(i => eager.observe(document.getElementById(i)));
</script></body></html>"""


class RevealMixedMarginTest(unittest.TestCase):
    """Two components on one page with different viewport margins keep
    their own: one revealed on screen at load never inherits the other's."""
    def test_margin_per_component(self):
        site = Path(TMP.name) / 'mixed'
        site.mkdir()
        (site / 'index.html').write_text(MIXED_APP)
        httpd, url = spa.serve(site, spa_fallback=False)
        self.addCleanup(httpd.shutdown)
        with spa.sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context(viewport={'width': 1024, 'height': 700})
            ctx.add_init_script(spa.HELPERS)
            page = ctx.new_page()
            page.goto(url + '/index.html', wait_until='commit')
            page.wait_for_selector('#eager2', state='attached')
            page.evaluate(spa.REVEAL_WATCH_JS)
            page.evaluate(spa.REVEAL_PROBE_JS)
            spa.settle(page)
            page.evaluate(spa.REVEAL_COLLECT_JS, None)
            at = page.evaluate("""() => Object.fromEntries(['held', 'eager', 'held2', 'eager2'].map(i => [i, document.getElementById(i).getAttribute('data-spa-reveal-at')]))""")
            browser.close()
        self.assertTrue(110 <= int(at['held']) <= 126, at)
        self.assertTrue(110 <= int(at['held2']) <= 126, at)
        # The eager component never inherits the held one's depth, and has no delay.
        for i in ('eager', 'eager2'):
            self.assertTrue(at[i] is None or ('+' not in at[i] and int(at[i]) <= 24), at)


ONSCREEN_APP = r"""<!doctype html><html><head><title>On screen</title><style>body{margin:0}</style></head><body>
<main><div style="height:360px"></div>
<section id="clock" style="opacity:0;transition:opacity .3s"><p>On a timer</p></section>
<section id="view" style="opacity:0;transform:translateY(20px);transition:opacity .3s,transform .3s"><p>On the viewport</p></section>
<div style="height:1600px"></div></main>
<script id="app">
setTimeout(() => { document.getElementById('clock').style.opacity = 1; }, 250);
new IntersectionObserver(es => es.forEach(e => { if (e.isIntersecting) { e.target.style.opacity = 1; e.target.style.transform = 'none'; } })).observe(document.getElementById('view'));
</script></body></html>"""


class RevealOnScreenTimerTest(unittest.TestCase):
    """Two reveals that both play at load on screen: the one on a timer is
    told apart from the one on the viewport, so a narrow screen where both
    sit below the fold still shows the first without a scroll."""
    def test_timer_told_from_viewport(self):
        site = Path(TMP.name) / 'onscreen'
        site.mkdir()
        (site / 'index.html').write_text(ONSCREEN_APP)
        httpd, url = spa.serve(site, spa_fallback=False)
        self.addCleanup(httpd.shutdown)
        with spa.sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context(viewport={'width': 1024, 'height': 700})
            ctx.add_init_script(spa.HELPERS)
            page = ctx.new_page()
            page.goto(url + '/index.html', wait_until='commit')
            page.wait_for_selector('#view', state='attached')
            page.evaluate(spa.REVEAL_WATCH_JS)
            page.evaluate(spa.REVEAL_PROBE_JS)
            ambiguous = page.evaluate(spa.REVEAL_AMBIGUOUS_JS, spa.REVEAL_SHORT_VIEWPORT)
            self.assertEqual(len(ambiguous), 2, ambiguous)
            timed, held = spa.timed_reveals(page, url + '/index.html', ambiguous)
            self.assertEqual(len(timed), 1, timed)
            self.assertEqual(len(held), 1, held)
            page.evaluate("(ps) => { for (const p of ps) window.__spa.elAt(p).__spaReveal.timed = true; }", timed)
            spa.settle(page)
            page.evaluate(spa.REVEAL_COLLECT_JS, None)
            at = page.evaluate("() => [document.getElementById('clock').getAttribute('data-spa-reveal-at'), document.getElementById('view').getAttribute('data-spa-reveal-at')]")
            browser.close()
        self.assertTrue(at[0] and at[0].startswith('t'), at)
        self.assertIsNone(at[1], at)


class RevealMarginTest(unittest.TestCase):
    """A reveal on screen at load that the app still holds until it is 150px
    into the viewport keeps that trigger depth; one far below the fold that
    the app shows on a timer is shown on the same timer, without a scroll."""
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
        page.evaluate(spa.REVEAL_PROBE_JS)
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
        self.assertTrue(140 <= at <= 156, at)
        page = self.browser.new_page(viewport={'width': 1024, 'height': 700})
        self.addCleanup(page.close)
        page.set_content(self.static.replace('</head>', spa.reveal_css(self.reveals) + '</head>').replace('</body>', '<script>' + spa.RUNTIME + '</script></body>'))
        page.wait_for_timeout(700)
        opacity = lambda: page.evaluate("+getComputedStyle(document.getElementById('near')).opacity")
        self.assertEqual(opacity(), 0)          # on screen, but not deep enough yet
        page.evaluate('scrollTo(0, 300)')
        page.wait_for_timeout(700)
        self.assertEqual(opacity(), 1)

    def test_timed_reveal_replayed_on_its_timer(self):
        at = self.static.split('id="late"')[1].split('>')[0]
        self.assertIn('data-spa-reveal-at="t', at)
        page = self.browser.new_page(viewport={'width': 1024, 'height': 700})
        self.addCleanup(page.close)
        page.set_content(self.static.replace('</head>', spa.reveal_css(self.reveals) + '</head>').replace('</body>', '<script>' + spa.RUNTIME + '</script></body>'))
        page.wait_for_timeout(1500)
        self.assertEqual(page.evaluate("+getComputedStyle(document.getElementById('late')).opacity"), 1)
        self.assertEqual(page.evaluate('scrollY'), 0)


def delay_of(at, index):
    """The delay a data-spa-reveal-at value gives the index-th sibling."""
    if not at or '+' not in at:
        return 0
    p = at.split('+')[1]
    return index * int(p[1:]) if p.startswith('i') else int(p)


STAGGER_APP = r"""<!doctype html><html><head><title>Stagger</title><style>body{margin:0}.r{opacity:0;transition:opacity .3s}</style></head><body>
<main><section id="first" class="r"><p>Mounts first</p></section>
<div style="height:1400px"></div>
<section id="clock" class="r"><p>Timer after mount</p></section>
<div style="height:900px"></div>
<div style="display:flex"><section id="s0" class="r"><p>One</p></section><section id="s1" class="r"><p>Two</p></section><section id="s2" class="r"><p>Three</p></section></div>
<div style="height:1400px"></div></main>
<script id="app">
// The app mounts late, as a bundle that hydrates after download does.
setTimeout(() => {
  const on = (id) => { document.getElementById(id).style.opacity = 1; };
  on('first');
  setTimeout(() => on('clock'), 300);
  const io = new IntersectionObserver(es => es.forEach(e => {
    if (!e.isIntersecting) return;
    io.unobserve(e.target);
    setTimeout(() => on(e.target.id), +e.target.id.slice(1) * 150);
  }));
  ['s0', 's1', 's2'].forEach(i => io.observe(document.getElementById(i)));
}, 450);
</script></body></html>"""


class RevealStaggerTest(unittest.TestCase):
    """Times are the app's own: a timer counts from the app's first motion,
    not from the request, and a staggered row keeps each member's delay."""
    @classmethod
    def setUpClass(cls):
        site = Path(TMP.name) / 'stagger'
        site.mkdir()
        (site / 'index.html').write_text(STAGGER_APP)
        cls.httpd, url = spa.serve(site, spa_fallback=False)
        cls.pw = spa.sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        ctx = cls.browser.new_context(viewport={'width': 1024, 'height': 700})
        ctx.add_init_script(spa.HELPERS)
        page = ctx.new_page()
        page.goto(url + '/index.html', wait_until='commit')
        page.wait_for_selector('#s2', state='attached')
        page.evaluate(spa.REVEAL_WATCH_JS)
        page.evaluate(spa.REVEAL_PROBE_JS)
        spa.settle(page)
        cls.reveals = page.evaluate(spa.REVEAL_COLLECT_JS, None)
        cls.at = page.evaluate("() => Object.fromEntries(['first', 'clock', 's0', 's1', 's2'].map(i => [i, document.getElementById(i).getAttribute('data-spa-reveal-at')]))")
        page.evaluate("() => document.getElementById('app').remove()")
        cls.static = '<!doctype html>\n' + page.evaluate('() => document.documentElement.outerHTML')
        ctx.close()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.httpd.shutdown()

    def delay(self, i):
        return delay_of(self.at[i], int(i[1:]))

    def test_timer_counts_from_the_apps_first_motion(self):
        self.assertTrue(self.at['clock'].startswith('t'), self.at)
        self.assertTrue(250 <= int(self.at['clock'][1:]) <= 360, self.at)   # not ~750 from the request

    def test_stagger_delays_recorded(self):
        self.assertEqual(self.delay('s0'), 0, self.at)
        self.assertTrue(110 <= self.delay('s1') <= 200, self.at)
        self.assertTrue(260 <= self.delay('s2') <= 350, self.at)

    def run_static(self, late_ms):
        page = self.browser.new_page(viewport={'width': 1024, 'height': 700})
        self.addCleanup(page.close)
        runtime = '<script>setTimeout(function () { var s = document.createElement("script"); s.textContent = %s; document.body.appendChild(s); }, %d);</script>' % (json.dumps(spa.RUNTIME), late_ms)
        page.set_content(self.static.replace('</head>', spa.reveal_css(self.reveals) + '</head>').replace('</body>', runtime + '</body>'))
        return page

    def test_timer_keeps_its_offset_when_the_runtime_starts_late(self):
        page = self.run_static(1200)
        op = lambda i: page.evaluate("(i) => +getComputedStyle(document.getElementById(i)).opacity", i)
        page.wait_for_timeout(1350)       # runtime started ~150 ms ago: the timer is not due yet
        self.assertEqual(op('clock'), 0)
        page.wait_for_timeout(700)
        self.assertEqual(op('clock'), 1)

    def test_stagger_kept_when_every_card_carries_the_first_ones_attribute(self):
        # A blog listing renders one card template per post: every card has
        # the same data-spa-reveal-at. The stagger is by position, so it holds.
        at = self.at['s1']
        self.assertIn('+i', at)
        self.static = self.static.replace('data-spa-reveal-at="%s"' % self.at['s0'], 'data-spa-reveal-at="%s"' % at).replace('data-spa-reveal-at="%s"' % self.at['s2'], 'data-spa-reveal-at="%s"' % at)
        self.test_stagger_replayed_in_order()

    def test_stagger_kept_when_a_listing_wraps_each_card(self):
        # A Gutenberg post template puts every card in its own <li>.
        at = self.at['s1']
        for i in ('s0', 's1', 's2'):
            self.static = self.static.replace('data-spa-reveal-at="%s"' % self.at[i], 'data-spa-reveal-at="%s"' % at)
        self.static = re.sub(r'(<section id="s\d".*?</section>)', r'<li style="list-style:none;flex:1">\1</li>', self.static, flags=re.S)
        self.assertEqual(self.static.count('<li style'), 3)
        self.test_stagger_replayed_in_order()

    def test_stagger_replayed_in_order(self):
        page = self.run_static(0)
        op = lambda i: page.evaluate("(i) => +getComputedStyle(document.getElementById(i)).opacity", i)
        page.wait_for_timeout(400)
        page.evaluate("document.getElementById('s0').scrollIntoView({block: 'center'})")
        page.wait_for_timeout(200)
        self.assertGreater(op('s0'), 0)
        self.assertEqual(op('s2'), 0)
        page.wait_for_timeout(900)
        self.assertEqual([op('s0'), op('s1'), op('s2')], [1, 1, 1])


COMPONENT_APP = r"""<!doctype html><html><head><title>Components</title><style>body{margin:0}
.h,.card{opacity:0;transform:translateY(24px);transition:opacity .3s,transform .3s}.row{display:flex;gap:10px}.card{height:120px;flex:1}</style></head><body>
<main><div style="height:1000px"></div>
<h2 class="h" id="h1">Heading one</h2><div style="height:500px"></div>
<div class="row"><div class="card" id="c0">A</div><div class="card" id="c1">B</div><div class="card" id="c2">C</div></div>
<div style="height:700px"></div><h2 class="h" id="h2">Heading two</h2><div style="height:500px"></div>
<div class="row"><div class="card" id="d0">A</div><div class="card" id="d1">B</div><div class="card" id="d2">C</div></div>
<div style="height:1400px"></div></main>
<script id="app">
const on = (e) => { e.style.opacity = 1; e.style.transform = 'none'; };
const watch = (sel, margin, stagger) => {
  const io = new IntersectionObserver(es => es.forEach(x => {
    if (!x.isIntersecting) return;
    io.unobserve(x.target);
    setTimeout(() => on(x.target), stagger ? +x.target.id.slice(1) * 80 : 0);
  }), { rootMargin: '0px 0px -' + margin + 'px 0px' });
  document.querySelectorAll(sel).forEach(e => io.observe(e));
};
watch('.h', 100, false);
watch('.card', 40, true);
</script></body></html>"""


class RevealComponentMarginTest(unittest.TestCase):
    """Headings and a staggered card row start from the same look but are
    held for different depths: each keeps its own, and a card that came in
    along with the row's first is not taken for revealing at once."""
    def test_same_look_components_keep_their_margins(self):
        site = Path(TMP.name) / 'components'
        site.mkdir()
        (site / 'index.html').write_text(COMPONENT_APP)
        httpd, url = spa.serve(site, spa_fallback=False)
        self.addCleanup(httpd.shutdown)
        with spa.sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context(viewport={'width': 1024, 'height': 700})
            ctx.add_init_script(spa.HELPERS)
            page = ctx.new_page()
            page.goto(url + '/index.html', wait_until='commit')
            page.wait_for_selector('#d2', state='attached')
            page.evaluate(spa.REVEAL_WATCH_JS)
            page.evaluate(spa.REVEAL_PROBE_JS)
            spa.settle(page)
            page.evaluate(spa.REVEAL_COLLECT_JS, None)
            at = page.evaluate("() => Object.fromEntries(['h1', 'h2', 'c0', 'c1', 'c2', 'd0', 'd1', 'd2'].map(i => [i, document.getElementById(i).getAttribute('data-spa-reveal-at')]))")
            browser.close()
        depth = lambda i: int(at[i].split('+')[0])
        delay = lambda i: delay_of(at[i], int(i[1:]))
        # A row staggered by position is recorded as one: a listing that
        # repeats its first card keeps the stagger.
        self.assertTrue(all('+i' in at[r + '0'] for r in 'cd'), at)
        for i in ('h1', 'h2'):
            self.assertTrue(92 <= depth(i) <= 106, at)
        for i in ('c0', 'c1', 'c2', 'd0', 'd1', 'd2'):
            self.assertTrue(32 <= depth(i) <= 46, at)
        for row in 'cd':
            self.assertEqual(delay(row + '0'), 0, at)
            self.assertTrue(50 <= delay(row + '1') <= 130, at)
            self.assertTrue(130 <= delay(row + '2') <= 210, at)


# Eighteen headings held 100 px deep, each its own component, then one of the
# same look with no margin of its own (the observer's default), far down.
MANY_APP = (r"""<!doctype html><html><head><title>Many</title><style>body{margin:0}section{height:120px}</style></head><body>
<main><div style="height:800px"></div>"""
    + "".join('<section id="m%d" class="k%d" style="opacity:0;transform:translateY(40px);transition:opacity .3s,transform .3s"><p>Held %d</p></section><div style="height:500px"></div>' % (i, i, i) for i in range(18))
    + r"""<section id="last" class="kz" style="opacity:0;transform:translateY(40px);transition:opacity .3s,transform .3s"><p>No margin</p></section>
<div style="height:1200px"></div></main>
<script id="app">
const show = (e) => { e.target.style.opacity = 1; e.target.style.transform = 'none'; };
const deep = new IntersectionObserver(es => es.forEach(e => e.isIntersecting && show(e)), { rootMargin: '0px 0px -100px 0px' });
const eager = new IntersectionObserver(es => es.forEach(e => e.isIntersecting && show(e)));
document.querySelectorAll('section[id^=m]').forEach(e => deep.observe(e));
eager.observe(document.getElementById('last'));
</script></body></html>""")


class RevealManyComponentsTest(unittest.TestCase):
    """Past the first sixteen components on a page, one that shares a look
    with a measured one but has no margin keeps none; it does not inherit."""
    def test_no_margin_component_far_down_keeps_none(self):
        site = Path(TMP.name) / 'many'
        site.mkdir()
        (site / 'index.html').write_text(MANY_APP)
        httpd, url = spa.serve(site, spa_fallback=False)
        self.addCleanup(httpd.shutdown)
        with spa.sync_playwright() as pw:
            browser = pw.chromium.launch()
            ctx = browser.new_context(viewport={'width': 1024, 'height': 700})
            ctx.add_init_script(spa.HELPERS)
            page = ctx.new_page()
            page.goto(url + '/index.html', wait_until='commit')
            page.wait_for_selector('#last', state='attached')
            page.evaluate(spa.REVEAL_WATCH_JS)
            page.evaluate(spa.REVEAL_PROBE_JS)
            spa.settle(page)
            page.evaluate(spa.REVEAL_COLLECT_JS, None)
            at = page.evaluate("() => Object.fromEntries([...document.querySelectorAll('section')].map(e => [e.id, e.getAttribute('data-spa-reveal-at')]))")
            browser.close()
        for i in range(18):
            self.assertTrue(92 <= int(at['m%d' % i]) <= 106, at)
        self.assertTrue(at['last'] is None or int(at['last']) <= 8, at)


if __name__ == '__main__':
    unittest.main()
