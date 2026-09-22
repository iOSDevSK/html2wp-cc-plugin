#!/usr/bin/env python3
"""Stage 6.5 — build the side-by-side pairs the AI review step reads.

  python3 compare-pages.py --manifest=conversion-manifest.json --wp <base-url>
      [--original <dir>] [--out <dir>]

The numeric gates measure; they do not judge. A dropped below-the-fold
section scored 0.4% on the pixel gate — under threshold, green — and a
person looking at the two pages side by side rejects it instantly. So the
pipeline gets an explicit review step: for EVERY page, the original and the
live converted render, captured full-page at the same width, composed into
one labeled image. The conversion is not done until each composite has been
READ — by the operator AI or a human — and the finding written into the
conversion report (SKILL.md names this step). This script only builds the
evidence; it never scores anything, deliberately: scoring is exactly what
already failed to see the dropped section.

Writes, per page: {out}/{key}.side-by-side.png (original left, WordPress
right, captioned), the same pair cut into {out}/{key}.tile-NN.png of one
viewport height each, and {out}/review-manifest.json listing every pair
with its tiles and per-side pixel heights — a height mismatch is the first
thing worth looking at. Articles are captured at their real post URL (matched by <h1>,
like verify-wp.py does). A Gutenberg (html2wp/2) manifest's pages, posts and
products are captured at the permalink WordPress reports for their slug.
"""

import argparse, functools, html as htmlmod, json, re, shutil, subprocess, sys, tempfile, threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright
from PIL import Image, ImageDraw

ap = argparse.ArgumentParser()
ap.add_argument("--manifest", required=True)
ap.add_argument("--wp", required=True)
ap.add_argument("--original", default="")
ap.add_argument("--out", default="")
ap.add_argument("--width", type=int, default=1440)
ap.add_argument("--jobs", type=int, default=1, help="pages captured at once, one Chromium each (default 1)")
ap.add_argument("--_pages", default="", help=argparse.SUPPRESS)
ap.add_argument("--_partial", default="", help=argparse.SUPPRESS)
args = ap.parse_args()

MF = json.loads(Path(args.manifest).read_text())
WS = Path(MF["workspace"]).resolve()
# A native Gutenberg (v2) manifest: WordPress routes come from each page's
# kind and slug, not from its key; fragments are not pages.
V2 = MF.get("schema") == "html2wp/2" or MF.get("target") == "gutenberg"
if V2:
    MF["pages"] = [p for p in MF["pages"] if p.get("kind") != "fragment"]
ORIG = Path(args.original or (MF.get("input") or {}).get("dir") or WS / "astro-project/dist").resolve()
OUT = Path(args.out or (WS / "visual-review")).resolve()
OUT.mkdir(parents=True, exist_ok=True)
WP = args.wp.rstrip("/")
VIEWPORT_H = 950


def permalink_for(found, slug, wp, others=()):
    """The permalink for a page whose full slug path is `slug`, out of what
    WordPress REST answered for its LAST segment (the only part REST filters
    on). Two pages under different parents — /a/about/ and /b/about/ — share
    that segment and both come back, so the one whose own path is the full
    slug wins. An answer at the full slug of ANOTHER page in the manifest
    (`others`) is that page's, never this one's: when the import flattened
    /b/about/ to about-2, REST answers ?slug=about with /a/about/ alone, and
    taking it captured a-about twice and hid that /b/about/ is not there.
    Otherwise the first answer, as before; with none, the compiler's slug
    rule — the address the page is meant to be at."""
    links = [f["link"] for f in found if isinstance(f, dict) and isinstance(f.get("link"), str)] \
        if isinstance(found, list) else []
    base = urlsplit(wp).path.rstrip("/")
    want = base + "/" + slug.strip("/")
    taken = {base + "/" + o.strip("/") for o in others} - {want}
    for link in links:
        if urlsplit(link).path.rstrip("/") == want:
            return link
    links = [link for link in links if urlsplit(link).path.rstrip("/") not in taken]
    return links[0] if links else f"{wp}/{slug}/"


def serve(directory):
    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(directory)))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd, f"http://127.0.0.1:{httpd.server_port}"


def settle(page):
    """Same discipline as the gates — a page that has finished becoming
    itself, so what the reviewer reads is what a visitor gets."""
    page.wait_for_load_state("networkidle")
    page.evaluate("document.fonts && document.fonts.ready")
    page.evaluate("""async () => {
      for (const i of document.querySelectorAll('img[loading=lazy]')) i.loading = 'eager';
      const pending = [...document.querySelectorAll('img')].filter((i) => !i.complete);
      await Promise.all(pending.map((i) => new Promise((r) => { i.onload = i.onerror = r; })));
    }""")
    page.evaluate("""async () => {
      const h = document.body.scrollHeight;
      for (let y = 0; y < h; y += 700) { window.scrollTo(0, y); await new Promise(r => setTimeout(r, 60)); }
      window.scrollTo(0, 0);
    }""")
    # `complete` only means the response finished; a large PNG may still be
    # waiting for its decode before Chromium can paint it. Full-page captures
    # of media-heavy converted pages then showed white cards below the fold
    # while the same images appeared normally after a human scrolled there.
    # Await decode explicitly after the lazy-load scroll so the review judges
    # the page, not Chrome's image-decoder queue.
    page.evaluate("""async () => {
      await Promise.all([...document.images].map(async (i) => {
        if (!i.complete) await new Promise((r) => { i.onload = i.onerror = r; });
        if (i.decode) { try { await i.decode(); } catch (e) {} }
      }));
    }""")
    page.evaluate("""() => {
      const s = document.createElement('style');
      s.textContent = '*,*::before,*::after{animation:none!important;transition:none!important;caret-color:transparent!important}';
      document.head.appendChild(s);
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
          for (const m of sel.matchAll(/\\.([A-Za-z0-9_-]+)\\.([A-Za-z0-9_-]+)/g)) {
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


def compose(left_path, right_path, out_path, title):
    l, r = Image.open(left_path).convert("RGB"), Image.open(right_path).convert("RGB")
    caption = 44
    gap = 24
    w = l.width + r.width + gap
    h = max(l.height, r.height) + caption
    board = Image.new("RGB", (w, h), (24, 24, 24))
    board.paste(l, (0, caption))
    board.paste(r, (l.width + gap, caption))
    d = ImageDraw.Draw(board)
    d.text((8, 12), f"{title} — ORIGINAL {l.width}x{l.height}", fill=(255, 255, 255))
    d.text((l.width + gap + 8, 12), f"CONVERTED (WordPress) {r.width}x{r.height}", fill=(255, 255, 255))
    board.save(out_path)
    return {"origHeight": l.height, "wpHeight": r.height, "tiles": tile(board, caption, out_path)}


def tile(board, caption, out_path):
    """The composite of a tall page is ~2900 px wide and many thousands tall;
    a vision model shrinks it until the text is unreadable. The same pair,
    cut at one viewport height and captioned with where it sits, reads at
    full size — the composite itself stays whole for a person to scroll."""
    stem = out_path.name[: -len(".side-by-side.png")]
    for stale in out_path.parent.glob(f"{stem}.tile-*.png"):
        stale.unlink()
    body = board.height - caption
    # A sliver under a quarter viewport rides on the tile above it.
    count = max(1, body // VIEWPORT_H + (body % VIEWPORT_H >= VIEWPORT_H // 4))
    names = []
    for i in range(count):
        top = i * VIEWPORT_H
        bottom = body if i == count - 1 else top + VIEWPORT_H
        piece = Image.new("RGB", (board.width, caption + bottom - top), (24, 24, 24))
        piece.paste(board.crop((0, 0, board.width, caption)), (0, 0))
        piece.paste(board.crop((0, caption + top, board.width, caption + bottom)), (0, caption))
        label = f"tile {i + 1}/{count}  page y {top}-{bottom}"
        d = ImageDraw.Draw(piece)
        d.text((board.width - 8 - d.textlength(label), 12), label, fill=(255, 210, 90))
        name = f"{stem}.tile-{i + 1:02d}.png"
        piece.save(out_path.parent / name)
        names.append(name)
    return names


def run(indices):
    """Capture the pages at these positions of MF["pages"], in this order, in
    one Chromium. Returns {position: pair}."""
    httpd, ORIG_URL = serve(ORIG)
    got = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(viewport={"width": args.width, "height": VIEWPORT_H})
        page = ctx.new_page()

        # HTML folds a wrapped headline's newlines and indentation into single
        # spaces, so the <h1> of a hand-formatted article page never equals the
        # post title WordPress stored — same fold verify-wp.py's C3 applies.
        def headline_text(fragment):
            # hotfix (creative-009): entities decoded too — WordPress returns
            # title.rendered as "…&#038;…" where the source <h1> holds "&", so an
            # ampersand in a headline skipped the article from the visual read.
            return htmlmod.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", fragment)).strip())

        # Articles live at their real post URLs (slug from title, not filename).
        article_urls = {}
        if (MF.get("blog") or {}).get("present"):
            resp = page.request.get(WP + "/wp-json/wp/v2/posts?per_page=100&status=publish")
            posts = resp.json() if resp.ok else []
            for entry in MF["pages"]:
                if entry.get("kind") != "article":
                    continue
                src = ORIG / entry["file"]
                if not src.exists():
                    continue
                m = re.search(r"<h1[^>]*>(.*?)</h1>", src.read_text(errors="replace"), re.S)
                headline = headline_text(m.group(1)) if m else ""
                # Some exports reuse one structural article heading (for example
                # "Blog Details") and keep the distinct title in <title>. Match
                # those articles exactly as verify-wp.py does so the visual audit
                # still captures every real post instead of silently skipping it.
                # A page with no <h1> at all reaches the same place: the title
                # lives in <title> and the design uses <h2>.
                if not headline or headline.lower() in {"blog details", "article details"}:
                    fallback = re.split(
                        r"\s+(?:\||-|—)\s+", str(entry.get("title", "")), maxsplit=1
                    )[0].strip()
                    if fallback:
                        headline = fallback
                for post in posts:
                    title = headline_text(post["title"]["rendered"])
                    if headline and (headline in title or title in headline):
                        article_urls[entry["file"]] = post["link"]
                        break

        def v2_slug(entry):
            plan = WS / "block-plan" / "pages" / f"{entry['key']}.json"
            proposal = json.loads(plan.read_text()) if plan.is_file() else {}
            return str(proposal.get("slug") or entry.get("slug") or entry["key"]).strip("/")

        def v2_url(entry):
            """The imported post/page/product's own permalink (REST `link`,
            looked up by slug); the compiler's slug rule is the fallback."""
            if entry.get("kind") == "front":
                return WP + "/"
            slug = v2_slug(entry)
            rest = {"post": "posts", "product": "product"}.get(entry.get("kind"), "pages")
            resp = page.request.get(f"{WP}/wp-json/wp/v2/{rest}?slug={slug.rsplit('/', 1)[-1]}&_fields=link")
            others = [v2_slug(e) for e in MF["pages"] if e is not entry and e.get("kind") != "front"]
            return permalink_for(resp.json() if resp.ok else [], slug, WP, others)

        for i in indices:
            entry = MF["pages"][i]
            f, key = entry["file"], entry["key"]
            if not (ORIG / f).exists():
                got[i] = {"page": f, "error": "missing in original"}
                continue
            if V2:
                wp_url = v2_url(entry)
            elif entry.get("kind") == "article":
                wp_url = article_urls.get(f)
                if not wp_url:
                    got[i] = {"page": f, "error": "no live post matches this article's <h1>"}
                    continue
            elif key == "front-page":
                wp_url = WP + "/"
            elif key == "404":
                wp_url = WP + "/html2wp-404-preview-x9q/"
            else:
                wp_url = f"{WP}/{key}/"

            page.goto(f"{ORIG_URL}/{f}")
            settle(page)
            left = OUT / f"{key}.orig.png"
            page.screenshot(path=str(left), full_page=True)

            page.goto(wp_url)
            settle(page)
            right = OUT / f"{key}.wp.png"
            page.screenshot(path=str(right), full_page=True)

            composite = OUT / f"{key}.side-by-side.png"
            heights = compose(left, right, composite, f)
            left.unlink(missing_ok=True)
            right.unlink(missing_ok=True)
            got[i] = {"page": f, "key": key, "wpUrl": wp_url, "composite": composite.name, **heights}

        browser.close()
    httpd.shutdown()
    return got


EVERY = range(len(MF["pages"]))

if args._partial:
    # A worker of a --jobs run: capture its share, hand it back, write nothing else.
    share = [int(i) for i in args._pages.split(",") if i]
    Path(args._partial).write_text(json.dumps(sorted(run(share).items())))
    sys.exit(0)

recaptured = []
if args.jobs <= 1:
    got = run(EVERY)
else:
    # One process per share, each with its own Chromium — sync Playwright is
    # bound to its thread, and one browser would share one raster budget.
    # Self-spawned rather than multiprocessing: this file has no __main__
    # guard. Shares are dealt round-robin; the manifest is assembled below
    # in page order, whatever order the workers finish in.
    n = min(args.jobs, len(MF["pages"]))
    tmp = Path(tempfile.mkdtemp(prefix="compare-pages-"))
    workers = []
    for k in range(n):
        partial = tmp / f"share-{k}.json"
        workers.append((partial, subprocess.Popen(
            [sys.executable, "-W", "ignore::SyntaxWarning", __file__, "--manifest", args.manifest, "--wp", args.wp,
             "--original", str(ORIG), "--out", str(OUT), "--width", str(args.width),
             "--_pages", ",".join(str(i) for i in EVERY[k::n]), "--_partial", str(partial)],
            stdout=open(tmp / f"share-{k}.log", "w"), stderr=subprocess.STDOUT)))
    got = {}
    for k, (partial, proc) in enumerate(workers):
        proc.wait()
        if proc.returncode != 0:
            # Its pages are re-captured alone below; say why, not only that.
            tail = (tmp / f"share-{k}.log").read_text(errors="replace").strip().splitlines()[-5:]
            print(f"  worker {k} exited {proc.returncode}: " + " | ".join(tail))
        if partial.exists():
            got.update({i: pair for i, pair in json.loads(partial.read_text())})
    shutil.rmtree(tmp, ignore_errors=True)
    # Captured under load, a page that has not settled differs in height —
    # the very thing the reviewer reads first. Every such pair, and any page
    # a worker failed to return, is captured again the way --jobs 1 does it:
    # alone, one page after another.
    recaptured = [i for i in EVERY if i not in got
                  or ("composite" in got[i] and got[i]["origHeight"] != got[i]["wpHeight"])]
    if recaptured:
        for i, pair in run(recaptured).items():
            got[i] = {**pair, "recaptured": True}
pairs = [got[i] for i in EVERY]

(OUT / "review-manifest.json").write_text(json.dumps({
    "width": args.width,
    "pairs": pairs,
    "instruction": "READ every composite (original left, converted right) top to bottom and record a "
                   "finding per page in the conversion report — 'matches' is a finding too. Height "
                   "mismatches first. This step judges what the pixel gates only measure.",
}, indent=2))

made = [x for x in pairs if "composite" in x]
errs = [x for x in pairs if "error" in x]
print(f"OK — {len(made)} side-by-side composite(s) → {OUT}" + (f"; {len(errs)} page(s) skipped: "
      + ", ".join(f"{e['page']} ({e['error']})" for e in errs[:4]) if errs else ""))
if recaptured:
    print(f"  {len(recaptured)} page(s) captured again alone (height mismatch or a failed worker)")
