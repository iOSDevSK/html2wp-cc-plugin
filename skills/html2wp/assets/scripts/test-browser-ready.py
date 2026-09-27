#!/usr/bin/env python3
"""Real browser: stream, delayed data, bad document and per-page isolation."""
import struct
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
from lib.browser_ready import track_context, goto_ready, wait_ready


class Readiness(unittest.TestCase):
    def test_readiness_is_content_aware_and_page_scoped(self):
        stop = threading.Event()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                if self.path.split('?')[0] in ('/hang', '/blocked'):
                    stop.wait(15)
                    return
                if self.path == '/audio.wav':
                    self.send_response(200)
                    self.send_header('Content-Type', 'audio/wav')
                    self.send_header('Content-Length', str(100000044))
                    self.end_headers()
                    header = struct.pack('<4sI4s4sIHHIIHH4sI', b'RIFF', 100000036, b'WAVE', b'fmt ', 16, 1, 1, 8000, 16000, 2, 16, b'data', 100000000)
                    try:
                        self.wfile.write(header + bytes(16000)); self.wfile.flush()
                        stop.wait(15)
                    except (BrokenPipeError, ConnectionResetError): pass
                    return
                if self.path == '/data':
                    time.sleep(0.8)
                    body = b'Loaded data'
                elif self.path == '/delayed':
                    body = b'<h1>Waiting</h1><script>fetch("/data").then(r=>r.text()).then(t=>document.querySelector("h1").textContent=t)</script>'
                elif self.path == '/stream':
                    body = b'<h1>Visible page</h1><audio preload="auto" src="/audio.wav"></audio>'
                elif self.path in ('/fetch-hang', '/slow-fetch-hang'):
                    if self.path == '/slow-fetch-hang':
                        time.sleep(0.8)
                    body = b'<h1>Pending content</h1><script>fetch("/blocked?token=SECRET")</script>'
                else:
                    body = b'<h1>Plain page</h1>'
                self.send_response(500 if self.path == '/error' else 404 if self.path == '/missing' else 200)
                self.send_header('Content-Type', 'text/html')
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                context = browser.new_context()
                track_context(context)
                page = context.new_page()
                notes = []
                goto_ready(page, base + '/stream', warn=notes.append, timeout=3000)
                self.assertEqual(page.locator('h1').inner_text(), 'Visible page')
                self.assertTrue(notes, 'the open media stream must be reported')
                goto_ready(page, base + '/delayed', timeout=3000)
                self.assertEqual(page.locator('h1').inner_text(), 'Loaded data')
                for route in ('/error', '/missing'):
                    with self.assertRaisesRegex(RuntimeError, 'Source document returned HTTP'):
                        goto_ready(page, base + route, timeout=3000)
                with self.assertRaises(PlaywrightTimeoutError):
                    goto_ready(page, base + '/hang', timeout=300)
                blocked = context.new_page()
                blocked.goto(base + '/fetch-hang', wait_until='domcontentloaded')
                other = context.new_page()
                goto_ready(other, base + '/', timeout=3000)
                self.assertEqual(other.locator('h1').inner_text(), 'Plain page')
                with self.assertRaises(PlaywrightTimeoutError) as failure:
                    wait_ready(blocked, timeout=300)
                self.assertIn('fetch age=', str(failure.exception))
                self.assertIn('/blocked', str(failure.exception))
                self.assertNotIn('SECRET', str(failure.exception))
                budget_page = context.new_page()
                started = time.monotonic()
                with self.assertRaises(PlaywrightTimeoutError):
                    goto_ready(budget_page, base + '/slow-fetch-hang', timeout=1000)
                self.assertLess(time.monotonic() - started, 1.45, 'document and pending data share one budget')
                browser.close()
        finally:
            stop.set()
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    unittest.main()
