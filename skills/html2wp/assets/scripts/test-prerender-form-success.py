#!/usr/bin/env python3
"""prerender-spa.py's form success probe, against hand-built pages.

A valid submit is recorded only when the app decides the success in the
browser: a toast, an inline "Thanks", a form swapped for a message. A form
that sends a request (and so would show whatever the server answered) is never
recorded, and a search form is never submitted.

  python3 test-prerender-form-success.py
"""
import http.server
import importlib.util
import json
import socketserver
import sys
import tempfile
import threading
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

HELPERS_OK = hasattr(spa, 'HELPERS')

PAGES = {
    # Newsletter: validates an email, then a toast in a fixed list outside the form; it leaves after 1s.
    'toast.html': """<!doctype html><html><body><footer><form id="nl"><input type="email" required name="email" placeholder="Email">
<button type="submit">Sign up</button></form></footer><div role="region" aria-label="Notifications"><ol class="toasts" style="position:fixed;top:0;right:0"></ol></div>
<script>document.getElementById('nl').addEventListener('submit', (e) => { e.preventDefault();
  const li = document.createElement('li'); li.setAttribute('role', 'status'); li.className = 'toast';
  li.innerHTML = '<div class="t">You\\'re subscribed!</div><div class="d">Thank you for signing up.</div>';
  document.querySelector('.toasts').appendChild(li); setTimeout(() => li.remove(), 1000); });</script></body></html>""",
    # Contact: the form is replaced by a thank-you block that stays.
    'replace.html': """<!doctype html><html><body><main><div id="box"><form><input name="name" required><textarea name="message" required></textarea>
<button>Send</button></form></div></main><script>document.querySelector('form').addEventListener('submit', (e) => { e.preventDefault();
  document.getElementById('box').innerHTML = '<p class="thanks">Thanks, we will be in touch.</p>'; });</script></body></html>""",
    # Answers after a pretend round-trip (a timer): the page is quiet until then.
    'delayed.html': """<!doctype html><html><body><form><input type="email" name="email" required><button>Join</button></form>
<script>document.querySelector('form').addEventListener('submit', (e) => { e.preventDefault();
  setTimeout(() => document.body.insertAdjacentHTML('beforeend', '<div role="status" style="position:fixed;top:0;padding:16px">Welcome aboard</div>'), 700); });</script></body></html>""",
    # Posts to an API first: never recorded, whatever it shows.
    'posting.html': """<!doctype html><html><body><form><input type="email" name="email" required><button>Go</button></form>
<script>document.querySelector('form').addEventListener('submit', async (e) => { e.preventDefault();
  try { await fetch('/api/subscribe', { method: 'POST' }); } catch (x) {}
  document.body.insertAdjacentHTML('beforeend', '<p class="err">Could not subscribe</p>'); });</script></body></html>""",
    # A validator stricter than the values filled: its field error is not a success.
    'strict.html': """<!doctype html><html><body><form novalidate><div><input name="code" required>
<p class="err" hidden></p></div><button>Send</button></form><script>document.querySelector('form').addEventListener('submit', (e) => { e.preventDefault();
  const i = document.querySelector('input'); if (!/^\\d{6}$/.test(i.value)) { i.setAttribute('aria-invalid', 'true');
  const p = document.createElement('p'); p.textContent = 'Enter the 6-digit code'; i.after(p); } });</script></body></html>""",
    # A message field with a minimum length gets a long enough value.
    'minlen.html': """<!doctype html><html><body><form><input name="subject"><textarea name="message" minlength="60" required></textarea>
<button>Send</button></form><script>document.querySelector('form').addEventListener('submit', (e) => { e.preventDefault();
  document.body.insertAdjacentHTML('beforeend', '<div class="ok" style="padding:20px">Message sent!</div>'); });</script></body></html>""",
    # A search form and a plain form that navigates: neither recorded.
    'search.html': """<!doctype html><html><body><form role="search"><input type="search" name="q" value="x"><button>Search</button></form>
<form action="/elsewhere"><input name="name" required><button>Send</button></form></body></html>""",
}


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


class FormSuccessTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        for name, html in PAGES.items():
            (Path(cls.dir.name) / name).write_text(html)
        handler = lambda *a, **k: Handler(*a, directory=cls.dir.name, **k)
        cls.httpd = socketserver.TCPServer(('127.0.0.1', 0), handler)
        cls.base = f'http://127.0.0.1:{cls.httpd.server_address[1]}/'
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch()
        cls.ctx = cls.browser.new_context()
        cls.ctx.add_init_script(spa.HELPERS)
        cls.page = cls.ctx.new_page()

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()
        cls.httpd.shutdown()

    def probe(self, name):
        return spa.record_form_success(self.page, self.base + name)

    def test_a_client_side_toast_is_recorded_with_its_list_and_duration(self):
        [fb] = self.probe('toast.html')
        self.assertEqual(fb['kind'], 'toast')
        self.assertEqual(fb['title'], "You're subscribed!")
        self.assertEqual(fb['description'], 'Thank you for signing up.')
        self.assertIn('class="toast"', fb['html'])
        self.assertTrue(fb['list'].startswith('<ol class="toasts"') and fb['list'].endswith('</ol>'))
        self.assertTrue(fb['region'].startswith('<div role="region"'))
        self.assertTrue(800 <= fb['ms'] <= 1600, fb['ms'])

    def test_a_form_swapped_for_a_message(self):
        [fb] = self.probe('replace.html')
        self.assertEqual(fb['kind'], 'replace')
        self.assertIn('Thanks, we will be in touch.', fb['text'])
        self.assertIsNone(fb['ms'])

    def test_a_form_that_sends_a_request_is_not_recorded(self):
        before = len(spa.report.get('formsPosting', []))
        self.assertEqual(self.probe('posting.html'), [])
        self.assertEqual(len(spa.report.get('formsPosting', [])), before + 1)

    def test_a_delayed_answer_is_waited_for(self):
        [fb] = self.probe('delayed.html')
        self.assertEqual((fb['kind'], fb['text']), ('toast', 'Welcome aboard'))

    def test_a_validation_error_is_not_a_success(self):
        self.assertEqual(self.probe('strict.html'), [])

    def test_min_length_is_met(self):
        [fb] = self.probe('minlen.html')
        self.assertEqual(fb['text'], 'Message sent!')

    def test_search_and_navigating_forms_are_not_recorded(self):
        self.assertEqual(self.probe('search.html'), [])


if __name__ == '__main__':
    unittest.main()
