#!/usr/bin/env python3
"""audit-woo-coverage.py: which target it audits, the report it writes, and
how send-verdicts.sh turns that report into the woo-coverage gate for both
targets. No WordPress and no network: the site probe is injected."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).parent
spec = importlib.util.spec_from_file_location('audit_woo', HERE / 'audit-woo-coverage.py')
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)  # must not touch the network at import time

GB_HOME = '<link rel="stylesheet" id=\'h2wp-gb-theme-css\' href="/wp-content/themes/x/style.css">'


def never():
    raise AssertionError('the site was probed although the target was already known')


class ReviewsVerdictTest(unittest.TestCase):
    def test_an_approved_review_must_show(self):
        v = audit.html_reviews_verdict
        self.assertEqual(v(False, True), "ok")                 # none before, the test review shows
        self.assertEqual(v(True, True), "ok")
        self.assertIn("not shown", v(False, False))           # the review never appeared
        self.assertEqual(v(True, None), "ok")                 # no wp-cli, reviews render anyway
        self.assertEqual(v(False, None), "unverified")        # no wp-cli, nothing to prove it with


class TargetTest(unittest.TestCase):
    def test_explicit_target_wins(self):
        self.assertEqual(audit.detect_target('html', {'schema': 'html2wp/2'}, never), 'html')
        self.assertEqual(audit.detect_target('gutenberg', {'schema': 'html2wp/1'}, never), 'gutenberg')

    def test_manifest_decides_without_probing(self):
        self.assertEqual(audit.detect_target('auto', {'schema': 'html2wp/2', 'target': 'gutenberg'}, never), 'gutenberg')
        self.assertEqual(audit.detect_target('auto', {'target': 'gutenberg'}, never), 'gutenberg')
        self.assertEqual(audit.detect_target('auto', {'schema': 'html2wp/2'}, never), 'gutenberg')
        self.assertEqual(audit.detect_target('auto', {'schema': 'html2wp/1', 'pages': []}, never), 'html')

    def test_installed_theme_decides_without_a_manifest(self):
        self.assertEqual(audit.detect_target('auto', None, lambda: (GB_HOME, None)), 'gutenberg')
        self.assertEqual(audit.detect_target('auto', {}, lambda: ('<html></html>', {'namespaces': ['wp/v2', 'h2wp-gb/v1']})), 'gutenberg')
        self.assertEqual(audit.detect_target('auto', None, lambda: ('<html></html>', {'namespaces': ['wp/v2', 'wc/store/v1']})), 'html')
        self.assertEqual(audit.detect_target('auto', None, lambda: (None, None)), 'html')


class ReportTest(unittest.TestCase):
    def test_shape(self):
        r = audit.build_report('gutenberg', {'ok': ['cart-native', 'reviews', 'order'], 'gaps': ['reviews', 'reviews']}, 2)
        self.assertEqual(r, {'schema': 'h2wp-woo-coverage/1', 'target': 'gutenberg', 'passed': False, 'gaps': 2,
                             'failures': ['reviews'], 'checked': ['cart-native', 'order']})
        self.assertTrue(audit.build_report('html', {'ok': ['order'], 'gaps': []}, 0)['passed'])

    def test_money_follows_the_store_format(self):
        row = {'prices': {'price': '123450', 'currency_minor_unit': 2, 'currency_prefix': '', 'currency_suffix': '\xa0€',
                          'currency_decimal_separator': ',', 'currency_thousand_separator': ' '}}
        self.assertEqual(audit.money(row), '1 234,50\xa0€')
        self.assertIn(audit.squash(audit.money(row)), audit.squash('Price: 1&nbsp;234,50 &euro; – 1 500,00 €'))


EUR = {'currency_minor_unit': 2, 'currency_prefix': '', 'currency_suffix': ' €',
       'currency_decimal_separator': ',', 'currency_thousand_separator': '.'}


def product(pid, slug, *, type='simple', price='1990', regular=None, stock=True, buyable=True,
            images=1, variations=(), attributes=(), categories=('Kit',)):
    """A Store API /products row, as much of it as the audit reads."""
    return {'id': pid, 'slug': slug, 'name': slug.replace('-', ' ').title(), 'type': type,
            'permalink': f'http://shop.test/anything/{slug}/', 'is_in_stock': stock, 'is_purchasable': buyable,
            'on_sale': regular is not None, 'images': [{'id': i} for i in range(images)],
            'prices': dict(EUR, price=price, regular_price=regular or price),
            'variations': list(variations), 'attributes': list(attributes),
            'categories': [{'name': c, 'link': f'http://shop.test/c/{c.lower()}/'} for c in categories]}


VARIABLE = product(2, 'wetsuit', type='variable', price='5000', variations=[
    {'id': 21, 'attributes': [{'name': 'Size', 'value': 'm'}, {'name': 'Colour', 'value': 'Red'}]},
    {'id': 22, 'attributes': [{'name': 'Size', 'value': 's'}, {'name': 'Colour', 'value': ''}]}],
    attributes=[{'name': 'Size', 'terms': [{'name': 'S', 'slug': 's'}, {'name': 'M', 'slug': 'm'}]}])


class ShapesTest(unittest.TestCase):
    """Any block shop: nothing assumes the fixture's products, slugs or pages."""

    def test_subjects_by_type_stock_and_sale(self):
        rows = [product(1, 'addon-towel', type='simple'), VARIABLE,
                product(3, 'gift-set', type='grouped'), product(4, 'sold-out-fins', stock=False),
                product(5, 'sale-cap', price='900', regular='1200'), product(6, 'voucher', type='external')]
        by = audit.gb_subjects(rows)
        self.assertEqual({k: (v or {}).get('slug') for k, v in by.items()},
                         {'simple': 'addon-towel', 'variable': 'wetsuit', 'soldout': 'sold-out-fins', 'onsale': 'sale-cap'})
        self.assertEqual(audit.gb_subjects([product(1, 'x', stock=False)])['simple'], None)
        # a simple product on sale is preferred to a variable one listed first
        sale_suit = dict(VARIABLE, on_sale=True)
        self.assertEqual(audit.gb_subjects([sale_suit, product(7, 'towel', price='2250', regular='3000')])['onsale']['slug'], 'towel')
        self.assertEqual(audit.gb_subjects([sale_suit])['onsale']['slug'], 'wetsuit')

    def test_listing_every_product_with_its_price(self):
        rows = [product(1, 'towel'), product(2, 'cap', price='900', regular='1200'),
                product(3, 'quote-only', price='', buyable=False), product(4, 'fins', stock=False)]
        cards = {1: 'Towel 19,90 €', 2: 'Cap 12,00 € 9,00 €', 3: 'Quote only Read more', 4: 'Fins 19,90 € Out of stock'}
        self.assertEqual(audit.listing_gaps(rows, cards), ([], []))
        self.assertEqual(audit.listing_gaps(rows, {1: 'Towel', 2: cards[2]}), (['quote-only', 'fins'], ['towel']))

    def test_product_page_requirements_follow_the_product(self):
        full = {'titles': ['Towel'], 'price': '19,90 €', 'gallery': True, 'form': True}
        self.assertEqual(audit.product_page_lacks(product(1, 'towel'), full), [])
        # the description's own h1 does not hide the title
        self.assertEqual(audit.product_page_lacks(product(1, 'towel'), dict(full, titles=['About it', 'Towel'])), [])
        self.assertEqual(audit.product_page_lacks(product(1, 'towel', images=0), dict(full, gallery=False)), [])
        self.assertEqual(audit.product_page_lacks(product(1, 'towel'), dict(full, gallery=False)), ['gallery'])
        self.assertEqual(audit.product_page_lacks(product(1, 'towel', stock=False), dict(full, form=False)), [])
        self.assertEqual(audit.product_page_lacks(product(1, 'towel'), {'titles': [], 'price': '', 'gallery': True}),
                         ['title', 'price', 'add-to-cart'])
        # a variable product shows a range starting at its lowest price
        self.assertEqual(audit.product_page_lacks(VARIABLE, dict(full, titles=['Wetsuit'], price='50,00 € – 65,00 €')), [])

    def test_variation_choices_match_real_variations(self):
        selects = [{'name': 'attribute_pa_size', 'options': ['', 's', 'm']},
                   {'name': 'attribute_colour', 'options': ['', 'Red', 'Blue']}]
        self.assertEqual(audit.variation_choices(VARIABLE, selects),
                         [{'attribute_pa_size': 'm', 'attribute_colour': 'Red'},
                          {'attribute_pa_size': 's', 'attribute_colour': 'Red'}])  # "any" colour -> first option
        self.assertEqual(audit.declared_values(VARIABLE), {'s', 'm', 'red'})

    def test_pages_found_by_block_whatever_their_slug(self):
        pages = [{'link': 'http://shop.test/basket/', 'content': {'rendered': '<div class="wp-block-woocommerce-cart">'}},
                 {'link': 'http://shop.test/pay/', 'content': {'rendered': '<div class="wp-block-woocommerce-checkout">'}},
                 {'link': 'http://shop.test/me/', 'content': {'rendered': '<form class="woocommerce-form woocommerce-form-login">'}},
                 {'link': 'http://shop.test/about/', 'content': {'rendered': '<p>cart and checkout talk</p>'}}]
        found = audit.pages_from_rest(pages)
        self.assertEqual(found, {'cart': 'http://shop.test/basket/', 'checkout': 'http://shop.test/pay/',
                                 'myaccount': 'http://shop.test/me/'})
        # WooCommerce's settings win over the page that merely renders a block
        # (a shop can keep WooCommerce's own /cart/ next to the configured one)
        settings = {'shop': {'id': 12, 'permalink': 'http://shop.test/'}, 'cart': {'id': 17, 'permalink': 'http://shop.test/kosik/'},
                    'checkout': {'id': 0, 'permalink': False}, 'terms': {'id': 3, 'permalink': 'http://shop.test/terms/'}}
        published = audit.store_pages(settings)
        self.assertEqual(published, {'shop': 'http://shop.test/', 'cart': 'http://shop.test/kosik/'})
        # a shop page that is the front page is the home URL
        self.assertEqual(audit.resolve_pages({}, published, found, 'http://shop.test'),
                         ('http://shop.test/', 'http://shop.test/kosik/', 'http://shop.test/pay/', 'http://shop.test/me/'))
        self.assertEqual(audit.resolve_pages({'cart': 'http://shop.test/c/'}, published, found, 'http://shop.test')[1],
                         'http://shop.test/c/')
        self.assertEqual(audit.resolve_pages({}, {}, {}, 'http://shop.test'),
                         ('http://shop.test/?post_type=product', None, None, None))

    def test_order_id_under_any_checkout_slug_and_permalinks(self):
        self.assertEqual(audit.order_id_from_url('http://s.test/pay/order-received/41/?key=wc_order_x'), '41')
        self.assertEqual(audit.order_id_from_url('http://s.test/?page_id=7&order-received=42&key=k'), '42')
        self.assertIsNone(audit.order_id_from_url('http://s.test/pay/'))

    def test_breadcrumb(self):
        audit.BASE = 'http://shop.test'
        row = product(1, 'towel', categories=('Kit',))
        self.assertEqual(audit.breadcrumb_verdict(row, ['http://shop.test/', 'http://shop.test/c/kit/']),
                         (True, 'http://shop.test/c/kit'))
        self.assertEqual(audit.breadcrumb_verdict(row, ['http://shop.test/', 'http://shop.test/c/other/']),
                         (False, 'http://shop.test/c/other'))
        self.assertIsNone(audit.breadcrumb_verdict(row, ['http://shop.test/']))


class SoldOutTest(unittest.TestCase):
    """A sold-out product passes only when the basket did not gain it."""

    def test_ids_cover_every_variation(self):
        self.assertEqual(audit.soldout_ids({'id': 40, 'variations': [{'id': 70}, {'id': 71}]}), {40, 70, 71})
        self.assertEqual(audit.soldout_ids({'id': 9}), {9})
        self.assertEqual(audit.soldout_ids({'id': 9, 'variations': None}), {9})

    def test_disabled_class_or_attribute_is_not_live(self):
        self.assertFalse(audit.button_is_live(True, 'single_add_to_cart_button button alt'))
        self.assertFalse(audit.button_is_live(False, 'single_add_to_cart_button button alt disabled wc-variation-is-unavailable'))
        self.assertTrue(audit.button_is_live(False, 'single_add_to_cart_button button alt wp-element-button'))
        self.assertTrue(audit.button_is_live(False, None))
        self.assertTrue(audit.button_is_live(False, 'is-disabled-look'))  # a word containing it is not the class

    def test_verdict_reads_the_basket(self):
        self.assertEqual(audit.soldout_verdict(True, 0, 1), 'bought')
        self.assertEqual(audit.soldout_verdict(False, 2, 3), 'bought')
        self.assertEqual(audit.soldout_verdict(True, 0, 0), 'refused at add to cart')
        self.assertEqual(audit.soldout_verdict(False, 1, 1), 'no live buy control')


class SpecLineTest(unittest.TestCase):
    """A shared spec line is frozen only when a product's own text lacks it."""

    GLOVES = {'description': '<p>Fine gloves.</p><p class="x"><span>Materials:</span> 100% extra-fine merino wool</p>'}
    SCARF = {'description': '<p>Two scarves.</p>', 'short_description': '<p>Materials: 100%&nbsp;extra-fine merino wool</p>'}
    JUMPER = {'description': '<p>A cashmere jumper.</p><p>Materials: 100% Mongolian cashmere</p>'}

    def test_both_products_really_share_it(self):
        line = 'Materials: 100% extra-fine merino wool'
        self.assertFalse(audit.spec_frozen([line, line], [self.GLOVES, self.SCARF]))

    def test_one_product_does_not_own_it(self):
        line = 'Materials: 100% extra-fine merino wool'
        self.assertTrue(audit.spec_frozen([line, line], [self.GLOVES, self.JUMPER]))
        self.assertTrue(audit.spec_frozen([line, line], [{}, {}]))

    def test_different_or_missing_lines_are_not_frozen(self):
        self.assertFalse(audit.spec_frozen(['Materials: wool', 'Materials: cashmere'], [{}, {}]))
        self.assertFalse(audit.spec_frozen(['', ''], [{}, {}]))
        self.assertFalse(audit.spec_frozen(['Materials: wool'], [{}]))


class ReviewsTest(unittest.TestCase):
    """Reviews work when placed in the product template and a real review shows."""

    def test_zero_reviews_render_nothing_until_one_exists(self):
        self.assertEqual(audit.reviews_verdict(True, False, True), 'ok')
        self.assertEqual(audit.reviews_verdict(None, True, None), 'ok')  # no wp-cli: a visible block is enough

    def test_missing_block_or_unshown_review_is_a_gap(self):
        self.assertIn('no reviews block', audit.reviews_verdict(False, False, None))
        self.assertIn('not shown', audit.reviews_verdict(True, False, False))
        self.assertIn('rendered nowhere', audit.reviews_verdict(None, False, None))


class VerdictMappingTest(unittest.TestCase):
    """The report lands where send-verdicts.sh reads it and maps to the gate."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.ws = Path(self.temp.name)
        (self.ws / '.h2wp-job.json').write_text(json.dumps({'job': 'job-1', 'token': 't'}))

    def tearDown(self):
        self.temp.cleanup()

    def woo_gate(self, manifest, report=None):
        (self.ws / 'conversion-manifest.json').write_text(json.dumps(dict(site={'slug': 'demo'}, pages=[], **manifest)))
        if report is not None:
            (self.ws / 'woo-coverage').mkdir(exist_ok=True)
            (self.ws / 'woo-coverage' / 'report.json').write_text(json.dumps(report))
        run = subprocess.run(['bash', str(HERE / 'send-verdicts.sh'), str(self.ws), '--dry-run'],
                             capture_output=True, text=True, env=dict(os.environ), timeout=30)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        gates = json.loads(run.stdout.split('This is the whole payload:', 1)[1])['gates']
        return next(g for g in gates if g['gate'] == 'woo-coverage')

    def test_v2_failed(self):
        report = audit.build_report('gutenberg', {'ok': ['order'], 'gaps': ['reviews']}, 1)
        self.assertEqual(self.woo_gate({'schema': 'html2wp/2', 'target': 'gutenberg'}, report),
                         {'gate': 'woo-coverage', 'verdict': 'failed', 'failedKeys': ['reviews']})

    def test_v2_passed(self):
        report = audit.build_report('gutenberg', {'ok': ['order'], 'gaps': []}, 0)
        self.assertEqual(self.woo_gate({'schema': 'html2wp/2', 'target': 'gutenberg'}, report),
                         {'gate': 'woo-coverage', 'verdict': 'passed'})

    def test_v1_maps_the_same_report(self):
        report = audit.build_report('html', {'ok': [], 'gaps': ['order', 'coupon']}, 2)
        self.assertEqual(self.woo_gate({'schema': 'html2wp/1'}, report),
                         {'gate': 'woo-coverage', 'verdict': 'failed', 'failedKeys': ['coupon', 'order']})

    def test_no_report_is_not_run(self):
        self.assertEqual(self.woo_gate({'schema': 'html2wp/2', 'target': 'gutenberg'}),
                         {'gate': 'woo-coverage', 'verdict': 'not-run'})


class WrittenReportTest(unittest.TestCase):
    def test_workspace_puts_the_report_where_send_verdicts_reads_it(self):
        with tempfile.TemporaryDirectory() as ws:
            audit.REPORT_PATH = os.path.join(ws, 'woo-coverage', 'report.json')
            audit.TARGET = 'gutenberg'
            audit.RESULT = {'ok': [], 'gaps': ['store-api']}
            audit.gaps = 1
            with self.assertRaises(SystemExit) as done:
                audit.finish(exit_code=1)
            self.assertEqual(done.exception.code, 1)
            self.assertEqual(json.loads(Path(audit.REPORT_PATH).read_text())['failures'], ['store-api'])


class FakeWp:
    """An option table behind wp-cli's option commands, recording calls."""
    def __init__(self, cod=None):
        self.cod = cod
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd.startswith("option get woocommerce_cod_settings"):
            return self.cod
        if cmd.startswith("option list --search=woocommerce_cod_settings"):
            return "0" if self.cod is None else "1"
        if cmd.startswith("option patch update woocommerce_cod_settings enabled yes"):
            if self.cod is None:
                return None  # wp-cli: "Could not get 'woocommerce_cod_settings' option"
            self.cod = json.dumps({**json.loads(self.cod), "enabled": "yes"})
            return ""
        if cmd.startswith("option update woocommerce_cod_settings --format=json "):
            self.cod = cmd.split("--format=json ", 1)[1].strip("'")
            return ""
        if cmd == "option delete woocommerce_cod_settings":
            self.cod = None
            return ""
        return None


class CodTest(unittest.TestCase):
    def test_fresh_shop_gets_cod_and_loses_it_again(self):
        wp = FakeWp(cod=None)
        prior, absent = audit.enable_cod(wp)
        self.assertEqual(json.loads(wp.cod)["enabled"], "yes")
        audit.restore_cod(wp, prior, absent)
        self.assertIsNone(wp.cod)

    def test_configured_shop_is_restored_exactly(self):
        before = json.dumps({"enabled": "no", "title": "Pay on delivery"})
        wp = FakeWp(cod=before)
        prior, absent = audit.enable_cod(wp)
        self.assertEqual(json.loads(wp.cod), {"enabled": "yes", "title": "Pay on delivery"})
        self.assertFalse(absent)
        audit.restore_cod(wp, prior, absent)
        self.assertEqual(json.loads(wp.cod), json.loads(before))

    def test_already_enabled_stays_enabled(self):
        before = json.dumps({"enabled": "yes"})
        wp = FakeWp(cod=before)
        audit.restore_cod(wp, *audit.enable_cod(wp))
        self.assertEqual(json.loads(wp.cod), {"enabled": "yes"})


if __name__ == '__main__':
    unittest.main()
