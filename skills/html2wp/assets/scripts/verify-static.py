#!/usr/bin/env python3
"""Gate A — the Astro build IS the site.

  python3 verify-static.py --original <dir> --dist <dir> [--out report-dir]
      [--threshold 0.006] [--pages a.html,b.html]

Per page, at desktop (1440) and mobile (390) widths:
  - full-page screenshot of original vs dist, pixel-diff ratio must be under
    the threshold (default 0.6% of pixels)
  - no console error on the dist page that the ORIGINAL does not also
    produce (see below)
Plus: every internal link in dist resolves to a file that exists.

Console errors are compared, not counted. This gate used to require ZERO
console errors on dist, which is an assertion about the INPUT's quality
wearing a conversion gate's clothes: a source that ships <link>/<script>
tags pointing at files that were never in the download 404s identically on
both sides, and a byte-faithful conversion of it could not pass. Measured on
a real campaign theme: original 4 console errors / 4 404s, dist the same 4,
pixel diff 0.0% at every width, gate A2 clean — and gate A red. Worse, the
input class this hits is not rare (113 of 188 queued themes reference at
least one missing local asset) and not always fixable by cleaning the input
(29 of them embed a keyless Google Maps loader whose error comes from
Google's own script — nothing local to restore, and deleting the map would
break the pixel comparison that must reproduce it).

So the rule is now the same shape as every other assertion here: a
COMPARISON against the original. An error present on both sides is the
source's own, recorded as `inheritedConsoleErrors` for the conversion report
to disclose; an error only dist produces is breakage the conversion
introduced, and fails the gate. Messages are normalised per side (each side
is served from its own port, so a URL inside a message would otherwise never
match) and compared as SETS: a new KIND of error is a regression, while the
same error firing a different number of times is lazy-load noise this gate
cannot meaningfully judge.

This gate compares RENDERINGS. Its companion, verify-parity.mjs, compares
the markup — together they cover what neither can alone. Under ?raw +
set:html the build no longer re-serialises anything, so a byte difference
is now a real signal rather than parser noise.

Exit 0 = gate passed. 1 = failed, per-page detail in report.json and diff
PNGs next to it. Never proceed to theme generation on a failed gate.
"""

import argparse, functools, json, os, re, sys, threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from net_guard import attach_network_guard  # noqa: E402
from web_assets import refuse_request_path  # noqa: E402

from playwright.sync_api import sync_playwright
from PIL import Image, ImageChops


def serve(directory):
    """A root-relative asset/link path (/live.css) only resolves against a
    real document root — file:// URIs have no such concept and resolve it
    against the filesystem root instead, 404ing everything. Root-relative
    paths are also exactly what a nested page needs to be depth-agnostic
    (blog/x.html referencing /live.css, not ../live.css), so this is the
    correct output to test, not a shortcut to work around — serve it for
    real over loopback HTTP instead of opening it as a bare file.

    It served the WHOLE directory, which is the caller's project: the page
    under test is untrusted markup, and `fetch('/.env')` from it was a read of
    whatever sat beside the site, over an origin the page already had. So the
    handler now answers only for web assets and never for a dotfile.
    """
    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send_head(self):
            # 404 rather than 403: a refusal that distinguishes "exists but you
            # may not have it" from "not here" is a file-existence oracle for
            # the page doing the asking.
            if refuse_request_path(self.path, directory):
                self.send_error(404, "Not Found")
                return None
            return super().send_head()

    handler = functools.partial(QuietHandler, directory=str(directory))
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_port}"

ap = argparse.ArgumentParser()
ap.add_argument("--original", required=True)
ap.add_argument("--dist", required=True)
ap.add_argument("--out", default="verify-static-report")
ap.add_argument("--threshold", type=float, default=0.006, help="empirically: real regressions run 4-100%%; sub-pixel font AA in a text-dense header band tops out ~0.44%% (confirmed via identical DOM box metrics) — 0.6%% keeps a wide margin above noise without hiding anything real")
ap.add_argument("--pages", default="")
# The ONE thing that may legitimately be in the source and not in the build:
# a page the SAME site served at two addresses (about.html + about/index.html),
# which stage 0 merges with proof — identical after normalising relative depth,
# and the surviving address is the one the site's own links point at. Those
# drops are recorded in analysis.json's duplicatePages[], so this gate reads
# that file rather than trusting the build: anything missing that is NOT on
# that list is still a hard failure, by name.
ap.add_argument("--merged", default="", help="analysis.json — its duplicatePages[].dropped are expected to be absent from dist")
# --variance is GONE, deliberately: it waived pages whose chrome was
# canonicalized away, and with every chrome variant now preserved as its own
# template part there is nothing left to waive. Every page must be 1:1.
args = ap.parse_args()

ORIG = Path(args.original).resolve()
DIST = Path(args.dist).resolve()
OUT = Path(args.out).resolve()
OUT.mkdir(parents=True, exist_ok=True)
# rglob, not iterdir: a real site can nest pages (blog/some-article.html) —
# the build preserves that structure in dist/, and a non-recursive scan
# silently drops every one of those pages from both the default page list
# and the link-existence check below.
#
# The list comes from the ORIGINAL, not from dist. Enumerating dist made this
# gate credit itself with coverage it never performed: a build that loses a
# page loses it from the work list too, so the gate compared what survived,
# found it identical, and reported a clean pass over a smaller site. Measured
# across the queue — Astro resolves a duplicate route by keeping one and
# emitting a [WARN], exit code 0, and six sites lost pages that way
# (bigspring-nextjs 79 -> 66, finprox-nextjs 43 -> 29, devgent-nextjs 67 -> 54).
# Nothing anywhere in the pipeline compared the two inventories.
#
# A page present in the source and absent from the build is therefore a hard
# failure of THIS gate, reported by name. The reverse — a page dist invented —
# is reported too: it is rarer and stranger, and silence about it would be the
# same mistake facing the other way.
def _html_under(root):
    return {str(f.relative_to(root)) for f in root.rglob("*") if f.suffix in (".html", ".htm")}


explicit_pages = [p.strip() for p in args.pages.split(",") if p.strip()]
orig_pages, dist_pages = _html_under(ORIG), _html_under(DIST)

merged_away = []
if args.merged:
    _a = json.loads(Path(args.merged).read_text())
    merged_away = sorted(
        d["dropped"] for d in (_a.get("duplicatePages") or []) if d.get("dropped") in orig_pages
    )
    orig_pages -= set(merged_away)

pages = explicit_pages or sorted(orig_pages)

# Tablet is a first-class width, not a nice-to-have: a layout that only
# breaks between 1440 and 390 is exactly the kind nobody sees until a
# visitor does.
WIDTHS = [("desktop", 1440), ("tablet", 820), ("mobile", 390)]
report = {"pages": {}, "links": [], "passed": True}

# Inventory parity, decided before a single screenshot is taken. It costs two
# directory walks and it is the cheapest failure in the whole gate, so it runs
# first: there is no point comparing pixels on 66 pages when 13 are missing.
#
# Skipped when --pages names an explicit subset, because then the caller has
# deliberately asked about part of the site and a partial list is the request,
# not a defect.
if not explicit_pages:
    missing = sorted(orig_pages - dist_pages)
    unexpected = sorted(dist_pages - orig_pages)
    if missing:
        report["missingFromDist"] = missing
        report["passed"] = False
    if unexpected:
        report["notInOriginal"] = unexpected
        report["passed"] = False
    report["inventory"] = {"original": len(orig_pages), "dist": len(dist_pages)}
    if merged_away:
        report["mergedDuplicates"] = merged_away


def settle(page):
    """Same discipline as the delivered-site audit: fonts ready, lazy-load
    forced, full scroll-through, animations killed — screenshot a page that
    has finished becoming itself."""
    page.wait_for_load_state("networkidle")
    page.evaluate("document.fonts && document.fonts.ready")
    page.evaluate("""async () => {
      for (const img of document.querySelectorAll('img[loading=lazy]')) img.loading = 'eager';
    }""")
    page.evaluate("""async () => {
      const h = document.body.scrollHeight;
      for (let y = 0; y < h; y += 700) { window.scrollTo(0, y); await new Promise(r => setTimeout(r, 60)); }
      window.scrollTo(0, 0);
    }""")
    # Waiting for images has to come AFTER the scroll-through, not before.
    # Forcing loading=eager only STARTS a fetch, and decode takes real,
    # variable time; worse, the scroll-through itself makes further images
    # eligible, so any wait done before it is a wait for the wrong set. An
    # image that finishes late either pushes everything below it down by its
    # own height or simply is not painted yet — and two separate page loads
    # (orig vs dist) never cross that finish line at the same instant.
    # Verified live on a byte-identical page: one blog card's lazy image was
    # painted in the original capture and blank in the dist capture, scoring
    # 1.1% on index and 5.0% on about — a pure capture artifact that read as
    # a conversion defect. The earlier pre-scroll wait caught the 92px
    # scrollHeight case and missed this one; polling after the scroll catches
    # both. A deadline rather than an unbounded await, because a genuinely
    # broken image URL must fail loudly instead of hanging the gate.
    deadline = 15000
    waited = 0
    while waited < deadline:
        pending = page.evaluate(
            "() => [...document.querySelectorAll('img')].filter((i) => !i.complete)"
            ".map((i) => i.currentSrc || i.src)"
        )
        if not pending:
            break
        page.wait_for_timeout(250)
        waited += 250
    else:
        print(f"    warn: {len(pending)} image(s) never finished loading: {pending[:3]}")
    # `complete` is necessary but NOT sufficient: it means the bytes arrived,
    # not that a raster exists. A full_page screenshot paints regions far
    # outside the viewport, and Chromium decodes lazily — and drops decodes
    # for offscreen images under memory pressure — so an image that is
    # `complete` can still paint as blank. Verified live: after the
    # post-scroll completeness wait above, index.html still alternated
    # between captures, and the blank side switched from dist to ORIGINAL on
    # the next run, which is the signature of a decode race rather than a
    # load race (a load race would favour the same side every time).
    # HTMLImageElement.decode() resolves only once a paintable raster is
    # ready, so awaiting it for every image removes the race outright.
    page.evaluate("""async () => {
      await Promise.all([...document.querySelectorAll('img')].map(
        (i) => (i.decode ? i.decode().catch(() => {}) : Promise.resolve())));
    }""")
    # A transition that is STILL RUNNING must be allowed to finish before the
    # kill switch below, because `transition:none` freezes it at whatever value
    # it currently holds — it does not jump to the end. A disclosure widget that
    # collapses its panels on init (Alpine's `:style="expanded ? 'max-height: '
    # + scrollHeight + 'px' : 'max-height: 0'"` with `transition-all
    # duration-300`) is mid-collapse for the first 300ms of every page load, so
    # whichever side of a diff got the style injected earlier keeps a taller
    # panel. Verified live on tidy's pricing page: the SAME byte-identical
    # artifacts scored 0.0 on one run and 0.05034 on the next, at mobile only,
    # entirely inside the FAQ accordion — a coin flip that can as easily hide a
    # real defect as invent one. Bounded, so a genuinely infinite animation
    # (a marquee) still reaches the kill switch instead of hanging the gate.
    page.evaluate("""async () => {
      const running = document.getAnimations ? document.getAnimations() : [];
      await Promise.race([
        Promise.all(running.map((a) => a.finished.catch(() => {}))),
        new Promise((r) => setTimeout(r, 1500)),
      ]);
    }""")
    page.evaluate("""() => {
      const s = document.createElement('style');
      // Killing CSS animation/transition does nothing for a Framer-Motion-
      // style entrance effect: those animate an INLINE style attribute
      // directly from JS (opacity/filter/transform), not a CSS class, so
      // a plain override rule never touches them — the element can still
      // be mid-fade at screenshot time, and WHICH elements haven't
      // finished varies slightly between two separate page loads. Verified
      // live: this alone produced a 27% diffRatio, page-wide and diffuse,
      // on an otherwise byte-identical page.
      s.textContent = '*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}';
      document.head.appendChild(s);
      // Forcing opacity/filter globally (a blanket *{opacity:1!important})
      // is one step too broad: it also overrides LEGITIMATE low-opacity
      // decoration set via a CSS class, not animation — verified live: a
      // footer's opacity-2 (2%) noise texture div rendered at full 100%
      // opacity, itself a large new false diff. Scoped instead to elements
      // that carry opacity/filter/transform directly on the INLINE style
      // attribute — that is specifically how JS animation libraries
      // (Framer Motion and equivalents) drive an entrance effect, and is
      // never how a page's own static CSS design sets a resting opacity.
      for (const el of document.querySelectorAll('[style*="opacity"], [style*="filter"], [style*="transform"]')) {
        el.style.setProperty('opacity', '1', 'important');
        el.style.setProperty('filter', 'none', 'important');
      }
      for (const v of document.querySelectorAll('video')) { try { v.pause(); v.currentTime = 0.5; } catch (e) {} }
      // reveal-style entrance animations: force their end state
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
    }""")
    page.wait_for_load_state("load")
    page.wait_for_timeout(600)
    # The LAST thing before the shot: every image holds a paintable raster.
    # The decode() above is necessary and still not sufficient on a very tall
    # page — a full-page capture of 6000px+ makes Chromium decode
    # far-offscreen tiles lazily and drop them again under memory pressure, so
    # an image that decoded during settle can still paint blank in the
    # capture. `decoding='sync'` stops the raster being deferred a second
    # time. Gate B carries the identical step; these two settles must not
    # drift, or a page passes one pixel gate and fails the other on timing.
    page.evaluate("""async () => {
      const imgs = [...document.querySelectorAll('img')];
      for (const i of imgs) i.decoding = 'sync';
      await Promise.all(imgs.map((i) => (i.decode ? i.decode().catch(() => {}) : Promise.resolve())));
    }""")
    page.wait_for_timeout(150)


def collect(entry, key, messages, cap=5):
    """Merge messages into entry[key], unique and capped.

    Each page is visited once per width, so a plain extend() would repeat
    every message three times and bury the distinct ones under the cap.
    """
    if not messages:
        return
    bucket = entry.setdefault(key, [])
    for m in messages:
        if m not in bucket and len(bucket) < cap:
            bucket.append(m)


def norm_console(text, base_url):
    """Make one side's console message comparable with the other's.

    The two sides are served from two throwaway ports, so any message that
    quotes a URL ("Failed to load http://localhost:51445/css/x.css") differs
    between them for a reason that has nothing to do with the conversion.
    Fold each side's own origin — and any other loopback origin that leaked
    in — to a placeholder before comparing.
    """
    t = text.replace(base_url, "{SITE}")
    t = re.sub(r"https?://(?:localhost|127\.0\.0\.1|\[::1\]):\d+", "{SITE}", t)
    return t.strip()


def diff_ratio(a_path, b_path):
    a, b = Image.open(a_path).convert("RGB"), Image.open(b_path).convert("RGB")
    if a.size != b.size:
        # pad the shorter to the taller — height drift is itself a finding,
        # reflected in the differing pixels of the padded band
        w = max(a.width, b.width); h = max(a.height, b.height)
        pa = Image.new("RGB", (w, h), (255, 0, 255)); pa.paste(a, (0, 0))
        pb = Image.new("RGB", (w, h), (255, 0, 255)); pb.paste(b, (0, 0))
        a, b = pa, pb
    d = ImageChops.difference(a, b).convert("L")
    hist = d.histogram()
    changed = sum(hist[16:])  # tolerance: per-channel delta > ~6%
    return changed / (a.width * a.height)


orig_httpd, ORIG_URL = serve(ORIG)
dist_httpd, DIST_URL = serve(DIST)


def _only_local(url, _allowed=(ORIG_URL, DIST_URL)):
    """Everything except the two servers this script started is refused.

    A pixel gate has no reason to reach the network at all: both sides are
    rendered under the same rule, so a blocked font or analytics tag is
    blocked identically in `orig` and `dist` and the comparison stays honest.
    Meanwhile the page under test is markup this script did not write, and an
    unrestricted context let it POST whatever it had read to anywhere —
    which is the half of the problem that serving fewer files does not fix.
    """
    return "only the local verification servers are reachable from this gate"


BLOCKED_REQUESTS = []

with sync_playwright() as p:
    browser = p.chromium.launch()
    for name, width in WIDTHS:
        ctx = browser.new_context(
            viewport={"width": width, "height": 950},
            # A service worker outlives the page and re-issues its requests
            # from a scope the route handler below has already let go.
            service_workers="block",
        )
        attach_network_guard(
            ctx,
            allowed_origins=(ORIG_URL, DIST_URL),
            checker=_only_local,
            allowed_methods=("GET", "HEAD"),
            on_block=lambda request, reason: BLOCKED_REQUESTS.append(
                {"url": request.url[:200], "reason": reason}
            ),
        )
        page = ctx.new_page()
        console_errors = []
        page.on("console", lambda m: console_errors.append(m.text) if m.type == "error" else None)
        for f in pages:
            entry = report["pages"].setdefault(f, {})
            shots = {}
            seen_errors = {}
            for label, base, base_url in (("orig", ORIG, ORIG_URL), ("dist", DIST, DIST_URL)):
                src = base / f
                if not src.exists():
                    entry[name] = {"error": f"{label} missing: {src}"}
                    report["passed"] = False
                    break
                console_errors.clear()
                page.goto(f"{base_url}/{f}")
                settle(page)
                shot = OUT / f"{f}.{name}.{label}.png"
                page.screenshot(path=str(shot), full_page=True)
                shots[label] = shot
                seen_errors[label] = {norm_console(t, base_url) for t in console_errors}
            if len(shots) == 2:
                # Compare, never count — see the module docstring. Only what
                # dist produces and the original does not is the conversion's
                # doing; the intersection is the source's own breakage, kept
                # so the conversion report can disclose it rather than hide it.
                collect(entry, "consoleErrors", sorted(seen_errors["dist"] - seen_errors["orig"]))
                collect(entry, "inheritedConsoleErrors", sorted(seen_errors["dist"] & seen_errors["orig"]))
                if entry.get("consoleErrors"):
                    report["passed"] = False
                ratio = diff_ratio(shots["orig"], shots["dist"])
                ok = ratio <= args.threshold
                first = None
                if not ok:
                    # CONFIRM before failing — gate B carries the identical
                    # step, and these two must not drift. A full-page capture
                    # of a tall page rasterises far outside the viewport, and
                    # Chromium drops those decodes under memory pressure, so
                    # one side paints an image the other does not. Proven on
                    # this pipeline: identical DOM on both sides (same reveal
                    # class, opacity 1, naturalWidth 1024) and a 0.0000
                    # element-level diff of the exact figure the failing band
                    # covered. A dropped raster picks a different side each
                    # run; a real difference does not, so the verdict is the
                    # SECOND measurement and both are recorded.
                    for label, base, base_url in (("orig", ORIG, ORIG_URL), ("dist", DIST, DIST_URL)):
                        page.goto(f"{base_url}/{f}")
                        settle(page)
                        page.screenshot(path=str(shots[label]), full_page=True)
                    first = round(ratio, 5)
                    ratio = diff_ratio(shots["orig"], shots["dist"])
                    ok = ratio <= args.threshold
                entry[name] = {"diffRatio": round(ratio, 5), "ok": ok}
                if first is not None:
                    entry[name]["reCaptured"] = {"firstRatio": first}
                    if ok:
                        entry[name]["status"] = "capture-artifact-not-reproduced"
                        print(f"    note: {f} {name} measured {first} then {round(ratio, 5)} on "
                              f"re-capture — a dropped offscreen raster, not a difference")
                if not ok:
                    report["passed"] = False
                else:
                    for s in shots.values():
                        s.unlink(missing_ok=True)
        ctx.close()
    browser.close()

orig_httpd.shutdown()
dist_httpd.shutdown()

# internal link check on dist. A nested page (blog/x.html) links to its
# siblings with paths resolved relative to ITS OWN directory (../about.html,
# ../blog.html, y.html) — checked by basename alone, "y.html" and
# "../y.html" both look fine even when only one of them is real. Resolve
# each href against the LINKING page's directory before checking existence.
import posixpath


def broken_links(root):
    """Every (page, href) in `root` whose target does not exist there.

    A nested page (blog/x.html) links to its siblings with paths resolved
    against ITS OWN directory (../about.html, y.html) — checked by basename
    alone, "y.html" and "../y.html" both look fine when only one is real, so
    resolve against the linking page's directory first.
    """
    targets = {str(p.relative_to(root)) for p in root.rglob("*") if p.suffix in (".html", ".htm")}
    out = set()
    for f in pages:
        src = root / f
        if not src.exists():
            continue
        page_dir = posixpath.dirname(f)
        for m in re.finditer(r"""<a\b[^>]*href=["']([A-Za-z0-9._/-]+\.html?)(?:[#?][^"']*)?["']""",
                             src.read_text(errors="replace")):
            href = m.group(1)
            # A root-relative href (/about.html) resolves against the ROOT
            # regardless of the linking page's depth — posixpath.join already
            # treats a leading "/" as absolute, but leaves a leading slash
            # the root-relative `targets` set never has.
            resolved = href[1:] if href.startswith("/") else posixpath.normpath(posixpath.join(page_dir, href))
            if resolved not in targets:
                out.add((f, href))
    return out


# Compared against the original, for the same reason the console rule is:
# this asserts what the CONVERSION did, and a link the source itself points at
# nothing is the source's own. Measured across the campaign queue: 19 of 188
# themes (10%) ship a broken internal link — a free template linking a page it
# never included — and every one of them would have failed here for being
# reproduced faithfully. Found by sweeping the gates for this exact shape after
# the same flaw turned up in gate B3, having been fixed in gate A's console
# rule and nowhere else.
dist_broken, orig_broken = broken_links(DIST), broken_links(ORIG)
for f, href in sorted(dist_broken - orig_broken):
    report["links"].append(f"{f} → {href} (missing)")
    report["passed"] = False
for f, href in sorted(dist_broken & orig_broken):
    report.setdefault("inheritedLinks", []).append(f"{f} → {href} (missing in the source too)")

(OUT / "report.json").write_text(json.dumps(report, indent=2))
bad = [f for f, e in report["pages"].items()
       if any(isinstance(v, dict) and v.get("ok") is False for v in e.values()) or e.get("consoleErrors")]
inherited_pages = [f for f, e in report["pages"].items() if e.get("inheritedConsoleErrors")]
print(f"{'GATE A PASSED' if report['passed'] else 'GATE A FAILED'} — {len(pages)} pages × {len(WIDTHS)} widths"
      + (f"; failing: {', '.join(bad[:6])}" if bad else "")
      + (f"; broken links: {len(report['links'])}" if report["links"] else "")
      + (f"; {len(report.get('inheritedLinks', []))} broken link(s) inherited from the source"
         f" — disclose in the conversion report" if report.get("inheritedLinks") else "")
      # Not a failure, but the owner must be told: these errors are in the
      # source and travel into the delivered theme unchanged.
      + (f"; source's own console errors carried through on {len(inherited_pages)} page(s)"
         f" — disclose in the conversion report" if inherited_pages else ""))
sys.exit(0 if report["passed"] else 1)
