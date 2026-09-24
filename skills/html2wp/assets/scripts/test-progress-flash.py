#!/usr/bin/env python3
"""progress.sh writes the progress.json a UI polls, and knows a Flash run.

- `progress.sh mode flash` records the mode in the workspace and lists every
  stage of the Flash table as pending, so a UI can draw the run before it
  starts; later calls without H2WP_MODE still use the Flash table.
- start/done/warn/skip/fail set that stage's state (running, done, warned,
  skipped, failed) and the run's (running; stopped after a fail; finished
  after stage 7).
- warn is a red check that was recorded and not repaired: the run goes on.
- The Flash table's percentages rise in the order the stages run.
- H2WP_PROGRESS_FILE puts the snapshot somewhere else.
- In Flash no stage starts twice: `start` of a stage that already ran exits 3.

  python3 test-progress-flash.py
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).with_name('progress.sh')


def progress(ws, *args, **env):
    full = {k: v for k, v in os.environ.items() if k not in ('H2WP_MODE', 'H2WP_PROGRESS_FILE')}
    full.update(H2WP_WORKSPACE=str(ws), **env)
    return subprocess.run(['bash', str(SCRIPT), *args], capture_output=True, text=True, env=full, timeout=30)


def snapshot(ws):
    return json.loads((Path(ws) / 'progress.json').read_text())


class ProgressFlash(unittest.TestCase):
    def test_a_flash_run_from_mode_to_finish(self):
        with tempfile.TemporaryDirectory() as ws:
            self.assertEqual(progress(ws, 'mode', 'flash').returncode, 0)
            doc = snapshot(ws)
            self.assertEqual((doc['schema'], doc['mode'], doc['state'], doc['percent']), ('h2wp-progress/1', 'flash', 'running', 0))
            self.assertTrue(all(s['state'] == 'pending' for s in doc['stages']))
            self.assertEqual((Path(ws) / '.h2wp-mode').read_text().strip(), 'flash')
            percents = [s['percent'] for s in doc['stages']]
            self.assertEqual(percents, sorted(percents), 'Flash percentages rise in run order')

            progress(ws, 'start', '0')
            self.assertEqual(snapshot(ws)['stages'][3]['state'], 'running')
            progress(ws, 'done', '0', '12 pages')
            out = progress(ws, 'warn', '2', 'gate A: 2 of 12 pages over 0.6%')
            self.assertIn('RED, recorded (not repaired)', out.stdout)
            progress(ws, 'skip', '5.6', 'no shop')
            doc = snapshot(ws)
            states = {s['stage']: s['state'] for s in doc['stages']}
            self.assertEqual((states['0'], states['2'], states['5.6'], states['3']), ('done', 'warned', 'skipped', 'pending'))
            self.assertEqual(doc['state'], 'running')
            self.assertEqual(doc['stages'][[s['stage'] for s in doc['stages']].index('2')]['note'],
                             'gate A: 2 of 12 pages over 0.6%')
            progress(ws, 'done', '7')
            doc = snapshot(ws)
            self.assertEqual((doc['state'], doc['percent']), ('finished', 100))
            events = [json.loads(line)['event'] for line in (Path(ws) / '.h2wp-timing.jsonl').read_text().splitlines()]
            self.assertEqual(events, ['start', 'done', 'warn', 'skip', 'done'])

    def test_no_stage_runs_twice(self):
        # The no-loop rule, refused by the script: a stage that finished —
        # passed, red and recorded, or skipped — never starts again.
        with tempfile.TemporaryDirectory() as ws:
            progress(ws, 'mode', 'flash')
            for event, stage in (('done', '2'), ('warn', '5'), ('skip', '5.6')):
                progress(ws, 'start', stage)
                progress(ws, event, stage, 'x')
                again = progress(ws, 'start', stage)
                self.assertEqual(again.returncode, 3, (event, again.stdout, again.stderr))
                self.assertIn('Every stage runs once', again.stderr)
            # A stage cut short (still running) may start again on Continue;
            # a stage that stopped the run, only once the run is stopped.
            progress(ws, 'start', '3')
            self.assertEqual(progress(ws, 'start', '3').returncode, 0)
            progress(ws, 'fail', '3', 'the service refused the upload')
            self.assertEqual(progress(ws, 'start', '3').returncode, 0, 'a Continue after a clean stop')
            # Full mode repairs by design: no refusal there.
            self.assertEqual(progress(ws, 'start', '2', H2WP_MODE='full').returncode, 0)

    def test_a_stage_reported_late_never_moves_the_bar_back(self):
        # -1b (the shop specimen) is decided only after stage 0; -4 has a row.
        with tempfile.TemporaryDirectory() as ws:
            progress(ws, 'mode', 'flash')
            self.assertEqual(progress(ws, 'done', '-4').returncode, 0)
            progress(ws, 'done', '0')
            progress(ws, 'done', '0.6')
            progress(ws, 'skip', '-1b', 'no shop')
            self.assertEqual(snapshot(ws)['percent'], 30)

    def test_mode_starts_a_new_run_and_a_continue_does_not(self):
        # A new run over a finished one (the owner starts Flash again, or Full
        # after Flash) begins with every stage pending; the last run's
        # progress is kept beside it. A Continue never calls mode.
        with tempfile.TemporaryDirectory() as ws:
            progress(ws, 'mode', 'flash')
            progress(ws, 'start', '2')
            progress(ws, 'warn', '2', 'A red')
            progress(ws, 'done', '7')
            self.assertEqual(progress(ws, 'start', '2').returncode, 3)
            progress(ws, 'mode', 'flash')
            self.assertEqual(snapshot(ws)['state'], 'running')
            self.assertTrue(all(s['state'] == 'pending' for s in snapshot(ws)['stages']))
            self.assertEqual(progress(ws, 'start', '2').returncode, 0)
            self.assertEqual(len(list(Path(ws).glob('progress-*.json'))), 1)
            self.assertTrue(snapshot(ws)['startedAt'])

    def test_mode_over_a_run_in_progress_needs_new(self):
        # Stop leaves a run reading `running`. A Continue must resume it, not
        # start every stage over (and maybe a new, billed conversion): mode
        # refuses unless the caller says --new.
        with tempfile.TemporaryDirectory() as ws:
            progress(ws, 'mode', 'flash')
            self.assertEqual(progress(ws, 'mode', 'flash').returncode, 0, 'nothing ran yet')
            progress(ws, 'start', '0')
            progress(ws, 'done', '0')
            progress(ws, 'start', '0.5')
            again = progress(ws, 'mode', 'flash')
            self.assertEqual(again.returncode, 3)
            self.assertIn('stage 0.5', again.stderr)
            self.assertEqual({s['stage']: s['state'] for s in snapshot(ws)['stages']}['0'], 'done', 'nothing was reset')
            self.assertEqual(progress(ws, 'mode', 'flash', '--new').returncode, 0)
            self.assertTrue(all(s['state'] == 'pending' for s in snapshot(ws)['stages']))

    def test_the_astro_run_has_its_own_table_and_the_no_rerun_rule(self):
        with tempfile.TemporaryDirectory() as ws:
            progress(ws, 'mode', 'astro')
            doc = snapshot(ws)
            self.assertEqual(doc['mode'], 'astro')
            self.assertEqual([s['stage'] for s in doc['stages']], ['-4', '-3', '-1', '0', '0.5', '0.6', '1', '2', '6', '7'])
            progress(ws, 'done', '1')
            self.assertEqual(progress(ws, 'start', '1').returncode, 3)

    def test_the_preview_is_in_the_progress_without_its_password(self):
        with tempfile.TemporaryDirectory() as ws:
            (Path(ws) / '.test-env-site.json').write_text(json.dumps(
                {'url': 'http://localhost:55123', 'project': 'h2wp-site-abc123', 'user': 'admin', 'password': 'admin123'}))
            progress(ws, 'mode', 'flash')
            progress(ws, 'done', '3')
            doc = snapshot(ws)
            self.assertEqual({k: doc['preview'][k] for k in ('url', 'user', 'project')},
                             {'url': 'http://localhost:55123', 'user': 'admin', 'project': 'h2wp-site-abc123'})
            self.assertNotIn('admin123', (Path(ws) / 'progress.json').read_text())

    def test_a_fail_stops_the_run(self):
        with tempfile.TemporaryDirectory() as ws:
            progress(ws, 'mode', 'flash')
            progress(ws, 'fail', '3', 'the service refused the upload: free_conversions_used')
            doc = snapshot(ws)
            self.assertEqual((doc['state'], doc['stage']), ('stopped', '3'))
            self.assertEqual({s['stage']: s['state'] for s in doc['stages']}['3'], 'failed')

    def test_full_runs_keep_the_full_table_and_the_file_can_move(self):
        with tempfile.TemporaryDirectory() as ws:
            elsewhere = Path(ws) / 'ui' / 'p.json'
            elsewhere.parent.mkdir()
            progress(ws, 'done', '5', H2WP_PROGRESS_FILE=str(elsewhere))
            doc = json.loads(elsewhere.read_text())
            self.assertEqual((doc['mode'], doc['percent'], doc['label']), ('full', 88, 'gates B + C in a real WordPress'))
            self.assertFalse((Path(ws) / 'progress.json').exists())
            self.assertIn('5.5', [s['stage'] for s in doc['stages']])

    def test_the_environment_wins_over_the_recorded_mode(self):
        with tempfile.TemporaryDirectory() as ws:
            progress(ws, 'mode', 'flash')
            progress(ws, 'done', '5', H2WP_MODE='full')
            self.assertEqual(snapshot(ws)['mode'], 'full')


if __name__ == '__main__':
    unittest.main()
