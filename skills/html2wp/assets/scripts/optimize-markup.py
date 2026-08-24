#!/usr/bin/env python3
"""Stage 0.6 — give every <img> the attributes a browser needs, in place.

Stage 0.5 makes the images small. This makes the browser fetch them at the
right time, which on an image-led design is the larger of the two wins and the
one no re-encode can buy.

Measured on a converted photographer's site, live: the front page shipped
4.26 MB across 24 images with `loading=` on none of them and `width`/`height`
on none of them — 99.4% of the page's weight, all of it fetched before the
visitor had scrolled a pixel. HTML was 8 kB and CSS 14 kB by comparison, and
the server answered in 9 ms, so nothing else on that page was worth measuring
until this was fixed.

Three attributes, and no more:

  width/height   from the file's real pixel size. This is what makes the other
                 two safe: without intrinsic dimensions the browser cannot
                 reserve the box, so lazy-loading below-fold images would
                 collapse the page and reflow it as they arrive. It is also
                 what stops layout shift. A design that sizes images in CSS is
                 unaffected — `max-width:100%; height:auto` (or any explicit
                 rule) overrides the attributes, and the attributes only supply
                 the ASPECT RATIO the browser uses before the bytes land.

  loading=lazy   on everything the visitor cannot see at rest, plus
  decoding=async on the same set. What counts as visible is deliberately
                 CRUDE — the first N images in document order, default 4 —
                 rather than a viewport simulation. A wrong guess here is
                 cheap in one direction (an above-fold image marked lazy loads
                 a moment late) and expensive in the other (a below-fold image
                 left eager keeps the megabytes), and a real browser measuring
                 the fold would make this stage depend on a headless run that
                 stages 1 and 2 already own. Verified after the fact on the
                 delivered site: at 1440x900 and 390x844, across every page,
                 no above-the-fold image was marked lazy.

  srcset/sizes   with --responsive. One variant per file, sized for that
                 file's WIDEST use — see variant_for(). The per-occurrence
                 `sizes` is what lets a small slot and a large slot share it.

  fetchpriority=high on the LCP candidate — the first image of the page, which
                 on this class of design is the hero. It tells the browser to
                 start that fetch ahead of the stylesheet's other work instead
                 of at its default "low" for images.

It will not touch: an <img> that already declares an attribute (the design's
own choice wins, always), an image inside <noscript>, or an SVG/data: source
that has no intrinsic size to read.

Verify it the way this pipeline verifies everything: point verify-static.py at
the untouched source as --original and the edited directory as --dist. These
attributes must not move a single pixel at rest, and gate A is what proves it
rather than an argument that they cannot.
"""
import argparse, json, re, sys
from pathlib import Path
from urllib.parse import unquote, urlparse

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow is required: python3 -m pip install pillow")

ap = argparse.ArgumentParser()
ap.add_argument("--input", help="the directory to edit (a COPY — this rewrites files)")
ap.add_argument("--manifest", help="conversion-manifest.json; --input is read from it when given")
ap.add_argument("--eager", type=int, default=4,
                help="how many leading images per page stay eager (default 4)")
ap.add_argument("--responsive", action="store_true",
                help="also measure display sizes in a browser and emit srcset for images "
                     "shipped far larger than they are ever drawn")
ap.add_argument("--widths", default="1440,390",
                help="viewport widths to measure at (default 1440,390)")
ap.add_argument("--quality", type=int, default=82, help="WebP quality for generated variants (default 82)")
ap.add_argument("--apply", action="store_true", help="write the changes; without it, only measure")
ap.add_argument("--out", default=None, help="where to write the report")
args = ap.parse_args()

if args.manifest:
    MF = json.loads(Path(args.manifest).read_text())
    INPUT = Path(MF["input"]["dir"]).resolve()
    WS = Path(MF.get("workspace") or Path(args.manifest).parent).resolve()
elif args.input:
    INPUT = Path(args.input).resolve()
    WS = INPUT.parent
else:
    sys.exit("pass --input or --manifest")
if not INPUT.is_dir():
    sys.exit(f"not a directory: {INPUT}")

OUT = Path(args.out) if args.out else WS / "optimize-markup-report.json"
report = {"applied": bool(args.apply), "eagerPerPage": args.eager, "pages": {}, "totals": {}}

IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
NOSCRIPT_RE = re.compile(r"<noscript\b.*?</noscript>", re.I | re.S)
SIZEABLE = {".webp", ".png", ".jpg", ".jpeg", ".gif", ".avif"}

# The chrome selectors, so an image inside SHARED chrome can be decided
# page-invariantly. This is not a refinement, it is a correctness rule the
# pipeline already states: anything stamped into shared chrome must not depend
# on the page it was stamped from. A per-page counter breaks it — measured
# here, the footer's Instagram strip landed inside the first four images on
# short pages (eager, and the first tile even got fetchpriority) and at #20 on
# long ones (lazy), so ONE footer design came out as SEVEN chrome groups and
# would have shipped as seven template parts.
CHROME_SELECTORS = []
if args.manifest:
    _chrome = MF.get("chrome") if isinstance(MF.get("chrome"), dict) else {}
    _hdr = _chrome.get("header") if isinstance(_chrome.get("header"), dict) else {}
    if _hdr.get("selector"):
        CHROME_SELECTORS.append(_hdr["selector"])
    for _t in (_chrome.get("trailing") or []):
        if isinstance(_t, dict):
            CHROME_SELECTORS += [s for s in (_t.get("selectors") or []) if isinstance(s, str)]
    _ftr = _chrome.get("footer") if isinstance(_chrome.get("footer"), dict) else {}
    if _ftr.get("selector"):
        CHROME_SELECTORS.append(_ftr["selector"])


def chrome_spans(html):
    """Byte ranges of the shared chrome regions in this page."""
    spans = []
    for sel in CHROME_SELECTORS:
        m = re.match(r"^([a-z0-9]+)(?:\.([\w.-]+))?$", sel.strip(), re.I)
        if not m:
            continue
        tag, classes = m.group(1), (m.group(2) or "").split(".") if m.group(2) else []
        for om in re.finditer(rf"<{tag}\b[^>]*>", html, re.I):
            have = re.search(r'class="([^"]*)"', om.group(0))
            have = (have.group(1).split() if have else [])
            if classes and not all(c in have for c in classes):
                continue
            depth, pos = 0, om.start()
            for tm in re.finditer(rf"<{tag}\b[^>]*>|</{tag}>", html[om.start():], re.I):
                depth += -1 if tm.group(0).startswith("</") else 1
                if depth == 0:
                    spans.append((om.start(), om.start() + tm.end()))
                    break
    return spans


def attr(tag, name):
    m = re.search(rf'\b{name}=("([^"]*)"|\'([^\']*)\')', tag, re.I)
    if not m:
        return None
    return m.group(2) if m.group(2) is not None else m.group(3)


def has(tag, name):
    return re.search(rf"\b{name}\s*=", tag, re.I) is not None


def resolve(src, page_path):
    """The file a src points at, or None for anything not on disk."""
    if not src or src.startswith(("data:", "blob:", "//", "http://", "https://")):
        return None
    clean = unquote(urlparse(src).path)
    candidate = (INPUT / clean.lstrip("/")) if clean.startswith("/") else (page_path.parent / clean)
    candidate = candidate.resolve()
    try:
        candidate.relative_to(INPUT)
    except ValueError:
        return None
    return candidate if candidate.is_file() and candidate.suffix.lower() in SIZEABLE else None


_dims: dict = {}


def dimensions(path):
    key = str(path)
    if key not in _dims:
        try:
            with Image.open(path) as im:
                _dims[key] = im.size
        except Exception:
            _dims[key] = None
    return _dims[key]


def add(tag, additions):
    """Insert attributes before the tag's closing bracket, preserving self-closing."""
    body = tag[:-1].rstrip()
    selfclose = body.endswith("/")
    if selfclose:
        body = body[:-1].rstrip()
    return f"{body} {' '.join(additions)}{' /' if selfclose else ''}>"


def measure_display_widths(page_names):
    """The widest each image is ever DRAWN, across every page and viewport.

    Guessing this from the markup is not possible — the same file is routinely
    a 240px thumbnail in the footer strip and a 460px gallery figure two pages
    away, and shrinking it to the thumbnail would wreck the gallery. So it is
    measured, once, in a real browser, and the MAXIMUM wins.
    """
    import http.server, socketserver, threading, functools
    from playwright.sync_api import sync_playwright

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a, **k):
            pass

    handler = functools.partial(Quiet, directory=str(INPUT))
    srv = socketserver.TCPServer(("127.0.0.1", 0), handler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    widest = {}      # filename -> widest anywhere (decides whether a variant is worth making)
    per_use = {}     # (page, nth img) -> widest at THAT spot (decides its `sizes`)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            for vw in [int(w) for w in args.widths.split(",") if w.strip()]:
                pg = browser.new_page(viewport={"width": vw, "height": 900}, device_scale_factor=1)
                for name in page_names:
                    pg.goto(f"http://127.0.0.1:{port}/{name}", wait_until="domcontentloaded")
                    pg.evaluate("document.documentElement.style.scrollBehavior='auto'")
                    height = pg.evaluate("document.body.scrollHeight")
                    for y in range(0, height, 800):
                        pg.evaluate(f"window.scrollTo(0,{y})")
                        pg.wait_for_timeout(15)
                    pg.wait_for_timeout(120)
                    for n, d in enumerate(pg.evaluate(
                        """() => [...document.querySelectorAll('img')].map(i => ({
                             src: (i.currentSrc || i.src), w: Math.round(i.getBoundingClientRect().width) }))"""
                    )):
                        if not d["w"]:
                            continue
                        key = Path(unquote(urlparse(d["src"]).path)).name
                        widest[key] = max(widest.get(key, 0), d["w"])
                        spot = (name, n)
                        prev = per_use.get(spot)
                        # Widest across viewports for THIS spot: `sizes` must
                        # describe the biggest box the image is drawn in, or a
                        # narrow viewport's number would ship a blurry image to
                        # a wide one.
                        if prev is None or d["w"] > prev[1]:
                            per_use[spot] = (key, d["w"])
                pg.close()
            browser.close()
    finally:
        srv.shutdown()
    return widest, per_use


display_widths, use_widths = {}, {}
variants = {}   # original filename -> (variant filename, variant width)
if args.responsive:
    display_widths, use_widths = measure_display_widths([p.name for p in sorted(INPUT.glob("*.html"))])


def variant_for(path):
    """A smaller copy of `path`, when one is worth making.

    ONE variant per file, sized for the WIDEST place that file is drawn. Each
    tag still carries its own `sizes`, so a 240px slot and a 460px slot both
    resolve to this single file — which is the point.

    Sizing it for the NARROWEST use saves more bytes per slot and is wrong.
    Tried that way first: a photograph in the footer strip resolved to a 480px
    variant while the same photograph in a gallery two pages away resolved to
    the 1024px original, so navigating between them fetched the same picture
    twice and it visibly popped in on arrival. The owner reported it as photos
    flashing when switching pages through the menu, which is exactly what a
    second download of an already-seen image looks like.

    A page's bytes are not the unit; a VISIT's are. One file per photograph
    means the second page that shows it pays nothing.

    The 2x is not optional either: a variant that only covers 1x is a blurry
    image on every retina phone.
    """
    if not args.responsive or path.name in variants:
        return variants.get(path.name)
    widest = display_widths.get(path.name, 0)
    dim = dimensions(path)
    if not widest or not dim:
        return None
    target = widest * 2
    if target >= dim[0] * 0.85:          # not enough headroom to be worth a second file
        return None
    out_name = f"{path.stem}-{target}{path.suffix}"
    out_path = path.with_name(out_name)
    if args.apply and not out_path.exists():
        try:
            with Image.open(path) as im:
                ratio = target / im.width
                small = im.convert("RGB").resize((target, max(1, round(im.height * ratio))), Image.LANCZOS)
                small.save(out_path, "WEBP", quality=args.quality, method=6)
        except Exception:
            return None
    variants[path.name] = (out_name, target)
    return variants[path.name]


pages = sorted(INPUT.glob("*.html"))
totals = {"pages": 0, "imgs": 0, "sized": 0, "lazied": 0, "priority": 0, "responsive": 0, "untouched": 0}

for page in pages:
    html = page.read_text(encoding="utf-8")
    # An <img> inside <noscript> is the no-JS fallback for something else on
    # the page; lazy-loading it is meaningless and sizing it can fight the
    # markup it stands in for. Blank the region so its tags never match, then
    # discard the blanked copy — edits are applied by offset to the original.
    scan = NOSCRIPT_RE.sub(lambda m: " " * len(m.group(0)), html)
    spans = chrome_spans(html)

    edits, seen, note = [], 0, {"sized": 0, "lazied": 0, "priority": 0, "responsive": 0, "untouched": 0}
    for m in IMG_RE.finditer(scan):
        tag = html[m.start():m.end()]
        seen += 1
        additions = []

        target = resolve(attr(tag, "src"), page)
        dim = dimensions(target) if target else None
        if dim and not has(tag, "width") and not has(tag, "height"):
            additions += [f'width="{dim[0]}"', f'height="{dim[1]}"']
            note["sized"] += 1

        # Shared chrome is decided by WHERE it is, never by how many images
        # happen to precede it on this particular page. The footer is below
        # the fold on every page by definition, and no header logo has ever
        # been a site's LCP element.
        in_chrome = any(s <= m.start() < e for s, e in spans)

        # The first image is the LCP candidate only if it is ONE image. When it
        # is the first tile of a repeating set — a mosaic, a gallery row — it
        # is neither the largest paint nor safe to mark: giving one member an
        # attribute its siblings lack makes the members structurally unequal,
        # and the editor's congruence rules then stop offering the set as an
        # editable list. Measured: the 404 page's three-tile mosaic went from
        # one editable group to none.
        sibling_set = False
        if seen == 1:
            nxt = IMG_RE.search(scan, m.end())
            if nxt:
                def enclosing(pos):
                    opens = re.findall(r"<([a-z][\w-]*)\b[^>]*>", scan[:pos], re.I)
                    tail = scan[:pos].rfind("<")
                    tag = re.match(r"<([a-z][\w-]*)\b([^>]*)>", scan[tail:pos + 200], re.I)
                    return (tag.group(1).lower() + "|" + (re.search(r'class="([^"]*)"', tag.group(2) or "").group(1)
                            if re.search(r'class="([^"]*)"', tag.group(2) or "") else "")) if tag else ""
                sibling_set = enclosing(m.start()) and enclosing(m.start()) == enclosing(nxt.start())

        first = seen == 1 and not in_chrome and not sibling_set
        if first and not has(tag, "fetchpriority") and not has(tag, "loading"):
            # The LCP candidate: fetched ahead of the browser's default for
            # images, and never lazy — the two would cancel out.
            additions.append('fetchpriority="high"')
            note["priority"] += 1
        elif (in_chrome or seen > args.eager) and not has(tag, "loading"):
            additions.append('loading="lazy"')
            if not has(tag, "decoding"):
                additions.append('decoding="async"')
            note["lazied"] += 1

        # A file drawn far smaller than it ships gets a second, smaller copy
        # and a srcset the browser chooses from. `sizes` states the WIDEST the
        # image is ever drawn — deliberately, because overstating it only ever
        # makes the browser pick the larger candidate, and understating it
        # would ship a blurry image to the page that draws it big.
        if target and not has(tag, "srcset"):
            v = variant_for(target)
            drawn = (use_widths.get((page.name, seen - 1)) or (None, 0))[1]
            if v and drawn:
                out_name, vw = v
                src_val = attr(tag, "src")
                base = src_val.rsplit("/", 1)[0] + "/" if "/" in src_val else ""
                additions.append(f'srcset="{base}{out_name} {vw}w, {src_val} {dim[0]}w"')
                additions.append(f'sizes="{drawn}px"')
                note["responsive"] += 1

        if additions:
            edits.append((m.start(), m.end(), add(tag, additions)))
        else:
            note["untouched"] += 1

    if edits:
        for start, end, replacement in reversed(edits):
            html = html[:start] + replacement + html[end:]
        if args.apply:
            page.write_text(html, encoding="utf-8")

    report["pages"][page.name] = {"images": seen, **note}
    totals["pages"] += 1
    totals["imgs"] += seen
    for k in ("sized", "lazied", "priority", "responsive", "untouched"):
        totals[k] += note[k]

report["totals"] = totals
OUT.write_text(json.dumps(report, indent=2) + "\n")

verb = "set" if args.apply else "would set"
print(f"{verb} dimensions on {totals['sized']}, loading=lazy on {totals['lazied']}, "
      f"fetchpriority=high on {totals['priority']} of {totals['imgs']} image(s) "
      f"across {totals['pages']} page(s) -> {OUT}")
if args.responsive:
    saved = 0
    for orig, (name, vw) in variants.items():
        a, b = INPUT / "assets" / orig, INPUT / "assets" / name
        if a.is_file() and b.is_file():
            saved += a.stat().st_size - b.stat().st_size
    print(f"  {totals['responsive']} srcset(s) over {len(variants)} generated variant(s)"
          + (f", {saved/1048576:.2f} MB smaller per full set" if saved else ""))
if totals["untouched"]:
    print(f"  {totals['untouched']} image(s) left exactly as authored "
          f"(already declared the attribute, or no readable file behind the src)")
if not args.apply:
    print("  nothing written — re-run with --apply, then rebuild from stage 1")
