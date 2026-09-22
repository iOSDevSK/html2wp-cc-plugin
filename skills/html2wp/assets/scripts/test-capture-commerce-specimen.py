#!/usr/bin/env python3
"""Offline test of capture-commerce-specimen.py's role finders.

Each case is a tiny SPA (one index.html that renders by pathname, the shape
the script serves) with an "Add to cart" button on "/" and a cart on
"/cart". The cases vary what the cart's finishing control IS and where it
sits, because a router link dressed as a button ("Proceed to checkout") went
unrecorded as the primary and was recorded as the QUIET link instead.

  python3 assets/scripts/test-capture-commerce-specimen.py
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "capture-commerce-specimen.py"

PAGE = """<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;font-family:sans-serif}
main{%(main_bg)s;padding:40px}
.card{background:#fff;border:1px solid #ddd;padding:24px}
.cta{display:block;background:#2e261f;color:#faf8f5;padding:16px;text-transform:uppercase}
.btn{background:#123456;color:#fff;padding:12px}
.quiet{color:#6a7181}
</style></head><body><main id="app"></main><script>
const views = {
  '/': '<h1>Shop</h1><button>Add to cart</button>',
  '/cart': %(cart)s,
};
document.getElementById('app').innerHTML = views[location.pathname] || '<h1>404</h1>';
</script></body></html>"""

TOTALS = '<div class="row"><span>Total</span><span>$285.00</span></div>'

CASES = {
    # the case that shipped: the CTA is a link, and the card's only link
    "link-cta-only-link": {
        "main_bg": "background:#f6f1ea",
        "cart": '<h1>Cart</h1><aside class="card"><h2>Order summary</h2>' + TOTALS
                + '<a class="cta" href="/checkout">Proceed to checkout</a></aside>',
        "expect": {"primary": ("a", "Proceed to checkout"), "quietLink": None, "card": "aside"},
    },
    # a real button and a separate quiet link — unchanged behaviour
    "button-cta-with-quiet-link": {
        "main_bg": "background:transparent",
        "cart": '<h1>Basket</h1><div class="card">' + TOTALS
                + '<button class="btn">Checkout</button><a class="quiet" href="/shop">Continue shopping</a></div>',
        "expect": {"primary": ("button", "Checkout"), "quietLink": ("a", "Continue shopping"), "card": "div"},
    },
    # the CTA link sits OUTSIDE the summary, on a page that has a background:
    # the card is the summary box, never the page
    "link-cta-outside-card": {
        "main_bg": "background:#eeeeee",
        "cart": '<h1>Cart</h1><section class="card">' + TOTALS
                + '</section><a class="cta" href="/checkout">Go to checkout</a>',
        "expect": {"primary": ("a", "Go to checkout"), "card": "section"},
    },
    # a button and a link both saying checkout: the button wins
    "button-beats-link": {
        "main_bg": "background:transparent",
        "cart": '<h1>Cart</h1><div class="card">' + TOTALS
                + '<a class="quiet" href="/checkout">Checkout as guest</a><button class="btn">Checkout</button></div>',
        "expect": {"primary": ("button", "Checkout"), "quietLink": ("a", "Checkout as guest")},
    },
}


def run_case(name, case, tmp):
    dist = tmp / name / "dist"
    out = tmp / name / "out"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text(PAGE % {"main_bg": case["main_bg"], "cart": json.dumps(case["cart"])})
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--dist", str(dist), "--out", str(out), "--routes", "/cart"],
        capture_output=True, text=True, timeout=180,
    )
    if proc.returncode != 0:
        return [f"exit {proc.returncode}: {proc.stdout[-400:]} {proc.stderr[-400:]}"]
    roles = json.loads((out / "cart.json").read_text())
    errors = []
    for role, want in case["expect"].items():
        got = roles.get(role)
        if want is None:
            if got:
                errors.append(f"{role}: expected none, got <{got['__tag']}> {got['__text']!r}")
        elif isinstance(want, tuple):
            if not got or got["__tag"] != want[0] or got["__text"] != want[1]:
                errors.append(f"{role}: expected <{want[0]}> {want[1]!r}, got "
                              + (f"<{got['__tag']}> {got['__text']!r}" if got else "none"))
        else:
            if not got or got["__tag"] != want:
                errors.append(f"{role}: expected <{want}>, got " + (f"<{got['__tag']}>" if got else "none"))
    return errors


def main():
    failed = 0
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        for name, case in CASES.items():
            errors = run_case(name, case, tmp)
            if errors:
                failed += 1
                print(f"FAIL {name}")
                for e in errors:
                    print(f"     {e}")
            else:
                print(f"ok   {name}")
    print(f"{len(CASES) - failed}/{len(CASES)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
