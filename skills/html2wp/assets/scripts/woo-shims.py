#!/usr/bin/env python3
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
"""A shop's cart behaviours the theme must have: the count, its sync, options.

    woo-shims.py {workspace} [--cart-count] [--cart-sync] [--option-cart-data] [--all] [--check]

Three things a shopper watches that no pixel gate can see, each found live on
a delivered shop after every check had passed:

--cart-count        the header's cart count. Every cart link of every header
                    part gets the count token the runtime fills
                    ([wp-cart-count empty="hide"]…[/wp-cart-count]) when it
                    has none: the design's own badge, read off the stage -1b
                    cart specimen (its header shows the bag with something in
                    it), else a small neutral badge — said in the report,
                    because the design drew none.
--cart-sync         the count follows WooCommerce's block cart. The runtime's
                    count moves with the classic add-to-cart fragments; the
                    block cart and checkout change the basket through the
                    Store API, where those never fire, so + / − / remove left
                    the header's number stale. A small script repaints every
                    count from the Store API when the block cart's store, or
                    either cart's events, say the basket changed.
--option-cart-data  a chosen option is part of the cart line. A design's size
                    (or colour) buttons on a product that has no variations in
                    WooCommerce are drawn, clickable and recorded nowhere: 1×XL
                    and 2×XS of one product became ONE line of 3. The chosen
                    option now rides into the cart item's own data — two sizes
                    are two lines, shown in the cart, the checkout and the order.

--all is the three (Flash stage 3.5 runs it for every shop, before make-zip);
each alone is a lever of the repair budget (assets/repair-levers.json).
Idempotent: a second run changes nothing. --check reports and writes nothing.

Writes inc/h2wp-shop-shims.php (required once from functions.php) and
assets/h2wp-shop-shims.js in the theme, and {workspace}/woo-shims.json.
Exit 0 = done (or nothing to do: no shop); 2 = usage.
"""
import argparse
import json
import re
import sys
from pathlib import Path

MARK = "h2wp-shop-shims"
CART_HREF = re.compile(r"""href=(["'])(?:[^"']*/)?cart(?:\.html)?/?(?:[?#][^"']*)?\1""", re.I)
DEFAULT_BADGE = '<span class="h2wp-cart-badge">{count}</span>'
DEFAULT_BADGE_CSS = (".h2wp-cart-badge{display:inline-block;min-width:1.5em;margin-left:.35em;padding:0 .3em;"
                     "border:1px solid currentColor;border-radius:999px;font-size:.72em;line-height:1.45em;"
                     "text-align:center}")

SYNC_JS = r"""
  // The header's cart count follows the basket, whichever cart changed it.
  function countNodes() {
    return document.querySelectorAll('[class$="-cart-count"][data-count],[class*="-cart-count "][data-count]');
  }
  function paint(n) {
    countNodes().forEach(function (el) {
      el.textContent = String(n);
      el.setAttribute('data-count', String(n));
    });
  }
  function apiRoot() {
    var link = document.querySelector('link[rel="https://api.w.org/"]');
    return (link && link.href) || (location.origin + '/wp-json/');
  }
  function pull() {
    if (!countNodes().length || !window.fetch) return;
    fetch(apiRoot().replace(/\/?$/, '/') + 'wc/store/v1/cart', { credentials: 'same-origin' })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (cart) { if (cart && typeof cart.items_count === 'number') paint(cart.items_count); })
      .catch(function () {});
  }
  ['wc-blocks_added_to_cart', 'wc-blocks_removed_from_cart'].forEach(function (name) {
    document.body.addEventListener(name, pull);
  });
  if (window.jQuery) {
    window.jQuery(document.body).on('added_to_cart removed_from_cart updated_cart_totals wc_cart_emptied', pull);
  }
  function watchStore() {
    var data = window.wp && window.wp.data;
    if (!data || !data.select || !data.select('wc/store/cart')) return false;
    var last = null;
    data.subscribe(function () {
      var cart = data.select('wc/store/cart').getCartData();
      var n = cart && typeof cart.itemsCount === 'number' ? cart.itemsCount : null;
      if (n !== null && n !== last) { last = n; paint(n); }
    });
    return true;
  }
  if (!watchStore()) window.addEventListener('load', watchStore);
  window.addEventListener('pageshow', function (e) { if (e.persisted) pull(); });
"""

OPTIONS_JS = r"""
  // A design's option buttons on a product with no variations: the chosen one
  // rides into the cart line (h2wp_option[<name>]), so two sizes are two lines.
  function label(el) {
    return (el.getAttribute('aria-label') || el.value || el.textContent || '').replace(/\s+/g, ' ').trim();
  }
  function isOption(el) {
    var text = label(el);
    return text && text.length <= 16 && !/^[+\-−–]$/.test(text) && !el.closest('.quantity')
      && el.type !== 'submit' && !/add.to.(cart|bag)|buy/i.test(text);
  }
  function groupName(group) {
    var probe = group.previousElementSibling;
    for (var i = 0; probe && i < 2; i++, probe = probe.previousElementSibling) {
      var t = (probe.textContent || '').replace(/\s+/g, ' ').trim();
      if (t && t.length <= 24) return t.replace(/[:\s]+$/, '');
    }
    return 'Option';
  }
  document.querySelectorAll('form.cart').forEach(function (form) {
    if (form.classList.contains('variations_form')) return;
    var scope = form.closest('main') || document.body;
    var groups = new Map();
    scope.querySelectorAll('button[type="button"],[role="radio"],input[type="radio"]').forEach(function (el) {
      if (!isOption(el) || !el.parentElement) return;
      if (!groups.has(el.parentElement)) groups.set(el.parentElement, []);
      groups.get(el.parentElement).push(el);
    });
    groups.forEach(function (items, group) {
      if (items.length < 2) return;
      var name = groupName(group);
      var input = document.createElement('input');
      input.type = 'hidden';
      input.name = 'h2wp_option[' + name + ']';
      form.appendChild(input);
      function choose(el) {
        input.value = label(el);
        items.forEach(function (i) { i.setAttribute('aria-pressed', i === el ? 'true' : 'false'); });
      }
      var chosen = items.filter(function (i) {
        return i.getAttribute('aria-pressed') === 'true' || i.getAttribute('aria-checked') === 'true' || i.checked
          || /(^|\s)(active|selected|is-active|is-selected|checked)(\s|$)/.test(i.className);
      })[0];
      if (chosen) choose(chosen);
      items.forEach(function (i) { i.addEventListener('click', function () { choose(i); }); });
    });
  });
"""

PHP_HEAD = """<?php
/**
 * The shop's cart behaviours (woo-shims.py): {features}.
 *
 * @package {slug}
 */

defined( 'ABSPATH' ) || exit;

add_action(
	'wp_enqueue_scripts',
	static function () {{
		wp_enqueue_script( '{mark}', get_template_directory_uri() . '/assets/{mark}.js', array(), filemtime( get_template_directory() . '/assets/{mark}.js' ), true );
	}}
);
"""

PHP_OPTIONS = """
// A chosen option (h2wp_option[<name>]) is the cart line's own data: two sizes
// of one product are two lines, and the line says which.
add_filter(
	'woocommerce_add_cart_item_data',
	static function ( $data ) {
		// phpcs:ignore WordPress.Security.NonceVerification.Missing -- WooCommerce verifies the add-to-cart request.
		$posted = isset( $_POST['h2wp_option'] ) && is_array( $_POST['h2wp_option'] ) ? wp_unslash( $_POST['h2wp_option'] ) : array();
		$chosen = array();
		foreach ( $posted as $name => $value ) {
			$name  = sanitize_text_field( (string) $name );
			$value = sanitize_text_field( (string) $value );
			if ( '' !== $name && '' !== $value ) {
				$chosen[ $name ] = $value;
			}
		}
		if ( $chosen ) {
			$data['h2wp_options'] = $chosen;
		}
		return $data;
	}
);
add_filter(
	'woocommerce_get_item_data',
	static function ( $item_data, $cart_item ) {
		foreach ( (array) ( $cart_item['h2wp_options'] ?? array() ) as $name => $value ) {
			$item_data[] = array(
				'key'   => $name,
				'value' => $value,
			);
		}
		return $item_data;
	},
	10,
	2
);
add_action(
	'woocommerce_checkout_create_order_line_item',
	static function ( $item, $key, $values ) {
		foreach ( (array) ( $values['h2wp_options'] ?? array() ) as $name => $value ) {
			$item->add_meta_data( $name, $value );
		}
	},
	10,
	3
);
"""


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def specimen_badge(ws):
    """The design's own badge: the stage -1b cart specimen's header draws the
    bag with something in it."""
    for name in ("cart.html", "cart-specimen.html"):
        path = ws / "style-specimens" / name
        if not path.is_file():
            continue
        html = path.read_text(encoding="utf-8", errors="replace")
        header = (re.search(r"<header\b[\s\S]*?</header>", html, re.I) or [html])[0]
        for link in re.finditer(r"<a\b[^>]*>[\s\S]*?</a>", header, re.I):
            if not CART_HREF.search(link.group(0)):
                continue
            m = re.search(r"<(span|em|i|b|sup|small)\b([^>]*)>\s*(\d+)\s*</\1>", link.group(0), re.I)
            if m:
                return f"<{m.group(1)}{m.group(2)}>{{count}}</{m.group(1)}>"
    return None


def cart_count(theme, ws, check):
    badge = specimen_badge(ws)
    changed, links = [], 0
    for part in sorted((theme / "parts").glob("header*.html")):
        text = part.read_text(encoding="utf-8")
        out, n = text, 0
        for m in reversed(list(re.finditer(r"<a\b[^>]*>[\s\S]*?</a>", text, re.I))):
            open_tag = re.match(r"<a\b[^>]*>", m.group(0)).group(0)
            if not CART_HREF.search(open_tag):
                continue
            links += 1
            if "[wp-cart-count" in m.group(0):
                continue
            close = m.start() + m.group(0).rfind("</a>")
            out = out[:close] + f'[wp-cart-count empty="hide"]{badge or DEFAULT_BADGE}[/wp-cart-count]' + out[close:]
            n += 1
        if n:
            changed.append(f"parts/{part.name}")
            if not check:
                part.write_text(out, encoding="utf-8")
    if changed and not badge and not check:
        style = theme / "style.css"
        css = style.read_text(encoding="utf-8")
        if ".h2wp-cart-badge{" not in css:
            style.write_text(css.rstrip("\n") + "\n" + DEFAULT_BADGE_CSS + "\n", encoding="utf-8")
    return {"cartLinks": links, "partsChanged": changed,
            "badge": "the design's own (cart specimen)" if badge else ("a neutral default — the design drew none"
                                                                        if changed else None)}


def install(theme, manifest, features, check):
    """inc/h2wp-shop-shims.php + assets/h2wp-shop-shims.js, required once from
    functions.php. Features accumulate: a lever adds its own to what ran."""
    php = theme / "inc" / f"{MARK}.php"
    have = set()
    if php.is_file():
        m = re.search(r"woo-shims\.py\): ([a-z, -]+)\.", php.read_text(encoding="utf-8"))
        have = {f.strip() for f in m.group(1).split(",")} if m else set()
    want = sorted(have | set(features))
    if want == sorted(have) and php.is_file():
        return {"features": want, "changed": []}
    if check:
        return {"features": want, "changed": [f"inc/{MARK}.php", f"assets/{MARK}.js"]}
    slug = (manifest.get("site") or {}).get("slug") or "theme"
    body = PHP_HEAD.format(features=", ".join(want), slug=slug, mark=MARK)
    if "option-cart-data" in want:
        body += PHP_OPTIONS
    js = "(function () {\n  'use strict';\n  if (!document.body) return;\n"
    js += (SYNC_JS if "cart-sync" in want else "") + (OPTIONS_JS if "option-cart-data" in want else "") + "})();\n"
    php.parent.mkdir(parents=True, exist_ok=True)
    (theme / "assets").mkdir(parents=True, exist_ok=True)
    php.write_text(body, encoding="utf-8")
    (theme / "assets" / f"{MARK}.js").write_text(js, encoding="utf-8")
    changed = [f"inc/{MARK}.php", f"assets/{MARK}.js"]
    functions = theme / "functions.php"
    text = functions.read_text(encoding="utf-8") if functions.is_file() else "<?php\n"
    line = f"require_once get_template_directory() . '/inc/{MARK}.php';"
    if line not in text:
        stripped = text.rstrip()
        if stripped.endswith("?>"):
            text = stripped[:-2].rstrip() + f"\n\n{line}\n"
        else:
            text = text.rstrip("\n") + f"\n\n{line}\n"
        functions.write_text(text, encoding="utf-8")
        changed.append("functions.php")
    return {"features": want, "changed": changed}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("workspace")
    ap.add_argument("--cart-count", action="store_true")
    ap.add_argument("--cart-sync", action="store_true")
    ap.add_argument("--option-cart-data", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    ws = Path(args.workspace).resolve()
    manifest = read_json(ws / "conversion-manifest.json")
    if not isinstance(manifest, dict):
        print(f"woo-shims: no conversion-manifest.json in {ws}", file=sys.stderr)
        return 2
    if not (manifest.get("shop") or {}).get("present"):
        print("woo-shims: no shop in the manifest — nothing to do")
        return 0
    theme = ws / "theme" / ((manifest.get("site") or {}).get("slug") or "")
    if not (theme / "functions.php").is_file():
        print(f"woo-shims: no theme at {theme} (stage 3 builds it)", file=sys.stderr)
        return 2
    wanted = {"cart-count": args.cart_count or args.all, "cart-sync": args.cart_sync or args.all,
              "option-cart-data": args.option_cart_data or args.all}
    if not any(wanted.values()):
        ap.error("name what to add: --cart-count, --cart-sync, --option-cart-data or --all")
    report = {}
    if wanted["cart-count"]:
        report["cartCount"] = cart_count(theme, ws, args.check)
    features = [f for f in ("cart-sync", "option-cart-data") if wanted[f]]
    if features:
        report["shims"] = install(theme, manifest, features, args.check)
    if not args.check:
        (ws / "woo-shims.json").write_text(json.dumps(report, indent=2) + "\n")
    cc = report.get("cartCount") or {}
    shims = report.get("shims") or {}
    print("woo-shims: "
          + (f"cart count in {len(cc.get('partsChanged', []))} header part(s) ({cc.get('badge')}); "
             if cc.get("partsChanged") else ("cart count already in every header cart link; " if cc else ""))
          + (f"{', '.join(shims.get('features', []))} in the theme" if shims else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
