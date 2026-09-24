#!/usr/bin/env python3
"""fetch-editor.py takes only the real Visual Edit Lite plugin.

The archive must hold visual-edit-lite/visual-edit-lite.php with a Plugin
Name header (an HTML error page saved as .zip is not a plugin); the release
must name a version and a plugin ZIP of at most 25 MB whose address is the
project's own release download. No network: the release answer is built here.

  python3 test-fetch-editor.py
"""
import importlib.util
import io
import unittest
import zipfile
from pathlib import Path

spec = importlib.util.spec_from_file_location('fetch_editor', Path(__file__).with_name('fetch-editor.py'))
fe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fe)


def archive(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w') as z:
        for name, text in files.items():
            z.writestr(name, text)
    return buffer.getvalue()


def release(**asset):
    base = {'name': 'visual-edit-lite-1.31.2.zip', 'size': 1000,
            'browser_download_url': fe.DOWNLOADS + 'v1.31.2/visual-edit-lite-1.31.2.zip'}
    return {'tag_name': 'v1.31.2', 'assets': [{**base, **asset}]}


class FetchEditor(unittest.TestCase):
    def test_only_the_plugin_is_a_plugin(self):
        self.assertTrue(fe.valid_archive(archive({fe.PLUGIN_FILE: '<?php\n/*\n * Plugin Name: Visual Edit Lite\n */'})))
        self.assertFalse(fe.valid_archive(archive({fe.PLUGIN_FILE: '<?php echo 1;'})))
        self.assertFalse(fe.valid_archive(archive({'other/other.php': 'Plugin Name: x'})))
        self.assertFalse(fe.valid_archive(b'<html>rate limited</html>'))

    def test_the_release_asset(self):
        self.assertEqual(fe.pick_asset(release()), ('v1.31.2', fe.DOWNLOADS + 'v1.31.2/visual-edit-lite-1.31.2.zip'))
        for bad, why in ((release(browser_download_url='https://evil.example/visual-edit-lite.zip'), 'official'),
                         (release(size=fe.MAX_BYTES + 1), 'large'),
                         (release(name='source.tar.gz'), 'no plugin ZIP'),
                         ({**release(), 'tag_name': 'v1; rm -rf /'}, 'version')):
            with self.assertRaises(ValueError) as caught:
                fe.pick_asset(bad)
            self.assertIn(why, str(caught.exception))


class Staged(unittest.TestCase):
    def run_with(self, staged, out):
        import os, subprocess, sys
        env = {**os.environ, 'H2WP_VE_LITE_ZIP': str(staged)}
        return subprocess.run([sys.executable, str(Path(__file__).with_name('fetch-editor.py')), str(out.parent), '--out', str(out)],
                              capture_output=True, text=True, env=env, timeout=30)

    def test_a_staged_zip_is_used_and_checked_never_downloaded(self):
        import tempfile, json
        with tempfile.TemporaryDirectory() as tmp:
            good = Path(tmp) / 'staged dir' / 'visual-edit-lite.zip'
            good.parent.mkdir()
            good.write_bytes(archive({fe.PLUGIN_FILE: '<?php\n/*\n * Plugin Name: Visual Edit Lite\n * Version: 1.31.2\n */'}))
            out = Path(tmp) / 'ws with space' / 'visual-edit-lite.zip'
            out.parent.mkdir()
            done = self.run_with(good, out)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(json.loads(done.stdout)['tag'], '1.31.2')
            self.assertEqual(out.read_bytes(), good.read_bytes())
            bad = Path(tmp) / 'bad.zip'
            bad.write_bytes(b'<html>not a zip</html>')
            self.assertEqual(self.run_with(bad, Path(tmp) / 'ws with space' / 'other.zip').returncode, 1)
            self.assertEqual(self.run_with(Path(tmp) / 'missing.zip', Path(tmp) / 'ws with space' / 'x.zip').returncode, 1)


if __name__ == '__main__':
    unittest.main()
