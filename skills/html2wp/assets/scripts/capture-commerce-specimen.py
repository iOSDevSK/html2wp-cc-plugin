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
  // on a size chip or a quantity stepper. A cart's "Proceed to checkout" is
  // routinely a router LINK dressed as a button, so a plain <a> with those
  // words counts too — after the real buttons, which win when both exist.
  const finishes = (b) => /place order|pay now|complete order|confirm order|checkout|pay$/i.test(text(b));
  const buttons = all('button, a[role=button], input[type=submit]');
  const primary = buttons.find(finishes) || all('a[href]').find(finishes) || null;

  // The summary card: the smallest element carrying BOTH a total row and the
  // primary button, or failing that the totals themselves.
  let card = null;
  const totalRow = all('*').filter((el) => /total/i.test(text(el)) && money.test(text(el)) && el.children.length <= 4).pop() || null;
  if (totalRow) {
    // The page itself (a <main> with a background) is framed too, and is
    // never the card: a primary that sits OUTSIDE the summary would otherwise
    // climb to it. Then the nearest framed box around the totals is the card.
    const pageEl = document.querySelector('main') || document.body;
    let up = totalRow.parentElement, best = null, nearest = null;
    while (up && up !== document.body && up !== pageEl) {
      const cs = getComputedStyle(up);
      const framed = parseFloat(cs.borderTopWidth) > 0 || cs.backgroundColor !== 'rgba(0, 0, 0, 0)';
      if (framed && !nearest) nearest = up;
      if (framed && (!primary || up.contains(primary))) { best = up; break; }
      up = up.parentElement;
    }
    card = best || nearest;
  }

  // The quiet link is never the primary itself — a card whose only link is
  // its dark CTA has no quiet link, and recording the CTA as one would give
  // Woo's "return to cart" the CTA's light text on a light page.
  const quiet = card ? Array.from(card.querySelectorAll('a')).filter((a) => a !== primary).pop() || null : null;
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

  // Whether the design's page shell STRETCHES — a column at least a window
  // tall whose <main> grows — so a short page (an empty cart) still puts the
  // footer at the bottom of the window. WordPress's own shell does not, and
  // only the design says whether it should.
  const main = document.querySelector('main');
  if (main && main.parentElement) {
    const ps = getComputedStyle(main.parentElement);
    geometry.shellStretch = /flex/.test(ps.display) && /^column/.test(ps.flexDirection)
      && parseFloat(ps.minHeight) >= window.innerHeight - 1
      && parseFloat(getComputedStyle(main).flexGrow) > 0;
  }

  const page = main || document.body;
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


# WHERE the line item puts its remove control and its line total — measured,
# because it is layout the cart's markup cannot say by itself: a design that
# pins "×" to the line's top-right corner and the total to its bottom-right
# looks nothing like a row of cells. Offsets are from the line item's own box
# (the smallest element holding the picture and the quantity field), at the
# width measured; null when the design has no such piece.
LINE_GEOMETRY_JS = r"""() => {
  const main = document.querySelector('main') || document.body;
  const qty = main.querySelector('input[type=number], input[aria-label*="uantity" i], input[name*="qty" i]');
  const img = main.querySelector('img');
  if (!qty || !img) return null;
  let line = qty.parentElement;
  while (line && line !== main && !line.contains(img)) line = line.parentElement;
  if (!line || line === main) return null;
  const box = line.getBoundingClientRect();
  const at = (el) => { if (!el) return null; const r = el.getBoundingClientRect();
    return { top: Math.round(r.top - box.top), right: Math.round(box.right - r.right), bottom: Math.round(box.bottom - r.bottom), left: Math.round(r.left - box.left) }; };
  const remove = [...line.querySelectorAll('button, a')].find((b) => /\b(remove|delete)\b/i.test((b.getAttribute('aria-label') || '') + ' ' + b.textContent));
  const MONEY = /^(?:[^\d\s]{0,3}\s?)\d[\d.,\s]*(?:\s?[^\d\s]{0,3})$/;
  const after = (a, b) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);
  const total = [...line.querySelectorAll('*')].find((e) => !e.children.length && MONEY.test((e.textContent || '').trim()) && after(qty, e));
  return { width: window.innerWidth, line: { width: Math.round(box.width), height: Math.round(box.height) }, image: at(img), remove: at(remove), total: at(total) };
}"""


# What the buy page says about the thing being bought, BEFORE the click: the
# product's name (its <h1>), the chosen option (the chip marked pressed or
# visually selected) and the quantity field — so the toast's sentence can be
# turned back into a template.
CONTEXT_JS = """() => {
  const h1 = document.querySelector('main h1, h1');
  const qty = document.querySelector('input[type=number]');
  const chips = [...document.querySelectorAll('main button')].filter((b) => {
    const t = (b.textContent || '').trim();
    return t && t.length <= 24 && !/add to|buy|sold|[+\u2212-]$/i.test(t);
  });
  const chosen = chips.find((b) => b.getAttribute('aria-pressed') === 'true' || b.getAttribute('data-state') === 'on'
    || b.getAttribute('aria-checked') === 'true')
    || chips.find((b) => getComputedStyle(b).backgroundColor !== getComputedStyle(chips[chips.length - 1]).backgroundColor);
  return { name: h1 ? h1.textContent.trim() : '', variation: chosen ? chosen.textContent.trim() : '',
           qty: qty ? String(qty.value) : '1' };
}"""

# The design's own confirmation after "add to cart" — a toast, a banner — if
# one appears: the newest visible element in a live region (or a toast list)
# that says so. Its markup is kept whole; stage 4.6 shows it for Woo's adds.
TOAST_JS = """() => {
  const said = (el) => /added|cart|bag|basket/i.test(el.textContent || '');
  const live = [...document.querySelectorAll('[role=status], [role=alert], [aria-live] li, ol[tabindex] > li, [data-sonner-toast]')]
    // A screen-reader announcer (1×1, clipped) says the same words and is not
    // the design's toast.
    .filter((el) => { const r = el.getBoundingClientRect(); return r.height > 16 && r.width > 40 && said(el); });
  const item = live[live.length - 1];
  if (!item) return null;
  const list = item.parentElement;
  const leaves = [...item.querySelectorAll('*')].filter((e) => !e.children.length && (e.textContent || '').trim())
    .map((e) => e.textContent.trim());
  return {
    item: item.outerHTML,
    list: list ? list.outerHTML.slice(0, list.outerHTML.indexOf('>') + 1) + '</' + list.tagName.toLowerCase() + '>' : '',
    region: list && list.parentElement && list.parentElement.getAttribute('role') === 'region'
      ? list.parentElement.outerHTML.slice(0, list.parentElement.outerHTML.indexOf('>') + 1) : '',
    title: leaves[0] || '',
    description: leaves[1] || '',
  };
}"""


def toast_template(toast, context):
    """The toast's markup with the purchase turned back into placeholders:
    {title}, and the description with {name} / {variation} / {qty} where the
    page's own values stood."""
    html = toast["item"]
    desc = toast["description"]
    tmpl = desc
    for key in ("name", "variation"):
        v = context.get(key) or ""
        if v and v in tmpl:
            tmpl = tmpl.replace(v, "{" + key + "}", 1)
    q = context.get("qty") or ""
    if q:
        tmpl = re.sub(r"(?<![\w{])" + re.escape(q) + r"(?![\w}])", "{qty}", tmpl, count=1)
    if toast["title"]:
        html = html.replace(">" + toast["title"] + "<", ">{title}<", 1)
    if desc:
        html = html.replace(">" + desc + "<", ">{description}<", 1)
    return {"html": html, "description": tmpl}


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
                context = page.evaluate(CONTEXT_JS)
                buy.click()
                toast = None
                for _ in range(12):
                    page.wait_for_timeout(150)
                    toast = page.evaluate(TOAST_JS)
                    if toast:
                        break
                if toast:
                    toast["template"] = toast_template(toast, context)
                    (OUT / "toast.json").write_text(json.dumps(toast, indent=2) + "\n")
                    report["toast"] = {"title": toast["title"], "template": toast["template"]}
                page.wait_for_timeout(600)
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
                if name == "cart":
                    # At the specimen's width and at a phone's: the pieces move.
                    geo = [page.evaluate(LINE_GEOMETRY_JS)]
                    page.set_viewport_size({"width": 390, "height": 900})
                    page.wait_for_timeout(300)
                    geo.append(page.evaluate(LINE_GEOMETRY_JS))
                    page.set_viewport_size({"width": args.width, "height": 1200})
                    page.wait_for_timeout(300)
                    roles["__lineGeometry"] = [g for g in geo if g]
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
