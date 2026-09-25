#!/usr/bin/env python3
"""
Stage 5.6 — the WooCommerce coverage audit. MANDATORY for a shop conversion.

  python3 assets/scripts/audit-woo-coverage.py --wp http://localhost:8080
  python3 assets/scripts/audit-woo-coverage.py --wp https://shop.example \\
      --wp-cli "docker exec -u www-data <container> wp"

WHY THIS EXISTS

Every check the pipeline runs before this one asks whether the shop LOOKS
right. None of them ask whether a shopper can actually use what WooCommerce
offers — and that class of failure ships green every time, because the page
renders beautifully while the control does nothing. All of these were found
LIVE, by a person, after every gate had passed:

  - "Add to cart" refused the size the page showed as chosen;
  - five products offered sizes they do not sell, and none they do;
  - the category tabs and the sort control did nothing at all;
  - a discount showed no struck price and no Sale badge anywhere;
  - every product claimed the specimen's materials and breadcrumb;
  - search could not find a single product;
  - a review left by a shopper was invisible forever.

So this audit shops. It discovers the catalogue through the public Store API,
walks the shop in a real browser, and asserts BEHAVIOUR: things land in the
basket with the right variant, promotions show what they save, each product
states its own facts, and — with wp-cli access — a coupon discounts a real
basket and a cash-on-delivery order completes end to end, after which every
fixture it created is removed again.

Read-only against any URL; the wp-cli half configures nothing permanent.
Exit code = number of GAP lines.

TWO TARGETS

An HTML-theme conversion (manifest html2wp/1) and a native Gutenberg one
(html2wp/2, target gutenberg) sell through different markup: the first
through the theme's own product parts, the second through WooCommerce's
blocks (product collection, add-to-cart form, the block cart and checkout).
The target is `--target` when given, else the workspace manifest
(`--workspace`), else the installed theme itself. The HTML walk is the one above,
unchanged. The Gutenberg walk asserts the same behaviour on the block
theme: the catalogue page lists every product with its price and link, each
product page renders its title, price, gallery and add-to-cart form, the
basket really fills, cart and checkout are WooCommerce's native blocks, and
with wp-cli a coupon + COD order completes, stock goes down by what was
bought, and the coupon, the order and the stock are put back.

`--workspace {ws}` also writes {ws}/woo-coverage/report.json (or `--report`),
which send-verdicts.sh reports as the `woo-coverage` gate.
"""

import argparse
import html
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.request
from urllib.parse import urlencode, urljoin

args = None
BASE = ""

gaps = 0
# What the report carries: the check keys that passed and failed. Keys only —
# send-verdicts.sh forwards `failures` as page-key-shaped names.
RESULT = {"ok": [], "gaps": []}


def parse_args(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--wp", required=True, help="site root")
    ap.add_argument("--wp-cli", default="", help="command prefix that runs wp-cli on the install "
                    "(enables the coupon + end-to-end order checks, with cleanup)")
    ap.add_argument("--workspace", default="", help="conversion workspace: its manifest names the "
                    "target, and the report lands in {workspace}/woo-coverage/report.json")
    ap.add_argument("--report", default="", help="write the JSON report here instead")
    ap.add_argument("--target", choices=("auto", "html", "gutenberg"), default="auto",
                    help="which theme kind to audit (default: from the manifest, else the site)")
    ap.add_argument("--probe-cart", action="store_true",
                    help="only the cart probe (the header count, its sync, options as lines), merged into "
                         "the existing report — the check a repair lever runs again")
    return ap.parse_args(argv)


def ok(msg, key=None):
    if key:
        RESULT["ok"].append(key)
    print(f"ok   {msg}")


# The cart probe's rows are failures Flash's repair budget has levers for
# (assets/repair-levers.json): each prints its signature.
SIGNATURES = {"cart-count-missing", "cart-count-stale", "cart-options-merged"}


def gap(msg, key="other"):
    global gaps
    gaps += 1
    RESULT["gaps"].append(key)
    print(f"GAP  {msg}")
    if key in SIGNATURES:
        print(f"h2wp-signature: {key}", file=sys.stderr)


def note(msg):
    print(f"     {msg}")


def fetch_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (woo-coverage audit)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001
        return None


def wpcli(cmd):
    """Run a wp-cli command; returns stdout or None. Never raises — a failed
    fixture is a note, not a crash, and cleanup must still run.

    No shell. `--wp-cli` is an operator-supplied command prefix by design
    (`docker exec ct wp --allow-root`), and it is split once with shlex; the
    command itself is split the same way and appended as argv. The reason is
    not the prefix — it is `cmd`, which some callers build from WordPress's
    own output (`post delete {cid} --force`, where cid came back from a
    previous wp-cli call). With shell=True that output was being re-parsed by
    a shell. One caller further down already escaped its interpolation by
    hand, which is what makes the omission here an inconsistency rather than
    a decision.
    """
    try:
        r = subprocess.run(shlex.split(args.wp_cli) + shlex.split(cmd),
                           capture_output=True, text=True, timeout=120)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- discovery
#
# The Store API is public and is the shop's own truth: slugs, live prices,
# sale state, stock, whether a product has options. Everything below picks
# its test subjects from it rather than from a hardcoded list, so the audit
# runs unchanged on any converted shop.

def discover():
    rows = fetch_json(f"{BASE}/wp-json/wc/store/v1/products?per_page=100") or []
    if not rows:
        gap("Store API unreachable — is WooCommerce active?", "store-api")
        finish(exit_code=1)

    by = {
        "variable": next((r for r in rows if r.get("has_options") and r.get("is_in_stock")), None),
        "simple": next((r for r in rows if not r.get("has_options") and r.get("is_in_stock")
                        and r.get("is_purchasable")), None),
        "soldout": next((r for r in rows if not r.get("is_in_stock")), None),
        "onsale": next((r for r in rows if r.get("on_sale") and r.get("is_in_stock")), None),
    }
    note(f"catalogue: {len(rows)} product(s); variable={bool(by['variable'])} simple={bool(by['simple'])} "
         f"soldout={bool(by['soldout'])} onsale={bool(by['onsale'])}")
    return rows, by


# ------------------------------------------------------------------- target

def manifest_target(manifest):
    """The same predicate send-verdicts.sh and compare-pages.py use."""
    if not isinstance(manifest, dict) or not manifest:
        return None
    if manifest.get("schema") == "html2wp/2" or manifest.get("target") == "gutenberg":
        return "gutenberg"
    return "html"


def site_target(home_html, rest_index):
    """What the installed theme is, when no manifest says so. The block theme
    enqueues its stylesheet as `h2wp-gb-theme` and registers the `h2wp-gb/v1`
    REST namespace; the HTML theme does neither."""
    if isinstance(home_html, str) and re.search(r"""id=['"]h2wp-gb-theme-css['"]""", home_html):
        return "gutenberg"
    namespaces = (rest_index or {}).get("namespaces") if isinstance(rest_index, dict) else None
    if isinstance(namespaces, list) and "h2wp-gb/v1" in namespaces:
        return "gutenberg"
    return "html"


def detect_target(requested, manifest, probe):
    """--target wins, then the workspace manifest, then the live site.
    `probe` is called only when neither answers, and returns (home_html, rest_index)."""
    if requested in ("html", "gutenberg"):
        return requested
    return manifest_target(manifest) or site_target(*probe())


def probe_site():
    home = None
    req = urllib.request.Request(f"{BASE}/", headers={"User-Agent": "Mozilla/5.0 (woo-coverage audit)"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            home = r.read().decode("utf-8", "replace")
    except Exception:  # noqa: BLE001
        pass
    return home, fetch_json(f"{BASE}/wp-json/")


def build_report(target, result, gap_count):
    """The shape send-verdicts.sh's gate() reads: `passed`, and the failing
    check keys under `failures`."""
    failures = sorted(set(result["gaps"]))
    return {
        "schema": "h2wp-woo-coverage/1",
        "target": target,
        "passed": gap_count == 0,
        "gaps": gap_count,
        "failures": failures,
        "checked": sorted(set(result["ok"]) - set(failures)),
    }


REPORT_PATH = ""
TARGET = "html"
PROBE_KEYS = ("cart-count-missing", "cart-count-stale", "cart-options-merged")


def merged_report(old, new):
    """--probe-cart: the probe's rows replace their own in the full report;
    every other row stays what the full audit found."""
    if not isinstance(old, dict) or not old:
        return new
    failures = sorted((set(old.get("failures") or []) - set(PROBE_KEYS)) | set(new["failures"]))
    checked = sorted((set(old.get("checked") or []) - set(PROBE_KEYS)) | set(new["checked"]))
    others = max(0, int(old.get("gaps") or 0) - len([k for k in (old.get("failures") or []) if k in PROBE_KEYS]))
    return {**old, "passed": not failures, "gaps": others + new["gaps"], "failures": failures,
            "checked": [c for c in checked if c not in failures]}


def finish(exit_code=None):
    if REPORT_PATH:
        os.makedirs(os.path.dirname(os.path.abspath(REPORT_PATH)), exist_ok=True)
        report = build_report(TARGET, RESULT, gaps)
        if args is not None and getattr(args, "probe_cart", False):
            try:
                with open(REPORT_PATH) as f:
                    report = merged_report(json.load(f), report)
            except (OSError, ValueError):
                pass
        with open(REPORT_PATH, "w") as f:
            json.dump(report, f, indent=2)
            f.write("\n")
    if exit_code is not None:
        sys.exit(exit_code)
    print(f"\n{'WOO COVERAGE CLEAN' if gaps == 0 else f'{gaps} GAP(s)'}")
    sys.exit(min(gaps, 120))


def price_of(row):
    p = row.get("prices") or {}
    minor = int(p.get("currency_minor_unit", 2))
    cur = p.get("currency_symbol", "$")
    def fmt(v):
        return f"{cur}{int(v) / (10 ** minor):,.2f}"
    return fmt(p.get("price", 0)), fmt(p.get("regular_price", 0))


REVIEWS_SEL = "#reviews, #review_form, .woocommerce-Reviews"


def html_reviews_verdict(shown_before, shown_after):
    """'ok' when reviews work on the HTML theme's product page, else the GAP.

    shown_before: the reviews section renders for the product as it is (a
    design without reviews shows none until one exists — not a gap);
    shown_after: an approved test review rendered (None = not exercised, no
    wp-cli)."""
    if shown_after is True:
        return "ok"
    if shown_after is None:
        return "ok" if shown_before else "unverified"
    return "an approved review is not shown on its product page"


def enable_cod(run):
    """Turn cash on delivery on for the audit's order; returns what to restore.

    On a shop whose payments were never configured the option does not exist,
    `option patch` fails, and the order is refused for want of a payment method
    ("There are no payment methods available") — so it is created, and deleted
    again by restore_cod(). Both targets use this; the HTML path used to patch
    only, and reported a GAP on every freshly installed shop.
    `run` is wpcli (injected in tests)."""
    prior = run("option get woocommerce_cod_settings --format=json")
    absent = prior is None and run("option list --search=woocommerce_cod_settings --format=count") == "0"
    if absent:
        run("""option update woocommerce_cod_settings --format=json '{"enabled":"yes"}'""")
    else:
        run("option patch update woocommerce_cod_settings enabled yes")
    return prior, absent


def restore_cod(run, prior, absent):
    """Put the COD settings back exactly as enable_cod() found them."""
    if prior:
        enc = prior.replace("'", "'\\''")
        run(f"option update woocommerce_cod_settings --format=json '{enc}'")
    elif absent:
        run("option delete woocommerce_cod_settings")


def place_block_order(page):
    """Fill WooCommerce's block checkout with a throwaway address and place
    the order; the caller reads the outcome off page.url."""
    page.fill("#email", "audit@coverage.test")
    # Country and state are selects, and an unchosen country is
    # the one field that silently blocks the whole order.
    for prefix in ("shipping", "billing"):
        c = page.query_selector(f"#{prefix}-country")
        if c and c.evaluate("e => e.tagName") == "SELECT":
            has_us = c.evaluate("e => [...e.options].some(o => o.value === 'US')")
            val = "US" if has_us else c.evaluate(
                "e => ([...e.options].find(o => o.value) || {}).value || ''")
            if val:
                page.select_option(f"#{prefix}-country", val)
                page.wait_for_timeout(800)
            st = page.query_selector(f"#{prefix}-state")
            if st:
                if st.evaluate("e => e.tagName") == "SELECT":
                    sv = st.evaluate("e => ([...e.options].find(o => o.value) || {}).value || ''")
                    if sv:
                        page.select_option(f"#{prefix}-state", sv)
                else:
                    st.fill("Audit State")
            break
    for fid, val in (("first_name", "Audit"), ("last_name", "Coverage"),
                     ("address_1", "1 Audit St"), ("city", "Testville"),
                     ("postcode", "10001")):
        for prefix in ("shipping", "billing"):
            el = page.query_selector(f"#{prefix}-{fid}")
            if el:
                el.fill(val)
                break
    page.wait_for_timeout(1500)
    # The block checkout disables its button while totals
    # recalculate (the coupon just changed them); a click that
    # lands in that window is swallowed without an error. Click,
    # wait, and click once more if nothing moved.
    for attempt in range(2):
        page.click(".wc-block-components-checkout-place-order-button")
        for _ in range(18):
            page.wait_for_timeout(900)
            if "order-received" in page.url:
                break
        if "order-received" in page.url:
            break
        page.wait_for_load_state("networkidle")


COUNT_SEL = '[class$="-cart-count"][data-count], [class*="-cart-count "][data-count]'
OPTION_JS = """() => {
  const label = (e) => (e.getAttribute('aria-label') || e.value || e.textContent || '').replace(/\\s+/g, ' ').trim();
  const scope = document.querySelector('main') || document.body;
  const groups = new Map();
  for (const e of scope.querySelectorAll('button[type="button"],[role="radio"],input[type="radio"]')) {
    const t = label(e);
    if (!t || t.length > 16 || /^[+\\-\u2212\u2013]$/.test(t) || e.closest('.quantity') || /add.to.(cart|bag)|buy/i.test(t)) continue;
    if (!groups.has(e.parentElement)) groups.set(e.parentElement, []);
    groups.get(e.parentElement).push(e);
  }
  for (const [g, items] of groups) if (items.length >= 2) {
    items.forEach((e, i) => e.setAttribute('data-h2wp-probe-option', String(i)));
    return items.map(label);
  }
  return [];
}"""


def probe_cart(ctx, by):
    """What a shopper watches and no pixel gate sees: the header's cart count
    appears after an add, follows the cart's + / − / remove, and two options of
    one product are two lines. A fresh browser context: an empty basket."""
    subject = by["simple"] or by["variable"]
    if not subject:
        note("cart probe: no product in stock to put in the basket")
        return
    page = ctx.new_page()

    def cart_state():
        return page.evaluate("""async () => {
          const link = document.querySelector('link[rel="https://api.w.org/"]');
          const root = ((link && link.href) || (location.origin + '/wp-json/')).replace(/\\/?$/, '/');
          const r = await fetch(root + 'wc/store/v1/cart', {credentials: 'same-origin'});
          const c = r.ok ? await r.json() : null;
          return c ? {count: c.items_count, lines: (c.items || []).length} : null;
        }""")

    def badges():
        return page.eval_on_selector_all(COUNT_SEL, "es => es.map(e => (e.getAttribute('data-count') || '').trim())")

    def buy(row, option=None):
        page.goto(row["permalink"], wait_until="networkidle")
        page.wait_for_timeout(1000)
        if option is not None:
            page.evaluate(OPTION_JS)
            chip = page.query_selector(f"[data-h2wp-probe-option='{option}']")
            if chip:
                chip.click()
                page.wait_for_timeout(300)
        btn = page.query_selector("form.cart button:not([type=button])") or page.query_selector("form.cart [type=submit]")
        if not btn:
            return False
        btn.click()
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(1200)
        return True

    # 1. the count shows after an add, on a page with the header
    if not buy(subject):
        note(f"cart probe: {subject['slug']} has no add-to-cart control — the probe did not run")
        page.close()
        return
    page.goto(f"{BASE}/", wait_until="networkidle")
    page.wait_for_timeout(1200)
    state = cart_state() or {}
    shown = badges()
    if state.get("count") and shown and all(b == str(state["count"]) for b in shown):
        ok(f"the header shows the cart count after an add ({state['count']})", "cart-count-missing")
    elif not shown:
        gap("after add to cart the header shows no cart count — no count element in any header", "cart-count-missing")
    else:
        gap(f"after add to cart the header says {shown} where the basket holds {state.get('count')}",
            "cart-count-missing")

    # 2. + / − / remove in the cart move it
    if shown:
        page.goto(f"{BASE}/cart/", wait_until="networkidle")
        page.wait_for_timeout(2500)
        steps = []
        plus = page.query_selector(".wc-block-components-quantity-selector__button--plus")
        if plus:
            plus.click()
            page.wait_for_timeout(2500)
            steps.append(("+", cart_state(), badges()))
            minus = page.query_selector(".wc-block-components-quantity-selector__button--minus")
            if minus:
                minus.click()
                page.wait_for_timeout(2500)
                steps.append(("−", cart_state(), badges()))
            remove = page.query_selector(".wc-block-cart-item__remove-link")
            if remove:
                remove.click()
                page.wait_for_timeout(2500)
                steps.append(("remove", cart_state(), badges()))
        else:
            qty = page.query_selector("form.woocommerce-cart-form input.qty")
            if qty:
                qty.fill("2")
                page.click("button[name=update_cart]")
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(1500)
                steps.append(("quantity 2", cart_state(), badges()))
        stale = [f"{what}: header {b} vs basket {(s or {}).get('count')}" for what, s, b in steps
                 if b and s is not None and any(x != str(s.get("count")) for x in b)]
        if not steps:
            note("cart probe: the cart offers no quantity control to change — the sync was not exercised")
        elif stale:
            gap("the header count does not follow the cart — " + "; ".join(stale), "cart-count-stale")
        else:
            ok(f"the header count follows the cart ({', '.join(w for w, _, _ in steps)})", "cart-count-stale")

    # 3. two options of one product are two lines
    row = by["simple"]
    if row:
        page.goto(row["permalink"], wait_until="networkidle")
        page.wait_for_timeout(1000)
        options = page.evaluate(OPTION_JS)
        if len(options) >= 2:
            page.goto(f"{BASE}/cart/", wait_until="networkidle")
            before = (cart_state() or {}).get("lines", 0)
            buy(row, 0)
            buy(row, 1)
            after = (cart_state() or {}).get("lines", 0)
            if after - before >= 2:
                ok(f"two options of {row['slug']} ({options[0]}, {options[1]}) are two cart lines", "cart-options-merged")
            else:
                gap(f"{row['slug']}: {options[0]} and {options[1]} went into the cart as {after - before} line(s) — "
                    "the option the shopper chose is recorded nowhere", "cart-options-merged")
        else:
            note(f"cart probe: {row['slug']} offers no option buttons — nothing to merge")
    page.close()


def run_probe(by):
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        try:
            probe_cart(b.new_context(viewport={"width": 1440, "height": 1200}), by)
        finally:
            b.close()


def run_html(by):
    from playwright.sync_api import sync_playwright

    cleanup = []
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        ctx = b.new_context(viewport={"width": 1440, "height": 1200})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        # The cart probe first, in its own context: an empty basket.
        probe_cart(b.new_context(viewport={"width": 1440, "height": 1200}), by)

        def product_url(row):
            return row["permalink"]

        def add_to_cart(p, row, expect_ok=True):
            p.goto(product_url(row), wait_until="networkidle")
            p.wait_for_timeout(1200)
            btn = p.query_selector("form.cart button:not([type=button])") \
                or p.query_selector("form.cart [type=submit]") \
                or p.query_selector("button.single_add_to_cart_button")
            if not btn:
                return None
            btn.click()
            p.wait_for_load_state("networkidle")
            p.wait_for_timeout(1200)
            return p.query_selector(".woocommerce-error, .wc-block-components-notice-banner.is-error") is None

        # ---- 1. the variable product sells the variant the page shows ----
        if by["variable"]:
            row = by["variable"]
            page.goto(product_url(row), wait_until="networkidle")
            page.wait_for_timeout(1200)
            sel = page.query_selector("form.variations_form select[name^='attribute_']")
            if sel:
                pre = sel.evaluate("e => e.value")
                vid = page.eval_on_selector("form.variations_form input[name=variation_id]", "e => e.value") \
                    if page.query_selector("form.variations_form input[name=variation_id]") else "0"
                if pre and vid not in ("", "0"):
                    ok(f"{row['slug']}: a variant is chosen on load and resolved ({pre})", "variant-chosen")
                else:
                    gap(f"{row['slug']}: nothing chosen on load (value={pre!r}, variation_id={vid}) — "
                        "the first click on Add to cart will be refused", "variant-chosen")
                accepted = add_to_cart(page, row)
                if accepted:
                    ok("the untouched-chooser purchase was accepted", "variable-add")
                elif accepted is False:
                    gap("clicking Add to cart without touching the chooser was refused", "variable-add")
                else:
                    gap(f"{row['slug']}: no add-to-cart control found", "variable-add")
                # re-clicking the chosen chip must not clear the choice
                # (fresh page — the add-to-cart above navigated away)
                page.goto(product_url(row), wait_until="networkidle")
                page.wait_for_timeout(1000)
                chip = page.query_selector(f"[data-cve-value='{pre}']")
                if chip:
                    chip.click()
                    page.wait_for_timeout(500)
                    still = page.eval_on_selector(
                        "form.variations_form select[name^='attribute_']", "e => e.value")
                    if still == pre:
                        ok("re-clicking the chosen variant keeps it chosen", "variant-chip")
                    else:
                        gap(f"re-clicking the chosen variant CLEARED it ({pre!r} -> {still!r})", "variant-chip")
                # the chips on offer are the product's own values
                chips = page.eval_on_selector_all("[data-cve-attribute][data-cve-value]",
                                                  "es => es.map(e => e.getAttribute('data-cve-value'))")
                if chips:
                    declared = set()
                    for v in row.get("variations", []):
                        for a in v.get("attributes", []):
                            declared.add(str(a.get("value", "")).lower())
                    extra = [c for c in set(chips) if declared and c.lower() not in declared]
                    if not extra:
                        ok(f"the chooser offers only this product's values ({sorted(set(chips))})", "variant-values")
                    else:
                        gap(f"the chooser offers values the product does not sell: {extra}", "variant-values")
            else:
                gap(f"{row['slug']}: variable product carries no wired variation form", "variable-form")

        # ---- 2. simple product ----
        if by["simple"]:
            if add_to_cart(page, by["simple"]):
                ok(f"{by['simple']['slug']}: simple product reaches the basket", "simple-add")
            else:
                gap(f"{by['simple']['slug']}: simple product could not be bought", "simple-add")

        # ---- 3. sold out cannot be bought ----
        if by["soldout"]:
            row = by["soldout"]
            page.goto(product_url(row), wait_until="networkidle")
            page.wait_for_timeout(1000)
            live_btn = page.query_selector("form.cart button:not([type=button]):not([disabled])")
            if live_btn is None:
                ok(f"{row['slug']}: sold out and not buyable", "soldout")
            else:
                gap(f"{row['slug']}: sold out but its buy control is live", "soldout")

        # ---- 4. a promotion shows what it saves ----
        if by["onsale"]:
            row = by["onsale"]
            sale, regular = price_of(row)
            page.goto(product_url(row), wait_until="networkidle")
            page.wait_for_timeout(1000)
            body = page.inner_text("main")
            if sale in body and regular in body:
                ok(f"{row['slug']}: sale shows both prices ({sale}, was {regular})", "sale-single")
            else:
                gap(f"{row['slug']}: sale page shows only {sale!r} — the struck {regular!r} is missing", "sale-single")
            listing = "/".join(product_url(row).rstrip("/").split("/")[:-2]) or f"{BASE}/shop"
            page.goto(f"{BASE}/shop/", wait_until="networkidle")
            page.wait_for_timeout(1200)
            card = page.query_selector(f"main a[href*='{row['slug']}']")
            if card:
                text = card.inner_text()
                if regular in text:
                    ok("the listing card carries the struck original too", "sale-listing")
                else:
                    gap(f"the listing card shows only the current price — no {regular!r}", "sale-listing")
        else:
            note("no product on sale — the sale-presentation checks were NOT exercised; "
                 "put one product on sale and re-run before handover")

        # ---- 5. each product states its own facts (the frozen-specimen class) ----
        picks = [r for r in [by["variable"], by["simple"], by["soldout"]] if r][:2]
        if len(picks) == 2:
            facts = []
            for row in picks:
                page.goto(product_url(row), wait_until="networkidle")
                page.wait_for_timeout(900)
                crumb = page.eval_on_selector("main a[href*='category']",
                                              "e => e.innerText.trim().toLowerCase()") \
                    if page.query_selector("main a[href*='category']") else ""
                spec = page.evaluate("() => { const m = [...document.querySelectorAll('main p')]"
                                     ".find(x => /^[A-Za-z\\u00C0-\\u017F ]{2,24}:/.test(x.innerText)); "
                                     "return m ? m.innerText : ''; }")
                cats = [c.get("name", "").lower() for c in row.get("categories", [])]
                if crumb and cats and crumb not in cats:
                    gap(f"{row['slug']}: breadcrumb says {crumb!r} but the product is in {cats} — "
                        "the specimen's category froze into the part", "breadcrumb")
                elif crumb:
                    ok(f"{row['slug']}: breadcrumb names its own category ({crumb})", "breadcrumb")
                facts.append(spec)
            if spec_frozen(facts, picks):
                gap(f"two different products state an identical spec line ({facts[0][:48]!r}) — "
                    "the specimen's value froze into the part", "spec-line")
            elif facts[0] or facts[1]:
                ok("each product states its own spec line", "spec-line")

        # ---- 6. search finds the shop ----
        word = ""
        if by["simple"] or by["variable"]:
            name = (by["simple"] or by["variable"])["name"]
            words = [w for w in re.findall(r"[A-Za-zÀ-ſ]{5,}", name)]
            word = (words or [name.split()[0]])[0]
        if word:
            page.goto(f"{BASE}/?s={word}", wait_until="networkidle")
            page.wait_for_timeout(1500)
            if page.query_selector("main a[href*='/product/']"):
                ok(f"search '{word}' surfaces products", "search")
            else:
                gap(f"search '{word}' returns no products — the catalogue is invisible to search", "search")

        # ---- 7. account, reviews, cart, checkout ----
        page.goto(f"{BASE}/my-account/", wait_until="networkidle")
        page.wait_for_timeout(1500)
        el = page.query_selector("main form, main .woocommerce")
        left = el.evaluate("e => Math.round(e.getBoundingClientRect().left)") if el else -1
        if left > 0:
            ok(f"my-account renders in the design's container ({left}px gutter)", "my-account")
        elif left == 0:
            gap("my-account renders at the window's edge — no container template", "my-account")
        else:
            gap("my-account did not render a login form", "my-account")

        anyrow = by["simple"] or by["variable"]
        reviews_on = wpcli("option get woocommerce_enable_reviews") if args.wp_cli else "yes"
        if anyrow and reviews_on != "no":
            # A design without reviews shows none until a product HAS one, so
            # the check is that an approved review appears once it exists —
            # and that a product without one shows exactly the design.
            page.goto(product_url(anyrow), wait_until="networkidle")
            page.wait_for_timeout(1200)
            before = bool(page.query_selector(REVIEWS_SEL))
            after = None
            if args.wp_cli and anyrow.get("id"):
                cid = wpcli(f"comment create --comment_post_ID={int(anyrow['id'])} --comment_type=review "
                            "--comment_approved=1 --comment_author='Coverage Audit' "
                            "--comment_author_email=audit@coverage.test "
                            "--comment_content='Audit review: soft and warm.' --porcelain")
                if cid:
                    wpcli(f"comment meta add {int(cid)} rating 5")
                    wpcli(f"eval 'wc_delete_product_transients({int(anyrow['id'])}); "
                          f"WC_Comments::clear_transients({int(anyrow['id'])});'")
                    page.goto(product_url(anyrow), wait_until="networkidle")
                    page.wait_for_timeout(1200)
                    box = page.query_selector(REVIEWS_SEL)
                    after = bool(box) and "Audit review: soft and warm." in box.inner_text()
                    wpcli(f"comment delete {int(cid)} --force")
                    wpcli(f"eval 'WC_Comments::clear_transients({int(anyrow['id'])});'")
            verdict = html_reviews_verdict(before, after)
            if verdict == "ok":
                ok("an approved review shows on its product page" if after else "a product page offers reviews", "reviews")
            elif verdict == "unverified":
                note("reviews: none render yet (the design has none) — pass --wp-cli to prove an approved one shows")
            else:
                gap(verdict, "reviews")

        page.goto(f"{BASE}/cart/", wait_until="networkidle")
        page.wait_for_timeout(2500)
        cart_text = page.inner_text("body").lower()
        if "coupon" in cart_text:
            ok("the cart offers a coupon field", "cart-coupon")
        else:
            gap("no coupon field in the cart", "cart-coupon")

        page.goto(f"{BASE}/checkout/", wait_until="networkidle")
        page.wait_for_timeout(2500)
        if page.query_selector("#email") or "checkout" in page.inner_text("body").lower():
            ok("the checkout renders", "checkout")
        else:
            gap("the checkout did not render", "checkout")

        # ---- 8. with wp-cli: a coupon discounts, an order completes ----
        if args.wp_cli:
            note("wp-cli provided — exercising a real discount and a real order")
            coupon = "AUDIT-COVERAGE"
            cid = wpcli(f"wc --user=admin shop_coupon create --code={coupon} "
                        "--discount_type=percent --amount=10 --porcelain")
            if cid:
                cleanup.append(f"post delete {cid} --force")
            cod_prior, cod_absent = enable_cod(wpcli)
            if by["simple"] or by["variable"]:
                add_to_cart(page, by["simple"] or by["variable"])
                page.goto(f"{BASE}/cart/", wait_until="networkidle")
                page.wait_for_timeout(2500)
                toggle = page.query_selector("text=Add coupons")
                if toggle and cid:
                    toggle.click()
                    page.wait_for_timeout(600)
                    page.fill(".wc-block-components-totals-coupon__input input", coupon)
                    page.click(".wc-block-components-totals-coupon__button")
                    page.wait_for_timeout(2500)
                    if "discount" in page.inner_text(".wc-block-components-sidebar").lower():
                        ok("the coupon discounts a real basket", "coupon")
                    else:
                        gap("the coupon did not apply", "coupon")
                page.goto(f"{BASE}/checkout/", wait_until="networkidle")
                page.wait_for_timeout(2500)
                try:
                    place_block_order(page)
                    if "order-received" in page.url:
                        ok(f"a cash-on-delivery order completed end to end ({page.url.split('/order-received/')[1].split('/')[0]})", "order")
                        oid = page.url.split("/order-received/")[1].split("/")[0]
                        cleanup.append(f"wc --user=admin shop_order delete {oid} --force=true")
                    else:
                        gap(f"placing the order did not reach the thank-you page ({page.url})", "order")
                except Exception as e:  # noqa: BLE001
                    gap(f"the checkout flow broke: {str(e)[:100]}", "order")
            # restore what was touched
            for c in cleanup:
                wpcli(c)
            restore_cod(wpcli, cod_prior, cod_absent)
            note("fixtures removed, payment settings restored")

        if errors:
            gap(f"{len(errors)} JS error(s) while shopping — first: {errors[0][:90]}", "js-errors")
        else:
            ok("no JS errors anywhere on the walk", "js-errors")
        b.close()


# ------------------------------------------------------ the Gutenberg walk
#
# A block theme sells through WooCommerce's own blocks: the catalogue is a
# product collection, the product page an add-to-cart-form block, the cart and
# checkout the native block pages. Native Woo does not preselect a variant, so
# "a variant is chosen on load" (the HTML theme's own chooser) becomes: the
# selects offer only the product's values, choosing a real variation resolves
# it, and that variation lands in the basket. Every other assertion is the
# HTML walk's, plus the ones only a native render lets us state exactly: every
# product listed with its price, every product page complete, stock moved by
# exactly what was bought, and nothing the audit created left behind.
#
# Nothing here knows the site. Products, prices, images, stock and categories
# come from the Store API; the shop, cart, checkout and account pages from
# WooCommerce's own settings (wp-cli) or, without wp-cli, from which published
# page renders which WooCommerce block; the administrator from the install.
# Markup is found by WooCommerce's block classes, never by copy — a shop in
# any language reads the same.

def squash(text):
    """Compare prices and names without caring about entities or spacing."""
    return re.sub(r"\s+", "", html.unescape(str(text or "")).replace("\xa0", " "))


def money(row, field="price"):
    """A Store API amount in the shop's own format (prefix, separators, suffix)."""
    p = row.get("prices") or {}
    minor = int(p.get("currency_minor_unit", 2))
    raw = int(p.get(field) or 0)
    whole, frac = divmod(abs(raw), 10 ** minor)
    grouped = f"{whole:,}".replace(",", p.get("currency_thousand_separator", ","))
    num = grouped + (p.get("currency_decimal_separator", ".") + str(frac).zfill(minor) if minor else "")
    return f"{p.get('currency_prefix', '')}{num}{p.get('currency_suffix', '')}"


def own_text(row):
    """A product's own description and short description, as squashed text."""
    raw = html.unescape(f"{row.get('description') or ''} {row.get('short_description') or ''}")
    return squash(re.sub(r"<[^>]+>", " ", raw))


def spec_frozen(specs, rows):
    """Two products printing the same spec line is the specimen's value frozen
    into the template, unless each product's own description states that line
    (two jumpers can really share a yarn)."""
    if len(specs) < 2 or not specs[0] or specs[0] != specs[1]:
        return False
    return not all(squash(specs[0]) in own_text(r) for r in rows)


def reviews_verdict(in_template, shown_before, shown_after):
    """'ok' when reviews work on a product page, else the GAP text.

    in_template: the product template places a reviews block (None = unknown,
    no wp-cli); shown_before: reviews render with the product as it is;
    shown_after: an approved test review rendered (None = not exercised)."""
    if in_template is False:
        return "reviews are enabled in Woo but the product template has no reviews block"
    if shown_after is True or (shown_after is None and shown_before):
        return "ok"
    if shown_after is None:
        return "reviews are enabled in Woo but rendered nowhere"
    return "an approved review is not shown on its product page"


def soldout_ids(row):
    """The product and every variation of it — what a basket line can carry."""
    return {row.get("id")} | {v.get("id") for v in row.get("variations") or [] if isinstance(v, dict)}


def button_is_live(disabled_attr, class_attr):
    """Woo marks an unavailable submit with the `disabled` CLASS as often as
    the attribute; either one means a click is refused."""
    return not disabled_attr and "disabled" not in str(class_attr or "").split()


def soldout_verdict(live, held_before, held_after):
    """'bought' when the basket gained the sold-out product, otherwise why not."""
    if held_after > held_before:
        return "bought"
    return "refused at add to cart" if live else "no live buy control"


def has_price(row):
    return str((row.get("prices") or {}).get("price") or "") != ""


def norm_url(url):
    return str(url or "").split("#")[0].rstrip("/")


def gb_subjects(rows):
    """The test subjects, by WooCommerce product type rather than by
    has_options (which is also true of simple products with add-ons)."""
    buyable = [r for r in rows if r.get("is_in_stock") and r.get("is_purchasable")]
    return {
        "variable": next((r for r in buyable if r.get("type") == "variable"), None),
        "simple": next((r for r in buyable if r.get("type") == "simple"), None),
        "soldout": next((r for r in rows if not r.get("is_in_stock")), None),
        # WooCommerce strikes a variable product's price only per variation,
        # so a simple product on sale is the plainer subject when there is one.
        "onsale": next((r for r in buyable if r.get("on_sale") and r.get("type") != "variable"),
                       next((r for r in buyable if r.get("on_sale")), None)),
    }


def listing_gaps(rows, cards):
    """cards: product id -> the text of its card on the catalogue. Returns the
    slugs not linked at all, and the slugs linked without their price."""
    missing = [r["slug"] for r in rows if r.get("permalink") and r["id"] not in cards]
    unpriced = [r["slug"] for r in rows if r["id"] in cards and has_price(r)
                and squash(money(r)) not in squash(cards[r["id"]])]
    return missing, unpriced


def product_page_lacks(row, seen):
    """seen: what the product page rendered — `titles` (h1/post-title texts),
    `price` (the price block's text), `gallery` (bool), `form` (bool).
    A product with no image may show WooCommerce's placeholder or nothing; a
    product with no price or not for sale needs no price or form."""
    lacks = []
    if squash(row.get("name")) not in {squash(t) for t in seen.get("titles", [])}:
        lacks.append("title")
    if has_price(row) and squash(money(row)) not in squash(seen.get("price")):
        lacks.append("price")
    if row.get("images") and not seen.get("gallery"):
        lacks.append("gallery")
    if row.get("is_in_stock") and row.get("is_purchasable") and not seen.get("form"):
        lacks.append("add-to-cart")
    return lacks


def declared_values(row):
    out = set()
    for a in row.get("attributes", []):
        for t in a.get("terms", []):
            out.update({str(t.get("name", "")).lower(), str(t.get("slug", "")).lower()})
    for v in row.get("variations", []):
        for a in v.get("attributes", []):
            out.add(str(a.get("value", "")).lower())
    out.discard("")
    return out


def variation_choices(row, selects):
    """selects: [{"name": "attribute_pa_size", "options": ["s", "m"]}].
    One {select name: option} per real variation of the product, in the
    Store API's order, matching each variation's values to the select that
    offers them (case-insensitively — taxonomy terms are slugs, local
    attributes their names). An "any" value takes the select's first option."""
    choices = []
    for v in row.get("variations", []):
        values = [str(a.get("value", "")).lower() for a in v.get("attributes", [])]
        pick = {}
        for s in selects:
            opts = [o for o in s.get("options", []) if o]
            hit = next((o for o in opts if o.lower() in values), None)
            pick[s["name"]] = hit if hit is not None else (opts[0] if opts else "")
        if pick and pick not in choices:
            choices.append(pick)
    if not choices and selects:
        choices.append({s["name"]: next((o for o in s.get("options", []) if o), "") for s in selects})
    return choices


def pages_from_rest(pages):
    """Published pages -> the WooCommerce page each one renders, told apart by
    the block or form WooCommerce renders into it (anonymous REST content)."""
    marks = {
        "cart": ("wp-block-woocommerce-cart", "woocommerce-cart-form"),
        "checkout": ("wp-block-woocommerce-checkout", "woocommerce-checkout"),
        "myaccount": ("woocommerce-form-login", "woocommerce-MyAccount", "wp-block-woocommerce-customer-account"),
    }
    found = {}
    for p in pages if isinstance(pages, list) else []:
        body = ((p.get("content") or {}).get("rendered") or "") if isinstance(p, dict) else ""
        for name, needles in marks.items():
            if name not in found and any(n in body for n in needles) and p.get("link"):
                found[name] = p["link"]
    return found


def store_pages(settings):
    """WooCommerce's own page settings as every page with a Woo block publishes
    them to the browser (`wcSettings.storePages`) — no wp-cli needed."""
    out = {}
    for name in ("shop", "cart", "checkout", "myaccount"):
        entry = (settings or {}).get(name) if isinstance(settings, dict) else None
        if isinstance(entry, dict) and entry.get("id") and isinstance(entry.get("permalink"), str):
            out[name] = entry["permalink"]
    return out


def resolve_pages(wc_urls, published, rest_pages, base):
    """shop, cart, checkout, account URLs: WooCommerce's settings (wp-cli, else
    what WooCommerce publishes to the page), then the page that renders the
    block. A shop page that is the front page is simply the home URL; with no
    shop page at all WooCommerce still serves the archive at ?post_type=product."""
    def pick(name):
        return wc_urls.get(name) or published.get(name) or rest_pages.get(name)
    return (pick("shop") or f"{base}/?post_type=product", pick("cart"), pick("checkout"), pick("myaccount"))


def order_id_from_url(url):
    """/…/order-received/123/?key=… with pretty permalinks, ?order-received=123 without."""
    m = re.search(r"order-received(?:/|=)(\d+)", str(url or ""))
    return m.group(1) if m else None


def breadcrumb_verdict(row, crumb_hrefs):
    """None when the breadcrumb links no category; else (ok, detail). A crumb
    that links another category the product is not in is the frozen-specimen
    defect."""
    own = {norm_url(c.get("link")) for c in row.get("categories", []) if c.get("link")}
    linked = [norm_url(h) for h in crumb_hrefs]
    if not own:
        return None
    mine = [h for h in linked if h in own]
    if mine:
        return True, mine[0]
    others = [h for h in linked if "/" in h and h not in own and h.rstrip("/") != norm_url(BASE)]
    return (False, others[0]) if others else None


def all_products():
    """Every product, not only the first hundred."""
    rows, n = [], 1
    while n <= 50:
        chunk = fetch_json(f"{BASE}/wp-json/wc/store/v1/products?per_page=100&page={n}")
        if not chunk:
            break
        rows += chunk
        if len(chunk) < 100:
            break
        n += 1
    return rows


def run_gutenberg(rows, _by):
    from playwright.sync_api import sync_playwright

    rows = all_products() or rows
    by = gb_subjects(rows)
    admin = ""
    if args.wp_cli:
        admin = (wpcli("user list --role=administrator --field=ID --number=1") or "").split("\n")[0].strip()
    as_admin = f"--user={admin}" if admin.isdigit() else "--user=admin"

    def wc_page(name):
        if not args.wp_cli:
            return None
        pid = wpcli(f"option get woocommerce_{name}_page_id")
        if pid and pid.isdigit() and int(pid) > 0 and wpcli(f"post get {int(pid)} --field=post_status") == "publish":
            url = wpcli(f"eval 'echo get_permalink({int(pid)});'")
            if url and url.startswith("http"):
                return url
        return None

    published_pages = fetch_json(f"{BASE}/wp-json/wp/v2/pages?per_page=100&_fields=link,content") or []
    rest_pages = pages_from_rest(published_pages)
    woo_block_pages = [p["link"] for p in published_pages if isinstance(p, dict) and p.get("link")
                       and "wp-block-woocommerce-" in ((p.get("content") or {}).get("rendered") or "")]
    wc_urls = {name: wc_page(name) for name in ("shop", "cart", "checkout", "myaccount")}
    permalinks = {r["id"]: norm_url(r["permalink"]) for r in rows if r.get("permalink")}

    def stock_of(pid):
        out = wpcli(f"eval 'echo wp_json_encode(wc_get_product({int(pid)}) ? "
                    f"wc_get_product({int(pid)})->get_stock_quantity() : false);'")
        try:
            return json.loads(out) if out is not None else None
        except ValueError:
            return None

    cleanup = []
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        ctx = b.new_context(viewport={"width": 1440, "height": 1200})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        def cart_items():
            r = ctx.request.get(f"{BASE}/wp-json/wc/store/v1/cart")
            try:
                return r.json().get("items", []) if r.ok else None
            except Exception:  # noqa: BLE001
                return None

        def go(url, settle=1200):
            page.goto(url, wait_until="networkidle")
            page.wait_for_timeout(settle)

        def root():
            """The page's content region: <main> when the theme has one, else the body."""
            return "main" if page.query_selector("main") else "body"

        def text_of(selector):
            el = page.query_selector(selector)
            return el.inner_text() if el else ""

        def click_add():
            btn = page.query_selector("form.cart button.single_add_to_cart_button") \
                or page.query_selector("form.cart [type=submit]")
            if not btn:
                return None
            btn.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_timeout(1200)
            return page.query_selector(".woocommerce-error, .wc-block-components-notice-banner.is-error") is None

        published = {}
        for link in ([] if all(wc_urls.values()) else woo_block_pages[:3]):
            go(link, 300)
            published = store_pages(page.evaluate("() => window.wcSettings && wcSettings.storePages"))
            if published:
                break
        shop_url, cart_url, checkout_url, account_url = resolve_pages(wc_urls, published, rest_pages, BASE)
        note(f"pages: shop={shop_url} cart={cart_url} checkout={checkout_url} account={account_url}")

        # ---- 1. the catalogue page lists every product, with link and price ----
        cards, seen_urls, url = {}, set(), shop_url
        for _ in range(60):  # follow the collection's pagination
            if url in seen_urls:
                break
            seen_urls.add(url)
            go(url)
            found = page.evaluate(
                """([scope, links]) => { const out = {};
                     const anchors = [...document.querySelectorAll(scope + ' a[href]')];
                     for (const [id, link] of links) {
                       const a = anchors.find(x => x.href.split('#')[0].replace(/\\/$/, '') === link);
                       if (!a) continue;
                       const c = a.closest('li, .wc-block-product, .wp-block-post, .product') || a.parentElement;
                       out[id] = c.innerText; }
                     return out; }""",
                [root(), [[str(k), v] for k, v in permalinks.items() if k not in cards]])
            cards.update({int(k): v for k, v in found.items()})
            nxt = page.query_selector(".wp-block-query-pagination-next[href], a.next.page-numbers[href]")
            if not nxt or len(cards) == len(permalinks):
                break
            url = urljoin(page.url, nxt.get_attribute("href"))
        missing, unpriced = listing_gaps(rows, cards)
        if missing:
            gap(f"the catalogue page ({shop_url}) does not link {len(missing)} product(s): {missing[:6]}",
                "catalog-listing")
        elif unpriced:
            gap(f"the catalogue lists these without their price: {unpriced[:6]}", "catalog-listing")
        else:
            ok(f"the catalogue lists all {len(cards)} product(s) with link and price ({shop_url})",
               "catalog-listing")

        # ---- 2. every product page renders its Woo blocks ----
        broken, sample = [], rows[:40]
        for row in sample:
            go(row["permalink"], 700)
            scope = root()
            seen = {
                "titles": page.eval_on_selector_all(f"{scope} h1, {scope} .wp-block-post-title",
                                                    "es => es.map(e => e.innerText)"),
                "price": text_of(".wp-block-woocommerce-product-price") or text_of(f"{scope} .price"),
                "gallery": bool(page.query_selector(".wp-block-woocommerce-product-image-gallery img, "
                                                    ".woocommerce-product-gallery img, "
                                                    ".wp-block-woocommerce-product-gallery img")),
                "form": bool(page.query_selector("form.cart")),
            }
            lacks = product_page_lacks(row, seen)
            if lacks:
                broken.append(f"{row['slug']} ({', '.join(lacks)})")
        if broken:
            gap(f"product pages missing Woo blocks: {broken[:6]}", "single-product")
        else:
            ok(f"all {len(sample)} product page(s) render title, price, gallery and add-to-cart "
               "(as each product has them)", "single-product")

        # ---- 3. a variable product sells the variant chosen ----
        if by["variable"]:
            row = by["variable"]
            go(row["permalink"])
            sel = "form.variations_form select[name^='attribute_']"
            selects = page.eval_on_selector_all(
                sel, "es => es.map(e => ({name: e.name, options: [...e.options].map(o => o.value)}))")
            if not selects:
                gap(f"{row['slug']}: variable product carries no variation form", "variable-form")
            else:
                declared = declared_values(row)
                offered = {o for s in selects for o in s["options"] if o}
                extra = [o for o in offered if declared and o.lower() not in declared]
                if extra:
                    gap(f"the chooser offers values the product does not sell: {extra}", "variant-values")
                else:
                    ok(f"the chooser offers only this product's values ({sorted(offered)})", "variant-values")
                vid, landed = "", False
                for choice in variation_choices(row, selects)[:6]:
                    go(row["permalink"], 800)
                    for name, value in choice.items():
                        if value:
                            page.select_option(f"form.variations_form select[name='{name}']", value)
                            page.wait_for_timeout(500)
                    page.wait_for_timeout(700)
                    vid = page.eval_on_selector("form.variations_form input[name=variation_id]", "e => e.value") \
                        if page.query_selector("form.variations_form input[name=variation_id]") else ""
                    live = page.query_selector("form.variations_form .single_add_to_cart_button"
                                               ":not(.disabled):not([disabled])")
                    if vid not in ("", "0") and live:
                        landed = bool(click_add()) and any(str(i.get("id")) == vid for i in cart_items() or [])
                        break
                if vid in ("", "0"):
                    gap(f"{row['slug']}: choosing a variation's values resolved no variation", "variant-chosen")
                else:
                    ok(f"{row['slug']}: choosing a variation's values resolves variation {vid}", "variant-chosen")
                    if landed:
                        ok(f"variation {vid} landed in the basket", "variable-add")
                    else:
                        gap(f"{row['slug']}: no chosen variation reached the basket", "variable-add")

        # ---- 4. simple product ----
        if by["simple"]:
            row = by["simple"]
            before = sum(i.get("quantity", 0) for i in (cart_items() or []) if i.get("id") == row["id"])
            go(row["permalink"])
            accepted = click_add()
            after = sum(i.get("quantity", 0) for i in (cart_items() or []) if i.get("id") == row["id"])
            if accepted and after > before:
                ok(f"{row['slug']}: simple product reaches the basket", "simple-add")
            else:
                gap(f"{row['slug']}: simple product could not be bought", "simple-add")

        # ---- 5. sold out cannot be bought ----
        # Native Woo renders a sold-out VARIABLE product's form with an
        # enabled-looking submit (its script only adds the `disabled` class
        # once a value is chosen), so the markup alone cannot answer this: try
        # to buy it and ask the basket.
        if by["soldout"]:
            row = by["soldout"]
            ids = soldout_ids(row)
            held = lambda: sum(i.get("quantity", 0) for i in (cart_items() or []) if i.get("id") in ids)
            before = held()
            go(row["permalink"], 1000)
            for s in page.query_selector_all("form.variations_form select[name^='attribute_']"):
                first = s.evaluate("e => ([...e.options].find(o => o.value) || {}).value || ''")
                if first:
                    s.select_option(first)
                    page.wait_for_timeout(400)
            btn = page.query_selector("form.cart button.single_add_to_cart_button") \
                or page.query_selector("form.cart [type=submit]")
            live = btn is not None and button_is_live(btn.is_disabled(), btn.get_attribute("class"))
            if live:
                btn.click()
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(1200)
            verdict = soldout_verdict(live, before, held())
            if verdict == "bought":
                gap(f"{row['slug']}: sold out but it reached the basket", "soldout")
            else:
                ok(f"{row['slug']}: sold out and not buyable ({verdict})", "soldout")

        # ---- 6. a promotion shows what it saves ----
        if by["onsale"]:
            row = by["onsale"]
            sale, regular = money(row, "price"), money(row, "regular_price")
            go(row["permalink"], 1000)
            if row.get("type") == "variable":
                # A range carries no struck price in WooCommerce; the saving
                # shows once the discounted variation is chosen.
                selects = page.eval_on_selector_all(
                    "form.variations_form select[name^='attribute_']",
                    "es => es.map(e => ({name: e.name, options: [...e.options].map(o => o.value)}))")
                struck = ""
                for choice in variation_choices(row, selects):
                    for name, value in choice.items():
                        if value:
                            page.select_option(f"form.variations_form select[name='{name}']", value)
                    page.wait_for_timeout(700)
                    struck = text_of(".woocommerce-variation-price del")
                    if struck:
                        break
                if struck:
                    ok(f"{row['slug']}: the discounted variation shows its struck price ({struck.strip()})",
                       "sale-single")
                else:
                    gap(f"{row['slug']}: no variation shows a struck original price", "sale-single")
            else:
                body = squash(page.inner_text(root()))
                if squash(sale) in body and squash(regular) in body:
                    ok(f"{row['slug']}: sale shows both prices ({sale}, was {regular})", "sale-single")
                else:
                    gap(f"{row['slug']}: sale page shows only {sale!r} — the struck {regular!r} is missing",
                        "sale-single")
            card = cards.get(row["id"]) if row.get("type") != "variable" else None
            if card is not None:
                if squash(regular) in squash(card):
                    ok("the listing card carries the struck original too", "sale-listing")
                else:
                    gap(f"the listing card shows only the current price — no {regular!r}", "sale-listing")
        else:
            note("no product on sale — the sale-presentation checks were NOT exercised; "
                 "put one product on sale and re-run before handover")

        # ---- 7. each product states its own facts ----
        picks = [r for r in [by["variable"], by["simple"], by["soldout"]] if r][:2]
        if len(picks) == 2:
            facts = []
            for row in picks:
                go(row["permalink"], 900)
                scope = root()
                hrefs = page.eval_on_selector_all(
                    ".wp-block-woocommerce-breadcrumbs a[href], .woocommerce-breadcrumb a[href]",
                    "es => es.map(e => e.href)")
                verdict = breadcrumb_verdict(row, hrefs)
                if verdict and not verdict[0]:
                    gap(f"{row['slug']}: breadcrumb links {verdict[1]} but the product is in "
                        f"{[c.get('name') for c in row.get('categories', [])]}", "breadcrumb")
                elif verdict:
                    ok(f"{row['slug']}: breadcrumb names its own category ({verdict[1]})", "breadcrumb")
                facts.append(page.evaluate(
                    "scope => { const m = [...document.querySelectorAll(scope + ' p')]"
                    ".find(x => /^[A-Za-z\\u00C0-\\u017F ]{2,24}:/.test(x.innerText)); "
                    "return m ? m.innerText : ''; }", scope))
            if spec_frozen(facts, picks):
                gap(f"two different products state an identical spec line ({facts[0][:48]!r})", "spec-line")
            elif facts[0] or facts[1]:
                ok("each product states its own spec line", "spec-line")

        # ---- 8. search finds the shop ----
        anyrow = by["simple"] or by["variable"]
        if anyrow:
            name = html.unescape(anyrow["name"])
            words = re.findall(r"[^\W\d_]{5,}", name)
            word = (words or [name.split()[0]])[0]
            page.goto(f"{BASE}/?{urlencode({'s': word})}", wait_until="networkidle")
            page.wait_for_timeout(1500)
            hrefs = page.eval_on_selector_all(f"{root()} a[href]",
                                              "es => es.map(e => e.href.split('#')[0].replace(/\\/$/, ''))")
            if set(hrefs) & set(permalinks.values()):
                ok(f"search '{word}' surfaces products", "search")
            else:
                gap(f"search '{word}' returns no products — the catalogue is invisible to search", "search")

        # ---- 9. account, reviews, native cart and checkout ----
        if account_url:
            go(account_url, 1500)
            scope = root()
            el = page.query_selector(f"{scope} form, {scope} .woocommerce")
            left = el.evaluate("e => Math.round(e.getBoundingClientRect().left)") if el else -1
            if left > 0:
                ok(f"my-account renders in the design's container ({left}px gutter)", "my-account")
            elif left == 0:
                gap("my-account renders at the window's edge — no container template", "my-account")
            else:
                gap(f"my-account did not render a login form ({account_url})", "my-account")
        else:
            gap("no published page renders the WooCommerce account form", "my-account")

        # A block theme may show its reviews block only once a product has a
        # review (a design without one stays as drawn), so "reviews work" is:
        # enabled in WooCommerce, placed in the product template, and a real
        # approved review rendered on the page (then deleted again).
        reviews_on = wpcli("option get woocommerce_enable_reviews") if args.wp_cli else "yes"
        if anyrow and reviews_on != "no":
            REVIEWS = ("#reviews, .woocommerce-Reviews, .wp-block-woocommerce-product-reviews, "
                       ".wp-block-woocommerce-product-details")
            in_template = None
            if args.wp_cli:
                found = wpcli("eval \"$t = get_block_template(get_stylesheet() . '//single-product'); echo $t ? (int) (strpos($t->content, 'wp:woocommerce/product-reviews') !== false || strpos($t->content, 'wp:woocommerce/product-details') !== false) : 2;\"")
                in_template = found == "1" if found in ("0", "1") else None
            go(anyrow["permalink"])
            before = page.query_selector(REVIEWS) is not None
            after, review = None, None
            if args.wp_cli:
                marker = "Woo audit review " + str(anyrow["id"])
                cid = wpcli(f"comment create --comment_post_ID={int(anyrow['id'])} --comment_type=review --comment_approved=1 "
                            f"--comment_author=Audit --comment_author_email=audit@example.com --comment_content={shlex.quote(marker)} --porcelain")
                if cid and cid.isdigit():
                    cleanup.append(f"comment delete {cid} --force")
                    go(anyrow["permalink"])
                    block = page.query_selector(REVIEWS)
                    after = bool(block and marker in (block.inner_text() or ""))
                    wpcli(f"comment delete {cid} --force")
            verdict = reviews_verdict(in_template, before, after)
            if verdict == "ok":
                ok("a product page offers reviews" + (" (rendered once a review exists)" if after and not before else ""), "reviews")
            else:
                gap(verdict, "reviews")

        if cart_url:
            go(cart_url, 2500)
            if page.query_selector(".wp-block-woocommerce-cart"):
                ok(f"the cart is WooCommerce's native cart block ({cart_url})", "cart-native")
            else:
                gap(f"the cart page is not the native cart block ({cart_url})", "cart-native")
            if page.query_selector(".wc-block-components-totals-coupon, .wp-block-woocommerce-cart-order-summary-coupon-form-block"):
                ok("the cart offers a coupon field", "cart-coupon")
            else:
                gap("no coupon field in the cart", "cart-coupon")
        else:
            gap("WooCommerce has no published cart page", "cart-native")

        if checkout_url:
            go(checkout_url, 2500)
            if page.query_selector(".wp-block-woocommerce-checkout"):
                ok(f"the checkout is WooCommerce's native checkout block ({checkout_url})", "checkout")
            else:
                gap(f"the checkout page is not the native checkout block ({checkout_url})", "checkout")
        else:
            gap("WooCommerce has no published checkout page", "checkout")

        # ---- 10. with wp-cli: coupon, order, stock, and nothing left behind ----
        if args.wp_cli and cart_url and checkout_url:
            note("wp-cli provided — exercising a real discount and a real order")
            coupon = "audit-coverage-" + os.urandom(3).hex()
            cid = wpcli(f"wc {as_admin} shop_coupon create --code={coupon} "
                        "--discount_type=percent --amount=10 --porcelain")
            if cid:
                cleanup.append(f"post delete {cid} --force")
            else:
                gap("could not create the throwaway coupon", "coupon")
            cod_prior, cod_absent = enable_cod(wpcli)
            oid, stock_before, bought = None, {}, {}
            if anyrow:
                if not cart_items():
                    go(anyrow["permalink"])
                    click_add()
                go(cart_url, 2500)
                toggle = page.query_selector(".wc-block-components-totals-coupon .wc-block-components-panel__button, "
                                             ".wc-block-components-totals-coupon-link")
                if toggle and cid:
                    if toggle.get_attribute("aria-expanded") != "true":
                        toggle.click()
                        page.wait_for_timeout(600)
                    page.fill(".wc-block-components-totals-coupon__input input", coupon)
                    page.click(".wc-block-components-totals-coupon__button")
                    page.wait_for_timeout(2500)
                    if page.query_selector(".wc-block-components-totals-discount"):
                        ok("the coupon discounts a real basket", "coupon")
                    else:
                        gap("the coupon did not apply", "coupon")
                elif cid:
                    gap("the cart offers no way to enter the coupon", "coupon")
                for i in cart_items() or []:
                    bought[i["id"]] = bought.get(i["id"], 0) + int(i.get("quantity", 0))
                stock_before = {pid: stock_of(pid) for pid in bought}
                go(checkout_url, 2500)
                try:
                    place_block_order(page)
                    oid = order_id_from_url(page.url)
                    if oid:
                        ok(f"a cash-on-delivery order completed end to end ({oid})", "order")
                        # A bare --force reaches the REST CLI as "needs a value"
                        # and the order only goes to the trash.
                        cleanup.append(f"wc {as_admin} shop_order delete {oid} --force=true")
                    else:
                        notice = text_of(".wc-block-components-notice-banner, .woocommerce-error")
                        gap(f"placing the order did not reach the thank-you page ({page.url})"
                            + (f" — {notice.strip()[:120]!r}" if notice else ""), "order")
                except Exception as e:  # noqa: BLE001
                    gap(f"the checkout flow broke: {str(e)[:100]}", "order")
            managed = {pid: q for pid, q in stock_before.items() if isinstance(q, int)}
            if oid and managed:
                moved = {pid: (q, stock_of(pid)) for pid, q in managed.items()}
                wrong = {pid: v for pid, v in moved.items() if v[1] != v[0] - bought[pid]}
                if wrong:
                    gap(f"stock did not go down by what was bought: {wrong}", "stock")
                else:
                    ok(f"stock went down by exactly what was bought ({sum(bought[p] for p in managed)} unit(s))",
                       "stock")
            elif oid:
                note("nothing bought manages stock — the stock decrement was NOT exercised")
            # restore what was touched: the order, the coupon, the stock, COD
            for c in cleanup:
                wpcli(c)
            if oid:
                for pid, q in managed.items():
                    wpcli(f"eval 'wc_update_product_stock({int(pid)}, {int(q)}, \"set\");'")
            restore_cod(wpcli, cod_prior, cod_absent)
            left_over = []
            if cid and wpcli(f"post get {int(cid)} --field=ID"):
                left_over.append(f"coupon {cid}")
            if oid and wpcli(f"eval 'echo wc_get_order({int(oid)}) ? 1 : 0;'") != "0":
                left_over.append(f"order {oid}")
            if oid and any(stock_of(pid) != q for pid, q in managed.items()):
                left_over.append("stock")
            if left_over:
                gap(f"the audit's fixtures were not all removed: {left_over}", "cleanup")
            else:
                ok("coupon, order and stock put back; payment settings restored", "cleanup")

        if errors:
            gap(f"{len(errors)} JS error(s) while shopping — first: {errors[0][:90]}", "js-errors")
        else:
            ok("no JS errors anywhere on the walk", "js-errors")
        b.close()


def main(argv=None):
    global args, BASE, REPORT_PATH, TARGET
    args = parse_args(argv)
    BASE = args.wp.rstrip("/")
    manifest = None
    if args.workspace:
        try:
            with open(os.path.join(args.workspace, "conversion-manifest.json")) as f:
                manifest = json.load(f)
        except Exception:  # noqa: BLE001
            manifest = None
    REPORT_PATH = args.report or (os.path.join(args.workspace, "woo-coverage", "report.json")
                                  if args.workspace else "")
    TARGET = detect_target(args.target, manifest, probe_site)
    rows, by = discover()
    if TARGET == "gutenberg":
        note("target: native Gutenberg block theme — auditing WooCommerce's blocks")
        run_gutenberg(rows, by)
    elif args.probe_cart:
        run_probe(by)
    else:
        run_html(by)
    finish()


if __name__ == "__main__":
    main()
