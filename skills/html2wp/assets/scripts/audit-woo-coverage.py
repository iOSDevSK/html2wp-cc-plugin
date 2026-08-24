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
"""

import argparse
import json
import re
import shlex
import subprocess
import sys
import urllib.request
from urllib.parse import urljoin

ap = argparse.ArgumentParser()
ap.add_argument("--wp", required=True, help="site root")
ap.add_argument("--wp-cli", default="", help="command prefix that runs wp-cli on the install "
                "(enables the coupon + end-to-end order checks, with cleanup)")
args = ap.parse_args()
BASE = args.wp.rstrip("/")

gaps = 0


def ok(msg):
    print(f"ok   {msg}")


def gap(msg):
    global gaps
    gaps += 1
    print(f"GAP  {msg}")


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

rows = fetch_json(f"{BASE}/wp-json/wc/store/v1/products?per_page=100") or []
if not rows:
    gap("Store API unreachable — is WooCommerce active?")
    sys.exit(1)

by = {
    "variable": next((r for r in rows if r.get("has_options") and r.get("is_in_stock")), None),
    "simple": next((r for r in rows if not r.get("has_options") and r.get("is_in_stock")
                    and r.get("is_purchasable")), None),
    "soldout": next((r for r in rows if not r.get("is_in_stock")), None),
    "onsale": next((r for r in rows if r.get("on_sale") and r.get("is_in_stock")), None),
}
note(f"catalogue: {len(rows)} product(s); variable={bool(by['variable'])} simple={bool(by['simple'])} "
     f"soldout={bool(by['soldout'])} onsale={bool(by['onsale'])}")


def price_of(row):
    p = row.get("prices") or {}
    minor = int(p.get("currency_minor_unit", 2))
    cur = p.get("currency_symbol", "$")
    def fmt(v):
        return f"{cur}{int(v) / (10 ** minor):,.2f}"
    return fmt(p.get("price", 0)), fmt(p.get("regular_price", 0))


def main():
    from playwright.sync_api import sync_playwright

    cleanup = []
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        ctx = b.new_context(viewport={"width": 1440, "height": 1200})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

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
                    ok(f"{row['slug']}: a variant is chosen on load and resolved ({pre})")
                else:
                    gap(f"{row['slug']}: nothing chosen on load (value={pre!r}, variation_id={vid}) — "
                        "the first click on Add to cart will be refused")
                accepted = add_to_cart(page, row)
                if accepted:
                    ok("the untouched-chooser purchase was accepted")
                elif accepted is False:
                    gap("clicking Add to cart without touching the chooser was refused")
                else:
                    gap(f"{row['slug']}: no add-to-cart control found")
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
                        ok("re-clicking the chosen variant keeps it chosen")
                    else:
                        gap(f"re-clicking the chosen variant CLEARED it ({pre!r} -> {still!r})")
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
                        ok(f"the chooser offers only this product's values ({sorted(set(chips))})")
                    else:
                        gap(f"the chooser offers values the product does not sell: {extra}")
            else:
                gap(f"{row['slug']}: variable product carries no wired variation form")

        # ---- 2. simple product ----
        if by["simple"]:
            if add_to_cart(page, by["simple"]):
                ok(f"{by['simple']['slug']}: simple product reaches the basket")
            else:
                gap(f"{by['simple']['slug']}: simple product could not be bought")

        # ---- 3. sold out cannot be bought ----
        if by["soldout"]:
            row = by["soldout"]
            page.goto(product_url(row), wait_until="networkidle")
            page.wait_for_timeout(1000)
            live_btn = page.query_selector("form.cart button:not([type=button]):not([disabled])")
            if live_btn is None:
                ok(f"{row['slug']}: sold out and not buyable")
            else:
                gap(f"{row['slug']}: sold out but its buy control is live")

        # ---- 4. a promotion shows what it saves ----
        if by["onsale"]:
            row = by["onsale"]
            sale, regular = price_of(row)
            page.goto(product_url(row), wait_until="networkidle")
            page.wait_for_timeout(1000)
            body = page.inner_text("main")
            if sale in body and regular in body:
                ok(f"{row['slug']}: sale shows both prices ({sale}, was {regular})")
            else:
                gap(f"{row['slug']}: sale page shows only {sale!r} — the struck {regular!r} is missing")
            listing = "/".join(product_url(row).rstrip("/").split("/")[:-2]) or f"{BASE}/shop"
            page.goto(f"{BASE}/shop/", wait_until="networkidle")
            page.wait_for_timeout(1200)
            card = page.query_selector(f"main a[href*='{row['slug']}']")
            if card:
                text = card.inner_text()
                if regular in text:
                    ok("the listing card carries the struck original too")
                else:
                    gap(f"the listing card shows only the current price — no {regular!r}")
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
                        "the specimen's category froze into the part")
                elif crumb:
                    ok(f"{row['slug']}: breadcrumb names its own category ({crumb})")
                facts.append(spec)
            if facts[0] and facts[0] == facts[1]:
                gap(f"two different products state an identical spec line ({facts[0][:48]!r}) — "
                    "the specimen's value froze into the part")
            elif facts[0] or facts[1]:
                ok("the spec line differs per product")

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
                ok(f"search '{word}' surfaces products")
            else:
                gap(f"search '{word}' returns no products — the catalogue is invisible to search")

        # ---- 7. account, reviews, cart, checkout ----
        page.goto(f"{BASE}/my-account/", wait_until="networkidle")
        page.wait_for_timeout(1500)
        el = page.query_selector("main form, main .woocommerce")
        left = el.evaluate("e => Math.round(e.getBoundingClientRect().left)") if el else -1
        if left > 0:
            ok(f"my-account renders in the design's container ({left}px gutter)")
        elif left == 0:
            gap("my-account renders at the window's edge — no container template")
        else:
            gap("my-account did not render a login form")

        anyrow = by["simple"] or by["variable"]
        if anyrow:
            page.goto(product_url(anyrow), wait_until="networkidle")
            page.wait_for_timeout(1200)
            if "review" in page.inner_text("body").lower():
                ok("a product page offers reviews")
            else:
                gap("reviews are enabled in Woo but rendered nowhere")

        page.goto(f"{BASE}/cart/", wait_until="networkidle")
        page.wait_for_timeout(2500)
        cart_text = page.inner_text("body").lower()
        if "coupon" in cart_text:
            ok("the cart offers a coupon field")
        else:
            gap("no coupon field in the cart")

        page.goto(f"{BASE}/checkout/", wait_until="networkidle")
        page.wait_for_timeout(2500)
        if page.query_selector("#email") or "checkout" in page.inner_text("body").lower():
            ok("the checkout renders")
        else:
            gap("the checkout did not render")

        # ---- 8. with wp-cli: a coupon discounts, an order completes ----
        if args.wp_cli:
            note("wp-cli provided — exercising a real discount and a real order")
            coupon = "AUDIT-COVERAGE"
            cod_prior = wpcli("option get woocommerce_cod_settings --format=json")
            cid = wpcli(f"wc --user=admin shop_coupon create --code={coupon} "
                        "--discount_type=percent --amount=10 --porcelain")
            if cid:
                cleanup.append(f"post delete {cid} --force")
            wpcli("option patch update woocommerce_cod_settings enabled yes")
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
                        ok("the coupon discounts a real basket")
                    else:
                        gap("the coupon did not apply")
                page.goto(f"{BASE}/checkout/", wait_until="networkidle")
                page.wait_for_timeout(2500)
                try:
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
                    if "order-received" in page.url:
                        ok(f"a cash-on-delivery order completed end to end ({page.url.split('/order-received/')[1].split('/')[0]})")
                        oid = page.url.split("/order-received/")[1].split("/")[0]
                        cleanup.append(f"wc --user=admin shop_order delete {oid} --force")
                    else:
                        gap(f"placing the order did not reach the thank-you page ({page.url})")
                except Exception as e:  # noqa: BLE001
                    gap(f"the checkout flow broke: {str(e)[:100]}")
            # restore what was touched
            for c in cleanup:
                wpcli(c)
            if cod_prior:
                enc = cod_prior.replace("'", "'\\''")
                wpcli(f"option update woocommerce_cod_settings --format=json '{enc}'")
            note("fixtures removed, payment settings restored")

        if errors:
            gap(f"{len(errors)} JS error(s) while shopping — first: {errors[0][:90]}")
        else:
            ok("no JS errors anywhere on the walk")
        b.close()

    print(f"\n{'WOO COVERAGE CLEAN' if gaps == 0 else f'{gaps} GAP(s)'}")
    sys.exit(min(gaps, 120))


if __name__ == "__main__":
    main()
