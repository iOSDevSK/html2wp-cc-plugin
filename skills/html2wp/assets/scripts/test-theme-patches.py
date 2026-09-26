#!/usr/bin/env python3
"""Real filesystem replay, transactions, and shared packaging guards."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE/'lib'))
from theme_patches import Patches, PatchError, safe_name
spec = importlib.util.spec_from_file_location('fixtures', HERE/'test-write-result.py')
f = importlib.util.module_from_spec(spec)
spec.loader.exec_module(f)


class ThemePatches(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = f.workspace(Path(self.tmp.name))
        with zipfile.ZipFile(self.ws/'test-site-1.2.0.zip') as archive:
            archive.extractall(self.ws/'theme')
        self.theme = self.ws/'theme/test-site'
        self.server = Path(self.tmp.name)/'server-theme'
        shutil.copytree(self.theme, self.server)
        f.write(self.ws, {'result.json': {'status': 'delivered'}})
        self.p = Patches(self.ws)
        self.p.initialize()
        self.p.integrate(self.server)

    def tearDown(self):
        self.tmp.cleanup()

    def edit(self, files):
        self.p.begin('5', 'repair observed output')
        for name, value in files.items():
            path = self.theme/name
            if value is None: path.unlink()
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(value if isinstance(value, bytes) else value.encode())
        self.p.record()

    def test_composed_patches_replay_idempotently_and_survive_later_original_build(self):
        self.edit({'style.css': '/* Theme Name: first repair */'})
        self.edit({'style.css': '/* Theme Name: final repair */'})
        self.p.integrate(self.server)
        self.assertIn('final repair', (self.theme/'style.css').read_text())
        patched = Path(self.tmp.name)/'already-patched'
        shutil.copytree(self.theme, patched)
        self.p.integrate(patched)
        self.p.integrate(self.server)
        self.assertIn('final repair', (self.theme/'style.css').read_text())
        self.assertTrue((self.ws/'test-site-1.2.0.zip').exists())

    def test_add_delete_and_binary_replay(self):
        (self.theme/'obsolete.txt').write_text('old')
        (self.server/'obsolete.txt').write_text('old')
        self.p.root.joinpath('state.json').unlink()
        self.p.initialize()
        self.p.integrate(self.server)
        self.edit({'assets/new.bin': b'\0new', 'obsolete.txt': None})
        self.p.integrate(self.server)
        self.assertFalse((self.theme/'obsolete.txt').exists())
        self.assertEqual((self.theme/'assets/new.bin').read_bytes(), b'\0new')

    def test_non_overlapping_text_changes_merge(self):
        source = '\n'.join(f'line {i}' for i in range(30))+'\n'
        (self.theme/'notes.txt').write_text(source)
        (self.server/'notes.txt').write_text(source)
        self.p.root.joinpath('state.json').unlink()
        self.p.initialize()
        self.p.integrate(self.server)
        self.edit({'notes.txt': source.replace('line 2\n', 'local fix\n')})
        (self.server/'notes.txt').write_text(source.replace('line 26\n', 'server fix\n'))
        self.p.integrate(self.server)
        merged = (self.theme/'notes.txt').read_text()
        self.assertIn('local fix', merged)
        self.assertIn('server fix', merged)

    def test_conflict_preserves_whole_working_tree_and_zip_then_resolves(self):
        self.edit({'style.css': '/* Theme Name: Local */', 'assets/owner.txt': 'keep'})
        before = self.p.snapshot(self.theme)
        previous_zip = (self.ws/'test-site-1.2.0.zip').read_bytes()
        (self.server/'style.css').write_text('/* Theme Name: Server */')
        (self.server/'functions.php').write_text('<?php // changed server')
        with self.assertRaises(PatchError): self.p.integrate(self.server)
        self.assertEqual(self.p.snapshot(self.theme), before)
        self.assertEqual((self.ws/'test-site-1.2.0.zip').read_bytes(), previous_zip)
        conflict = self.p.load()['conflict']
        self.assertEqual(conflict['files'], ['style.css'])
        candidate = self.p.root/conflict['directory']
        (candidate/'style.css').write_text('/* Theme Name: Merged */')
        f.write(self.ws, {'.h2wp-result.json': {'status': 'FAILED_CLIENT', 'code': 'LOCAL_PATCH_CONFLICT'}})
        self.p.resolve('5', 'retain local design on the newer server runtime')
        self.assertIn('Merged', (self.theme/'style.css').read_text())
        self.assertIn('changed server', (self.theme/'functions.php').read_text())
        self.assertEqual(json.loads((self.ws/'.h2wp-result.json').read_text())['status'], 'SUCCESS')
        self.p.check()

    def test_pending_patch_blocks_common_packer_and_abort_restores(self):
        before = self.p.snapshot(self.theme)
        self.p.begin('5', 'attempted fix')
        (self.theme/'style.css').write_text('/* Theme Name: Pending */')
        output = self.ws/'test-site-1.2.0.zip'
        previous = output.read_bytes()
        result = subprocess.run(['bash', str(HERE/'make-zip.sh'), str(self.theme), str(output)], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(output.read_bytes(), previous)
        self.p.abort()
        self.assertEqual(self.p.snapshot(self.theme), before)
        self.assertTrue(list(self.p.root.glob('theme-backup-*')))

    def test_bad_php_is_not_recorded_and_can_be_aborted(self):
        self.p.begin('5', 'repair PHP')
        (self.theme/'functions.php').write_text('<?php function broken( {')
        with self.assertRaises(PatchError): self.p.record()
        self.assertTrue(self.p.load()['pending'])
        self.p.abort()
        self.p.check()

    def test_unrecorded_owner_files_survive_but_removed_generated_files_do_not(self):
        (self.theme/'obsolete.css').write_text('old generated asset')
        (self.server/'obsolete.css').write_text('old generated asset')
        self.p.root.joinpath('state.json').unlink()
        self.p.initialize()
        self.p.integrate(self.server)
        (self.server/'obsolete.css').unlink()
        (self.theme/'assets').mkdir()
        (self.theme/'assets/owner.txt').write_text('owner addition')
        self.p.integrate(self.server)
        self.assertEqual((self.theme/'assets/owner.txt').read_text(), 'owner addition')
        self.assertFalse((self.theme/'obsolete.css').exists())

    def test_legacy_edited_workspace_requires_reconciliation_instead_of_silent_overwrite(self):
        shutil.rmtree(self.p.root)
        (self.theme/'style.css').write_text('/* Theme Name: Earlier owner edit */')
        self.p = Patches(self.ws)
        before = self.p.snapshot(self.theme)
        with self.assertRaises(PatchError): self.p.integrate(self.server)
        self.assertEqual(self.p.snapshot(self.theme), before)
        self.assertIn('style.css', self.p.load()['conflict']['files'])
        self.p.resolve('5', 'keep the existing owner design after checking the new server output')
        self.assertFalse(self.p.load().get('legacyUnknown'))
        self.p.integrate(self.server)
        self.assertIn('Earlier owner edit', (self.theme/'style.css').read_text())

    def test_paths_links_and_corrupt_blobs_are_refused(self):
        for name in ('../escape', '/tmp/escape', 'a/../../escape', 'a\\b'):
            with self.assertRaises(PatchError): safe_name(name)
        (self.server/'link').symlink_to(self.theme/'style.css')
        with self.assertRaises(PatchError): self.p.integrate(self.server)
        (self.server/'link').unlink()
        sha = self.p.load()['base']['style.css']
        (self.p.blobs/sha).write_bytes(b'corrupt')
        with self.assertRaises(PatchError): self.p.data(sha)

    def test_corrupt_existing_ledger_is_never_reinitialized(self):
        path = self.p.root/'state.json'
        path.write_text('{broken ledger')
        with self.assertRaises(PatchError): self.p.capture()
        self.assertEqual(path.read_text(), '{broken ledger')

    def test_active_full_direct_edit_cannot_bypass_counting_via_capture(self):
        (self.ws/'result.json').unlink()
        f.write(self.ws, {'progress.json': {'mode': 'full', 'repairs': []}})
        (self.theme/'style.css').write_text('/* Theme Name: Uncounted */')
        with self.assertRaises(PatchError): self.p.capture()
        f.write(self.ws, {'progress.json': {'mode': 'full', 'repairs': [{'id': 'counted', 'stage': '5', 'outcome': 'open'}]}})
        self.p.capture()
        self.assertEqual(self.p.load()['events'][-1]['attempt'], 'counted')

    def test_conflict_preserves_and_finishes_server_metadata_after_download_is_removed(self):
        remote = Path(self.tmp.name)/'download'
        incoming = remote/'theme/test-site'
        shutil.copytree(self.server, incoming)
        f.write(self.ws, {'theme-report.json': {'marker': 'previous'}})
        f.write(remote, {'theme-report.json': {'marker': 'new', 'menusDeclared': [
            {'location': 'test_site_nav_1', 'selector': '[data-ve-nav="1"]'}]},
            'chrome-groups.json': {'regions': {}, 'marker': 'new'}})
        self.edit({'style.css': '/* Theme Name: Local */'})
        (incoming/'style.css').write_text('/* Theme Name: Server */')
        with self.assertRaises(PatchError): self.p.integrate(incoming)
        self.assertEqual(json.loads((self.ws/'theme-report.json').read_text())['marker'], 'previous')
        shutil.rmtree(remote)
        self.p.resolve('5', 'keep local style with server metadata')
        self.assertEqual(json.loads((self.ws/'theme-report.json').read_text())['marker'], 'new')
        self.assertEqual(json.loads((self.ws/'chrome-groups.json').read_text())['marker'], 'new')
        manifest = json.loads((self.ws/'conversion-manifest.json').read_text())
        self.assertEqual(manifest['nav'][0]['zoneSelector'], '[data-ve-nav="1"]')

    def test_full_repair_needs_an_open_counted_attempt(self):
        (self.ws/'result.json').unlink()
        with self.assertRaises(PatchError): self.p.begin('5', 'repair')
        f.write(self.ws, {'progress.json': {'mode': 'full', 'repairs': [{'id': 'r1', 'stage': '5', 'outcome': 'open'}]}})
        self.p.begin('5', 'repair')
        (self.theme/'style.css').write_text('/* Theme Name: Counted */')
        self.p.record()
        self.assertEqual(self.p.load()['events'][-1]['attempt'], 'r1')

    def test_interrupted_swap_recovers_old_theme(self):
        state = self.p.load()
        backup = self.p.root/'theme-backup-test'
        os.replace(self.theme, backup)
        self.p.materialize(state['expected'], self.p.root/'promotion')
        f.write(self.p.root, {'promotion.json': {'backup': backup.name, 'state': state}})
        self.p.recover()
        self.assertEqual(self.p.snapshot(self.theme), state['expected'])
        self.assertFalse((self.p.root/'promotion.json').exists())

    def test_real_server_assembly_reapplies_patch_before_packaging(self):
        remote_ws = Path(self.tmp.name)/'server-job'
        manifest = {'schema': 'html2wp/1', 'workspace': str(remote_ws),
                    'site': {'name': 'Test', 'slug': 'test-site', 'prefix': 'test_site', 'version': '1.2.0'},
                    'input': {'dir': str(remote_ws/'source'), 'type': 'built-dist'},
                    'design': {'palette': [], 'fonts': []}, 'chrome': {'frontOwnsFooter': True}, 'nav': [],
                    'pages': [{'file': 'index.html', 'key': 'front-page', 'kind': 'front', 'chrome': 'self-contained'}],
                    'blog': {'present': False, 'reason': 'Single static page without articles'},
                    'shop': {'present': False, 'reason': 'No products'}}
        f.write(remote_ws, {'conversion-manifest.json': manifest,
            'source/index.html': '<html><head><title>Test</title></head><body><main>Retain this content</main></body></html>'})
        core = HERE.parents[4]/'server/core/scripts'
        def generate():
            commands = [
                ['node', str(HERE/'html-to-astro.mjs'), f'--manifest={remote_ws}/conversion-manifest.json'],
                ['node', str(core/'make-theme.mjs'), f'--manifest={remote_ws}/conversion-manifest.json'],
                ['node', str(core/'dist-to-bundle.mjs'), str(remote_ws/'astro-project/dist'), str(remote_ws/'bundle-out'),
                 '--theme-slug=test-site', '--theme-name=Test', '--theme-version=1.2.0',
                 f'--theme-dir={remote_ws}/theme/test-site', f'--manifest={remote_ws}/conversion-manifest.json', '--forbid-in-front=']]
            for i, command in enumerate(commands):
                built = subprocess.run(command, cwd=remote_ws, capture_output=True, text=True, timeout=60)
                self.assertEqual(built.returncode, 0, built.stdout+built.stderr)
                if i == 0: shutil.copytree(remote_ws/'astro-project/public', remote_ws/'astro-project/dist')
            applied = subprocess.run([sys.executable, str(HERE/'theme-patches.py'), str(self.ws), 'integrate',
                                      '--theme', str(remote_ws/'theme/test-site')], capture_output=True, text=True)
            self.assertEqual(applied.returncode, 0, applied.stdout+applied.stderr)
        generate()
        self.edit({'style.css': (self.theme/'style.css').read_text()+'\nbody { color: #123456; }\n'})
        generate()
        self.assertIn('#123456', (self.theme/'style.css').read_text())
        local_manifest = dict(manifest, workspace=str(self.ws))
        f.write(self.ws, {'conversion-manifest.json': local_manifest})
        env = {**os.environ, 'H2WP_WORKSPACE': str(self.ws), 'MAKE_ZIP_BEST_EFFORT': '1',
               'MAKE_ZIP_MANIFEST': str(self.ws/'conversion-manifest.json')}
        packed = subprocess.run(['bash', str(HERE/'make-zip.sh'), str(self.theme), str(self.ws/'patched.zip')],
                                env=env, capture_output=True, text=True)
        self.assertEqual(packed.returncode, 0, packed.stdout+packed.stderr)
        with zipfile.ZipFile(self.ws/'patched.zip') as archive:
            self.assertIn(b'#123456', archive.read('test-site/style.css'))
            self.assertIn(b'Retain this content', archive.read('test-site/clara-content/sources/front-page.html'))
            self.assertFalse(any('theme-patches' in n or n.endswith('.patch') for n in archive.namelist()))

    def test_start_over_archives_ledger_and_normal_cleanup_keeps_it(self):
        self.edit({'style.css': '/* Theme Name: Keep patch */'})
        env = {**os.environ, 'H2WP_WORKSPACE': str(self.ws), 'H2WP_START_OVER': '1'}
        f.write(self.ws, {'progress.json': {'mode': 'full', 'stages': []}, '.h2wp-verdicts-sent': 'yes'})
        clean = subprocess.run(['bash', str(HERE/'cleanup.sh'), str(self.ws), '--dry-run', '--force'],
                               env=env, capture_output=True, text=True)
        self.assertEqual(clean.returncode, 0, clean.stdout+clean.stderr)
        self.assertIn('theme-patches', clean.stdout.split('keeping:')[-1])
        reset = subprocess.run(['bash', str(HERE/'progress.sh'), 'mode', 'full', '--new'],
                               env=env, capture_output=True, text=True)
        self.assertEqual(reset.returncode, 0, reset.stdout+reset.stderr)
        self.assertFalse(self.p.root.exists())
        self.assertTrue(list(self.ws.glob('theme-patches-*')))

    def test_actual_cleanup_preserves_owner_edit_and_release_workflow(self):
        from PIL import Image
        Image.new('RGB', (1200, 900), 'white').save(self.theme/'screenshot.png')
        manifest = json.loads((self.ws/'conversion-manifest.json').read_text())
        manifest['blog'] = {'present': False, 'reason': 'No articles in this fixture'}
        f.write(self.ws, {'conversion-manifest.json': manifest, '.h2wp-verdicts-sent': 'yes'})
        clean = subprocess.run(['bash', str(HERE/'cleanup.sh'), str(self.ws), '--force'], capture_output=True, text=True)
        self.assertEqual(clean.returncode, 0, clean.stdout+clean.stderr)
        self.assertTrue((self.ws/'result.json').exists())
        self.p = Patches(self.ws)
        self.edit({'style.css': '/* Theme Name: Repaired after cleanup */'})
        released = subprocess.run([sys.executable, str(HERE/'package-theme.py'), str(self.ws)], capture_output=True, text=True)
        self.assertEqual(released.returncode, 0, released.stdout+released.stderr)
        self.assertTrue((self.ws/'out'/json.loads(released.stdout)['file']).is_file())


if __name__ == '__main__': unittest.main()
