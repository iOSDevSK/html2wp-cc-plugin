"""Which manifest pages WooCommerce owns once a shop is converted.

A product page stops being a page and becomes a product record (its key
301s to the product permalink); the cart and the checkout are excluded from
the bundle and answered by WooCommerce's own. Checks that compare a converted
PAGE against itself or its source have nothing to say about these — what
renders there is Woo's, per session and per login — so they name them as
skipped rather than failing on them. verify-wp.py classifies them the same
way for gate B (woocommerce-product / woocommerce-owned-page).
"""


def woo_owned_files(manifest):
    """The page files a converted shop hands to WooCommerce, or an empty set."""
    shop = manifest.get("shop") if isinstance(manifest.get("shop"), dict) else {}
    if not shop.get("present"):
        return set()
    owned = {f for f in (shop.get("cartPage"), shop.get("checkoutPage")) if isinstance(f, str) and f}
    for page in manifest.get("pages") or []:
        if isinstance(page, dict) and page.get("kind") == "product" and page.get("file"):
            owned.add(page["file"])
    owned |= {f for f in (shop.get("products") or []) if isinstance(f, str) and f}
    return owned


def woo_owned_keys(manifest):
    files = woo_owned_files(manifest)
    return [p.get("key") for p in (manifest.get("pages") or [])
            if isinstance(p, dict) and p.get("key") and p.get("file") in files]
