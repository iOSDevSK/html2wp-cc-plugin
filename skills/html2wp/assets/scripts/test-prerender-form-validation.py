#!/usr/bin/env python3
"""Recorder + runtime test for prerender-spa.py's empty-submit validation.

A fixture application has two script-validated forms, the two shapes real
component forms use: one prints a message UNDER each empty required field
(and clears it when the field is edited); the other raises one toast outside
the form that removes itself. The recorder submits each form empty and keeps
what appeared; the capture puts those messages into the markup hidden; the
emitted runtime must then behave like the application on the static page.

  python3 test-prerender-form-validation.py
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

APP = r"""<!doctype html><html><head><title>Contact</title>
<style>.err{color:#c00;margin:4px 0}.toast{position:fixed;bottom:10px;right:10px}</style></head><body>
<main>
<form id="contact" novalidate>
  <div><label>Name</label><input id="n"></div>
  <div><label>Email</label><input id="e" type="email"></div>
  <div><label>Subject</label><input id="s"></div>
  <div><label>Message</label><textarea id="m"></textarea></div>
  <button type="submit">Send</button>
</form>
<form id="signup">
  <input id="u" placeholder="Your email">
  <select id="plan"><option>Basic</option><option>Pro</option></select>
  <button>Join</button>
</form>
</main>
<script id="app">
const req = { n: 'Please enter your name', e: 'Please enter a valid email', m: 'Tell me a little more' };
const contact = document.getElementById('contact');
contact.addEventListener('submit', (ev) => {
  ev.preventDefault();
  for (const [id, text] of Object.entries(req)) {
    const input = document.getElementById(id), box = input.parentElement;
    const old = box.querySelector('.err'); if (old) old.remove();
    if (!input.value.trim()) { const p = document.createElement('p'); p.className = 'err'; p.textContent = text; box.appendChild(p); }
  }
});
contact.addEventListener('input', (ev) => { const p = ev.target.parentElement.querySelector('.err'); if (p) p.remove(); });
document.getElementById('signup').addEventListener('submit', (ev) => {
  ev.preventDefault();
  if (!document.getElementById('u').value.trim()) {
    const t = document.createElement('div'); t.className = 'toast'; t.setAttribute('role', 'status');
    t.textContent = 'Please fill in your email.'; document.body.appendChild(t);
    setTimeout(() => t.remove(), 1500);
  }
});
</script></body></html>"""

# After the runtime, a bubbling listener stands in for the submission itself:
# it runs only when the runtime let the submit through.
PROBE = """<script>window.sent = [];
document.addEventListener('submit', (ev) => { ev.preventDefault(); window.sent.push(ev.target.id); });</script>"""

VISIBLE = """(sel) => [...document.querySelectorAll(sel)].filter((e) => e.getBoundingClientRect().height > 0).map((e) => e.textContent.trim())"""


class FormValidationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        site = Path(TMP.name) / 'site'
        site.mkdir()
        (site / 'index.html').write_text(APP)
        cls.httpd, base = spa.serve(site, spa_fallback=False)
        cls.pw = spa.sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        ctx = cls.browser.new_context(viewport={'width': 1440, 'height': 900})
        ctx.add_init_script(spa.HELPERS)
        page = ctx.new_page()
        url = base + '/index.html'
        cls.records = spa.record_form_validation(page, url)
        # The capture's own order: resolve, APPLY_JS, stamp.
        page.goto(url, wait_until='networkidle')
        page.evaluate(spa.FORM_RESOLVE_JS, cls.records)
        page.evaluate(spa.APPLY_JS, {'records': [], 'scroll': [], 'groups': {}, 'entrance': None})
        cls.notes = page.evaluate(spa.FORM_STAMP_JS)
        page.evaluate("() => document.getElementById('app').remove()")
        cls.static = '<!doctype html>\n' + page.evaluate('() => document.documentElement.outerHTML')
        ctx.close()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.httpd.shutdown()

    def replay(self):
        page = self.browser.new_page(viewport={'width': 1440, 'height': 900})
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.set_content(self.static.replace('</body>', '<script>' + spa.RUNTIME + '</script>' + PROBE + '</body>'))
        page.wait_for_timeout(100)
        self.addCleanup(page.close)
        self.addCleanup(lambda: self.assertEqual(errors, []))
        return page

    def test_recorded_per_field_and_form_level_messages(self):
        self.assertEqual(self.notes, [])
        contact, signup = self.records
        self.assertEqual([(a['text'], bool(a['field'])) for a in contact['added']],
                         [('Please enter your name', True), ('Please enter a valid email', True), ('Tell me a little more', True)])
        self.assertNotIn('ttl', contact['added'][0])
        self.assertEqual([(a['text'], bool(a['field'])) for a in signup['added']], [('Please fill in your email.', False)])
        self.assertTrue(1000 <= signup['added'][0]['ttl'] <= 2500)

    def test_messages_are_in_the_markup_but_hidden_at_rest(self):
        for text in ('Please enter your name', 'Tell me a little more', 'Please fill in your email.'):
            self.assertEqual(self.static.count(text), 1)
        page = self.replay()
        self.assertEqual(page.evaluate(VISIBLE, '[data-spa-invalid]'), [])

    def test_empty_submit_shows_each_field_message_under_its_field_and_stops(self):
        page = self.replay()
        page.click('#contact button')
        self.assertEqual(page.evaluate(VISIBLE, '[data-spa-invalid]'),
                         ['Please enter your name', 'Please enter a valid email', 'Tell me a little more'])
        self.assertEqual(page.evaluate("() => document.querySelector('#n').parentElement.lastElementChild.textContent"),
                         'Please enter your name')
        self.assertEqual(page.evaluate('window.sent'), [])

    def test_editing_a_field_clears_its_message_and_the_rest_stay(self):
        page = self.replay()
        page.click('#contact button')
        page.fill('#n', 'Ada')
        self.assertEqual(page.evaluate(VISIBLE, '[data-spa-invalid]'), ['Please enter a valid email', 'Tell me a little more'])
        page.click('#contact button')
        self.assertEqual(page.evaluate(VISIBLE, '[data-spa-invalid]'), ['Please enter a valid email', 'Tell me a little more'])
        self.assertEqual(page.evaluate('window.sent'), [])

    def test_a_filled_form_submits(self):
        page = self.replay()
        for sel, v in (('#n', 'Ada'), ('#e', 'a@b.c'), ('#m', 'A long enough message')):
            page.fill(sel, v)
        page.click('#contact button')
        self.assertEqual(page.evaluate(VISIBLE, '[data-spa-invalid]'), [])
        self.assertEqual(page.evaluate('window.sent'), ['contact'])

    def test_toast_shows_on_empty_submit_and_leaves_on_its_own(self):
        page = self.replay()
        page.click('#signup button')
        self.assertEqual(page.evaluate(VISIBLE, '[data-spa-invalid]'), ['Please fill in your email.'])
        self.assertEqual(page.evaluate('window.sent'), [])
        page.wait_for_timeout(2700)
        self.assertEqual(page.evaluate(VISIBLE, '[data-spa-invalid]'), [])

    def test_form_level_message_only_in_the_recorded_state(self):
        # A changed select is not "empty": the toast's state was every field at rest.
        page = self.replay()
        page.fill('#u', 'a@b.c')
        page.click('#signup button')
        self.assertEqual(page.evaluate(VISIBLE, '[data-spa-invalid]'), [])
        self.assertEqual(page.evaluate('window.sent'), ['signup'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
