#!/usr/bin/env python3
"""wp-relay.py forwards a loopback port to the test WordPress, byte for byte.

test-env.sh's container mode starts it so http://localhost:<port> — the
WordPress's own siteurl — answers inside the agent's container too. Checked
here without Docker: an HTTP server on one loopback port, the relay on
another, a request through the relay returns the server's bytes and the
request reaches the server with its Host header untouched (WordPress
redirects any other host to its siteurl). A relay asked to listen on a
non-loopback address is refused, so it can never publish a WordPress with
known credentials. --stop-matching stops a relay by the target it names,
the way `down` finds one whose pid was lost.

  python3 test-wp-relay.py
"""
import http.server
import socket
import subprocess
import sys
import threading
import time
import unittest
import urllib.request
from pathlib import Path

SCRIPT = Path(__file__).with_name('wp-relay.py')
BODY = b'<!doctype html><title>wp</title>' + bytes(range(256)) * 64


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


class Page(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        Page.seen.append(self.headers.get('Host'))
        self.send_response(200)
        self.send_header('Content-Length', str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    def log_message(self, *args):
        pass


def wait_listening(port, seconds=10):
    end = time.time() + seconds
    while time.time() < end:
        try:
            socket.create_connection(('127.0.0.1', port), timeout=1).close()
            return True
        except OSError:
            time.sleep(0.1)
    return False


class Relay(unittest.TestCase):
    def setUp(self):
        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Page)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.target = self.server.server_address[1]
        self.port = free_port()
        self.relay = subprocess.Popen([sys.executable, str(SCRIPT), '--listen', f'127.0.0.1:{self.port}',
                                       '--to', f'127.0.0.1:{self.target}'],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.assertTrue(wait_listening(self.port), 'the relay never listened')

    def tearDown(self):
        if self.relay.poll() is None:
            self.relay.terminate()
            self.relay.wait(timeout=10)
        self.server.shutdown()
        self.server.server_close()

    def test_the_bytes_and_the_host_header_pass_through(self):
        Page.seen.clear()
        for _ in range(3):   # several connections, one after another
            with urllib.request.urlopen(f'http://localhost:{self.port}/', timeout=10) as response:
                self.assertEqual(response.read(), BODY)
        self.assertEqual(Page.seen, [f'localhost:{self.port}'] * 3)

    def test_sigterm_stops_it_cleanly(self):
        self.relay.terminate()
        self.assertEqual(self.relay.wait(timeout=10), 0)

    def test_stop_matching_finds_it_by_its_target(self):
        out = subprocess.run([sys.executable, str(SCRIPT), '--stop-matching', f'127.0.0.1:{self.target}'],
                             capture_output=True, text=True, timeout=30)
        self.assertIn('stopped 1 relay', out.stdout)
        self.assertEqual(self.relay.wait(timeout=10), 0)


class Refusals(unittest.TestCase):
    def run_relay(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True, timeout=30)

    def test_only_loopback_is_listened_on(self):
        for listen in ('0.0.0.0:18080', '192.168.1.10:18080', '[::]:18080', 'example.com:18080'):
            result = self.run_relay('--listen', listen, '--to', 'wp:80')
            self.assertEqual(result.returncode, 2, listen)
            self.assertIn('loopback only', result.stderr, listen)

    def test_a_malformed_address_is_usage(self):
        result = self.run_relay('--listen', '127.0.0.1', '--to', 'wp:80')
        self.assertEqual(result.returncode, 2)

    def test_a_port_in_use_fails(self):
        with socket.socket() as held:
            held.bind(('127.0.0.1', 0))
            held.listen()
            port = held.getsockname()[1]
            result = self.run_relay('--listen', f'127.0.0.1:{port}', '--to', 'wp:80')
        self.assertEqual(result.returncode, 1)
        self.assertIn('cannot listen', result.stderr)


if __name__ == '__main__':
    unittest.main()
