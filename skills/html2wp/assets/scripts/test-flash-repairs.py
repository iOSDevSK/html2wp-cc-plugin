#!/usr/bin/env python3
"""Flash repairs itself inside a bounded budget, and a result is final.

- Stage 6's draft result never marks the workspace delivered, so stage 6.5
  starts; only stage 7's final result does, and then nothing starts.
- A stopped run starts no stage and no `mode`; a new run over any result
  needs the owner's Start over (H2WP_START_OVER=1).
- The budget: a repair is a named lever for the failure a script named,
  inside the running stage; the same lever on the same failure twice, a
  third attempt at one stage and a fifth in the run are refused (exit 3);
  a stuck lever gives up after two attempts, the stage is recorded red and
  the run goes on.
- result.json carries every attempt and what the attempts could not fix;
  the PDF's HTML has a "Repairs" table and "What Flash could not fix".
- test-env.sh info names the preview's address, or says plainly it is not up.
- A stopped run is repaired only in the owner's turn (H2WP_MODE=repair-stop):
  2 attempts per message on the stage that stopped it; a fixed one puts the
  stopped result aside and the run continues from that stage. The turn's
  kind never changes the run's mode.

  python3 test-flash-repairs.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('twr', HERE / 'test-write-result.py')
twr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(twr)
spec = importlib.util.spec_from_file_location('report_pdf', HERE / 'report-pdf.py')
report_pdf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report_pdf)


def run(*argv, env=None):
    return subprocess.run([str(a) for a in argv], capture_output=True, text=True, timeout=300,
                          env={**{k: v for k, v in os.environ.items() if not k.startswith('H2WP_')}, **(env or {})})


class Flash(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = twr.workspace(Path(self.tmp.name) / 'a folder')
        self.out = Path(self.tmp.name) / 'out dir'
        self.env = {'H2WP_WORKSPACE': str(self.ws), 'H2WP_OUTPUT_DIR': str(self.out), 'H2WP_MODE': 'flash'}
        self.assertEqual(self.progress('mode', 'flash').returncode, 0)

    def tearDown(self):
        self.tmp.cleanup()

    def progress(self, *args, env=None):
        return run('bash', HERE / 'progress.sh', *args, env={**self.env, **(env or {})})

    def result(self, *args, env=None):
        return run(sys.executable, HERE / 'write-result.py', self.ws, '--no-pdf', *args, env={**self.env, **(env or {})})

    def doc(self, name='progress.json'):
        return json.loads((self.ws / name).read_text())

    def test_the_draft_never_marks_the_workspace_delivered(self):
        for stage in ('6',):
            self.assertEqual(self.progress('start', stage).returncode, 0)
        done = self.result('--draft')
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads((self.ws / 'result-draft' / 'result.json').read_text())['status'], 'delivered')
        self.assertFalse((self.ws / 'result.json').exists(), 'a draft is not the run\'s result')
        # The habit the table used to teach is a draft too.
        self.assertEqual(self.result('--output', self.ws / 'result-draft').returncode, 0)
        self.assertFalse((self.ws / 'result.json').exists())
        self.assertEqual(self.progress('done', '6').returncode, 0)
        self.assertEqual(self.progress('start', '6.5').returncode, 0, 'the verdicts go out before the result')
        self.assertEqual(self.progress('done', '6.5').returncode, 0)
        self.assertEqual(self.progress('start', '7').returncode, 0)
        self.assertEqual(self.result().returncode, 0)
        self.assertEqual(self.doc('result.json')['status'], 'delivered')
        self.assertEqual(self.progress('done', '7').returncode, 0)
        self.assertEqual(self.progress('start', '5').returncode, 3, 'delivered: nothing starts')

    def test_a_stopped_run_starts_nothing_and_never_starts_over_by_itself(self):
        self.assertEqual(self.progress('start', '3.5').returncode, 0)
        self.assertEqual(self.progress('fail', '3.5', 'make-zip refused').returncode, 0)
        self.assertEqual(self.result('--status', 'stopped', '--stopped-stage', '3.5',
                                     '--stopped-reason', 'make-zip refused').returncode, 0)
        before = (self.ws / 'progress.json').read_text()
        for args in (('start', '3.5'), ('start', '5'), ('mode', 'flash'), ('mode', 'flash', '--new')):
            done = self.progress(*args)
            self.assertEqual(done.returncode, 3, args)
        self.assertIn('repair', self.progress('start', '5').stderr)
        self.assertEqual((self.ws / 'progress.json').read_text(), before, 'a refusal writes nothing')
        # Only the owner's Start over begins a new run over a result.
        new = self.progress('mode', 'flash', '--new', env={'H2WP_START_OVER': '1'})
        self.assertEqual(new.returncode, 0, new.stderr)
        self.assertFalse((self.ws / 'result.json').exists())
        self.assertEqual(self.progress('start', '-4').returncode, 0)

    def test_a_delivered_project_starts_over_only_on_the_owners_word(self):
        self.assertEqual(self.result().returncode, 0)
        self.assertEqual(self.progress('mode', 'flash', '--new').returncode, 3)
        self.assertEqual(self.progress('mode', 'flash', '--new', env={'H2WP_START_OVER': '1'}).returncode, 0)

    def test_a_stuck_fix_gives_up_after_three_attempts_and_the_run_goes_on(self):
        repair = lambda stage, lever, sig: self.progress('repair', stage, lever, sig)
        self.assertEqual(repair('3.5', 'article-part-residue', 'article-part-foreign').returncode, 3,
                         'not running yet: a repair happens inside the stage')
        self.assertEqual(self.progress('start', '3.5').returncode, 0)
        self.assertEqual(repair('3.5', 'cart-count', 'article-part-foreign').returncode, 3, 'not its lever')
        self.assertEqual(repair('3.5', 'cart-count', 'cart-count-stale').returncode, 3, 'not its stage')
        self.assertEqual(repair('3.5', 'article-part-residue', 'no-such-failure').returncode, 3)
        self.assertEqual(repair('3.5', 'ai-fix', 'article-part-foreign').returncode, 3,
                         'the named levers come before the AI\'s own fix')
        first = repair('3.5', 'article-part-residue', 'article-part-foreign')
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertIn('repair 1/3 of stage 3.5', first.stdout)
        self.assertEqual(repair('3.5', 'article-part-other-page', 'article-part-foreign').returncode, 3, 'one open')
        self.assertEqual(self.progress('repaired', '3.5', 'failed', 'make-zip still refuses').returncode, 0)
        again = repair('3.5', 'article-part-residue', 'article-part-foreign')
        self.assertEqual(again.returncode, 3, 'the same lever on the same failure twice is a loop')
        self.assertIn('article-part-other-page', again.stderr)
        self.assertEqual(repair('3.5', 'article-part-other-page', 'article-part-foreign').returncode, 0)
        self.assertEqual(self.progress('repaired', '3.5', 'failed').returncode, 0)
        spent = repair('3.5', 'article-part-other-page', 'article-part-foreign')
        self.assertEqual(spent.returncode, 3)
        self.assertIn('ai-fix', spent.stderr, 'the named levers are spent: the AI\'s own fix is next')
        self.assertEqual(repair('3.5', 'ai-fix', 'article-part-foreign').returncode, 0)
        self.assertEqual(self.progress('repaired', '3.5', 'failed', 'changed X; still refused').returncode, 0)
        self.assertEqual(repair('3.5', 'ai-fix', 'article-part-foreign').returncode, 3, 'three attempts per stage')
        self.assertEqual(self.progress('warn', '3.5', 'make-zip: parts/article.html foreign').returncode, 0)
        self.assertEqual(self.progress('start', '3.5').returncode, 3, 'never a second run of the stage')
        self.assertEqual(self.progress('start', '5').returncode, 0, 'the run goes on')
        unnamed = repair('5', 'ai-fix', 'unnamed')
        self.assertEqual(unnamed.returncode, 0, 'a failure no script named: the AI\'s own fix, at any stage')
        self.assertEqual(self.progress('repaired', '5', 'fixed', 'restarted the service').returncode, 0)
        self.assertEqual(self.progress('done', '5').returncode, 0)
        self.assertEqual(self.progress('start', '5.6').returncode, 0)
        before = self.doc()['percent']
        self.assertEqual(repair('5.6', 'cart-count', 'cart-count-missing').returncode, 0)
        self.assertEqual(self.doc()['percent'], before, 'a repair inside a stage never moves the bar')
        self.assertEqual(self.progress('repaired', '5.6', 'fixed', 'the badge shows').returncode, 0)
        self.assertEqual(self.progress('warn', '5.6', 'the count does not follow the cart').returncode, 0)

        repairs = self.doc()['repairs']
        self.assertEqual([(r['stage'], r['attempt'], r['lever'], r['outcome']) for r in repairs],
                         [('3.5', 1, 'article-part-residue', 'failed'), ('3.5', 2, 'article-part-other-page', 'failed'),
                          ('3.5', 3, 'ai-fix', 'failed'), ('5', 1, 'ai-fix', 'fixed'), ('5.6', 1, 'cart-count', 'fixed')])
        self.assertEqual(self.result().returncode, 0)
        result = self.doc('result.json')
        self.assertEqual(len(result['repairs']), 5)
        self.assertEqual(sorted((u['stage'], u['signature']) for u in result['couldNotFix']),
                         [('3.5', 'article-part-foreign')])
        page = report_pdf.document(result, '# Report\n')
        self.assertIn('<h2>Repairs</h2>', page)
        self.assertIn('What Flash could not fix', page)
        self.assertIn('still red', page)

    def test_the_apps_turn_id_bounds_each_message_and_the_run_goes_on_after_a_fix(self):
        self.assertEqual(self.progress('start', '3.5').returncode, 0)
        self.assertEqual(self.progress('fail', '3.5', 'make-zip refused').returncode, 0)
        stop = lambda env: self.result('--status', 'stopped', '--stopped-stage', '3.5', '--stopped-reason', 'x', env=env)
        self.assertEqual(stop({}).returncode, 0)
        turn_a = {'H2WP_MODE': 'repair-stop', 'H2WP_TURN': 'msg-a'}
        for lever in ('article-part-residue', 'article-part-other-page', 'ai-fix'):
            self.assertEqual(self.progress('repair', '3.5', lever, 'article-part-foreign', env=turn_a).returncode, 0)
            self.assertEqual(self.progress('repaired', '3.5', 'failed', env=turn_a).returncode, 0)
        # Writing the stop again inside the same turn mints nothing.
        self.assertEqual(stop(turn_a).returncode, 0)
        self.assertEqual(self.progress('repair', '3.5', 'article-part-residue', 'article-part-foreign',
                                       env=turn_a).returncode, 3)
        turn_b = {'H2WP_MODE': 'repair-stop', 'H2WP_TURN': 'msg-b'}
        self.assertEqual(self.progress('repair', '3.5', 'article-part-residue', 'article-part-foreign',
                                       env=turn_b).returncode, 0, "the owner's next message")
        self.assertEqual(self.progress('repaired', '3.5', 'fixed', env=turn_b).returncode, 0)
        self.assertEqual(self.doc()['state'], 'running', 'a Continue resumes a running run, not a stop')
        self.assertEqual(self.progress('done', '3.5', env=turn_b).returncode, 0)
        self.assertEqual(self.progress('start', '5.6', env=turn_b).returncode, 0)
        later = self.progress('repair', '5.6', 'cart-count', 'cart-count-missing', env=turn_b)
        self.assertEqual(later.returncode, 0, later.stderr)
        self.assertEqual(self.doc()['repairs'][-1]['by'], 'run', "after the fix the run's own budget")

    def test_every_signature_a_script_prints_has_levers_and_every_lever_is_reachable(self):
        import re
        table = json.loads((HERE.parent / 'repair-levers.json').read_text())
        printed = set()
        for path in list(HERE.glob('*.py')) + list(HERE.glob('*.sh')) + list(HERE.glob('*.mjs')):
            if path.name.startswith('test-'):
                continue
            text = path.read_text(errors='replace')
            printed |= set(re.findall(r'h2wp-signature: ([a-z-]+)', text))
            printed |= set(re.findall(r'"(cart-[a-z-]+)"', text)) & set(table['signatures'])
        # `unnamed` is the failure no script names; `ai-fix` is its lever and every spent failure's last one.
        self.assertEqual(printed | {'unnamed'}, set(table['signatures']), 'a key no script prints, or a printed key with no levers')
        used = {lv for sig in table['signatures'].values() for lv in sig['levers']}
        self.assertEqual(used | {'ai-fix'}, set(table['levers']))
        for lever in table['levers'].values():
            self.assertTrue(lever['label'] and lever['remedy'] and lever['again'])

    def test_the_preview_address_is_never_empty(self):
        info = lambda slug: subprocess.run(['bash', str(HERE / 'test-env.sh'), 'info', slug, 'url'], cwd=self.ws,
                                           capture_output=True, text=True, timeout=60)
        self.assertEqual(info('test-site').stdout.strip(), 'http://localhost:55123')
        missing = info('another-site')
        self.assertEqual(missing.returncode, 1)
        self.assertEqual(missing.stdout, '', 'never an empty address on stdout')
        self.assertIn('not up', missing.stderr)

    def test_a_lever_edit_goes_into_the_runs_zip_only_inside_an_attempt(self):
        spec = importlib.util.spec_from_file_location('tap', HERE / 'test-article-part.py')
        tap = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tap)
        page = f'<html><body>{tap.SITE_HEADER}<main class="page">{tap.ARTICLE}</main></body></html>'
        ws, theme = tap.workspace(Path(self.tmp.name) / 'shop', page)
        env = {**self.env, 'H2WP_WORKSPACE': str(ws)}
        self.assertEqual(run('bash', HERE / 'progress.sh', 'mode', 'flash', env=env).returncode, 0)
        self.assertEqual(run('bash', HERE / 'theme-zip.sh', ws, env=env).returncode, 0)
        apply = lambda: run(sys.executable, HERE / 'apply-change.py', ws, '--repair', '5.6', '--skip-install', env=env)
        (theme / 'parts' / 'header.html').write_text(
            '<!-- wp:html -->\n<header class="site-header"><a href="/cart/">Bag'
            '[wp-cart-count empty="hide"]<span class="bag">{count}</span>[/wp-cart-count]</a></header>\n<!-- /wp:html -->\n')
        self.assertEqual(apply().returncode, 2, 'no open repair: no application')
        self.assertEqual(run('bash', HERE / 'progress.sh', 'start', '5.6', env=env).returncode, 0)
        self.assertEqual(run('bash', HERE / 'progress.sh', 'repair', '5.6', 'cart-count', 'cart-count-missing',
                             env=env).returncode, 0)
        done = apply()
        self.assertEqual(done.returncode, 0, done.stderr)
        import zipfile
        with zipfile.ZipFile(ws / 'label-1.0.0.zip') as z:
            self.assertIn('wp-cart-count', z.read('label/parts/header.html').decode())
        row = json.loads((ws / 'progress.json').read_text())['repairs'][-1]
        self.assertEqual(row['applied']['files'], ['parts/header.html'])
        self.assertEqual(apply().returncode, 3, 'nothing more to apply')

    def test_the_owner_repairs_a_stopped_run_and_it_continues_from_there(self):
        self.assertEqual(self.progress('start', '3.5').returncode, 0)
        self.assertEqual(self.progress('fail', '3.5', 'make-zip refused parts/article.html').returncode, 0)
        self.assertEqual(self.result('--status', 'stopped', '--stopped-stage', '3.5',
                                     '--stopped-reason', 'make-zip refused').returncode, 0)
        owner = {'H2WP_MODE': 'repair-stop'}
        sig = ('article-part-residue', 'article-part-foreign')
        self.assertEqual(self.progress('repair', '3.5', *sig).returncode, 3, 'only in the owner\'s turn')
        self.assertEqual(self.progress('repair', '5', 'preview-again', 'preview-down', env=owner).returncode, 3,
                         'only the stage that stopped the run')
        self.assertEqual(self.progress('repair', '3.5', *sig, env=owner).returncode, 0)
        self.assertEqual(self.progress('repaired', '3.5', 'failed', env=owner).returncode, 0)
        self.assertEqual(self.progress('repair', '3.5', 'article-part-other-page', 'article-part-foreign',
                                       env=owner).returncode, 0)
        self.assertEqual(self.progress('repaired', '3.5', 'failed', env=owner).returncode, 0)
        self.assertEqual(self.progress('repair', '3.5', *sig, env=owner).returncode, 3, 'two per message')
        # The owner's next message is the owner's own decision to try again.
        self.assertEqual(self.result('--status', 'stopped', '--stopped-stage', '3.5',
                                     '--stopped-reason', 'make-zip refused', env=owner).returncode, 0)
        self.assertEqual(self.doc('result.json')['mode'], 'flash', 'the turn\'s kind is not the run\'s mode')
        self.assertEqual(self.progress('repair', '3.5', *sig, env=owner).returncode, 0)
        self.assertEqual(self.progress('repaired', '3.5', 'fixed', 'make-zip passes', env=owner).returncode, 0)
        self.assertFalse((self.ws / 'result.json').exists(), 'the stop is repaired')
        self.assertEqual(len(list(self.ws.glob('result-stopped-*.json'))), 1)
        self.assertEqual(self.progress('done', '3.5', env=owner).returncode, 0)
        self.assertEqual(self.progress('start', '3.5', env=owner).returncode, 3, 'never a second run')
        self.assertEqual(self.progress('start', '5', env=owner).returncode, 0, 'continues from there')
        self.assertEqual(self.doc()['mode'], 'flash')
        self.assertEqual(self.doc()['state'], 'running')


if __name__ == '__main__':
    unittest.main()
