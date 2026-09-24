#!/usr/bin/env python3
"""The plugin works where every path holds a space.

The desktop app mounts each project at its own host path, and on a Mac that is
~/Library/Application Support/… — with a space in every workspace, output and
TMPDIR path the plugin sees. A script that realpaths one of them and uses the
result unquoted breaks there and nowhere else. This runs, under a directory
named with a space: progress.sh (mode, start, done, the snapshot),
detect-project.py --prepare, static-site.py, flash-manifest.py after the real
analyze-input.mjs, check-manifest.py, write-result.py with its Astro ZIP and
PDF, and — when Docker answers — the build sandbox with TMPDIR under that
directory (a bind mount of a path with a space).

  python3 test-space-paths.py
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'lib'))
import sandbox  # noqa: E402

PAGE = ('<!doctype html><html><head><title>{t} | North Studio</title><link rel="stylesheet" href="site.css"></head>'
        '<body><header><nav class="menu"><a href="index.html">Home</a> <a href="about.html">About</a></nav></header>'
        '<main><h1>{t}</h1><p>Words enough to be a page, not an app shell, on a path with a space.</p></main>'
        '<footer><p>North Studio</p></footer></body></html>')


def run(*argv, env=None, cwd=None):
    return subprocess.run([str(a) for a in argv], capture_output=True, text=True, timeout=600,
                          env={**os.environ, **(env or {})}, cwd=cwd)


class SpacePaths(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.tmp.name) / 'Application Support' / 'projects' / 'a b'
        cls.ws, cls.out, cls.src = cls.root / 'work space', cls.root / 'out dir', cls.root / 'source files'
        for d in (cls.ws, cls.out, cls.src):
            d.mkdir(parents=True)
        for rel, title in (('index.html', 'Home'), ('about.html', 'About')):
            (cls.src / rel).write_text(PAGE.format(t=title))
        (cls.src / 'site.css').write_text('main{padding:1rem}')
        cls.env = {'H2WP_WORKSPACE': str(cls.ws), 'H2WP_OUTPUT_DIR': str(cls.out), 'H2WP_MODE': 'flash'}

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_progress(self):
        for args in (('mode', 'flash'), ('start', '0'), ('done', '0', 'two pages')):
            done = run('bash', HERE / 'progress.sh', *args, env=self.env)
            self.assertEqual(done.returncode, 0, done.stderr)
        doc = json.loads((self.ws / 'progress.json').read_text())
        self.assertEqual((doc['mode'], {s['stage']: s['state'] for s in doc['stages']}['0']), ('flash', 'done'))

    def test_stage_0_and_the_result(self):
        detected = run(sys.executable, HERE / 'detect-project.py', self.src, '--out', self.ws / 'detect.json')
        self.assertEqual(detected.returncode, 0, detected.stderr)
        self.assertEqual(json.loads((self.ws / 'detect.json').read_text())['kind'], 'static-html')
        analysed = run('node', HERE / 'analyze-input.mjs', self.src, f'--out={self.ws / "analysis.json"}')
        self.assertEqual(analysed.returncode, 0, analysed.stdout + analysed.stderr)
        drafted = run(sys.executable, HERE / 'flash-manifest.py', '--analysis', self.ws / 'analysis.json',
                      '--input', self.src, '--workspace', self.ws)
        self.assertEqual(drafted.returncode, 0, drafted.stderr)
        manifest = json.loads((self.ws / 'conversion-manifest.json').read_text())
        self.assertEqual(manifest['input']['dir'], str(self.src.resolve()))
        checked = run(sys.executable, HERE / 'check-manifest.py', '--manifest', self.ws / 'conversion-manifest.json')
        self.assertEqual(checked.returncode, 0, checked.stdout)
        slug = manifest['site']['slug']
        (self.ws / f'{slug}-1.0.0.zip').write_bytes(b'PK-theme')
        (self.ws / 'CONVERSION-REPORT.md').write_text('# Report\n')
        (self.ws / 'astro-project' / 'dist').mkdir(parents=True)
        (self.ws / 'astro-project' / 'package.json').write_text('{}')
        (self.ws / 'astro-project' / 'dist' / 'index.html').write_text('<h1>x</h1>')
        done = run(sys.executable, HERE / 'write-result.py', self.ws, env=self.env)
        self.assertEqual(done.returncode, 0, done.stderr)
        doc = json.loads((self.out / 'result.json').read_text())
        self.assertEqual(doc['theme']['file'], f'{slug}-1.0.0.zip')
        self.assertTrue((self.out / f'{slug}-astro-1.0.0.zip').is_file())
        self.assertTrue(doc['builtSite']['path'].endswith('work space/astro-project/dist'))
        if doc['report'].get('pdf'):
            self.assertEqual((self.out / 'conversion-report.pdf').read_bytes()[:4], b'%PDF')

    def test_a_static_site_build(self):
        project = self.root / 'astro site'
        (project / 'dist' / 'about').mkdir(parents=True)
        (project / 'package.json').write_text('{"scripts":{"build":"astro build"},"devDependencies":{"astro":"5"}}')
        (project / 'dist' / 'index.html').write_text(PAGE.format(t='Home').replace('about.html', '/about/'))
        (project / 'dist' / 'about' / 'index.html').write_text(PAGE.format(t='About'))
        done = run(sys.executable, HERE / 'static-site.py', '--project', project, '--out', self.ws / 'static-src', '--skip-build')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertTrue((self.ws / 'static-src' / 'about.html').is_file())

    @unittest.skipUnless(sandbox.available() or os.environ.get('GITHUB_ACTIONS') == 'true', 'Docker is not running')
    def test_the_build_sandbox_under_a_path_with_a_space(self):
        project = self.root / 'app project'
        project.mkdir()
        (project / 'package.json').write_text('{"name":"x","version":"1.0.0"}')
        (project / 'index.js').write_text('1')
        tmpdir = self.root / 'tmp dir'
        tmpdir.mkdir()
        probe = (
            'import sys, tempfile; sys.path.insert(0, sys.argv[1]); import sandbox\n'
            'work, deps = sandbox.prepare_workspace(sys.argv[2])\n'
            'assert " " in str(work), work\n'
            'r = sandbox.run_in_sandbox("node -e \\"require(\'fs\').writeFileSync(\'built.txt\', \'ok\')\\"", work, 300, "probe")\n'
            'assert r.returncode == 0, r\n'
            'print(open(str(work) + "/built.txt").read())\n')
        done = run(sys.executable, '-c', probe, HERE / 'lib', project, env={'TMPDIR': str(tmpdir)})
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('ok', done.stdout)


if __name__ == '__main__':
    unittest.main()
