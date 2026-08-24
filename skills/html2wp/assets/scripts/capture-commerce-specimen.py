#!/usr/bin/env python3
"""
Stage -1b — the design's own cart and checkout, with something in the basket.

  python3 assets/scripts/capture-commerce-specimen.py \
      --dist path/to/spa/dist --out workspace/style-specimens

WHY THIS EXISTS

The cart and the checkout are WooCommerce's, deliberately — a pixel copy of a
checkout that takes no money is not a checkout. But Woo's checkout is a React
application rendered in the BROWSER: its fields are in no server response, so
nothing can put the design's classes on them. The only way it can wear the
design's clothes is a stylesheet, and a stylesheet needs values.

Those values are nowhere in the conversion's input. Stage -1 captures each
route once, with an EMPTY basket, because a recorder that fills the basket
bakes its contents into every page — that bug is on record. So the design's
cart and checkout are captured in their empty state: one heading, one line of
copy, and not a single form field. The design's checkout — labelled fields, a
summary card, a dark full-width button — was never seen by the pipeline at all.

This script goes and looks at it. It puts one product in the basket, opens the
commerce routes, and writes down what the design does with each ROLE a checkout
has: a field, its label, a select, the summary card, the primary button, the
quiet link under it. Stage 4.6 turns that into `commerce.css`.

WHAT IT MAY NOT DO

Nothing here writes into the conversion's input, its page set, its bundle or
its gates. It writes one directory of specimens, read by one stage, and a
conversion that never runs it emits no commerce.css and looks exactly as it
did. The basket it fills lives in a browser context of its own and is thrown
away with it — the reason stage -1 stopped filling baskets in the first place.

WHAT IT CANNOT DO

Measure a role the design never drew. A shop whose checkout has no select
records no select, and the stylesheet leaves Woo's alone rather than guessing
at one. Every role is optional and its absence is reported, not filled in.
"""

import argparse
import functools
import http.server
import json
import re
import socketserver
import sys
import threading
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--dist", required=True, help="the built SPA (the directory index.html lives in)")
ap.add_argument("--out", required=True, help="directory to write the specimens into")
ap.add_argument("--routes", default="/cart,/checkout", help="the commerce routes to capture")
ap.add_argument("--start", default="/", help="the route to start hunting for a buy control from")
ap.add_argument("--max-pages", type=int, default=14, help="how many pages to open looking for one")
ap.add_argument("--width", type=int, default=1440)
args = ap.parse_args()

DIST = Path(args.dist).resolve()
OUT = Path(args.out).resolve()
ROUTES = [r.strip() for r in args.routes.split(",") if r.strip()]

if not (DIST / "index.html").exists():
    print(f"no index.html in {DIST}", file=sys.stderr)
    sys.exit(2)

report = {"routes": {}, "missing": [], "buyClickedOn": None}


# ------------------------------------------------------------------ serving
class SPAHandler(http.server.SimpleHTTPRequestHandler):
    """Any unknown path is the app's own route, not a 404."""

    def send_head(self):
        path = self.translate_path(self.path)
        if not Path(path).exists() and "." not in Path(path).name:
            self.path = "/index.html"
        return super().send_head()

    def log_message(self, *a):  # noqa: D401 - quiet
        pass


def serve(directory):
    handler = functools.partial(SPAHandler, directory=str(directory))
    httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_address[1]}/"


# ------------------------------------------------------------------- roles
#
# One expression per role, evaluated in the page. Each returns an element or
# null; null is recorded as a role the design does not have, never as a
# default. The whitelist below is what a stylesheet can honestly copy —
# geometry a layout owns (width, position, display) is deliberately absent.
PROPS = [
    "backgroundColor", "color", "fontFamily", "fontSize", "fontWeight", "lineHeight",
    "letterSpacing", "textTransform", "borderTopWidth", "borderTopStyle", "borderTopColor",
    "borderRightWidth", "borderRightColor", "borderBottomWidth", "borderBottomColor",
    "borderLeftWidth", "borderLeftColor", "borderTopLeftRadius", "paddingTop", "paddingRight",
    "paddingBottom", "paddingLeft", "marginBottom", "textAlign", "opacity",
]

FINDERS = """
() => {
  const inMain = (el) => {
    const main = document.querySelector('main') || document.body;
    return main.contains(el);
  };
  const all = (sel) => Array.from(document.querySelectorAll(sel)).filter(inMain);
  const text = (el) => (el.textContent || '').trim();
  const money = /[$€£¥₹]|\\d[\\d.,]*\\s*(?:USD|EUR|GBP|CHF|CZK|PLN|SEK|NOK|DKK|Kč|zł|kr)/i;

  const field = all('input:not([type=hidden]):not([type=checkbox]):not([type=radio]):not([type=number])')[0] || null;
  const select = all('select')[0] || null;

  // The label of that field: its own <label for>, or the nearest label-ish
  // element immediately before it.
  let label = null;
  if (field) {
    if (field.id) label = document.querySelector('label[for="' + CSS.escape(field.id) + '"]');
    if (!label) label = field.closest('label');
    if (!label) {
      let prev = field.previousElementSibling, hops = 0;
      let scope = field;
      while (!label && hops < 3) {
        prev = scope.previousElementSibling;
        if (prev && text(prev) && text(prev).length <= 30 && !prev.querySelector('input,select,textarea')) label = prev;
        scope = scope.parentElement; hops++;
        if (!scope) break;
      }
    }
  }

  // The primary control: the button a shopper presses to finish. Matched on
  // its own words — "place order", "pay", "complete", "checkout" — and never
  // on a size chip or a quantity stepper.
  const buttons = all('button, a[role=button], input[type=submit]');
  const primary = buttons.find((b) => /place order|pay now|complete order|confirm order|checkout|pay$/i.test(text(b))) || null;

  // The summary card: the smallest element carrying BOTH a total row and the
  // primary button, or failing that the totals themselves.
  let card = null;
  const totalRow = all('*').filter((el) => /total/i.test(text(el)) && money.test(text(el)) && el.children.length <= 4).pop() || null;
  if (totalRow) {
    let up = totalRow.parentElement, best = null;
    while (up && up !== document.body) {
      const cs = getComputedStyle(up);
      const framed = parseFloat(cs.borderTopWidth) > 0 || cs.backgroundColor !== 'rgba(0, 0, 0, 0)';
      if (framed && (!primary || up.contains(primary))) { best = up; break; }
      up = up.parentElement;
    }
    card = best;
  }

  const quiet = card ? Array.from(card.querySelectorAll('a')).pop() : null;
  const h1 = all('h1')[0] || null;
  // A design marks a checkout's sections with a <legend> as often as with
  // a heading — the source this was written against uses one.
  const sectionHeading = all('h2, h3, legend').find((h) => !card || !card.contains(h)) || null;

  const pick = (el) => {
    if (!el) return null;
    const cs = getComputedStyle(el);
    const out = {};
    __PROPS__.forEach((p) => { out[p] = cs[p]; });
    out.__text = text(el).slice(0, 40);
    out.__tag = el.tagName.toLowerCase();
    out.__class = (typeof el.className === 'string' ? el.className : '').slice(0, 200);
    return out;
  };

  // WHERE the design puts a field's label is a design decision, and the one
  // that separates these two checkouts: above the field (a block of its own)
  // or floating inside it, the way Woo does. Measured, not assumed.
  const geometry = {};
  if (label && field) {
    const lr = label.getBoundingClientRect(), fr = field.getBoundingClientRect();
    geometry.labelAbove = lr.bottom <= fr.top + 1 && lr.height > 0;
    geometry.labelGap = Math.max(0, Math.round(fr.top - lr.bottom));
  }
  // What FOCUS looks like is a design decision too. Left unmeasured, a field
  // whose design darkens its border on focus shows the browser's blue ring
  // instead — the one element on the page no design drew. Focus is entered and
  // left again so the rest of the capture still reads the resting state.
  if (field) {
    field.focus();
    const fs = getComputedStyle(field);
    geometry.focusOutlineStyle = fs.outlineStyle;
    geometry.focusOutlineColor = fs.outlineColor;
    geometry.focusBorderColor = fs.borderTopColor;
    geometry.focusBoxShadow = fs.boxShadow;
    field.blur();
  }

  const page = document.querySelector('main') || document.body;
  return {
    __geometry: geometry,
    page: pick(page),
    heading: pick(h1),
    sectionHeading: pick(sectionHeading),
    label: pick(label),
    field: pick(field),
    select: pick(select),
    card: pick(card),
    totalsLabel: pick(totalRow ? totalRow.firstElementChild : null),
    totalsValue: pick(totalRow ? totalRow.lastElementChild : null),
    primary: pick(primary),
    quietLink: pick(quiet),
  };
}
""".replace("__PROPS__", json.dumps(PROPS))


def main():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright is required: pip install playwright && playwright install chromium", file=sys.stderr)
        sys.exit(2)

    httpd, base = serve(DIST)
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            # A context of its own, thrown away with the run: the basket this
            # fills must never be visible to anything else the pipeline does.
            ctx = browser.new_context(viewport={"width": args.width, "height": 1200})
            page = ctx.new_page()

            # --- put one product in the basket ---
            page.goto(base + args.start.lstrip("/"), wait_until="networkidle")
            page.wait_for_timeout(600)
            seen = set()
            queue = [args.start]
            for href in page.eval_on_selector_all(
                "a[href]", "es => es.map(e => e.getAttribute('href'))"
            ):
                if href and href.startswith("/") and href not in queue:
                    queue.append(href)
            clicked = False
            for route in queue[: args.max_pages]:
                if route in seen:
                    continue
                seen.add(route)
                page.goto(base + route.lstrip("/"), wait_until="networkidle")
                page.wait_for_timeout(400)
                buy = page.query_selector(
                    "button:has-text('Add to cart'), button:has-text('Add to bag'), "
                    "button:has-text('Add to basket'), button:has-text('Buy now')"
                )
                if not buy or not buy.is_enabled():
                    continue
                buy.click()
                page.wait_for_timeout(900)
                report["buyClickedOn"] = route
                clicked = True
                break
            if not clicked:
                print("  warn: no buy control found — the specimens will be the EMPTY cart and checkout,")
                print("        which is what stage -1 already captured. Nothing is written.")
                report["missing"].append("buy-control")
                (OUT / "report.json").write_text(json.dumps(report, indent=2) + "\n")
                sys.exit(1)

            # --- capture each commerce route ---
            for route in ROUTES:
                name = route.strip("/").replace("/", "-") or "index"
                page.goto(base + route.lstrip("/"), wait_until="networkidle")
                page.wait_for_timeout(1200)
                roles = page.evaluate(FINDERS)
                filled = [k for k, v in roles.items() if v]
                missing = [k for k, v in roles.items() if not v]
                for m in missing:
                    report["missing"].append(f"{name}:{m}")
                (OUT / f"{name}.json").write_text(json.dumps(roles, indent=2) + "\n")
                # The markup too, for the record: a value nobody can explain is
                # a value nobody will trust six months from now.
                (OUT / f"{name}.html").write_text(page.content())
                page.screenshot(path=str(OUT / f"{name}.png"), full_page=True)
                report["routes"][name] = {"roles": filled, "missing": missing}
                print(f"  {name}: {len(filled)} role(s) recorded" + (f", missing {', '.join(missing)}" if missing else ""))
            ctx.close()
            browser.close()
    finally:
        httpd.shutdown()

    (OUT / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(f"OK — specimens → {OUT}")


if __name__ == "__main__":
    main()
