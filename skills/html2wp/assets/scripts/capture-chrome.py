#!/usr/bin/env python3
"""Stage 2.5 — capture the chrome a visitor actually sees, at rest, PER
DESIGN GROUP.

  node chrome-groups.mjs --manifest=conversion-manifest.json   # first
  python3 capture-chrome.py --manifest=conversion-manifest.json [--dist <dir>]
      [--out <dir>]

A static export freezes whatever runtime state each page happened to be in
when it was snapshotted. On a real site that made one header look like
seven: app.js swaps `relative` + `h-[var(--header-height)]` for
`fixed top-0 right-0 left-0 translate-y-2 px-5.5` on scroll, so pages
exported mid-scroll carry the sticky variant and pages exported at the top
carry the at-rest one. Grouping those by page count then picks whichever
state was more common in the export — on the reference site, the SCROLLED
one, which would ship a theme whose header floats before you have scrolled.

The export is not the authority here; the browser is. This loads a page,
lets its own scripts settle at scroll 0, and reads the chrome back out of
the live DOM — by definition the state every visitor sees first.

PER GROUP, not once: a site legitimately ships several header/footer
DESIGNS (chrome-groups.json is the partition, written by chrome-groups.mjs
— design variance settled of active-nav and runtime state). Each group's
capture comes from a page OF THAT GROUP, so a variant is a design captured
at rest, never a frozen scroll state of some other design. Capturing only
the canonical group and applying it everywhere is exactly the flattening
the owner rejected.

Writes <out>/{region}-g{N}.html per region and group. Stage 3 prefers these
over the exported fragments when present.
"""

import argparse, json, sys, threading, functools
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--manifest", required=True)
ap.add_argument("--dist", default="")
ap.add_argument("--out", default="")
args = ap.parse_args()

MF = json.loads(Path(args.manifest).read_text())
WS = Path(MF["workspace"]).resolve()
DIST = Path(args.dist or (WS / "astro-project" / "dist")).resolve()
OUT = Path(args.out or (WS / "chrome-at-rest")).resolve()
OUT.mkdir(parents=True, exist_ok=True)

groups_file = WS / "chrome-groups.json"
if not groups_file.exists():
    print("chrome-groups.json not found — run `node chrome-groups.mjs --manifest=…` first, "
          "so captures are taken per design group rather than once.", file=sys.stderr)
    sys.exit(1)
GROUPS = json.loads(groups_file.read_text())["regions"]


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass


httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(DIST)))
threading.Thread(target=httpd.serve_forever, daemon=True).start()

# A trailing component can own SEVERAL sibling nodes — the canonical shape
# is {"component": "SiteDrawer", "selectors": ["div.drawer-veil",
# "aside.drawer"]} — and stage 1 joins all of them into one fragment. Taking
# only selectors[0] here would hand stage 3 a fragment missing every node
# past the first, silently deleting the mobile drawer from the header part.
#
# Read defensively, because `chrome.header` is not the only `chrome` in a
# manifest: `pages[].chrome` is a STRING ("consensus" / "self-contained"), and
# a manifest arrived with the top-level one written the same way —
# {"header": "index.html", "footer": "index.html"}, naming the page the chrome
# comes from. Python then raised `'str' object has no attribute 'get'` on the
# first read and the whole conversion stopped before the first shot, on a site
# whose header is a plain <header>. make-theme reads the same fields through
# `?.` and would have carried on with the default, so tolerating it here is
# not leniency — it is the two stages agreeing on the region they partition.
def _spec(value):
    """A {selector: …} block, or None for anything that is not one."""
    return value if isinstance(value, dict) else None


chrome = _spec(MF.get("chrome")) or {}
header_spec = _spec(chrome.get("header")) or {}
footer_spec = _spec(chrome.get("footer")) or {}

header_sel = header_spec.get("selector") or "header"
region_selectors = {"header": [header_sel]}
trailing_specs = chrome.get("trailing")
if not isinstance(trailing_specs, list) or not trailing_specs:
    trailing_specs = [{"selectors": [footer_spec["selector"]]}] if footer_spec.get("selector") else []
for i, spec in enumerate(trailing_specs):
    selectors = _spec(spec).get("selectors") if _spec(spec) else None
    if isinstance(selectors, list) and selectors:
        region_selectors[f"trailing-{i}"] = selectors

# The span, read from the live DOM as a Range. Shared by the at-rest capture
# and the scripts-disabled one below, so the two are the same shape.
SPAN_JS = """(sels) => {
  // hotfix (simple-002): chrome lives OUTSIDE the content
  // region, so a match inside <main> is not chrome. A bare
  // `footer` selector otherwise takes the first CARD footer
  // — this site's blog and testimonial cards use <footer>.
  const pick = (s) => {
    const all = [...document.querySelectorAll(s)];
    const outside = all.filter((e) => !e.closest('main'));
    if (outside.length) return outside[0];
    // Stage 1 uses the last nested footer when a page wrapper contains card
    // bylines before the site's actual footer. Keep the live at-rest capture
    // on that same element so chrome-groups and make-theme partition the same
    // region.
    if (/^footer(?:[.#:]|$)/i.test(s)) return all[all.length - 1] || null;
    return all[0] || null;
};
  const els = sels.map(pick);
  if (els.some((e) => !e)) return null;
  const r = document.createRange();
  r.setStartBefore(els[0]);
  r.setEndAfter(els[els.length - 1]);
  const box = document.createElement('div');
  box.appendChild(r.cloneContents());
  return box.innerHTML;
}"""

# Nodes the page's own JavaScript ADDED are removed again before the capture
# is written.
#
# The at-rest capture is taken from a live browser, so it holds whatever the
# design's scripts built by the time it settles — and a control a script
# appends to the chrome (the near-universal `header.appendChild(menuButton)`
# hamburger) is exactly that. Baking it into the template part does not move
# the behaviour into the markup: the script still ships, still runs on the
# WordPress page, and appends a SECOND one. Measured live on
# minimal-portfolio-template: two hamburger buttons side by side in the
# header at 390px, on every page, against the original's one. No gate can
# see it — a 40x40 icon on a 5000px page is 0.0008 of the pixels, well under
# gate B's threshold, and it is display:none at the widths where a reviewer
# looks first.
#
# So the region is captured a second time with JavaScript DISABLED — the
# markup as authored — and the two child sequences are aligned greedily per
# parent. A child the at-rest tree has and the authored one does not is an
# injection: dropped, and reported. Alignment is by tag+class+id and in
# order, so a class the design's own script SWAPS at rest still matches by
# tag and survives; only genuinely extra nodes go.
PRUNE_JS = """([liveHtml, staticHtml]) => {
  const box = (h) => { const d = document.createElement('div'); d.innerHTML = h; return d; };
  const live = box(liveHtml), stat = box(staticHtml);
  const sig = (e) => e.tagName + '|' + (e.getAttribute('class') || '') + '|' + (e.id || '');
  const dropped = [];
  const walk = (a, b) => {
    const A = [...a.children], B = [...b.children];
    let j = 0;
    for (const el of A) {
      // Find this node in what is left of the authored sequence. Matching
      // on the full signature first keeps a swapped class from stealing a
      // slot; the tag-only pass is what lets a swap match at all.
      let k = B.findIndex((c, i) => i >= j && sig(c) === sig(el));
      if (k < 0) k = B.findIndex((c, i) => i >= j && c.tagName === el.tagName);
      if (k < 0) { dropped.push(sig(el)); el.remove(); continue; }
      walk(el, B[k]);
      j = k + 1;
    }
  };
  walk(live, stat);
  return { html: live.innerHTML, dropped };
}"""

written = []
pruned = []
with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1440, "height": 950})
    nojs = browser.new_context(java_script_enabled=False).new_page()

    for region, sels in region_selectors.items():
        for group in GROUPS.get(region, []):
            page_file = next((f for f in group["pages"] if (DIST / f).exists()), None)
            if page_file is None:
                print(f"  warn: {region} group {group['index']} has no page in dist — left to the exported fragment",
                      file=sys.stderr)
                continue
            page.goto(f"http://127.0.0.1:{httpd.server_port}/{page_file}")
            page.wait_for_load_state("networkidle")
            # Deliberately NO scrolling: the point is the at-rest state. Give
            # the page's own scripts a beat to apply it.
            page.wait_for_timeout(700)

            # The exact SPAN from the first matched node to the last, gaps
            # included — the same shape stage 1 writes, so the two fragments
            # stay interchangeable. A Range gives it directly from the live DOM.
            html = page.evaluate(SPAN_JS, sels)
            if html is None:
                print(f"  warn: {', '.join(sels)} not all present at rest on {page_file} — "
                      f"{region} group {group['index']} left to the exported fragment", file=sys.stderr)
                continue

            nojs.goto(f"http://127.0.0.1:{httpd.server_port}/{page_file}")
            nojs.wait_for_load_state("load")
            authored = nojs.evaluate(SPAN_JS, sels)
            if authored is None:
                print(f"  warn: {region} group {group['index']} — the same span is not present with scripts off, "
                      f"so runtime-injected nodes could not be pruned; check the part by hand", file=sys.stderr)
            else:
                res = page.evaluate(PRUNE_JS, [html, authored])
                html = res["html"]
                for d in res["dropped"]:
                    pruned.append(f"{region}-g{group['index']}: {d}")

            name = f"{region}-g{group['index']}.html"
            (OUT / name).write_text(html)
            written.append(f"{name} (from {page_file})")

    browser.close()
httpd.shutdown()

print(f"captured at-rest chrome per design group: {', '.join(written) or 'nothing'} → {OUT}")
if pruned:
    print(f"  pruned {len(pruned)} runtime-injected node(s) the page's own scripts add — they are NOT baked into the "
          f"part, because the script still ships and would add a second one: {'; '.join(pruned)}", file=sys.stderr)
