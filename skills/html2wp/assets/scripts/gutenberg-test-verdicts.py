#!/usr/bin/env python3
"""send-verdicts.sh for both targets: the gate set follows the manifest, and
the Gutenberg gates are read off h2wp-local-verification/2 as the service's
docs/GUTENBERG-VERDICTS.md specifies. Runs with --dry-run; nothing is sent."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

CLIENT = Path(__file__).with_name('send-verdicts.sh')
PAGES = [{'key': 'front-page', 'file': 'index.html', 'kind': 'front'},
         {'key': 'about', 'file': 'about/index.html', 'kind': 'page'},
         {'key': 'nav', 'file': 'nav.html', 'kind': 'fragment'}]


def report(**over):
    r = {'schema': 'h2wp-local-verification/2', 'passed': True, 'contractSchema': 'h2wp-blocks/2',
         'visual': [{'path': '/', 'width': w, 'diff': 0.001, 'passed': True} for w in (1440, 390)]
                   + [{'path': '/about/', 'width': 1440, 'diff': 0.004, 'passed': True}],
         'editor': [{'slug': 'front-page', 'kind': 'page', 'invalid': [], 'unknown': [],
                     'roundtrip': {'invalid': [], 'unknown': [], 'textPersisted': True}},
                    {'slug': 'about', 'kind': 'page', 'invalid': [], 'unknown': [],
                     'roundtrip': {'invalid': [], 'unknown': [], 'textPersisted': True}}],
         'editorVisual': [{'diff': 0.002, 'passed': True}],
         'newPost': {'passed': True}, 'newPage': {'passed': True},
         'import': {'passed': True}, 'preview': {'passed': True}}
    r.update(over)
    return r


class VerdictsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp.name)
        (self.ws / '.h2wp-job.json').write_text(json.dumps({'job': 'job-1', 'token': 't'}))
        self.manifest(schema='html2wp/2', target='gutenberg')
        (self.ws / 'gutenberg-routes.json').write_text(json.dumps(
            [{'source': '/index.html', 'target': '/'}, {'source': '/about/index.html', 'target': '/about/'}]))

    def tearDown(self):
        self.temp.cleanup()

    def manifest(self, **fields):
        (self.ws / 'conversion-manifest.json').write_text(json.dumps(dict(site={'slug': 'demo'}, pages=PAGES, **fields)))

    def payload(self, verification=None, **env):
        if verification is not None:
            (self.ws / 'gutenberg-verification.json').write_text(json.dumps(verification))
        run = subprocess.run(['bash', str(CLIENT), str(self.ws), '--dry-run', '--outcome=delivered'],
                             capture_output=True, text=True, env=dict(os.environ, **env), timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        body = run.stdout.split('This is the whole payload:', 1)[1]
        return {g['gate']: g for g in json.loads(body)['gates']}

    def test_v1_manifest_keeps_the_html_gates(self):
        self.manifest(schema='html2wp/1')
        self.assertEqual(list(self.payload(report())), ['A', 'A2', 'B', 'C', 'smoke-editor', 'woo-coverage'])

    def test_passing_report(self):
        gates = self.payload(report())
        self.assertEqual(list(gates), ['A', 'A2', 'G-front', 'G-editor', 'G-roundtrip', 'G-import', 'woo-coverage'])
        self.assertEqual(gates['G-front'], {'gate': 'G-front', 'verdict': 'passed', 'pages': 2, 'worstPct': 0.4})
        self.assertEqual(gates['G-editor'], {'gate': 'G-editor', 'verdict': 'passed', 'pages': 2, 'worstPct': 0.2})
        self.assertEqual(gates['G-roundtrip'], {'gate': 'G-roundtrip', 'verdict': 'passed', 'pages': 2})
        self.assertEqual(gates['G-import'], {'gate': 'G-import', 'verdict': 'passed'})

    def test_missing_report_is_not_run_everywhere(self):
        gates = self.payload()
        for name in ('G-front', 'G-editor', 'G-roundtrip', 'G-import'):
            self.assertEqual(gates[name], {'gate': name, 'verdict': 'not-run', 'notRun': ['no-report']})

    def test_failures_travel_as_keys_only(self):
        r = report()
        r['visual'][2].update(passed=False, diff=0.03)
        r['visual'].append({'path': '/unknown-route/', 'width': 390, 'diff': 0.5, 'passed': False})
        r['editor'][1]['invalid'] = ['core/paragraph']
        r['editor'][0]['roundtrip']['textPersisted'] = False
        r['newPage'] = {'passed': False}
        gates = self.payload(r)
        self.assertEqual(gates['G-front']['verdict'], 'failed')
        self.assertEqual(gates['G-front']['failedKeys'], ['about'])
        self.assertEqual(gates['G-front']['worstPct'], 50.0)
        self.assertEqual(gates['G-editor']['failedKeys'], ['about'])
        self.assertEqual(gates['G-roundtrip']['failedKeys'], ['front-page', 'new-page'])
        self.assertNotIn('/unknown-route/', json.dumps(gates))

    def test_absent_sections_are_not_passes(self):
        r = report(editorVisual=[], newPage=None, preview=None)
        r.pop('newPage'); r.pop('preview')
        gates = self.payload(r)
        self.assertEqual(gates['G-editor']['verdict'], 'failed')
        self.assertEqual(gates['G-editor']['notRun'], ['editor-visual'])
        self.assertEqual((gates['G-roundtrip']['verdict'], gates['G-roundtrip']['notRun']), ('failed', ['new-page']))
        self.assertEqual((gates['G-import']['verdict'], gates['G-import']['notRun']), ('failed', ['preview']))

    def test_aborted_run_and_skipped_roundtrip(self):
        r = {'schema': 'h2wp-local-verification/2', 'passed': False, 'error': 'boom',
             'visual': [], 'editor': [], 'editorVisual': []}
        gates = self.payload(r)
        for name in ('G-front', 'G-editor', 'G-roundtrip', 'G-import'):
            self.assertEqual(gates[name], {'gate': name, 'verdict': 'not-run', 'notRun': ['aborted']})

    def test_job_state_from_environment(self):
        moved = self.ws / 'private.json'
        (self.ws / '.h2wp-job.json').rename(moved)
        self.payload(report(), H2WP_JOB_STATE=str(moved))


if __name__ == '__main__':
    unittest.main()
