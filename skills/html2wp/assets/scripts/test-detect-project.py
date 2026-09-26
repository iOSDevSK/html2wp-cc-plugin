#!/usr/bin/env python3
"""detect-project.py names the project kind and what prepares it for stage 0.

The desktop app decided this (files.rs detect(), conversion.rs
preparation_steps); the plugin decides it now, the same way:

- a folder of .html pages (or one folder inside the upload) is static-html;
- an Astro project whose output is not `server` is a static-site build,
  prepared by static-site.py without a browser;
- an Astro 5 project html2wp exported is html2wp-astro, and --prepare copies
  its dist/ to static-src/ and the project to astro-project/;
- any other project with a build script is a web-app for prerender-spa.py,
  TanStack Start named as the framework;
- a package.json with no build script beside finished pages is static-html;
  with no pages it is nothing to convert.

  python3 test-detect-project.py
"""
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from lib.theme_state import zip_astro_project

SCRIPT = Path(__file__).with_name('detect-project.py')
spec = importlib.util.spec_from_file_location('detect_project', SCRIPT)
dp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dp)


def write(root, files):
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    return root


def pkg(deps=None, build='vite build', dev=None):
    scripts = {'build': build} if build else {}
    return json.dumps({'scripts': scripts, 'dependencies': deps or {}, 'devDependencies': dev or {}})


class Detect(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def kind(self, path=None):
        return dp.detect(path or self.root)

    def test_static_html_and_the_folder_inside_an_upload(self):
        write(self.root, {'site/index.html': '<h1>x</h1>', 'site/about.html': '<h1>a</h1>', '__MACOSX/._x': ''})
        found = self.kind()
        self.assertEqual((found['kind'], found['pages'], found['prepare']), ('static-html', 2, []))
        self.assertEqual(Path(found['root']).name, 'site')

    def test_an_astro_project_is_a_static_site_unless_it_renders_on_request(self):
        write(self.root, {'package.json': pkg(dev={'astro': '5'}, build='astro build'),
                          'astro.config.mjs': 'export default {}'})
        self.assertEqual((self.kind()['kind'], self.kind()['generator']), ('static-site', 'astro'))
        write(self.root, {'astro.config.mjs': "export default defineConfig({ output: 'server' })"})
        self.assertEqual(self.kind()['kind'], 'web-app')

    def test_an_html2wp_astro_export_is_prepared_by_copying(self):
        write(self.root, {'package.json': pkg(deps={'astro': '5'}, build='astro build'),
                          'astro.config.mjs': 'export default {}', 'src/fragments/bodies/index.html': '<h1>x</h1>',
                          'dist/index.html': '<h1>x</h1>', 'dist/about.html': '<h1>a</h1>',
                          '.html2wp/astro-report.json': '{"variants":[]}', 'node_modules/x/index.js': ''})
        found = self.kind()
        self.assertEqual((found['kind'], found['pages']), ('html2wp-astro', 2))
        ws = self.root.parent / (self.root.name + '-ws')
        try:
            result = subprocess.run([sys.executable, str(SCRIPT), str(self.root), '--prepare', str(ws)],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((ws / 'static-src/about.html').is_file())
            self.assertTrue((ws / 'astro-project/src/fragments/bodies/index.html').is_file())
            self.assertFalse((ws / 'astro-project/node_modules').exists())
            self.assertFalse((ws / 'astro-project/.html2wp').exists())
            self.assertEqual(json.loads((ws / 'astro-report.json').read_text()), {'variants': []})
        finally:
            import shutil
            shutil.rmtree(ws, ignore_errors=True)

    def test_self_contained_export_survives_zip_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = write(base / 'astro-project', {
                'package.json': pkg(deps={'astro': '5.18.2'}, build='astro build'),
                'astro.config.mjs': 'export default {}',
                'public/index.html': '<main>Home</main>',
                'public/about.html': '<main>About</main>',
                'dist/index.html': '<main>Home</main>',
                'dist/about.html': '<main>About</main>',
            })
            (project / 'src/fragments/bodies').mkdir(parents=True)
            (base / 'astro-report.json').write_text('{"pages":[],"warnings":[]}')
            archive = base / 'export.zip'
            self.assertTrue(zip_astro_project(project, archive, 'site-astro'))
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(base / 'upload')
            imported = base / 'upload/site-astro'
            self.assertFalse((imported / 'src/fragments/bodies').exists())
            found = dp.detect(base / 'upload')
            self.assertEqual((found['kind'], found['pages']), ('html2wp-astro', 2))
            workspace = base / 'workspace'
            dp.prepare_astro_export(imported, workspace)
            self.assertEqual((workspace / 'astro-project/public/about.html').read_text(), '<main>About</main>')
            self.assertEqual((workspace / 'static-src/about.html').read_text(), '<main>About</main>')
            self.assertTrue((workspace / '.astro-project-imported').is_file())
            (imported / '.html2wp/astro-report.json').unlink()
            self.assertEqual(dp.detect(imported)['kind'], 'static-site')

    def test_a_buildable_app_is_a_web_app_and_tanstack_is_named(self):
        write(self.root, {'package.json': pkg(deps={'@tanstack/react-start': '1', 'react': '19'})})
        found = self.kind()
        self.assertEqual((found['kind'], found['framework'], found['prepare']),
                         ('web-app', 'tanstack-start', ['prerender-spa.py']))
        write(self.root, {'package.json': pkg(deps={'react-router-dom': '6', 'react': '19'})})
        self.assertEqual(self.kind()['framework'], 'react-router')

    def test_no_build_script(self):
        write(self.root, {'package.json': pkg(build=None)})
        self.assertEqual(self.kind()['kind'], 'none')
        write(self.root, {'index.html': '<h1>x</h1>'})
        self.assertEqual(self.kind()['kind'], 'static-html')

    def test_nothing_to_convert_exits_1(self):
        (self.root / 'readme.txt').write_text('hello')
        result = subprocess.run([sys.executable, str(SCRIPT), str(self.root)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['kind'], 'none')


if __name__ == '__main__':
    unittest.main()
