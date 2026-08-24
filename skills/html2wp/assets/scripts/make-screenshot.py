#!/usr/bin/env python3
"""Stage 3.5 — the theme's screenshot.png.

  python3 make-screenshot.py --manifest=conversion-manifest.json [--wp URL]

WordPress shows this image in Appearance → Themes and in the theme installer.
Without it the theme is a blank checkerboard tile with nothing but its name —
which is how both reference conversions shipped, because the only screenshot
tool lived outside the pipeline and depended on a live WordPress: the order
was zip → install → verify → screenshot → RE-ZIP, and nothing ever forced
that last step. Shot from the DIST BUILD by default, this stage has no such
dependency and runs immediately after the theme is generated, so a theme is
never screenshot-less. make-zip.sh refuses without it.

The dist build is a faithful choice, not a shortcut: gates A/A2 have already
proven it is the site, pixel for pixel. Pass --wp <url> after stage 5 to
re-shoot from the live install if you want the WordPress render specifically
(template parts, enqueued CSS and all) — the file is simply overwritten.

Exactly 1200×900: the viewport IS the crop, so no fullPage and no resize
afterwards. deviceScaleFactor 1 keeps it 1200×900 device pixels rather than a
retina capture that quadruples the bytes in every theme ZIP for no visible
gain at tile size.
"""

import argparse, functools, json, re, sys, threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--manifest", required=True)
ap.add_argument("--wp", default="", help="shoot this live URL instead of the dist build")
ap.add_argument("--dist", default="")
ap.add_argument("--out", default="")
args = ap.parse_args()

MF = json.loads(Path(args.manifest).read_text())
WS = Path(MF["workspace"]).resolve()
DIST = Path(args.dist or (WS / "astro-project" / "dist")).resolve()

# The slug becomes a directory name that this script CREATES, and it comes out
# of the manifest — so `"slug": "../../.."` made mkdir(parents=True) walk out
# of the workspace and put the screenshot wherever it landed. The server
# already refuses a slug that is not this shape; the client has no reason to
# be laxer about a value it is about to write with.
SLUG = MF["site"]["slug"]
if not re.fullmatch(r"[a-z][a-z0-9-]{0,48}", str(SLUG or "")):
    print(f'site.slug must be lowercase letters, digits and hyphens; got {SLUG!r}', file=sys.stderr)
    sys.exit(2)

OUT = Path(args.out or (WS / "theme" / SLUG / "screenshot.png")).resolve()
OUT.parent.mkdir(parents=True, exist_ok=True)

front = next((p["file"] for p in MF["pages"] if p.get("key") == "front-page"), None)
if front is None:
    print("no front-page in the manifest — nothing to shoot", file=sys.stderr)
    sys.exit(1)

httpd = None
if args.wp:
    target = args.wp.rstrip("/") + "/"
else:
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(DIST)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    target = f"http://127.0.0.1:{httpd.server_port}/{front}"

with sync_playwright() as p:
    browser = p.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1200, "height": 900}, device_scale_factor=1)
    page = ctx.new_page()
    page.set_default_timeout(60000)
    errors = []
    page.on("console", lambda m: errors.append(m.text) if m.type == "error" else None)
    page.goto(target)
    page.wait_for_load_state("networkidle")
    # The same settle discipline the gates use, minus the scroll-through: this
    # is the ABOVE-THE-FOLD shot, and scrolling would leave a sticky header in
    # its scrolled state — the tile would show the site mid-scroll.
    page.evaluate("document.fonts && document.fonts.ready")
    page.evaluate("""async () => {
      for (const i of document.querySelectorAll('img[loading=lazy]')) i.loading = 'eager';
      const pending = [...document.querySelectorAll('img')].filter((i) => !i.complete);
      await Promise.all(pending.map((i) => new Promise((r) => { i.onload = i.onerror = r; })));
    }""")
    page.evaluate("""() => {
      const s = document.createElement('style');
      s.textContent = '*,*::before,*::after{animation:none!important;transition:none!important}';
      document.head.appendChild(s);
      // Entrance animations driven from inline styles (Framer Motion and
      // equivalents) leave the hero half-faded in the tile otherwise.
      for (const el of document.querySelectorAll('[style*="opacity"], [style*="filter"], [style*="transform"]')) {
        el.style.setProperty('opacity', '1', 'important');
        el.style.setProperty('filter', 'none', 'important');
      }
      for (const v of document.querySelectorAll('video')) { try { v.pause(); v.currentTime = 0.5; } catch (e) {} }
      // reveal-style entrance animations: force their end state. The hook is
      // whatever the site named it — `.reveal` is common, and this family
      // routinely carries a SECOND one for figures (`.js .reveal, .js
      // .curtain{opacity:0}` … `.js .reveal.in, .js .curtain.in{opacity:1}`)
      // — so the hooks are read out of the page's OWN stylesheets instead of
      // guessed: a class paired with a reveal MARKER in some rule
      // (`.curtain.in`) is a hook. Only elements still in the hidden
      // pre-reveal state are touched, so a tooltip or a resting low-opacity
      // decoration is never forced open. Verified live on a byte-identical
      // page: two `.curtain` figures the scroll-through had not intersected
      // stayed at opacity 0 in one capture and painted in the other — 1.9%
      // on the desktop width, reproducible, and invisible to a `.reveal`-only
      // net. Every capture script carries this block verbatim; they must not
      // drift (test-reveal-net-parity.sh).
      const REVEAL_MARKERS = ['in', 'is-visible', 'in-view', 'inview'];
      const revealHooks = new Map([['reveal', 'in']]);
      const readRules = (rules) => {
        for (const r of rules || []) {
          if (r.cssRules) { readRules(r.cssRules); continue; }
          const sel = r.selectorText;
          if (!sel) continue;
          for (const m of sel.matchAll(/\.([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]+)/g)) {
            if (REVEAL_MARKERS.includes(m[2])) revealHooks.set(m[1], m[2]);
          }
        }
      };
      for (const sheet of document.styleSheets) {
        try { readRules(sheet.cssRules); } catch (e) { /* cross-origin sheet */ }
      }
      for (const [hook, marker] of revealHooks) {
        let nodes = [];
        try { nodes = document.querySelectorAll('.' + hook); } catch (e) { continue; }
        for (const el of nodes) {
          const cs = getComputedStyle(el);
          if (parseFloat(cs.opacity) < 1 || (cs.transform && cs.transform !== 'none')) {
            el.classList.add(marker, 'in', 'is-visible');
          }
        }
      }
      const bar = document.getElementById('wpadminbar'); if (bar) bar.remove();
      document.documentElement.style.marginTop = '0';
    }""")
    page.wait_for_timeout(600)
    page.screenshot(path=str(OUT))  # viewport-sized, deliberately not full_page
    browser.close()
if httpd:
    httpd.shutdown()

size = OUT.stat().st_size
dims = ""
try:
    from PIL import Image
    with Image.open(OUT) as im:
        dims = f"{im.width}x{im.height}"
        if (im.width, im.height) != (1200, 900):
            print(f"screenshot is {dims}, WordPress requires 1200x900", file=sys.stderr)
            sys.exit(1)
except ImportError:
    pass

print(f"screenshot {dims or '1200x900'} ({size // 1024} KB) from {'the live site' if args.wp else front} → {OUT}")
for e in errors[:3]:
    print(f"  console error on the shot page: {e}", file=sys.stderr)
