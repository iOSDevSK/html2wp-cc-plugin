#!/usr/bin/env python3
"""Full repairs before delivering a real, possibly imperfect product."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('fixtures', HERE / 'test-write-result.py')
fixtures = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixtures)


class FullDelivery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = fixtures.workspace(Path(self.tmp.name))
        self.env = {**{k: v for k, v in os.environ.items() if not k.startswith('H2WP_')},
                    'H2WP_WORKSPACE': str(self.ws), 'H2WP_MODE': 'full'}
        self.assertEqual(self.progress('mode', 'full').returncode, 0)

    def tearDown(self):
        self.tmp.cleanup()

    def call(self, script, *args):
        return subprocess.run([sys.executable, str(HERE / script), str(self.ws), *map(str, args)],
                              env=self.env, capture_output=True, text=True, timeout=60)

    def progress(self, *args):
        return subprocess.run(['bash', str(HERE / 'progress.sh'), *args], env=self.env,
                              capture_output=True, text=True, timeout=30)

    def doc(self, name):
        return json.loads((self.ws / name).read_text())

    def plan(self, stage, hypothesis):
        fixtures.write(self.ws, {'repair-plan.json': {'stage': stage, 'hypothesis': hypothesis,
                                                     'files': ['conversion-manifest.json']}})

    def test_full_attempts_are_bounded_and_identical_hypothesis_is_refused(self):
        self.progress('start', '2')
        for i in range(3):
            self.plan('2', f'diagnosed cause {i}')
            self.assertEqual(self.progress('repair', '2', 'ai-fix', 'unnamed').returncode, 0)
            self.assertEqual(self.progress('repaired', '2', 'failed', 'still red').returncode, 0)
            self.assertEqual(self.progress('repair', '2', 'ai-fix', 'unnamed').returncode, 3)
        self.plan('2', 'a fourth cause')
        self.assertEqual(self.progress('repair', '2', 'ai-fix', 'unnamed').returncode, 3)

    def test_verification_alone_does_not_spend_a_repair_attempt(self):
        before = self.doc('progress.json').get('repairs', [])
        checked = self.call('repair-check.py', '0', '--', sys.executable, HERE/'check-manifest.py', '--manifest', self.ws/'conversion-manifest.json')
        self.assertEqual(checked.returncode, 0, checked.stderr)
        self.assertEqual(self.doc('progress.json').get('repairs', []), before)
        self.assertEqual(self.doc('repair-check-0.json')['exit'], 0)

    def test_first_full_repair_cannot_reuse_unbound_green_reports(self):
        self.progress('start', '2')
        self.plan('2', 'change styling')
        self.assertEqual(self.progress('repair', '2', 'ai-fix', 'unnamed').returncode, 0)
        self.assertEqual(self.call('write-result.py', '--no-pdf').returncode, 0)
        self.assertEqual(next(g for g in self.doc('result.json')['gates'] if g['id']=='B')['status'], 'not_run')

    def test_prerender_receipt_tracks_capture_and_original_dist(self):
        sys.path.insert(0, str(HERE/'lib'))
        from full_delivery import verification_state
        fixtures.write(self.ws, {'original/dist/index.html': 'source', 'capture/index.html': 'capture',
            'prerender-report.json': {'project': str(self.ws/'original'), 'out': str(self.ws/'capture')}})
        original = verification_state(self.ws, 'prerender-spa.py')
        self.assertIsNotNone(original)
        fixtures.write(self.ws, {'capture/index.html': 'changed capture'})
        changed = verification_state(self.ws, 'prerender-spa.py')
        self.assertNotEqual(original, changed)
        fixtures.write(self.ws, {'original/dist/index.html': 'changed source'})
        self.assertNotEqual(changed, verification_state(self.ws, 'prerender-spa.py'))

    def test_full_stop_has_an_owner_repair_and_requires_fresh_check(self):
        self.progress('start', '0')
        self.progress('fail', '0', 'fixture')
        fixtures.write(self.ws, {'result.json': {'status': 'stopped', 'stopped': {'stage': '0'}}})
        self.env.update(H2WP_MODE='repair-stop', H2WP_TURN='owner1')
        self.plan('0', 'correct the named source region')
        self.assertEqual(self.progress('repair', '0', 'ai-fix', 'unnamed').returncode, 0)
        self.assertEqual(self.progress('repaired', '0', 'fixed').returncode, 3)
        self.assertNotEqual(self.call('repair-check.py', '0', '--', sys.executable, '-c', 'assert 2 + 2 == 4').returncode, 0)
        check = self.call('repair-check.py', '0', '--', sys.executable, HERE / 'check-manifest.py', '--manifest', self.ws / 'conversion-manifest.json')
        self.assertEqual(check.returncode, 0, check.stderr)
        self.assertEqual(self.progress('repaired', '0', 'fixed').returncode, 0)
        self.assertFalse((self.ws / 'result.json').exists())
        self.assertEqual(next(s for s in self.doc('progress.json')['stages'] if s['stage']=='0')['state'], 'running')

    def test_homeware_shape_red_behavior_can_deliver_with_usable_capture(self):
        fixtures.write(self.ws, {'capture/index.html': '<html><body><main>Products</main></body></html>',
                                 'detect.json': {'kind': 'react-vite'},
                                 'prerender-report.json': {'passed': False, 'routes': ['/'], 'pages': {'index': {'behavior': {'passed': False}}}}})
        deferred = self.call('full-delivery.py', 'defer', '--stage', '-1', '--reason', 'filter does not open',
                             '--capture', self.ws / 'capture')
        self.assertEqual(deferred.returncode, 0, deferred.stderr)
        result = self.call('write-result.py', '--no-pdf')
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = self.doc('result.json')
        self.assertEqual(doc['status'], 'delivered')
        self.assertEqual(next(g for g in doc['gates'] if g['id']=='prerender')['status'], 'failed')
        self.assertTrue(doc['recovery']['available'])
        self.assertTrue(doc['artifactValidation']['passed'])
        self.assertTrue((self.ws / 'out' / doc['theme']['file']).is_file())

    def test_missing_preview_does_not_withhold_zip_but_corrupt_zip_is_not_delivered(self):
        (self.ws / 'install-theme/report.json').unlink()
        done = self.call('write-result.py', '--status', 'stopped', '--stopped-stage', '5',
                         '--stopped-reason', 'Docker unavailable', '--no-pdf')
        self.assertEqual(done.returncode, 0)
        self.assertEqual(self.doc('result.json')['status'], 'delivered')
        (self.ws / 'test-site-1.2.0.zip').write_text('not a zip')
        self.assertEqual(self.call('write-result.py', '--status', 'delivered', '--no-pdf').returncode, 2)

    def test_owner_repair_keeps_zip_and_cannot_reuse_old_result(self):
        self.assertEqual(self.call('write-result.py', '--no-pdf').returncode, 0)
        self.env.update(H2WP_MODE='repair-delivery', H2WP_TURN='new-model-turn')
        self.assertEqual(self.call('full-delivery.py', 'begin').returncode, 0)
        session = self.doc('repair-session.json')
        backup = Path(session['base']) / 'test-site-1.2.0.zip'
        self.assertTrue(backup.exists())
        self.assertFalse((self.ws / 'out/result.json').exists())
        self.assertFalse((self.ws / 'result.json').exists())
        self.assertEqual(self.call('full-delivery.py', 'begin').returncode, 0)
        (self.ws / 'test-site-1.2.0.zip').write_bytes(b'broken repair')
        fixtures.write(self.ws, {'verify-static/report.json': {'passed': True}})
        self.assertEqual(self.call('write-result.py', '--no-pdf').returncode, 0)
        doc = self.doc('result.json')
        self.assertEqual(doc['repairRequestId'], 'new-model-turn')
        self.assertEqual(doc['status'], 'delivered')
        self.assertEqual(next(g for g in doc['gates'] if g['id']=='A')['status'], 'failed')
        self.assertEqual((self.ws/'test-site-1.2.0.zip').read_bytes(), backup.read_bytes())

    def test_retained_zip_keeps_its_own_source_asset_report(self):
        old = {'schema': 'html2wp-source-assets/1', 'passed': False, 'recovered': [],
               'unresolved': [{'file': 'old-missing.png'}]}
        fixtures.write(self.ws, {'source-assets-report.json': old})
        self.assertEqual(self.call('write-result.py', '--no-pdf').returncode, 0)
        self.env.update(H2WP_MODE='repair-delivery', H2WP_TURN='asset-candidate')
        self.assertEqual(self.call('full-delivery.py', 'begin').returncode, 0)
        fixtures.write(self.ws, {'source-assets-report.json': {'schema': 'html2wp-source-assets/1',
                        'passed': True, 'recovered': [], 'unresolved': []}})
        self.assertEqual(self.call('write-result.py', '--no-pdf').returncode, 0)
        self.assertEqual(self.doc('out/source-assets-report.json'), old)
        self.assertEqual(next(g for g in self.doc('result.json')['gates'] if g['id']=='source-assets')['status'], 'failed')

    def test_old_zip_without_asset_report_does_not_export_candidate_report(self):
        self.assertEqual(self.call('write-result.py', '--no-pdf').returncode, 0)
        self.env.update(H2WP_MODE='repair-delivery', H2WP_TURN='asset-candidate')
        self.assertEqual(self.call('full-delivery.py', 'begin').returncode, 0)
        fixtures.write(self.ws, {'source-assets-report.json': {'schema': 'html2wp-source-assets/1',
                        'passed': True, 'recovered': [], 'unresolved': []}})
        self.assertEqual(self.call('write-result.py', '--no-pdf').returncode, 0)
        self.assertFalse((self.ws / 'out/source-assets-report.json').exists())

    def test_new_candidate_does_not_inherit_previous_green_checks(self):
        self.assertEqual(self.call('write-result.py', '--no-pdf').returncode, 0)
        self.env.update(H2WP_MODE='repair-delivery', H2WP_TURN='fresh-candidate')
        self.assertEqual(self.call('full-delivery.py', 'begin').returncode, 0)
        with zipfile.ZipFile(self.ws/'test-site-1.2.0.zip') as archive:
            archive.extractall(self.ws/'theme')
        m = self.doc('conversion-manifest.json')
        m['blog'] = {'present': False, 'reason': 'fixture'}
        fixtures.write(self.ws, {'conversion-manifest.json': m, 'theme/test-site/style.css': '/* Theme Name: Repaired */'})
        self.assertEqual(self.call('full-delivery.py', 'package').returncode, 0)
        self.assertEqual(self.call('write-result.py', '--no-pdf').returncode, 0)
        result = self.doc('result.json')
        self.assertEqual(result['revision'], 2)
        self.assertEqual(result['theme']['file'], 'test-site-1.2.0-r2.zip')
        self.assertEqual(next(g for g in result['gates'] if g['id']=='B')['status'], 'not_run')

    def test_check_only_repair_can_pass_but_a_later_patch_invalidates_its_receipt(self):
        import hashlib
        sys.path.insert(0, str(HERE/'lib'))
        from full_delivery import verification_state
        self.call('write-result.py', '--no-pdf')
        self.env.update(H2WP_MODE='repair-delivery', H2WP_TURN='verification')
        self.assertEqual(self.call('full-delivery.py', 'begin').returncode, 0)
        with zipfile.ZipFile(self.ws/'test-site-1.2.0.zip') as archive:
            archive.extractall(self.ws/'theme')
        fixtures.write(self.ws, {'verify-wp/report.json': {'passed': True, 'pages': {}, 'testMarker': 'fresh'},
            'delivery-artifact.json': {'turn': 'verification', 'file': 'test-site-1.2.0.zip',
                'sha256': hashlib.sha256((self.ws/'test-site-1.2.0.zip').read_bytes()).hexdigest()}})
        proof = {'checker': 'verify-wp.py', 'exit': 0, 'turn': 'verification', 'verificationStable': True,
                 'verificationInputs': verification_state(self.ws, 'verify-wp.py'),
                 'reports': {'verify-wp/report.json': hashlib.sha256((self.ws/'verify-wp/report.json').read_bytes()).hexdigest()}}
        fixtures.write(self.ws, {'repair-checks.json': {'5': {'verify-wp.py': proof}}})
        self.assertEqual(self.call('write-result.py', '--no-pdf').returncode, 0)
        self.assertEqual(next(g for g in self.doc('result.json')['gates'] if g['id']=='B')['status'], 'passed')
        m = self.doc('conversion-manifest.json')
        m['blog'] = {'present': False, 'reason': 'fixture'}
        fixtures.write(self.ws, {'conversion-manifest.json': m, 'theme/test-site/style.css': '/* Theme Name: Changed after check */'})
        self.assertEqual(self.call('full-delivery.py', 'package').returncode, 0)
        self.assertEqual(self.call('write-result.py', '--no-pdf').returncode, 0)
        self.assertEqual(next(g for g in self.doc('result.json')['gates'] if g['id']=='B')['status'], 'not_run')

    def test_fallback_preserves_requested_blog_and_every_page(self):
        m = self.doc('conversion-manifest.json')
        for page in m['pages']:
            fixtures.write(self.ws, {'astro-project/dist/' + page['file']: '<html><body><main>Content</main></body></html>'})
        result = self.call('full-delivery.py', 'fallback', '--reason', 'nested token after failed repairs')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.doc('requested-manifest.json'), m)
        after = self.doc('conversion-manifest.json')
        self.assertFalse(after['blog']['present'])
        self.assertEqual([p['file'] for p in after['pages']], [p['file'] for p in m['pages']])
        self.assertTrue((self.ws/'fallback-input/index.html').is_file())
        self.assertNotEqual(self.call('full-delivery.py', 'fallback', '--reason', 'again').returncode, 0)

    def test_packaging_keeps_content_and_refuses_broken_php(self):
        with zipfile.ZipFile(self.ws / 'test-site-1.2.0.zip') as archive:
            archive.extractall(self.ws / 'theme')
        manifest = self.doc('conversion-manifest.json')
        manifest['blog'] = {'present': False, 'reason': 'fixture has no articles'}
        fixtures.write(self.ws, {'conversion-manifest.json': manifest})
        packed = self.call('full-delivery.py', 'package')
        self.assertEqual(packed.returncode, 0, packed.stdout + packed.stderr)
        good = (self.ws / 'test-site-1.2.0.zip').read_bytes()
        fixtures.write(self.ws, {'theme/test-site/functions.php': '<?php function broken( {'})
        self.assertNotEqual(self.call('full-delivery.py', 'package').returncode, 0)
        self.assertEqual((self.ws / 'test-site-1.2.0.zip').read_bytes(), good)
        (self.ws / 'theme/test-site/functions.php').unlink()
        manifest['pages'].append({'file': 'missing.html', 'key': 'missing', 'kind': 'page'})
        fixtures.write(self.ws, {'conversion-manifest.json': manifest})
        self.assertNotEqual(self.call('full-delivery.py', 'package').returncode, 0, 'missing content is a fallback, not a quality waiver')

    def test_static_fallback_builds_with_actual_server_generators(self):
        m = self.doc('conversion-manifest.json')
        m.update(workspace=str(self.ws), anchors={'obsolete': '#missing'}, declaredCollections=[{'key': 'old'}])
        m['site']['prefix'] = 'test_site'
        m['design'] = {'palette': [], 'fonts': []}
        m['pages'] = [{'file': 'index.html', 'key': 'front-page', 'kind': 'front'},
                      {'file': 'article.html', 'key': 'article', 'kind': 'article'}]
        fixtures.write(self.ws, {'conversion-manifest.json': m,
            'astro-project/dist/index.html': '<html><head><title>Home</title></head><body><main>Home content</main><footer>Footer</footer></body></html>',
            'astro-project/dist/article.html': '<html><head><title>Story</title></head><body><main>Article content</main></body></html>'})
        self.progress('start', '1')
        self.progress('done', '1')
        fallback = self.call('full-delivery.py', 'fallback', '--reason', 'dynamic assembly failed')
        self.assertEqual(fallback.returncode, 0, fallback.stderr)
        self.assertEqual(self.progress('start', '1').returncode, 0)
        astro = subprocess.run(['node', str(HERE/'html-to-astro.mjs'), f'--manifest={self.ws}/conversion-manifest.json'],
                               cwd=self.ws, capture_output=True, text=True, timeout=60)
        self.assertEqual(astro.returncode, 0, astro.stdout + astro.stderr)
        # All fallback pages are self-contained public files: model Astro's
        # unchanged public→dist copy without a network/npm dependency.
        shutil.copytree(self.ws/'astro-project/public', self.ws/'astro-project/dist')
        core = HERE.parents[4] / 'server/core/scripts'
        for script, args in (
            ('make-theme.mjs', [f'--manifest={self.ws}/conversion-manifest.json']),
            ('dist-to-bundle.mjs', [str(self.ws/'astro-project/dist'), str(self.ws/'bundle-out'),
                '--theme-slug=test-site', '--theme-name=Test Site', '--theme-version=1.2.0',
                f'--theme-dir={self.ws}/theme/test-site', f'--manifest={self.ws}/conversion-manifest.json', '--forbid-in-front='])):
            built = subprocess.run(['node', str(core/script), *args], cwd=self.ws, capture_output=True, text=True, timeout=60)
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
        packed = self.call('full-delivery.py', 'package')
        self.assertEqual(packed.returncode, 0, packed.stdout + packed.stderr)
        with zipfile.ZipFile(self.ws/'test-site-1.2.0.zip') as archive:
            sources = json.loads(archive.read('test-site/clara-content/sources/index.json'))
            self.assertEqual(len(sources), 2)
            content = '\n'.join(archive.read('test-site/clara-content/' + row['file']).decode() for row in sources)
            self.assertIn('Home content', content)
            self.assertIn('Article content', content)
        self.assertTrue(self.doc('requested-manifest.json')['blog']['present'])

    def test_repair_preserves_capture_provenance_and_rejects_incomplete_zip(self):
        capture = {'passed': True, 'routes': ['/'], 'linkedRoutes': ['/article/']}
        fixtures.write(self.ws, {'prerender-report.json': capture})
        self.call('write-result.py', '--no-pdf')
        self.env.update(H2WP_MODE='repair-delivery', H2WP_TURN='provenance')
        self.assertEqual(self.call('full-delivery.py', 'begin').returncode, 0)
        self.assertEqual(self.doc('prerender-report.json'), capture)
        self.assertIn('prerender-report.json', self.doc('repair-session.json')['reports'])
        sys.path.insert(0, str(HERE/'lib'))
        from full_delivery import valid_zip
        with zipfile.ZipFile(self.ws/'incomplete.zip', 'w') as target, zipfile.ZipFile(self.ws/'test-site-1.2.0.zip') as source:
            for name in source.namelist():
                if not name.endswith('sources/index.json'):
                    target.writestr(name, source.read(name))
        self.assertFalse(valid_zip(self.ws/'incomplete.zip'))

    def test_repair_of_revision_two_retains_revision_two_on_failure(self):
        self.call('write-result.py', '--no-pdf')
        result = self.doc('result.json')
        revised = fixtures.theme_zip() + b'revision-two'
        (self.ws / 'out/test-site-1.2.0-r2.zip').write_bytes(revised)
        import hashlib
        result.update(revision=2, checkedRevision=1)
        result['theme'].update(file='test-site-1.2.0-r2.zip', sha256=hashlib.sha256(revised).hexdigest())
        fixtures.write(self.ws, {'result.json': result, 'out/result.json': result})
        self.env.update(H2WP_MODE='repair-delivery', H2WP_TURN='owner-A')
        self.assertEqual(self.call('full-delivery.py', 'begin').returncode, 0)
        self.env['H2WP_TURN'] = 'owner-B'
        self.assertEqual(self.call('full-delivery.py', 'begin').returncode, 0, 'new model resumes the same repair')
        self.assertEqual(self.doc('repair-session.json')['turn'], 'owner-B')
        self.assertEqual(self.call('write-result.py', '--no-pdf').returncode, 0)
        final = self.doc('result.json')
        self.assertEqual(final['revision'], 2)
        self.assertEqual(final['checkedRevision'], 1)
        self.assertEqual(final['theme']['file'], 'test-site-1.2.0-r2.zip')
        self.assertEqual((self.ws/'out/test-site-1.2.0-r2.zip').read_bytes(), revised)
        self.env['H2WP_START_OVER'] = '1'
        self.assertEqual(self.progress('mode', 'full', '--new').returncode, 0)
        for path in ('repair-session.json', 'delivery-issues.json', 'delivery-artifact.json'):
            self.assertFalse((self.ws/path).exists(), path)
        self.assertFalse((self.ws/'test-site-1.2.0.zip').exists())

    def test_restarting_completed_full_stage_needs_counted_changed_inputs(self):
        self.progress('start', '2')
        self.progress('done', '2')
        self.assertEqual(self.progress('start', '2').returncode, 3)
        self.progress('start', '3')
        self.plan('3', 'repair source selectors')
        self.assertEqual(self.progress('repair', '3', 'ai-fix', 'unnamed').returncode, 0)
        manifest = self.doc('conversion-manifest.json')
        manifest['site']['description'] = 'new input'
        fixtures.write(self.ws, {'conversion-manifest.json': manifest})
        self.assertEqual(self.call('full-delivery.py', 'invalidate', '--stage', '2', '--reason', 'changed selectors').returncode, 0)
        self.assertEqual(self.progress('start', '2').returncode, 0)
        self.progress('done', '2')
        self.assertEqual(self.progress('start', '2').returncode, 3)
        self.assertNotEqual(self.call('full-delivery.py', 'invalidate', '--stage', '2', '--reason', 'same again').returncode, 0)

    def test_security_and_empty_capture_are_not_deferred(self):
        for stage in ('-4', '-3', '-1'):
            self.assertNotEqual(self.call('full-delivery.py', 'defer', '--stage', stage, '--reason', 'fixture').returncode, 0)


if __name__ == '__main__':
    unittest.main()
