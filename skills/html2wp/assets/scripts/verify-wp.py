#!/usr/bin/env python3
"""Gate B/C — the WordPress render IS the dist build, and the site is alive.

  python3 verify-wp.py --dist <dir> --wp <base-url> --manifest <path>
      [--out report-dir] [--threshold 0.006]

Per page (key-mapped: about.html ↔ /about/, index.html ↔ /):
  B1  pixel diff dist vs WP, desktop + mobile (same settle discipline)
  B2  computed-style assertions a screenshot provably cannot catch:
        - marginBlockStart === '0px' on every .wp-site-blocks child
          (the blockGap seam band)
        - no element whose grid-template-columns collapsed to one column
          when dist shows several (zone-wrapper / width-crush class)
  B3  failed-network watch AFTER scrolling — the only way to catch an
      unresolved __CLARA_THEME_URI__ in a lazy-loaded image. Compared
      against dist, not asserted absolutely: a request that fails on both
      sides is a dead reference the SOURCE ships (a <link> to a file that
      was never in the download), and failing the conversion for it grades
      the input's quality instead of the output's — the same reasoning that
      already governs gate A's console rule. Those are reported as
      `inheritedFailedRequests` for the conversion report to disclose; only
      a request WordPress fails and dist does not fails this gate.
  C1  /llms.txt serves 200 text/plain; front page carries exactly one
      <title>, a meta description, and at most one JSON-LD Organization
  C2  the Visual Edit bridge answers on an edit-preview load (plugin alive)
      — deep editor smoke (edit round-trip, idempotent save, form submit)
      remains the operator's checklist in SKILL.md
  C3  blog card->post SEMANTIC fidelity, when manifest.blog.present: live
      post count matches manifest.blog.articles; each article's own <h1>
      (read from the dist build, not the manifest's <title> text) matches
      some live post title; the listing page renders the same card count
      as articles. Pixel diffs can't see this — the listing can render a
      plausible-looking wrong set of cards and still pass B1.
      NOTE: only exercised when the AI's blog-weaving step actually ran;
      untested against a real blog-bearing conversion as of this writing —
      read report.json's "blogFidelity" block on first real use.
  C6  shop card->product SEMANTIC fidelity, when manifest.shop.present: live
      product count matches manifest.shop.products (the DECLARED catalogue,
      never the crawl — a client-side "Load more" listing prerenders only its
      first screen, so a pipeline that counted what it found would build a
      partial catalogue and agree with itself everywhere downstream); each
      product's own <h1> matches some live product name; each product's
      printed price matches what the WooCommerce Store API serves; and the
      listing renders every live product rather than a frozen copy of the
      design's first page. Price is checked because a shop showing the right
      products at the wrong prices passes every structural test there is.

Exit 0 = gates passed; 1 = failed, detail in report.json + kept screenshots.
"""

import argparse, functools, html, json, re, shlex, subprocess, sys, threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from PIL import Image, ImageChops

ap = argparse.ArgumentParser()
ap.add_argument("--dist", required=True)
ap.add_argument("--wp", required=True)
ap.add_argument("--manifest", required=True)
ap.add_argument("--out", default="verify-wp-report")
ap.add_argument("--band-threshold", type=float, default=0.015, help="per-400px-band ceiling, derived from the campaign's worst regression rather than guessed: a nav collapsed from 367px to 184px scored 0.00097 whole-page — green against 0.006 — because the page was 9000px tall. Those 12,571 changed pixels come to 2.18%% inside their own 400px band. 1.5%% sits 3.4x above the ~0.44%% sub-pixel AA ceiling a text-dense band can reach and below the real defect, so it fires on structure, not on font rendering.")
ap.add_argument("--threshold", type=float, default=0.006, help="empirically: real regressions run 4-100%%; sub-pixel font AA in a text-dense header band tops out ~0.44%% (confirmed via identical DOM box metrics) — 0.6%% keeps a wide margin above noise without hiding anything real")
ap.add_argument("--pages", default="")
ap.add_argument("--wp-cli", default="", dest="wp_cli",
                help="command prefix that runs wp-cli against the target site, e.g. 'docker exec clara-test-wp wp --allow-root'. Enables check C2b: every non-article page must hold a NON-EMPTY stored source. Without it that check is reported as NOT RUN, never as passed.")
args = ap.parse_args()

DIST = Path(args.dist).resolve()
WP = args.wp.rstrip("/")
MF = json.loads(Path(args.manifest).read_text())
OUT = Path(args.out).resolve(); OUT.mkdir(parents=True, exist_ok=True)

def serve(directory):
    """The dist side must be served over HTTP, not opened as file://. A
    root-relative asset (/live.css) has no meaning under a file:// URI — it
    resolves against the filesystem root and 404s — so the dist screenshot
    would render unstyled and every page would fail B1 with a diff that says
    nothing about WordPress. Gate A learned this already; this is the same
    fix."""
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(directory)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_port}"


dist_httpd, DIST_URL = serve(DIST)


def volatile_classes(input_dir):
    """Classes the site's own JS toggles. Read from the source rather than
    guessed, so this adapts to any site."""
    out = set()
    root = Path(input_dir)
    if not root.exists():
        return out
    for f in root.rglob("*.js"):
        if any(p in ("node_modules", ".git") for p in f.parts):
            continue
        try:
            js = f.read_text(errors="replace")
        except OSError:
            continue
        for m in re.finditer(r"classList\.(?:toggle|add|remove)\(\s*[\"'`]([^\"'`]+)", js):
            out.update(c for c in m.group(1).split() if c)
    return out


VOLATILE = volatile_classes(MF.get("input", {}).get("dir", ""))


def chrome_state(page):
    """Which JS-toggled classes the chrome is currently wearing."""
    if not VOLATILE:
        return None
    return set(page.evaluate(
        """() => {
          const els = [document.querySelector('header'), document.querySelector('footer')].filter(Boolean);
          return els.flatMap((e) => [...e.classList]);
        }"""
    )) & VOLATILE

page_map = {p["file"]: p["key"] for p in MF["pages"]}
kind_map = {p["file"]: p.get("kind", "page") for p in MF["pages"]}
only = {p.strip() for p in args.pages.split(",") if p.strip()}
# kind "fragment": an .html file in the input that is NOT a document — a
# scrape's partials/header.html, which has no <body> and no <head>. Gate A
# still holds the build to it byte-for-byte (it is a file the source served),
# but stage 4 was told to keep it out of the bundle, so there is no WordPress
# page behind it and every check here would be measuring its absence. Named
# in the manifest rather than inferred, so nothing is skipped silently.
files = [f for f in page_map if (not only or f in only) and kind_map.get(f) != "fragment"]
skipped_fragments = sorted(f for f in page_map if kind_map.get(f) == "fragment")

report = {"pages": {}, "checks": {}, "passed": True}
if skipped_fragments:
    report["skippedFragments"] = skipped_fragments
# Tablet is a first-class width here for the same reason gate A tests it: a
# layout that only breaks between desktop and phone widths is exactly the
# kind nobody sees until a visitor does.
WIDTHS = [("desktop", 1440), ("tablet", 820), ("mobile", 390)]


def headline_text(html_fragment):
    """The words a reader sees, with HTML's own whitespace folding applied.

    A hand-formatted source wraps a long headline across lines, so the <h1>
    holds "…web development\n\t\t\t\t\tprojects" while the browser — and
    therefore the imported post title — reads "…web development projects".
    Comparing the raw text makes C3 report a correct import as a mismapped
    article, which is a false failure on every prettified article page.
    """
    # hotfix (creative-009): entities are DECODED before comparing. WordPress
    # runs the post title through convert_chars/wptexturize, so REST's
    # title.rendered comes back as "Concept Art &#038; Illustrations…" while
    # the source <h1> holds a literal "&". Comparing them raw reports a
    # correctly imported post as unmatched — a false failure on every article
    # whose headline contains an ampersand or a typographic quote.
    return html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html_fragment)).strip())


def url_for(key):
    if key == "front-page":
        return WP + "/"
    if key == "404":
        # the 404 design renders at any missing address; a page named /404/
        # does not exist (the key is a chrome template part)
        return f"{WP}/html2wp-404-preview-x9q/"
    return f"{WP}/{key}/"


def norm_request(url):
    """Key a request URL so the SAME asset compares equal across the two sides.

    The two sides never agree on a request's spelling: they are served from
    different origins, WordPress serves theme assets from
    /wp-content/themes/<slug>/ and appends its own ?ver=, and the dist build
    serves them from the site root. The last two path segments name the file
    without either prefix ("css/jcarousel.css",
    "owl-carousel/owl.carousel.js") — enough to tell two different assets
    apart, and blind to everything that only differs because of where the
    page is being served from.
    """
    segments = [s for s in urlsplit(url).path.split("/") if s]
    return "/".join(segments[-2:]) if segments else url


def _paint_ready(page):
    """The LAST thing before the shot: every image holds a paintable raster.

    An earlier decode() is necessary but still not sufficient on a very tall
    page. A full-page capture of 6000px+ makes Chromium decode far-offscreen
    tiles lazily and drop them again under memory pressure, so an image that
    decoded during settle can still paint blank minutes later in the capture.
    Verified live: one gallery tile came out blank on the WordPress side only,
    at mobile only, with identical page height and a completely healthy live
    page (11/11 images loaded, none zero-box) — and the blank side alternated
    between runs, which is the decode-race signature rather than a load bug.

    `decoding = 'sync'` is what stops the raster being deferred a second time;
    re-awaiting decode() after it is what proves it is there now.
    """
    page.evaluate("""async () => {
      const imgs = [...document.querySelectorAll('img')];
      for (const i of imgs) i.decoding = 'sync';
      await Promise.all(imgs.map((i) => (i.decode ? i.decode().catch(() => {}) : Promise.resolve())));
    }""")
    page.wait_for_timeout(150)


def settle(page):
    page.wait_for_load_state("networkidle")
    page.evaluate("document.fonts && document.fonts.ready")
    # See verify-static.py's settle() for why: setting eager only starts
    # the fetch, decode still takes real variable time, and two separate
    # page loads (dist vs live WP) finishing that at different instants
    # produces a spurious scrollHeight mismatch — nothing wrong on either
    # page, just a race between this script and the browser.
    page.evaluate("""async () => {
      for (const i of document.querySelectorAll('img[loading=lazy]')) i.loading = 'eager';
    }""")
    page.evaluate("""async () => {
      const h = document.body.scrollHeight;
      for (let y = 0; y < h; y += 700) { window.scrollTo(0, y); await new Promise(r => setTimeout(r, 60)); }
      window.scrollTo(0, 0);
    }""")
    # Waiting for images belongs AFTER the scroll-through, and `complete` is
    # not the finish line — same two corrections verify-static.py's settle()
    # carries, and for the same reason: the scroll makes further images
    # eligible, so a wait done before it waits on the wrong set; and `complete`
    # means the bytes arrived, not that a paintable raster exists, which for a
    # full-page screenshot of regions far outside the viewport is a different
    # thing entirely. Left uncorrected here, this gate alternated a ~5% diff
    # between runs on a page whose markup was right.
    deadline, waited, pending = 15000, 0, []
    while waited < deadline:
        pending = page.evaluate(
            "() => [...document.querySelectorAll('img')].filter((i) => !i.complete)"
            ".map((i) => i.currentSrc || i.src)"
        )
        if not pending:
            break
        page.wait_for_timeout(250)
        waited += 250
    if pending:
        print(f"    warn: {len(pending)} image(s) never finished loading: {pending[:3]}")
    page.evaluate("""async () => {
      await Promise.all([...document.querySelectorAll('img')].map(
        (i) => (i.decode ? i.decode().catch(() => {}) : Promise.resolve())));
    }""")
    # Let a running transition FINISH before the kill switch: `transition:none`
    # freezes it where it stands rather than jumping it to the end, so a widget
    # that collapses its panels on init (Alpine + `transition-all duration-300`)
    # photographs at a different height on each side of the diff depending on
    # which page load got the injection first. Same reasoning, same bound, as
    # verify-static.py's settle().
    page.evaluate("""async () => {
      const running = document.getAnimations ? document.getAnimations() : [];
      await Promise.race([
        Promise.all(running.map((a) => a.finished.catch(() => {}))),
        new Promise((r) => setTimeout(r, 1500)),
      ]);
    }""")
    page.evaluate("""() => {
      const s = document.createElement('style');
      s.textContent = '*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}';
      document.head.appendChild(s);
      // Scoped, not a blanket *{opacity:1!important} — that also fights a
      // legitimate low-opacity CSS-class decoration (verified live: a
      // footer's 2%-opacity noise texture rendered at full 100%). Only
      // elements carrying opacity/filter directly on the INLINE style
      // attribute are what a JS animation library (Framer Motion and
      // equivalents) actually drives — never how static CSS design sets a
      // resting opacity.
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
    # Same wait as gate A's settle(): the two must not drift, or a page can
    # pass one pixel gate and fail the other for timing reasons alone.
    page.wait_for_load_state("load")
    page.wait_for_timeout(600)
    _paint_ready(page)


def _aligned(a_path, b_path):
    a, b = Image.open(a_path).convert("RGB"), Image.open(b_path).convert("RGB")
    if a.size != b.size:
        w, h = max(a.width, b.width), max(a.height, b.height)
        pa = Image.new("RGB", (w, h), (255, 0, 255)); pa.paste(a, (0, 0))
        pb = Image.new("RGB", (w, h), (255, 0, 255)); pb.paste(b, (0, 0))
        a, b = pa, pb
    return a, b


def diff_ratio(a_path, b_path):
    a, b = _aligned(a_path, b_path)
    d = ImageChops.difference(a, b).convert("L")
    return sum(d.histogram()[16:]) / (a.width * a.height)


def worst_band(a_path, b_path, band_px=400):
    """The same difference, measured per horizontal band instead of per page.

    A whole-page ratio is a denominator problem: a page is as tall as its
    content, so the SAME defect scores differently depending on how much
    unrelated page sits below it. The campaign's worst single regression was
    exactly this — the front page's nav rebuilt wrong, 367px of chrome
    collapsed to 184px, and the whole-page ratio came to 0.00097 against a
    0.006 threshold. Green. The header was destroyed and the number said the
    page was fine, because the page was 9000px tall.

    Banding removes the denominator. A defect confined to a band is measured
    against that band, so a broken header is a broken header whether the page
    below it is short or endless.

    Returns (top_y, ratio) of the worst band.
    """
    a, b = _aligned(a_path, b_path)
    d = ImageChops.difference(a, b).convert("L")
    worst = (0, 0.0)
    for top in range(0, d.height, band_px):
        bottom = min(top + band_px, d.height)
        band = d.crop((0, top, d.width, bottom))
        px = band.width * band.height
        if not px:
            continue
        r = sum(band.histogram()[16:]) / px
        if r > worst[1]:
            worst = (top, r)
    return worst


def preflight(browser):
    """B0 — is this WordPress actually ROUTING? Everything after this point
    compares a dist page against whatever the live site returns, and a site
    that is not routing still returns something for every URL: Apache's own
    404 (mod_rewrite present but .htaccess has no rules — WordPress writes
    empty BEGIN/END markers whenever got_mod_rewrite() is false, which it is
    behind a proxy or a non-standard Apache build, and it REWRITES the file
    on every theme/plugin install, so a hand-written .htaccess silently
    reverts mid-run), or the FRONT PAGE for every path on plain permalinks.

    Both produce a full page of pixels and a 200-looking pipeline. Without
    this check the gate reports a huge diff on 19 of 20 pages and the
    conversion gets blamed for an environment fault. Verified live: a Kinto
    run 404'd every subpage after wp-cli regenerated .htaccess during theme
    install.
    """
    ctx = browser.new_context(viewport={"width": 1440, "height": 950})
    page = ctx.new_page()
    # An article becomes a Post whose real permalink is derived from its
    # title, not from the source key. Probing /blog-single/ therefore returns
    # a legitimate 404 and misdiagnoses routing whenever --pages contains
    # only a listing plus its article. Article URLs are resolved later from
    # the REST API; B0 needs an ordinary Page.
    probe = next((page_map[f] for f in files
                  if page_map[f] not in ("front-page", "404") and kind_map.get(f) != "article"), None)
    try:
        if probe is None:
            return None
        r = page.goto(url_for(probe))
        if r is None or r.status >= 400:
            return (f"{url_for(probe)} returned HTTP {r.status if r else 'no response'} — WordPress is not "
                    "serving pretty permalinks. Check .htaccess (WordPress rewrites it on theme/plugin "
                    "install and leaves the markers EMPTY when got_mod_rewrite() is false) and mod_rewrite.")
        subpage = page.evaluate("document.body.innerHTML.length")
        page.goto(WP + "/")
        front = page.evaluate("document.body.innerHTML.length")
        if subpage == front:
            return (f"{url_for(probe)} served a byte-identical body to the front page — the classic "
                    "plain-permalinks symptom (HTTP 200 everywhere, every path IS the homepage). "
                    "Set a permalink structure before running this gate.")
        return None
    finally:
        ctx.close()


with sync_playwright() as p:
    browser = p.chromium.launch()

    # ---- B0: routing preflight (before any pixel work) ----
    routing = preflight(browser)
    if routing:
        report["checks"]["routing"] = {"ok": False, "detail": routing}
        report["passed"] = False
        (OUT / "report.json").write_text(json.dumps(report, indent=2))
        print(f"GATE B/C ABORTED — WordPress routing is broken, not the conversion:\n  {routing}")
        browser.close()
        sys.exit(1)
    report["checks"]["routing"] = {"ok": True}

    # An article page does NOT live at /{key}/ once the blog stage has run:
    # it became a WordPress Post, and a Post's slug comes from its TITLE, not
    # from the source filename ("journaling-for-anxiety.html" publishes as
    # /journaling-as-a-tool-for-managing-anxiety/). Guessing /{key}/ probes a
    # URL that does not exist, so the gate reports a 404 as a failed request
    # and a ~78% pixel diff against WordPress's 404 design — nine fabricated
    # failures on a conversion that is entirely correct. Resolve each
    # article's real address from WordPress itself, by matching the built
    # page's own <h1> to a live post title.
    article_urls = {}
    if (MF.get("blog") or {}).get("present"):
        _ctx = browser.new_context()
        _pg = _ctx.new_page()
        _resp = _pg.request.get(WP + "/wp-json/wp/v2/posts?per_page=100&status=publish")
        _posts = _resp.json() if _resp.ok else []
        for f in [x for x in files if kind_map.get(x) == "article"]:
            src = DIST / f
            if not src.exists():
                continue
            m = re.search(r"<h1[^>]*>(.*?)</h1>", src.read_text(errors="replace"), re.S)
            headline = headline_text(m.group(1)) if m else ""
            # No <h1> AT ALL is the same situation as a repeated shell
            # label, and commoner: a blog template can render the post
            # title as <h2> and reserve <h1> for nothing. Without this
            # the headline stays empty, no post ever matches, and every
            # article page fails the gate on a site that imported
            # perfectly.
            if not headline or headline.lower() in {"blog details", "article details"}:
                page_meta = next((p for p in MF.get("pages", []) if p.get("file") == f), {})
                fallback = re.split(r"\s+(?:\||-|—)\s+", str(page_meta.get("title", "")), maxsplit=1)[0].strip()
                if fallback:
                    headline = fallback
            for p_ in _posts:
                title = headline_text(p_["title"]["rendered"])
                if headline and (headline in title or title in headline):
                    article_urls[f] = p_["link"]
                    break
        _ctx.close()

    # ---- B1 + B3 ----
    for name, width in WIDTHS:
        ctx = browser.new_context(viewport={"width": width, "height": 950})
        page = ctx.new_page()
        failed_requests = []
        page.on("requestfailed", lambda r: failed_requests.append(r.url))
        page.on("response", lambda r: failed_requests.append(r.url) if r.status >= 400 else None)
        for f in files:
            key = page_map[f]
            entry = report["pages"].setdefault(f, {})
            shots = {}
            states = {}

            if kind_map.get(f) == "article":
                # A Post renders through templates/single.html — a DESIGNED
                # template, deliberately not a byte copy of the static article
                # page it came from. Pixel-diffing the two asks the site to
                # stop being a blog, exactly as for the listing above. The
                # article's semantic fidelity (right count, right headline,
                # nothing dropped) is gate C3's job. What still applies here
                # is B3: the live post must load with no failed requests and
                # no unresolved portability token, so run that at its REAL
                # address and skip only the comparison.
                wp_url = article_urls.get(f)
                if not wp_url:
                    entry[name] = {"ok": False, "status": "no live post matches this article's <h1>"}
                    report["passed"] = False
                    continue
                failed_requests.clear()
                page.goto(wp_url)
                settle(page)
                bad = [u for u in failed_requests if "favicon" not in u]
                if bad:
                    entry.setdefault("failedRequests", []).extend(bad[:5])
                    report["passed"] = False
                if "__CLARA_" in page.content():
                    entry["unresolvedTokens"] = True
                    report["passed"] = False
                entry[name] = {"ok": None, "status": "post-via-single-template", "wpUrl": wp_url}
                continue

            seen_failed = {}
            for label, target in (("dist", f"{DIST_URL}/{f}"), ("wp", url_for(key))):
                failed_requests.clear()
                page.goto(target)
                settle(page)
                shot = OUT / f"{key}.{name}.{label}.png"
                page.screenshot(path=str(shot), full_page=True)
                shots[label] = shot
                states[label] = chrome_state(page)
                seen_failed[label] = {norm_request(u) for u in failed_requests
                                      if "favicon" not in u
                                      and not (key == "404" and "404-preview" in u)}
                if label == "wp":
                    # unresolved portability tokens in the served HTML
                    # page_html, not html — that name is the stdlib module
                    # this script imports for entity decoding, and a module-
                    # scope reassignment here shadowed it for the rest of the
                    # run (crashing the menus-wired check).
                    page_html = page.content()
                    if "__CLARA_" in page_html:
                        entry["unresolvedTokens"] = True
                        report["passed"] = False
            # B3, as a COMPARISON — the same shape gate A's console rule
            # already has. A request that fails on BOTH sides is the source's
            # own dead reference travelling into the theme unchanged; it is
            # recorded for the conversion report to disclose, not failed on.
            # Only what WordPress fails to load and dist does not is breakage
            # this conversion introduced.
            new_bad = seen_failed["wp"] - seen_failed["dist"]
            if new_bad:
                entry["failedRequests"] = sorted(set(entry.get("failedRequests", [])) | new_bad)
                report["passed"] = False
            inherited = seen_failed["wp"] & seen_failed["dist"]
            if inherited:
                entry["inheritedFailedRequests"] = sorted(
                    set(entry.get("inheritedFailedRequests", [])) | inherited)
            ratio = diff_ratio(shots["dist"], shots["wp"])
            ok = ratio <= args.threshold
            # Every page the blog stage wired is dynamic, not just the one
            # blog.listing names. A paginated archive ships as several files
            # (blog/index.html + blog/page/2/index.html on a directory-routed
            # export), each one hosting its own [wp-posts] token — and holding
            # the second one to pixel parity with its frozen snapshot fails a
            # conversion for the very thing the blog stage exists to do.
            #
            # Wave 1 produced two ways of saying which pages those are, and
            # both are honoured rather than one being picked: dexler's manifest
            # marks them kind "listing", bigspring's lists them in
            # blog.listingPages. Either convention alone would silently fail
            # sites written the other way, and the union costs nothing — a page
            # that is still static is kind "page", is in no list, and stays
            # under the pixel rule.
            blog_mf = MF.get("blog") or {}
            listing = blog_mf.get("listing")
            kinds = {p.get("file"): p.get("kind") for p in MF.get("pages", [])}
            listing_files = {listing} | set(blog_mf.get("listingPages") or [])
            dynamic = f in listing_files or kinds.get(f) == "listing"
            # The shop's own pages, for the same reason one layer over — and a
            # stronger one. A product page is no longer a page: it is a
            # WooCommerce record, and its buy region is a REAL cart form, so
            # the markup necessarily gains a <form class="variations_form
            # cart">, hidden add-to-cart/product_id/variation_id inputs and a
            # visually-hidden proxy <select> that Woo's own script drives.
            # The listing gains the rest of the catalogue: the source paginated
            # client-side ("showing 8 of 12"), the live shop shows all twelve,
            # and it is taller for it. Both differences are the conversion
            # WORKING.
            #
            # So these pages are measured and reported, never failed on
            # identity. What still fails them is BREAKAGE — a request the live
            # page loses and dist does not, a grid collapsed to one column, the
            # blockGap seam — and correctness is C6's job: right products,
            # right names, right prices, right count. "Nothing broken, and it
            # looks right" is the standard here; byte-equality with a snapshot
            # of a shop that could not take money is not.
            shop_mf = MF.get("shop") or {}
            shop_files = {shop_mf.get("listing")} | set(shop_mf.get("listingPages") or [])
            commerce_files = {shop_mf.get("cartPage"), shop_mf.get("checkoutPage")} - {None}
            wc_owned = ""
            if shop_mf.get("present"):
                if f in shop_files or kinds.get(f) == "shop":
                    wc_owned = "woocommerce-listing"
                elif kinds.get(f) == "product":
                    wc_owned = "woocommerce-product"
                elif f in commerce_files:
                    # The cart and the checkout are not converted AT ALL — they
                    # are excluded from the bundle and redirected to
                    # WooCommerce's own, which is the decision the whole stage
                    # rests on: the source's were a simulation, and a
                    # pixel-perfect copy of a checkout that takes no money is
                    # not a checkout. Comparing Woo's cart against a React demo
                    # measures nothing.
                    wc_owned = "woocommerce-owned-page"
            if not ok and wc_owned:
                entry[name] = {"diffRatio": round(ratio, 5), "ok": None, "status": wc_owned}
            elif not ok and blog_mf.get("present") and dynamic:
                # The listing is DRIVEN BY POSTS now — that is the entire
                # point of the blog stage. It cannot match its own static
                # snapshot, and should not: the cards come from whatever the
                # owner has published. C3 checks that it is showing the right
                # posts; B1 pixel-matching it against the frozen source would
                # only be asking the site to stop being a blog.
                entry[name] = {"diffRatio": round(ratio, 5), "ok": None, "status": "dynamic-listing"}
            elif not ok and states.get("dist") is not None and states["dist"] != states.get("wp"):
                # A static export freezes whatever runtime state each page was
                # snapshotted in, and the site's own JS does not necessarily
                # re-normalise it on load. So the DIST side can sit in a
                # scrolled state this page never shows a fresh visitor, while
                # WordPress renders the at-rest chrome stage 2.5 captured.
                # That is the export being internally inconsistent, not the
                # conversion drifting — reported by name, with the evidence,
                # rather than as an unexplained percentage.
                entry[name] = {
                    "diffRatio": round(ratio, 5), "ok": None,
                    "status": "export-scroll-state",
                    "distChrome": sorted(states["dist"]), "wpChrome": sorted(states.get("wp") or []),
                }
            else:
                band_top, band_r = worst_band(shots["dist"], shots["wp"])
                band_ok = band_r <= args.band_threshold
                confirm = None
                if not (ok and band_ok):
                    # CONFIRM before failing. A full-page screenshot of a very
                    # tall page asks Chromium to rasterise far outside the
                    # viewport, and it drops those decodes under memory
                    # pressure — so one side paints an image the other does
                    # not, at the same band, to the same digits, on a page
                    # whose DOM is provably identical on both sides (probed:
                    # same reveal class, opacity 1, naturalWidth 1024, and a
                    # 0.0000 element-level diff of the very figure the band
                    # covers). settle() already forces decoding='sync' and
                    # awaits decode(); this is what survives that.
                    #
                    # The two classes separate cleanly by REPETITION: a
                    # dropped raster lands on whichever side lost the race
                    # that time, a real difference lands every time. So the
                    # page is captured again and the verdict is the SECOND
                    # measurement, with both recorded. Cheap — it only runs
                    # for a page that already failed — and it is the same
                    # reasoning the settle comments arrive at by hand.
                    for label, target in (("dist", f"{DIST_URL}/{f}"), ("wp", url_for(key))):
                        page.goto(target)
                        settle(page)
                        page.screenshot(path=str(shots[label]), full_page=True)
                    ratio2 = diff_ratio(shots["dist"], shots["wp"])
                    band_top2, band_r2 = worst_band(shots["dist"], shots["wp"])
                    confirm = {"firstRatio": round(ratio, 5),
                               "firstBand": {"topPx": band_top, "ratio": round(band_r, 5)}}
                    ratio, band_top, band_r = ratio2, band_top2, band_r2
                    ok = ratio <= args.threshold
                    band_ok = band_r <= args.band_threshold
                entry[name] = {"diffRatio": round(ratio, 5), "ok": ok and band_ok,
                               "worstBand": {"topPx": band_top, "ratio": round(band_r, 5)}}
                if confirm:
                    entry[name]["reCaptured"] = confirm
                    if ok and band_ok:
                        entry[name]["status"] = "capture-artifact-not-reproduced"
                        print(f"    note: {f} {name} measured {confirm['firstRatio']} then "
                              f"{round(ratio, 5)} on re-capture — a dropped offscreen raster, not a difference")
                if not ok or not band_ok:
                    report["passed"] = False
                    if ok and not band_ok:
                        # Worth saying out loud: the page as a whole matched and
                        # a band did not. That is the shape of every regression
                        # this measure exists to catch, and reading it as "the
                        # page is fine" is what let one ship.
                        entry[name]["status"] = "band-regression-hidden-by-page-height"
                elif band_ok:
                    # a page that matched needs no screenshots kept
                    for s in shots.values():
                        s.unlink(missing_ok=True)
        ctx.close()

    # ---- B2: computed-style assertions on a representative subpage + front ----
    ctx = browser.new_context(viewport={"width": 1440, "height": 950})
    page = ctx.new_page()
    picks = [f for f in files if page_map[f] == "front-page"] + [f for f in files if page_map[f] != "front-page"][:2]
    # What the DESIGN itself puts on its top-level elements, measured on the
    # dist build. A margin WordPress ADDED is a seam; a margin the site
    # already had is the design — and a real site had a 56px `mt-14` on its
    # nav that this check reported as a WordPress seam on every page.
    #
    # One value per TAG NAME collapses every sibling of the same tag and the
    # last one wins. A one-page design is a run of <section>s, so a single
    # section carrying a deliberate margin — agency's
    # `section.newsletter-section` overlaps the band above it by -160px — got
    # compared against some other section's 0px and reported as a WordPress
    # seam, on a page whose live render is byte-identical there (same top,
    # same height, 0.0% at all three widths). So the map holds the SET of
    # margins the design uses per tag: a value the design never puts on that
    # tag anywhere is still a seam, which is the case this check exists for.
    DESIGN_MARGINS = """() => {
      const out = {};
      for (const el of document.body.children) {
        if (!el.tagName || el.hasAttribute('hidden')) continue;
        (out[el.tagName] = out[el.tagName] || []).push(getComputedStyle(el).marginBlockStart);
      }
      return out;
    }"""
    for f in picks:
        page.goto(f"{DIST_URL}/{f}"); settle(page)
        design_margin = page.evaluate(DESIGN_MARGINS)
        page.goto(url_for(page_map[f])); settle(page)
        checks = page.evaluate("""(design) => {
          const out = { blockGapSeam: [], collapsedGrids: [] };
          const rootKids = document.querySelectorAll('.wp-site-blocks > *');
          for (const el of rootKids) {
            const m = getComputedStyle(el).marginBlockStart;
            if (!m || m === '0px') continue;
            // Only flag what the design did NOT already have — anywhere on
            // the page, on that tag. See DESIGN_MARGINS.
            const had = design[el.tagName] || [];
            if (had.includes(m)) continue;
            out.blockGapSeam.push(el.tagName + ':' + m + ' (design had ' + (had.join('/') || 'none') + ')');
          }
          for (const el of document.querySelectorAll('*')) {
            const cs = getComputedStyle(el);
            if (cs.display === 'grid') {
              const cols = cs.gridTemplateColumns.split(' ').length;
              const kids = el.children.length;
              if (kids >= 3 && cols === 1 && el.clientWidth > 900) {
                out.collapsedGrids.push((el.className || el.tagName).toString().slice(0, 60));
              }
            }
          }
          return out;
        }""", design_margin)
        report["checks"][page_map[f]] = checks
        if checks["blockGapSeam"] or checks["collapsedGrids"]:
            report["passed"] = False

    # ---- C1: SEO surface ----
    llms = page.request.get(WP + "/llms.txt")
    report["checks"]["llms"] = {"status": llms.status, "ok": llms.status == 200 and llms.text().startswith("#")}
    if not report["checks"]["llms"]["ok"]:
        report["passed"] = False
    page.goto(WP + "/"); settle(page)
    page_html = page.content()  # not `html` — that's the imported stdlib module
    # Count only titles in the HEAD. An <svg> carries its own <title> as the
    # chart's or icon's accessible name — real content, deliberately
    # preserved — and counting those reported "2 titles" on a page that has
    # exactly one.
    head_html = page_html[: page_html.find("</head>")] if "</head>" in page_html else page_html
    titles = len(re.findall(r"<title[\s>]", head_html))
    report["checks"]["front"] = {
        "titles": titles,
        "metaDescription": bool(re.search(r'<meta[^>]+name="description"', page_html)),
        "jsonld": len(re.findall(r"application/ld\+json", page_html)),
    }
    if titles != 1:
        report["passed"] = False

    # ---- C2: bridge presence on an edit preview (unauthenticated probe:
    # the preview requires login, so only assert the URL doesn't 500) ----
    prev = page.request.get(WP + "/?clara_edit=1")
    report["checks"]["editPreviewHttp"] = prev.status
    if prev.status >= 500:
        report["passed"] = False

    # ---- C2b: every page actually HAS a stored, editable source ----
    # The hole this closes: a page can render perfectly and hold no editable
    # source at all. A generated theme ships its front page as a static PHP
    # pattern too, so if the import refuses the source (a shape-guard mismatch,
    # historically), WordPress still paints the right pixels from the pattern
    # and B1 passes clean — while the one thing the product sells, being able
    # to click and edit that page, is silently absent. Verified live: a front
    # page imported as ZERO BYTES and every other check in this file stayed
    # green. Rendering is not evidence of editability; only the stored source
    # is, so ask for it directly.
    # The sources live in options, and there is no unauthenticated way to read
    # them, so this needs wp-cli. Pass --wp-cli="docker exec <ct> wp --allow-root"
    # (or any equivalent prefix). Without it the check is recorded as NOT RUN
    # rather than counted as a pass — an unasserted check that reports green is
    # how this hole stayed open in the first place.
    if args.wp_cli:
        # A page the conversion deliberately does not bundle has no stored
        # source to find, and demanding one fails a correct conversion. Three
        # kinds qualify, each for its own reason: an article became a Post, a
        # product became a WooCommerce product (both would otherwise have two
        # things claiming one slug), and the cart and checkout were handed to
        # WooCommerce whole — the conversion ships neither and redirects to
        # Woo's own instead.
        _shop = MF.get("shop") or {}
        _commerce = {_shop.get("cartPage"), _shop.get("checkoutPage")} - {None}
        want = [k for f, k in page_map.items()
                if kind_map.get(f) not in ("article", "fragment", "product") and f not in _commerce]
        # No shell escaping here on purpose: this is passed as an argv
        # element, not through a shell, so backslash-escaping the quotes
        # would put literal backslashes into the PHP source and it would
        # parse-error into an empty result — which reads as "every page is
        # missing a source" and fails the gate for the wrong reason.
        # Ask the PLUGIN where a source lives; never rebuild the option name
        # here. From plugin v1.15 the name is scoped per theme
        # (clara_ve_source__{theme}__{key}), so the hand-built
        # "clara_ve_source__" . $k became a name nothing writes: this check
        # reported all nine pages empty on a conversion whose sources were
        # 54 KB and rendering correctly on screen. The failure mode in the
        # other direction is worse — a hand-built name that happens to match
        # the LEGACY option would report green off another theme's data. The
        # store's own accessor is the only thing that stays right across a
        # storage change, which is exactly the kind of change that already
        # happened once.
        php = (
            'foreach ( ' + json.dumps(want) + ' as $k ) {'
            # Without the plugin there is no store, and the fallback has to
            # spell the name the THEME writes — which is scoped by stylesheet
            # (clara_ve_source__{theme}__{key}), because two converted themes
            # on one install would otherwise share one set of sources. The
            # unscoped name is the LEGACY one; reading it on a standalone
            # theme reported all 22 pages empty while their sources were 3 KB
            # each and rendering correctly on screen.
            ' $v = class_exists( "Clara_VE_Source_Store" )'
            ' ? Clara_VE_Source_Store::get_current_source( $k )'
            ' : ( ( $t = get_option( "clara_ve_source__" . sanitize_key( get_stylesheet() ) . "__" . $k, "" ) )'
            ' ? $t'
            ' : ( ( "front-page" === $k ) ? get_option( "clara_ve_front_source", "" )'
            ' : get_option( "clara_ve_source__" . $k, "" ) ) );'
            ' echo $k . "=" . strlen( (string) $v ) . "\\n"; }'
        )
        # timeout: a wp-cli that hangs (a WordPress waiting on a DB that is
        # not answering) used to hang the whole gate with no output. The
        # return code is still not checked on purpose — an empty parse is
        # handled below and is a better failure here than an exception.
        out = subprocess.run(shlex.split(args.wp_cli) + ["eval", php],
                             capture_output=True, text=True, timeout=120)
        sizes = {}
        for line in out.stdout.splitlines():
            if "=" in line:
                k, _, n = line.rpartition("=")
                if n.strip().isdigit():
                    sizes[k] = int(n)
        empty = sorted(k for k, n in sizes.items() if n == 0)
        missing = sorted(k for k in want if k not in sizes)
        report["checks"]["storedSources"] = {"checked": len(sizes), "empty": empty, "missing": missing}
        if empty or missing or not sizes:
            report["passed"] = False
    else:
        report["checks"]["storedSources"] = {
            "status": "NOT RUN — pass --wp-cli=\"<prefix>\" to assert every page has an editable source"
        }

    # ---- C3: blog card->post fidelity ----
    # Pixel diffs can't see this: the listing page LOOKS right (the [wp-posts]
    # token renders something), but the semantic question B1 never asks is
    # whether it renders the RIGHT somethings — one post per source article,
    # matching headlines, none silently dropped by a pagination default or a
    # weaving mistake. Checked two ways: live post COUNT against the manifest
    # (catches drops), and each article's own <h1> from the Astro dist output
    # against every live post title (catches a mismapped or renamed one) —
    # the h1 is read from the built page itself, not the manifest's <title>
    # text, because <title> commonly carries a " | Site Name" suffix the
    # post_title never has.
    blog = MF.get("blog") or {}
    if blog.get("present"):
        articles = blog.get("articles") or []
        posts_resp = page.request.get(WP + "/wp-json/wp/v2/posts?per_page=100&status=publish")
        live_posts = posts_resp.json() if posts_resp.ok else []
        live_titles = [headline_text(p["title"]["rendered"]) for p in live_posts]
        check = {"expected": len(articles), "live": len(live_posts), "unmatched": []}
        if len(live_posts) != len(articles):
            report["passed"] = False
        for f in articles:
            src = DIST / f
            if not src.exists():
                check["unmatched"].append(f"{f}: not in dist")
                report["passed"] = False
                continue
            m = re.search(r"<h1[^>]*>(.*?)</h1>", src.read_text(errors="replace"), re.S)
            headline = headline_text(m.group(1)) if m else ""
            # Some documentation-style blog exports use one structural
            # headline (for example "Blog Details") on every article while
            # the unique article title lives in the listing card and the
            # document <title>. Treat that repeated shell label as a source
            # limitation and use the manifest's page title for the semantic
            # post mapping; do not force the importer to rename real posts to
            # the generic shell label just to satisfy this check. A page with
            # NO <h1> at all is the same case reached from the other side.
            if not headline or headline.lower() in {"blog details", "article details"}:
                page_meta = next((p for p in MF.get("pages", []) if p.get("file") == f), {})
                fallback = re.split(r"\s+(?:\||-|—)\s+", str(page_meta.get("title", "")), maxsplit=1)[0].strip()
                if fallback:
                    headline = fallback
            if not headline:
                check["unmatched"].append(f"{f}: no <h1> found to match against")
                continue
            if not any(headline in t or t in headline for t in live_titles):
                check["unmatched"].append(f"{f}: headline \"{headline}\" matches no live post title")
                report["passed"] = False
        # the listing page itself must show the same count as cards, not just
        # exist in the database with the right count. Weak on its own — a
        # listing that still carries its ORIGINAL static cards (never wired
        # to [wp-posts] at all) can coincidentally match the right count and
        # pass this alone. It is a supplementary signal; the count+title
        # checks above are the ones that actually prove the cards are live.
        listing_key = next((p["key"] for p in MF["pages"] if p["file"] == blog.get("listing")), None)
        if listing_key:
            container = blog.get("cardContainer")
            # Compare against how many cards the ORIGINAL listing showed, not
            # against the article count. A designed listing routinely shows a
            # subset — the reference site ships ten articles and a nine-card
            # grid — so measuring against the article count fails a listing
            # that is faithfully reproducing its own design.
            expected_cards = None
            if container:
                dist_listing = DIST / blog["listing"]
                if dist_listing.exists():
                    page.goto(f"{DIST_URL}/{blog['listing']}"); settle(page)
                    expected_cards = page.locator(f"{container} > *").count()
                page.goto(url_for(listing_key)); settle(page)
                rendered = page.locator(f"{container} > *").count()
                check["renderedCards"] = rendered
                check["cardsInSource"] = expected_cards
                # A live listing cannot render more cards than there are posts.
                # Templates routinely ship a listing full of placeholder cards
                # that all link to the ONE article page they include (aelen:
                # six cards, one blog-post.html), and the rule that such a
                # listing gets wired anyway is deliberate — one live card beats
                # six leading nowhere. Demanding the source's count there fails
                # a correctly wired blog for having told the truth, and the
                # only way to "fix" it would be to invent the five missing
                # articles. So the expectation is the source's count CAPPED by
                # what exists; a listing that still shows its original static
                # cards is still caught, because its count then exceeds the
                # post count instead of matching it.
                if expected_cards is not None:
                    expect_now = min(expected_cards, len(live_posts))
                    check["cardsExpected"] = expect_now
                    if rendered != expect_now:
                        report["passed"] = False
        report["checks"]["blogFidelity"] = check

    # ---- C6: shop card->product fidelity ----
    # C3's argument, one post type over, plus the two questions only a shop
    # raises.
    #
    # The count is measured against the DECLARED catalogue rather than against
    # anything the crawl produced, and that is the whole point of the check. A
    # listing that pages client-side ("Load more") prerenders with its first
    # screen of products linked and no others, so a pipeline that trusted what
    # it found would build eight products out of twelve, report eight, and
    # agree with itself at every later step. Nothing downstream knows the real
    # number; the manifest does.
    #
    # And a price is checked, because a shop that renders the right products at
    # the wrong prices passes every structural test there is. It is the one
    # value on the page where being plausibly wrong costs money.
    shop = MF.get("shop") or {}
    if shop.get("present"):
        declared = shop.get("products") or []
        prod_resp = page.request.get(WP + "/wp-json/wc/store/v1/products?per_page=100")
        live_products = prod_resp.json() if prod_resp.ok else []
        check = {"expected": len(declared), "live": len(live_products), "unmatched": [], "priceMismatches": []}
        if not prod_resp.ok:
            # The Store API is public and needs no key; its absence means
            # WooCommerce is not active, which for a shop conversion is a
            # failed gate rather than a skipped one.
            check["status"] = ("WooCommerce Store API did not answer (%s) — WooCommerce is not active on the target, "
                               "so no product was imported" % prod_resp.status)
            report["passed"] = False
        if len(live_products) != len(declared):
            report["passed"] = False
        live_names = [headline_text(p.get("name", "")) for p in live_products]
        # Live prices by name, in minor units per the Store API's own
        # price_range/prices contract.
        live_price_by_name = {}
        for p in live_products:
            prices = p.get("prices") or {}
            minor = prices.get("price")
            try:
                dp = int(prices.get("currency_minor_unit", 2))
                live_price_by_name[headline_text(p.get("name", ""))] = round(int(minor) / (10 ** dp), 2)
            except (TypeError, ValueError):
                pass
        for f in declared:
            src = DIST / f
            if not src.exists():
                check["unmatched"].append(f"{f}: not in dist")
                report["passed"] = False
                continue
            raw = src.read_text(errors="replace")
            m = re.search(r"<h1[^>]*>(.*?)</h1>", raw, re.S)
            name = headline_text(m.group(1)) if m else ""
            if not name:
                check["unmatched"].append(f"{f}: no <h1> found to match against")
                continue
            match = next((t for t in live_names if name in t or t in name), None)
            if match is None:
                check["unmatched"].append(f'{f}: product name "{name}" matches no live product')
                report["passed"] = False
                continue
            # The price the design PRINTS, against the price WooCommerce
            # serves. Read from the named price region so a "free shipping over
            # $200" line elsewhere on the page cannot be mistaken for it.
            region = raw
            price_sel = shop.get("productPrice") or ""
            if price_sel:
                cls = price_sel.split(".", 1)[1].replace(".", " ") if "." in price_sel else ""
                mreg = re.search(
                    r"<[a-z0-9]+[^>]*class=\"[^\"]*%s[^\"]*\"[^>]*>(.*?)</[a-z0-9]+>" % re.escape(cls.split()[0]),
                    raw, re.S | re.I) if cls else None
                if mreg:
                    region = mreg.group(1)
            amounts = re.findall(r"[\d][\d\s.,]*", re.sub(r"<[^>]*>", " ", region))
            parsed = []
            for a in amounts:
                a = a.strip().replace(" ", "")
                if not a:
                    continue
                if re.match(r"^\d{1,3}([.,]\d{3})*([.,]\d{1,2})?$|^\d+([.,]\d{1,2})?$", a):
                    norm = a.replace(",", ".") if a.count(",") == 1 and len(a.split(",")[-1]) <= 2 else a.replace(",", "")
                    try:
                        parsed.append(round(float(norm), 2))
                    except ValueError:
                        pass
            live_price = live_price_by_name.get(match)
            if parsed and live_price is not None and live_price not in parsed:
                check["priceMismatches"].append(
                    f'{f}: design prints {parsed[:3]}, WooCommerce serves {live_price}')
                report["passed"] = False
        # The listing renders the catalogue, not a frozen copy of it.
        listing_key = next((p["key"] for p in MF["pages"] if p["file"] == shop.get("listing")), None)
        container = shop.get("cardContainer")
        if listing_key and container:
            page.goto(url_for(listing_key)); settle(page)
            rendered = page.locator(f"{container} > *").count()
            check["renderedCards"] = rendered
            # Unlike the blog, the expectation is NOT the source's card count:
            # a shop listing that paged client-side deliberately showed fewer,
            # and the converted one is supposed to show them all.
            check["cardsExpected"] = len(live_products)
            if rendered != len(live_products):
                report["passed"] = False
        report["checks"]["shopFidelity"] = check

    # ---- C4: every declared navigation group is a WIRED WordPress menu ----
    # Reliability is the requirement, not best effort: a nav group the
    # manifest declares and nothing wires is a menu the owner will edit in
    # wp-admin and watch change nothing. Three assertions per entry, each
    # named by selector on failure:
    #   1. the zone EXISTS in the live DOM (selector matches on a probed page)
    #   2. its location has a menu ASSIGNED (wp-cli; NOT RUN reported, never
    #      counted as a pass, when no --wp-cli is given)
    #   3. every link the zone renders is an item of that menu (consistency —
    #      the rendered nav IS the menu, not a leftover hardcoded copy that
    #      happens to survive)
    # What this cannot prove is that an EDIT propagates (at import time the
    # menu was generated from these very links, so hardcoded-and-identical
    # passes 3); the live-edit mutation check stays in the handover
    # checklist, where a human or agent changes an item and watches the nav.
    nav_entries = MF.get("nav") or []
    if nav_entries:
        prefix = (MF.get("site") or {}).get("prefix", "")
        c4 = {"entries": [], "ok": True, "assignmentChecked": bool(args.wp_cli)}
        menu_data = None
        if args.wp_cli:
            php = (
                '$locs = get_nav_menu_locations(); $out = array();'
                'foreach ( $locs as $loc => $id ) {'
                ' $items = wp_get_nav_menu_items( (int) $id );'
                ' $out[ $loc ] = array( "menu" => (int) $id, "titles" => array_values( array_map('
                ' function ( $i ) { return trim( $i->title ); }, $items ? $items : array() ) ) ); }'
                'echo wp_json_encode( $out );'
            )
            out = subprocess.run(shlex.split(args.wp_cli) + ["eval", php],
                                 capture_output=True, text=True, timeout=120)
            try:
                menu_data = json.loads(out.stdout.strip() or "{}")
            except ValueError:
                menu_data = None

        # EVERY page is a candidate canvas, not the front page and whichever
        # subpage happens to come first in the manifest. A navigation group
        # does not have to appear on those two: aelen's mobile drawer is
        # absent from the front page (which owns its chrome inline and carries
        # a different link set) and from pricing (a third link set), and
        # present on fourteen others — so a zone wired correctly on fourteen
        # pages was reported as "not in the rendered site". The loop below
        # stops at the first page that renders the zone, so the common case
        # still costs one or two page loads; only a genuinely missing zone
        # pays for the full walk, and being sure about that one is the point.
        # Named in mc-001 as the reason gate C4 cannot see a mis-stamped menu.
        front = next((f for f in files if page_map[f] == "front-page"), None)
        rest = [f for f in files if page_map[f] not in ("front-page", "404")
                and kind_map.get(f) != "article"]
        probes = [url_for(page_map[f]) for f in ([front] if front else []) + rest]

        # Read each link the way the plugin WRITES it. fill_link() puts the
        # menu item's label into the link's LONGEST TEXT RUN, deliberately, so
        # an icon or badge inside the link survives the swap. A nav item built
        # as <a><span class="icon">💬</span>Text Generator</a> therefore
        # renders textContent "💬Text Generator" while its menu item is titled
        # "Text Generator" — and the whole-textContent comparison failed a
        # zone that is rendering exactly right. Putting the icon INTO the
        # title is not the fix: fill_link would then write it into the longest
        # run and the page would show the icon twice.
        # ...but reading ONLY the longest run made this check share an
        # assumption with the code it checks, and a check that derives its
        # expectation the way the code under test does cannot fail on their
        # common mistake — it must fail on the correction instead. Measured on
        # bigspring-nextjs: the design's dropdown item is two-line (icon +
        # <span>CRM</span> + <p>For great customer relationships</p>), so the
        # longest run is the DESCRIPTION. fill_link overwrote it with the menu
        # label, C4 read that overwritten run, found the label, and passed —
        # while every page of the site rendered "CRM / CRM". Restore the
        # description and C4 goes RED on a correct page (bigspring-023).
        #
        # So collect EVERY text run of the link and pass it if ANY run is a
        # menu item's title. The icon case that motivated the original rule
        # still passes — "Text Generator" is one of the runs — and the
        # two-line case now passes when correct and fails when the description
        # has been eaten, which is the whole point.
        zone_labels_js = """(sel) => {
                      const el = document.querySelector(sel);
                      if (!el) return null;
                      const runs = (a) => {
                        const w = document.createTreeWalker(a, NodeFilter.SHOW_TEXT);
                        const out = [];
                        for (let n = w.nextNode(); n; n = w.nextNode()) {
                          const t = n.textContent.replace(/\\s+/g, ' ').trim();
                          if (t) out.push(t);
                        }
                        // The whole link too: a label split across sibling
                        // text nodes has no single run equal to it.
                        const whole = a.textContent.replace(/\\s+/g, ' ').trim();
                        if (whole && !out.includes(whole)) out.push(whole);
                        return out;
                      };
                      return [...el.querySelectorAll('a')]
                        .map(runs)
                        .filter((r) => r.length);
                    }"""

        # The zone as the SERVER wrote it. The plugin renders a menu in PHP,
        # so every label it produced is already in the response; a label that
        # only differs in the settled DOM was changed by the SITE'S OWN
        # script, which is design behaviour this gate does not own.
        prejs_ctx = browser.new_context(viewport={"width": 1440, "height": 950},
                                        java_script_enabled=False)
        prejs_page = prejs_ctx.new_page()

        for i, entry in enumerate(nav_entries):
            loc = f"{prefix}_nav_{i + 1}"
            # zoneSelector is the STAMPED [data-ve-nav="n"] selector
            # make-theme wrote back; the authored selector is only the
            # fallback for a manifest that predates stamping.
            sel = entry.get("zoneSelector") or entry.get("selector", "")
            rec = {"selector": sel, "location": loc}
            rendered = None
            for url in probes:
                page.goto(url)
                page.wait_for_load_state("networkidle")
                rendered = page.evaluate(zone_labels_js, sel)
                if rendered is not None:
                    rec["page"] = url
                    break
            if rendered is None:
                rec["ok"] = False
                rec["detail"] = "selector matches no element on any probed page — this navigation group is not in the rendered site"
                c4["ok"] = False
            elif menu_data is not None:
                assigned = menu_data.get(loc) or {}
                # Entity-decode BOTH sides: wp-cli hands back the stored
                # title ("Terms &#038; Conditions" — WordPress's normal
                # convention), the DOM hands back decoded textContent
                # ("Terms & Conditions"). Comparing raw against decoded
                # spuriously failed every menu item containing an entity.
                titles = set(html.unescape(t) for t in (assigned.get("titles") or []))
                if not assigned.get("menu"):
                    rec["ok"] = False
                    rec["detail"] = "no menu assigned to this location — the group renders only its hardcoded links"
                    c4["ok"] = False
                else:
                    # A link passes on ANY matching run, and is reported by
                    # its longest run so the message still names something a
                    # human recognises on the page.
                    def _stray(runs_per_link):
                        out = []
                        for runs in runs_per_link:
                            if not any(html.unescape(t) in titles for t in runs):
                                out.append(max(runs, key=len))
                        return out
                    stray = _stray(rendered)
                    if stray:
                        # Before blaming the wiring, ask what the plugin
                        # actually wrote. agency's own main.js renames the
                        # fifth header item from "Docs" to "Pages" and turns
                        # it into a dropdown toggle; the menu is wired
                        # perfectly and the settled DOM shows a label no menu
                        # item has. Only a label missing from the SERVER's
                        # markup too is a menu the plugin failed to render.
                        prejs_page.goto(rec["page"])
                        served = prejs_page.evaluate(zone_labels_js, sel) or []
                        served_stray = _stray(served)
                        if served and not served_stray:
                            rec["runtimeRelabelled"] = stray[:5]
                            stray = []
                    rec["ok"] = not stray
                    if stray:
                        rec["detail"] = f"zone renders links that are not items of its menu: {stray[:5]}"
                        c4["ok"] = False
            else:
                rec["ok"] = None
                rec["detail"] = "DOM present; assignment NOT CHECKED (pass --wp-cli to assert it)"
            c4["entries"].append(rec)

        prejs_ctx.close()
        report["checks"]["menusWired"] = c4
        if not c4["ok"]:
            report["passed"] = False

    # ---- C5: every repeating group recorded at conversion time is still
    # offered on the live render, with the same item count ----
    # detect-collections.py recorded, per page, every congruent contiguous
    # sibling run the plugin's own rules find (lib/collection-detect.js is a
    # port of bridge.js's detection). Here the SAME rules run against the
    # live WordPress page: a group that no longer detects — WP wrapping
    # broke sibling congruence, an id rewrite made members differ, a zone
    # swallowed it — is a repeating group the editor will not offer, which
    # is a failure with the page and group named, never a silent gap.
    # Matching is by multiset: groups keyed by (parent tag+classes, member
    # shape), counts compared as sorted lists, so three identical footer
    # columns don't mask one going missing.
    recorded = MF.get("collections") or []
    if recorded:
        _lib = (Path(__file__).parent / "lib" / "collection-detect.js").read_text()
        detect_js = _lib[_lib.index("(config)"):]  # past the header comments, so playwright sees a function
        base_exclude = [e.get("selector", "") for e in (MF.get("nav") or [])]
        card_container = (MF.get("blog") or {}).get("cardContainer")
        if card_container:
            base_exclude.append(card_container)
        base_exclude = [s for s in base_exclude if s] + ["[data-cve-zone]"]
        header_selector = ((MF.get("chrome") or {}).get("header") or {}).get("selector", "header")
        trailing_selectors = []
        for chrome_entry in (MF.get("chrome") or {}).get("trailing", []):
            trailing_selectors.extend(chrome_entry.get("selectors") or [])
        if not trailing_selectors:
            trailing_selectors = ["footer"]

        def exclude_for_page(filename):
            entry = next((p for p in MF.get("pages", []) if p.get("file") == filename), {})
            selectors = list(base_exclude) + [header_selector]
            if entry.get("kind") != "front" or not (MF.get("chrome") or {}).get("frontOwnsFooter", True):
                selectors.extend(trailing_selectors)
            return list(dict.fromkeys(s for s in selectors if s))

        c5 = {"pages": {}, "ok": True}
        nojs_ctx = browser.new_context(viewport={"width": 1440, "height": 950}, java_script_enabled=False)
        nojs_page = nojs_ctx.new_page()
        by_page = {}
        for g in recorded:
            by_page.setdefault(g["page"], []).append(g)
        for f, groups in by_page.items():
            if kind_map.get(f) == "article":
                # became a Post; its repeating groups live in the article
                # template now, exercised through the template's own key
                continue
            if kind_map.get(f) in ("product", "shop") or f in {
                    (MF.get("shop") or {}).get("cartPage"), (MF.get("shop") or {}).get("checkoutPage")}:
                # A shop page's repeating groups are QUERY RESULTS now — a
                # related strip returns however many products share the
                # category (one, on a category with two members), a variant
                # chooser is absent from a product that has no variants, and
                # the gallery fills as many slots as the product has pictures.
                # Comparing those against the groups a single frozen page
                # happened to show measures how dynamic the page has become,
                # not whether anything broke. Same exemption, same reason, as
                # the pixel rule.
                c5.setdefault("exempt", []).append(f)
                continue
            key = page_map.get(f)
            if not key:
                continue
            nojs_page.goto(url_for(key))
            live = nojs_page.evaluate(detect_js, {"excludeSelectors": exclude_for_page(f)})

            def keyed(gs):
                out = {}
                for g in gs:
                    # hotfix (sidefolio-013): stage 4 UNWRAPS the source's
                    # <main> and demotes its class onto a wrapper div — a
                    # deliberate, documented transformation (WordPress renders
                    # its own <main> around post-content). A group whose
                    # container IS that <main> is therefore recorded against
                    # `main` in dist and detected against `div` live, with the
                    # same classes, member shape and count. Keying on the raw
                    # tag reports it as missing on a correct conversion —
                    # verified live: `2× <img class="detail-img">` on both
                    # project detail pages, present in the live editor's own
                    # detection, named as missing by this gate. Normalise the
                    # one transformation, the way gate A2 normalises its two.
                    parent = "div" if g["parentTag"] == "main" else g["parentTag"]
                    k = (parent, g.get("parentClasses", ""), g["memberShape"])
                    out.setdefault(k, []).append(g["count"])
                return {k: sorted(v) for k, v in out.items()}

            want, have = keyed(groups), keyed(live)
            # A group that MOVED INTO THE CHROME is not a group that was lost.
            #
            # Live detection deliberately excludes the chrome regions (they are
            # template parts, edited through their own roots), while the
            # recorded set was taken before chrome was extracted — so on a site
            # whose front page carried its own footer, every footer group is
            # recorded against index.html and can never be detected live. The
            # gate then failed a conversion whose footer renders perfectly, for
            # markup that simply lives in parts/footer.html now.
            #
            # Confirmed by looking there: a container class present in a chrome
            # part is relocated, and reported as such rather than as missing.
            chrome_html = ""
            parts_dir = Path(MF.get("workspace", "")) / "theme" / MF["site"]["slug"] / "parts"
            if parts_dir.is_dir():
                for part in sorted(parts_dir.glob("*.html")):
                    chrome_html += part.read_text(errors="replace")

            missing, relocated = [], []
            for k, counts in want.items():
                if have.get(k) == counts:
                    continue
                entry = {
                    "group": f"{counts}× <{k[2].split('|')[0]}> in <{k[0]}"
                             + (f' class="{k[1]}"' if k[1] else "") + ">",
                    "recorded": counts,
                    "live": have.get(k, []),
                }
                cls = k[1].strip()
                in_chrome = bool(cls) and all(c in chrome_html for c in cls.split() if c)
                (relocated if in_chrome and not have.get(k) else missing).append(entry)

            c5["pages"][f] = {"recorded": len(groups), "live": len(live), "missing": missing}
            if relocated:
                c5["pages"][f]["relocatedToChrome"] = relocated
            if missing:
                c5["ok"] = False
        nojs_ctx.close()
        report["checks"]["collections"] = c5
        if not c5["ok"]:
            report["passed"] = False

    ctx.close()
    browser.close()
dist_httpd.shutdown()

(OUT / "report.json").write_text(json.dumps(report, indent=2))
bad = [f for f, e in report["pages"].items() if any(isinstance(v, dict) and v.get("ok") is False for v in e.values())]
inherited_pages = [f for f, e in report["pages"].items() if e.get("inheritedFailedRequests")]
# Named, never silent. A page exempted from the pixel rule is still a page
# somebody has to LOOK at — the exemption says "this cannot be identical", not
# "this is fine" — and a green gate that quietly excused six pages would read
# as an identity it never checked.
exempt = {}
for f, e in report["pages"].items():
    for v in e.values():
        if isinstance(v, dict) and v.get("status") in ("woocommerce-listing", "woocommerce-product", "woocommerce-owned-page", "dynamic-listing"):
            exempt.setdefault(v["status"], set()).add(f)
for status, pages in sorted(exempt.items()):
    worst = max(
        (v.get("diffRatio", 0) for f in pages for v in report["pages"][f].values()
         if isinstance(v, dict) and v.get("status") == status),
        default=0)
    print(f"  note: {len(pages)} page(s) exempted from the pixel rule as {status} "
          f"(worst {worst:.1%}) — content now comes from WordPress, so identity with the static "
          f"snapshot is not the test. Read them at stage 5.5: {', '.join(sorted(pages)[:4])}"
          + (" …" if len(pages) > 4 else ""))
print(f"{'GATE B/C PASSED' if report['passed'] else 'GATE B/C FAILED'} — {len(files)} pages"
      + (f"; failing: {', '.join(bad[:6])}" if bad else "")
      # Not a failure, but the owner must be told: these requests fail in the
      # source too and travel into the delivered theme unchanged.
      + (f"; source's own failed requests carried through on {len(inherited_pages)} page(s)"
         f" — disclose in the conversion report" if inherited_pages else ""))
sys.exit(0 if report["passed"] else 1)
