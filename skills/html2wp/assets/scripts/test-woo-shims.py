#!/usr/bin/env python3
"""A shop's cart behaviours in the theme (woo-shims.py) and the cart probe's
report (audit-woo-coverage.py --probe-cart). No WordPress: the theme files
and the report shapes.

- --cart-count puts the count token into every cart link of every header
  part: the design's own badge from the cart specimen, else a neutral one
  (and its CSS); a link that has one is left alone; a card linking to a
  product is not a cart link.
- --cart-sync and --option-cart-data write one PHP include (required once
  from functions.php, PHP that lints) and one script; features accumulate;
  a second run changes nothing.
- No shop: nothing to do.
- The probe's rows print their repair signatures, and --probe-cart merges
  its rows into the full report without losing the others.

  python3 test-woo-shims.py
"""
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('audit_woo', HERE / 'audit-woo-coverage.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)

HEADER = ('<!-- wp:html -->\n<header><a href="/">Home</a><a class="card" href="/product/scarf/">Scarf</a>'
          '<a class="bag" href="/cart/">Bag</a></header>\n<!-- /wp:html -->\n')


def shop(root, shop_present=True, specimen=None):
    ws = root / 'work space'
    theme = ws / 'theme' / 'knit'
    (theme / 'parts').mkdir(parents=True)
    (theme / 'functions.php').write_text("<?php\ndefine( 'KNIT_DIR', __DIR__ );\n")
    (theme / 'style.css').write_text('/*\nTheme Name: Knit\n*/\n')
    (theme / 'parts' / 'header.html').write_text(HEADER)
    (theme / 'parts' / 'header-front.html').write_text(HEADER.replace('class="bag"', 'class="bag light"'))
    (ws / 'conversion-manifest.json').write_text(json.dumps({
        'site': {'slug': 'knit', 'prefix': 'knit'}, 'shop': {'present': shop_present}}))
    if specimen:
        (ws / 'style-specimens').mkdir()
        (ws / 'style-specimens' / 'cart.html').write_text(specimen)
    return ws, theme


def shims(ws, *flags):
    return subprocess.run([sys.executable, str(HERE / 'woo-shims.py'), str(ws), *flags],
                          capture_output=True, text=True, timeout=60)


class Shims(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_design_badge_goes_into_every_header_cart_link(self):
        specimen = ('<header><a href="/cart/">Bag <span class="bag-count">2</span></a></header><main></main>')
        ws, theme = shop(Path(self.tmp.name), specimen=specimen)
        self.assertEqual(shims(ws, '--cart-count').returncode, 0)
        for part in ('header.html', 'header-front.html'):
            text = (theme / 'parts' / part).read_text()
            self.assertIn('Bag[wp-cart-count empty="hide"]<span class="bag-count">{count}</span>[/wp-cart-count]</a>', text)
            self.assertEqual(text.count('[wp-cart-count'), 1, 'the product card is not a cart link')
        self.assertNotIn('h2wp-cart-badge', (theme / 'style.css').read_text())
        before = (theme / 'parts' / 'header.html').read_text()
        self.assertEqual(shims(ws, '--cart-count').returncode, 0)
        self.assertEqual((theme / 'parts' / 'header.html').read_text(), before, 'idempotent')

    def test_no_specimen_badge_a_neutral_one_said_so(self):
        ws, theme = shop(Path(self.tmp.name))
        done = shims(ws, '--cart-count')
        self.assertIn('neutral default', done.stdout)
        self.assertIn('<span class="h2wp-cart-badge">{count}</span>', (theme / 'parts' / 'header.html').read_text())
        self.assertIn('.h2wp-cart-badge{', (theme / 'style.css').read_text())

    def test_sync_and_options_are_one_include_that_lints(self):
        ws, theme = shop(Path(self.tmp.name))
        self.assertEqual(shims(ws, '--cart-sync').returncode, 0)
        php = (theme / 'inc' / 'h2wp-shop-shims.php').read_text()
        js = (theme / 'assets' / 'h2wp-shop-shims.js').read_text()
        self.assertIn('wc/store/v1/cart', js)
        self.assertNotIn('woocommerce_add_cart_item_data', php)
        self.assertEqual(shims(ws, '--option-cart-data').returncode, 0)
        php = (theme / 'inc' / 'h2wp-shop-shims.php').read_text()
        js = (theme / 'assets' / 'h2wp-shop-shims.js').read_text()
        self.assertIn('woocommerce_add_cart_item_data', php)
        self.assertIn('wc/store/v1/cart', js, 'features accumulate')
        self.assertIn("h2wp_option[", js)
        functions = (theme / 'functions.php').read_text()
        self.assertEqual(functions.count("inc/h2wp-shop-shims.php"), 1)
        self.assertEqual(shims(ws, '--all').returncode, 0)
        snapshot = {p: p.read_text() for p in theme.rglob('*') if p.is_file()}
        self.assertEqual(shims(ws, '--all').returncode, 0)
        self.assertEqual(shims(ws, '--cart-sync').returncode, 0)
        self.assertEqual({p: p.read_text() for p in theme.rglob('*') if p.is_file()}, snapshot,
                         'a second run changes nothing')
        if shutil.which('php'):
            for f in (theme / 'inc' / 'h2wp-shop-shims.php', theme / 'functions.php'):
                lint = subprocess.run(['php', '-l', str(f)], capture_output=True, text=True)
                self.assertEqual(lint.returncode, 0, lint.stdout + lint.stderr)
        if shutil.which('node'):
            check = subprocess.run(['node', '--check', str(theme / 'assets' / 'h2wp-shop-shims.js')],
                                   capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)

    def test_no_shop_nothing_to_do(self):
        ws, theme = shop(Path(self.tmp.name), shop_present=False)
        done = shims(ws, '--all')
        self.assertEqual(done.returncode, 0)
        self.assertFalse((theme / 'inc').exists())


class Probe(unittest.TestCase):
    def setUp(self):
        audit.RESULT = {'ok': [], 'gaps': []}
        audit.gaps = 0

    def test_a_probe_row_names_its_repair_signature(self):
        err, out = io.StringIO(), io.StringIO()
        with redirect_stderr(err), redirect_stdout(out):
            audit.gap('the header shows no cart count', 'cart-count-missing')
            audit.gap('no coupon field', 'cart-coupon')
        self.assertIn('h2wp-signature: cart-count-missing', err.getvalue())
        self.assertNotIn('cart-coupon', err.getvalue(), 'only failures with levers carry a signature')

    def test_the_probe_again_replaces_only_its_own_rows(self):
        full = {'schema': 'h2wp-woo-coverage/1', 'target': 'html', 'passed': False, 'gaps': 3,
                'failures': ['cart-count-missing', 'cart-count-stale', 'cart-coupon'],
                'checked': ['simple-add', 'order']}
        probe = {'schema': 'h2wp-woo-coverage/1', 'target': 'html', 'passed': False, 'gaps': 1,
                 'failures': ['cart-count-stale'], 'checked': ['cart-count-missing']}
        merged = audit.merged_report(full, probe)
        self.assertEqual(merged['failures'], ['cart-count-stale', 'cart-coupon'])
        self.assertEqual(merged['gaps'], 2)
        self.assertIn('cart-count-missing', merged['checked'])
        self.assertIn('order', merged['checked'])
        self.assertFalse(merged['passed'])
        fixed = audit.merged_report(merged, {**probe, 'gaps': 0, 'failures': [],
                                             'checked': ['cart-count-missing', 'cart-count-stale']})
        self.assertEqual(fixed['failures'], ['cart-coupon'])


if __name__ == '__main__':
    unittest.main()
