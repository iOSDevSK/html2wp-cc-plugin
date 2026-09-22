#!/usr/bin/env python3
"""lib/woo_pages.py — which pages a converted shop hands to WooCommerce.

  python3 assets/scripts/test-woo-pages.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from woo_pages import woo_owned_files, woo_owned_keys  # noqa: E402

PAGES = [
    {"file": "index.html", "key": "front-page", "kind": "front"},
    {"file": "shop.html", "key": "shop", "kind": "shop"},
    {"file": "cart.html", "key": "cart", "kind": "utility"},
    {"file": "checkout.html", "key": "checkout", "kind": "utility"},
    {"file": "product/a.html", "key": "product-a", "kind": "product"},
    {"file": "products/b.html", "key": "products-b", "kind": "page"},   # declared in shop.products only
    {"file": "journal/x.html", "key": "journal-x", "kind": "article"},
]
CASES = [
    ("no shop at all", {"pages": PAGES}, set()),
    ("shop present:false", {"pages": PAGES, "shop": {"present": False, "reason": "lookbook"}}, set()),
    ("full shop", {"pages": PAGES, "shop": {"present": True, "cartPage": "cart.html", "checkoutPage": "checkout.html",
                                            "products": ["product/a.html", "products/b.html"]}},
     {"cart.html", "checkout.html", "product/a.html", "products/b.html"}),
    ("cart-only shop, product by kind only", {"pages": PAGES, "shop": {"present": True, "cartPage": "cart.html"}},
     {"cart.html", "product/a.html"}),
    ("malformed shop block", {"pages": PAGES, "shop": "yes"}, set()),
]

failed = 0
for name, mf, want in CASES:
    got = woo_owned_files(mf)
    ok = got == want
    failed += not ok
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f" — got {sorted(got)}, want {sorted(want)}"))
keys = woo_owned_keys(CASES[2][1])
ok = keys == ["cart", "checkout", "product-a", "products-b"]
failed += not ok
print(("ok   " if ok else "FAIL ") + "keys follow the manifest's page order" + ("" if ok else f" — {keys}"))
print(f"{'FAILED' if failed else 'all passed'}")
sys.exit(1 if failed else 0)
