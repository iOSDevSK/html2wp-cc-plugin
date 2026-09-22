#!/usr/bin/env python3
"""A copied workspace works on ITSELF: lib/manifest_paths.py resolves the
workspace and input.dir from where the manifest is, not from the absolute
paths it recorded — and optimize-markup, run on a copy, leaves the original
untouched.

  python3 test-manifest-paths.py
"""
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
from manifest_paths import input_dir_of, workspace_of  # noqa: E402


class ManifestPathsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmp)
        self.orig = self.tmp / "orig"
        (self.orig / "static-src").mkdir(parents=True)
        (self.orig / "static-src" / "index.html").write_text('<html><body><main><img src="a.png"></main></body></html>')
        self.mf = {"workspace": str(self.orig), "input": {"dir": str(self.orig / "static-src")},
                   "pages": [{"key": "home", "file": "index.html"}]}
        (self.orig / "conversion-manifest.json").write_text(json.dumps(self.mf))
        self.copy = self.tmp / "copy"
        shutil.copytree(self.orig, self.copy)

    def test_a_copy_resolves_to_itself(self):
        m = self.copy / "conversion-manifest.json"
        self.assertEqual(workspace_of(self.mf, m), self.copy)
        self.assertEqual(input_dir_of(self.mf, m), self.copy / "static-src")

    def test_the_original_and_other_shapes(self):
        m = self.orig / "conversion-manifest.json"
        self.assertEqual(workspace_of(self.mf, m), self.orig)
        self.assertEqual(input_dir_of(self.mf, m), (self.orig / "static-src").resolve())
        # Relative input; an input outside the workspace stays where it is.
        self.assertEqual(input_dir_of({"input": {"dir": "static-src"}}, self.copy / "conversion-manifest.json"), (self.copy / "static-src").resolve())
        outside = self.tmp / "elsewhere"
        self.assertEqual(input_dir_of({"workspace": str(self.orig), "input": {"dir": str(outside)}}, self.copy / "conversion-manifest.json"), outside.resolve())
        # A manifest under another name (a fixture) keeps its recorded workspace.
        other = self.tmp / "fixture.json"
        self.assertEqual(workspace_of(self.mf, other), self.orig.resolve())

    def test_a_stage_run_on_the_copy_leaves_the_original_alone(self):
        before = (self.orig / "static-src" / "index.html").read_text()
        r = subprocess.run([sys.executable, str(HERE / "optimize-markup.py"), f"--manifest={self.copy / 'conversion-manifest.json'}", "--apply"],
                           capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual((self.orig / "static-src" / "index.html").read_text(), before)
        self.assertFalse((self.orig / "optimize-markup-report.json").exists())
        self.assertTrue((self.copy / "optimize-markup-report.json").exists())
        self.assertNotEqual((self.copy / "static-src" / "index.html").read_text(), before)  # the copy was the one optimised


if __name__ == "__main__":
    unittest.main()
