#!/usr/bin/env python3
"""Changes after delivery: in the live theme, never a new build.

A delivered workspace (result.json status delivered, its theme ZIP in the
output directory), under a path with a space:

- the model edits a page source of the installed theme and runs
  apply-change.py: the change is logged (what, files, when), changedSinceZip
  is set, and no stage starts — progress.json is not touched;
- apply-change.py with nothing changed has nothing to apply (exit 3);
- progress.sh refuses a stage start and a `mode` without --new while the
  project is delivered; `mode --new` (the app's "Start over from the
  original") starts a new run and
  puts the delivered result and change log aside;
- the change brief never tells the model to package: the ZIP is the owner's
  "Make release";
- package-theme.py ("Make release") delivers the changed theme as revision 2 with
  its sha256 in result.json and clears changedSinceZip; with nothing changed
  it answers the existing ZIP (reused).

The install into a preview is --skip-install here (no Docker); the live path
is install-theme.py, proven on its own.

  python3 test-change-mode.py
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def run(*argv, env=None):
    return subprocess.run([str(a) for a in argv], capture_output=True, text=True, timeout=300,
                          env={**{k: v for k, v in os.environ.items() if not k.startswith('H2WP_')}, **(env or {})})


def delivered_workspace(root):
    ws, out = root / 'work space', root / 'out dir'
    theme = ws / 'theme' / 'studio'
    (theme / 'clara-content' / 'sources').mkdir(parents=True)
    (theme / 'templates').mkdir()
    (theme / 'style.css').write_text('/*\nTheme Name: Studio\nVersion: 1.0.0\n*/\n')
    (theme / 'theme.json').write_text('{"version": 3}')
    (theme / 'functions.php').write_text('<?php\n')
    (theme / 'templates' / 'index.html').write_text('<!-- wp:post-content /-->')
    (theme / 'clara-content' / 'sources' / 'front-page.html').write_text('<h1>Studio</h1>')
    from PIL import Image
    Image.new('RGB', (1200, 900), (240, 240, 240)).save(theme / 'screenshot.png')
    (ws / 'conversion-manifest.json').write_text(json.dumps({
        'schema': 'html2wp/1', 'site': {'name': 'Studio', 'slug': 'studio', 'version': '1.0.0'},
        'workspace': str(ws), 'pages': [{'file': 'index.html', 'key': 'front-page', 'kind': 'front',
                                         'title': 'Studio', 'chrome': 'consensus'}]}))
    built = run('bash', HERE / 'make-zip.sh', theme, out / 'studio-1.0.0.zip',
                env={'MAKE_ZIP_MANIFEST': str(ws / 'conversion-manifest.json')})
    assert built.returncode == 0, built.stdout + built.stderr
    result = {'schema': 'h2wp-result/1', 'mode': 'flash', 'target': 'html', 'status': 'delivered', 'revision': 1,
              'theme': {'file': 'studio-1.0.0.zip', 'sha256': hashlib.sha256((out / 'studio-1.0.0.zip').read_bytes()).hexdigest()}}
    for folder in (ws, out):
        (folder / 'result.json').write_text(json.dumps(result))
    progress = {'schema': 'h2wp-progress/1', 'mode': 'flash', 'state': 'finished', 'stages': [{'stage': '7', 'state': 'done'}]}
    (ws / 'progress.json').write_text(json.dumps(progress))
    return ws, out, theme


class ChangeMode(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws, self.out, self.theme = delivered_workspace(Path(self.tmp.name) / 'Application Support')
        # What the app sets on every command of a chat turn after delivery.
        self.env = {'H2WP_WORKSPACE': str(self.ws), 'H2WP_OUTPUT_DIR': str(self.out), 'H2WP_MODE': 'change'}

    def tearDown(self):
        self.tmp.cleanup()

    def apply(self, what='make the heading italic'):
        return run(sys.executable, HERE / 'apply-change.py', self.ws, '--what', what, '--skip-install', env=self.env)

    def test_a_change_is_made_in_the_theme_and_logged_without_a_stage(self):
        before = (self.ws / 'progress.json').read_text()
        self.assertEqual(self.apply().returncode, 3, 'nothing changed yet')
        (self.theme / 'clara-content' / 'sources' / 'front-page.html').write_text('<h1><em>Studio</em></h1>')
        done = self.apply()
        self.assertEqual(done.returncode, 0, done.stderr)
        log = json.loads((self.ws / 'changes.json').read_text())
        self.assertEqual(log['schema'], 'h2wp-changes/1')
        self.assertTrue(log['changedSinceZip'])
        self.assertEqual(log['sinceZip'], 1)
        entry = log['changes'][-1]
        self.assertEqual((entry['what'], entry['files'], entry['applied']),
                         ('make the heading italic', ['clara-content/sources/front-page.html'], True))
        self.assertEqual((self.ws / 'progress.json').read_text(), before, 'no stage started')
        self.assertEqual(self.apply().returncode, 3, 'applied: nothing new to apply')

    def test_the_guard_a_delivered_project_starts_no_stage(self):
        before = (self.ws / 'progress.json').read_text()
        stale = {**self.env, 'H2WP_MODE': 'flash'}  # a shell that kept the run's mode
        for args, env in [(a, e) for a in (('start', '-1'), ('start', '0'), ('mode', 'flash'), ('mode', 'full'))
                          for e in (self.env, stale)]:
            done = run('bash', HERE / 'progress.sh', *args, env=env)
            self.assertEqual(done.returncode, 3, (args, env['H2WP_MODE']))
            self.assertIn('apply-change.py', done.stderr)
            # The owner's rule: a change never points at starting over.
            self.assertNotRegex(done.stderr.lower(), r'rebuild|--new|start over')
        self.assertEqual((self.ws / 'progress.json').read_text(), before, 'a refusal writes nothing: still delivered')
        results = [(f / 'result.json').read_text() for f in (self.ws, self.out)]
        done = run(sys.executable, HERE / 'write-result.py', self.ws, '--no-pdf', env=self.env)
        self.assertEqual(done.returncode, 3, 'a change turn never rewrites the delivered result')
        self.assertEqual([(f / 'result.json').read_text() for f in (self.ws, self.out)], results)
        # The app's "Start over from the original": the run's own mode, --new.
        rebuild = run('bash', HERE / 'progress.sh', 'mode', 'flash', '--new', env=stale)
        self.assertEqual(rebuild.returncode, 0, rebuild.stderr)
        self.assertFalse((self.ws / 'result.json').exists())
        self.assertEqual(len(list(self.ws.glob('result-*.json'))), 1)
        self.assertEqual(run('bash', HERE / 'progress.sh', 'start', '-4', env=stale).returncode, 0)

    def test_get_zip_packages_the_live_theme_as_the_next_revision(self):
        package = lambda: run(sys.executable, HERE / 'package-theme.py', self.ws, env=self.env)
        same = package()
        self.assertEqual(same.returncode, 0, same.stderr)
        self.assertEqual(json.loads(same.stdout), {**json.loads(same.stdout), 'reused': True, 'file': 'studio-1.0.0.zip'})
        (self.theme / 'clara-content' / 'sources' / 'front-page.html').write_text('<h1><em>Studio</em></h1>')
        self.assertEqual(self.apply().returncode, 0)
        new = package()
        self.assertEqual(new.returncode, 0, new.stderr)
        got = json.loads(new.stdout)
        self.assertEqual((got['file'], got['revision'], got['reused']), ('studio-1.0.0-r2.zip', 2, False))
        zipped = self.out / 'studio-1.0.0-r2.zip'
        self.assertEqual(got['sha256'], hashlib.sha256(zipped.read_bytes()).hexdigest())
        self.assertIn(b'<em>Studio</em>', zipfile.ZipFile(zipped).read('studio/clara-content/sources/front-page.html'))
        for folder in (self.ws, self.out):
            result = json.loads((folder / 'result.json').read_text())
            self.assertEqual((result['theme']['file'], result['revision'], result['checkedRevision'], result['status']),
                             ('studio-1.0.0-r2.zip', 2, 1, 'delivered'))
        self.assertEqual({k: json.loads((self.ws / 'changes.json').read_text())[k] for k in ('changedSinceZip', 'sinceZip')},
                         {'changedSinceZip': False, 'sinceZip': 0})
        again = package()
        self.assertEqual((json.loads(again.stdout)['reused'], json.loads(again.stdout)['file']), (True, 'studio-1.0.0-r2.zip'))

    def test_the_change_brief_never_offers_starting_over(self):
        # After delivery every change goes into the live theme. What the theme
        # cannot change is said plainly — never "Rebuild", never a new run.
        skill = (HERE.parent.parent / 'SKILL.md').read_text()
        start = skill.index('## Changes after delivery')
        section = skill[start:skill.index('\n## ', start + 3)]
        self.assertIn('apply-change.py', section)
        self.assertNotRegex(section.lower(), r'rebuild|--new|start over|mode <')
        for script in ('apply-change.py', 'package-theme.py'):
            self.assertNotRegex((HERE / script).read_text().lower(), r'rebuild|start over', script)

    def test_the_change_brief_never_tells_the_model_to_package(self):
        # The ZIP is the owner's, with the app's "Make release"; the model
        # never packages in a change turn, not even when asked for the ZIP.
        skill = (HERE.parent.parent / 'SKILL.md').read_text()
        start = skill.index('## Changes after delivery')
        section = skill[start:skill.index('\n## ', start + 3)]
        self.assertIn('"Make release"', section)
        self.assertIn('You never package in a change turn', ' '.join(section.split()))
        self.assertNotRegex(section, r'package-theme|Get ZIP')
        # The only command the section hands the model is apply-change.py.
        commands = re.findall(r'```\n(.*?)```', section, re.S) + re.findall(r'`([^`\n]*\.(?:py|sh)[^`\n]*)`', section)
        self.assertTrue(commands)
        for command in commands:
            self.assertNotRegex(command, r'package-theme|make-zip|zip ', command)
        self.assertNotRegex(section.lower(), r'\b(run|use|call)\b[^.\n]{0,40}\bmake-zip')
        for script in ('apply-change.py', 'write-result.py'):
            self.assertNotIn('package-theme.py makes', (HERE / script).read_text(), script)

    def test_not_a_delivered_project(self):
        (self.ws / 'result.json').write_text(json.dumps({'status': 'stopped'}))
        self.assertEqual(self.apply().returncode, 2)
        self.assertEqual(run(sys.executable, HERE / 'package-theme.py', self.ws, env=self.env).returncode, 2)


if __name__ == '__main__':
    unittest.main()
