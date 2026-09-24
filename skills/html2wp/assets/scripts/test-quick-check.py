#!/usr/bin/env python3
"""quick-check.py — reading what the reporter says about a route, no Docker.

The test WordPress prints no PHP errors, so quick-check-mu.php appends each
checked request's problems as a base64 comment. A warning or an error counts
from any file; a notice or a deprecation only from the theme's own files (a
core deprecation is not the theme's problem). A route that is not a 200, that
prints a PHP error into the page, or whose answer carries no reporter comment
(its errors cannot be seen) fails. Permalinks are fetched on the test
WordPress's own origin, whatever host the importer wrote into them.

  python3 assets/scripts/test-quick-check.py
"""
import base64
import importlib.util
import json
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name('quick-check.py')
spec = importlib.util.spec_from_file_location('quick_check', SCRIPT)
qc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qc)

ROOT = '/var/www/html/wp-content/themes'
E_WARNING, E_NOTICE, E_DEPRECATED = 2, 8, 8192


def body(problems, root=ROOT, html='<html><body>page</body></html>'):
    data = base64.b64encode(json.dumps({'themeRoot': root, 'problems': problems}).encode()).decode()
    return f'{html}\n<!--h2wp-quick:{data}-->'


def row(kind, file, message='boom', line=7):
    return {'type': kind, 'message': message, 'file': file, 'line': line}


class Reporter(unittest.TestCase):
    def test_a_clean_answer_is_no_problem(self):
        self.assertEqual(qc.reported_problems(body([])), [])
        self.assertIsNone(qc.route_problem(200, body([])))

    def test_core_deprecation_ignored_theme_notice_counted_warning_counted_anywhere(self):
        found = qc.reported_problems(body([
            row(E_DEPRECATED, '/var/www/html/wp-includes/functions.php', 'old API'),
            row(E_NOTICE, f'{ROOT}/site/inc/runtime.php', 'Undefined index: x'),
            row(E_WARNING, '/var/www/html/wp-content/plugins/other/plugin.php', 'Division by zero'),
        ]))
        self.assertEqual(found, ['PHP Undefined index: x in /site/inc/runtime.php:7',
                                 'PHP Division by zero in plugin.php:7'])

    def test_no_reporter_comment_means_the_errors_cannot_be_seen(self):
        self.assertIsNone(qc.reported_problems('<html>no comment</html>'))
        self.assertIn('did not answer', qc.route_problem(200, '<html>no comment</html>'))
        self.assertIsNone(qc.reported_problems('<!--h2wp-quick:@@@-->'))

    def test_route_problems(self):
        self.assertEqual(qc.route_problem(404, body([])), 'HTTP 404')
        self.assertEqual(qc.route_problem(0, 'unreachable: refused'), 'HTTP 0')
        printed = body([], html='<b>Warning</b>:  Undefined variable $x in <b>/var/www/html/wp-content/themes/site/functions.php</b>')
        self.assertTrue(qc.route_problem(200, printed).startswith('Warning:'))
        critical = body([], html='<p>There has been a critical error on this website.</p>')
        self.assertIn('critical error', qc.route_problem(200, critical))
        reported = qc.route_problem(200, body([row(E_WARNING, f'{ROOT}/site/functions.php', 'bad')]))
        self.assertEqual(reported, 'PHP bad in /site/functions.php:7')


class Origins(unittest.TestCase):
    def test_permalinks_are_fetched_on_the_test_origin(self):
        base = 'http://localhost:63838'
        self.assertEqual(qc.local_url('http://example.test/about/', base), 'http://localhost:63838/about/')
        self.assertEqual(qc.local_url('https://localhost:1/blog/post/?p=1', base + '/'), 'http://localhost:63838/blog/post/?p=1')
        self.assertEqual(qc.route_path('http://localhost:63838/'), '/')
        self.assertEqual(qc.route_path('http://localhost:63838'), '/')
        self.assertEqual(qc.route_path('http://x.test/a/b/'), '/a/b/')

    def test_target_follows_the_manifest_unless_named(self):
        class A:
            target = ''
        import os
        saved = os.environ.pop('H2WP_TARGET', None)
        try:
            self.assertEqual(qc.target_of(A(), {'schema': 'html2wp/1'}), 'html')
            self.assertEqual(qc.target_of(A(), {'schema': 'html2wp/2', 'target': 'gutenberg'}), 'gutenberg')
            A.target = 'html'
            self.assertEqual(qc.target_of(A(), {'schema': 'html2wp/2'}), 'html')
        finally:
            if saved is not None:
                os.environ['H2WP_TARGET'] = saved


if __name__ == '__main__':
    unittest.main()
