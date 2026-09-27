#!/usr/bin/env python3
"""Capture config keeps TanStack SSR while bypassing deployment-only bundling."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

from lib.tanstack_build import enable_tanstack_prerender, lovable_capture_config, UnsupportedCaptureConfig, CAPTURE_NOTE

CONFIG = '''import { defineConfig } from "@lovable.dev/vite-tanstack-config";
export default defineConfig({
  nitro: { preset: "cloudflare-module", output: { serverDir: "dist/server" } },
  tanstackStart: { server: { entry: "server" }, prerender: { enabled: false } },
  vite: { resolve: { alias: { "@": "/src" } } }
});
'''


class CaptureConfig(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'package.json').write_text(json.dumps({
            'devDependencies': {'@lovable.dev/vite-tanstack-config': '2.5.3'}}))

    def evaluate(self, text):
        # Exercise actual JS object semantics, without importing project code.
        lines = text.splitlines()
        alias = lines[0].split(' as ')[1].split(' }')[0]
        code = f'const {alias} = options => options;\n' + '\n'.join(lines[1:])
        code = code.replace('export default ', 'const result = ')
        result = subprocess.run(['node', '--input-type=module', '-e', code + '\nconsole.log(JSON.stringify(result));'],
                                capture_output=True, text=True, check=True)
        return json.loads(result.stdout)

    def test_cloudflare_copy_uses_native_ssr_and_preserves_other_options(self):
        cfg = self.root / 'vite.config.ts'
        cfg.write_text(CONFIG)
        note = enable_tanstack_prerender(self.root)
        self.assertIn('Nitro deployment adapter disabled', note)
        options = self.evaluate(cfg.read_text())
        self.assertIs(options['nitro'], False)
        self.assertEqual(options['tanstackStart']['server'], {'entry': 'server'})
        self.assertIs(options['tanstackStart']['prerender']['enabled'], True)
        self.assertEqual(options['vite']['resolve']['alias'], {'@': '/src'})
        before = cfg.read_text()
        enable_tanstack_prerender(self.root)
        self.assertEqual(cfg.read_text(), before)

    def test_alias_and_identifier_collision(self):
        text = CONFIG.replace('defineConfig }', 'defineConfig as makeConfig }').replace('default defineConfig(', 'default makeConfig(')
        text += '\nconst __h2wpCaptureDefineConfig = 1;'
        transformed, note = lovable_capture_config(text, self.root)
        self.assertIsNotNone(note)
        self.assertIs(self.evaluate(transformed)['nitro'], False)

    def test_unknown_major_and_function_config_are_not_rewritten(self):
        for text in (CONFIG.replace('defineConfig({', 'defineConfig(() => ({'),
                     '/* example\n' + CONFIG + '\n*/',
                     CONFIG.replace('@lovable.dev/vite-tanstack-config', 'vite')):
            self.assertEqual(lovable_capture_config(text, self.root), (text, None))
        (self.root / 'package.json').write_text('{"dependencies":{"@lovable.dev/vite-tanstack-config":"3.0.0"}}')
        self.assertEqual(lovable_capture_config(CONFIG, self.root), (CONFIG, None))
        cfg = self.root / 'vite.config.ts'
        cfg.write_text(CONFIG)
        with self.assertRaisesRegex(UnsupportedCaptureConfig, 'left unchanged'):
            enable_tanstack_prerender(self.root)
        self.assertEqual(cfg.read_text(), CONFIG)

    def test_plain_tanstack_keeps_existing_prerender_path(self):
        cfg = self.root / 'vite.config.ts'
        cfg.write_text('import { tanstackStart } from "@tanstack/react-start/plugin/vite";\nexport default {plugins: [tanstackStart()]};')
        enable_tanstack_prerender(self.root)
        self.assertIn('crawlLinks: true', cfg.read_text())
        self.assertNotIn('nitro: false', cfg.read_text())

    def test_commented_examples_do_not_authorize_adaptation(self):
        callback = CONFIG.replace('defineConfig({', 'defineConfig(() => ({') + '\n// export default defineConfig({\n'
        cfg = self.root / 'vite.config.ts'
        cfg.write_text(callback)
        with self.assertRaises(UnsupportedCaptureConfig):
            enable_tanstack_prerender(self.root)
        self.assertEqual(cfg.read_text(), callback)
        cfg.write_text(callback + '\n' + CAPTURE_NOTE)
        with self.assertRaises(UnsupportedCaptureConfig):
            enable_tanstack_prerender(self.root)
        self.assertEqual(cfg.read_text(), callback + '\n' + CAPTURE_NOTE)
        example = CONFIG.replace('nitro: { preset: "cloudflare-module", output: { serverDir: "dist/server" } },',
                                 '/* nitro: { preset: "cloudflare-module" }, */')
        self.assertEqual(lovable_capture_config(example, self.root), (example, None))


if __name__ == '__main__':
    unittest.main()
