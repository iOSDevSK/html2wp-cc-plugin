#!/usr/bin/env python3
"""Stage 0.5 — re-encode the input's raster images to WebP, in place.

Why this is a STAGE and not a nicety: the theme carries every image twice by
construction — once under assets/ for its own parts and pattern, once under
clara-content/media for the Media Library import — so a photographer's folder
of PNG-encoded photographs arrives as a theme ZIP twice its size. Measured on
a 32-page site: 120 MB of source images, a 240 MB ZIP, which is past the
upload limit of every shared host and most managed ones. The owner's first
experience of their new site was then "the file is too large", and the answer
"install it over SFTP" is a worse product than an image pipeline.

It runs on the INPUT, before stage 1, so every later stage — and every gate —
sees the images the site will actually ship. That ordering is the point: run
it afterwards and nothing has verified the result.

What it will not do:
  - touch SVG, ICO or animated GIF (WebP is not the right answer for any of
    them, and an animated GIF silently becoming a still frame is the kind of
    loss this pipeline exists to prevent)
  - keep a re-encode that came out BIGGER (already-optimised JPEGs, flat
    graphics with few colours) — the original stays and is reported
  - rewrite anything it cannot see: references are rewritten in .html, .css,
    .js and .json, which is where this pipeline's inputs put them, and every
    file it rewrote is named in the report

Verify the result the way the pipeline verifies everything else: point
verify-static.py at the UNTOUCHED source as --original and the optimised
directory as --dist. That measures what the re-encode actually cost, in
pixels, at three widths, instead of trusting a quality number.
"""
import argparse, json, re, shutil, sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow is required: python3 -m pip install pillow")

RASTER = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG"}
TEXT_SUFFIXES = {".html", ".htm", ".css", ".js", ".mjs", ".json", ".xml", ".txt"}

ap = argparse.ArgumentParser()
ap.add_argument("--input", required=True, help="the directory to optimise (a COPY — this rewrites files)")
ap.add_argument("--quality", type=int, default=82, help="WebP quality for photographs (default 82)")
ap.add_argument("--min-bytes", type=int, default=20_000,
                help="leave anything smaller than this alone (default 20000)")
ap.add_argument("--apply", action="store_true", help="write the changes; without it, only measure")
ap.add_argument("--out", default=None, help="where to write the report (default <input>/../optimize-images-report.json)")
ap.add_argument("--remote", action="store_true",
                help="also bring images the pages load from OTHER hosts (https <img>/srcset) into the site")
ap.add_argument("--remote-max-bytes", type=int, default=25_000_000, help="largest remote image fetched (default 25 MB)")
ap.add_argument("--jobs", type=int, default=1,
                help="images re-encoded at once (default 1); the report and every written byte are the same at any N")
args = ap.parse_args()

INPUT = Path(args.input).resolve()
if not INPUT.is_dir():
    sys.exit(f"not a directory: {INPUT}")

report = {"quality": args.quality, "applied": bool(args.apply),
          "converted": [], "skipped": [], "rewrote": [], "localized": [], "remoteFailed": [], "totals": {}}

# ---- remote images ---------------------------------------------------------
# A design that hotlinks its photographs (a Lovable/v0 export loads every one
# from images.pexels.com or unsplash) leaves WordPress nothing to attach: no
# post gets a featured image, so every listing card renders an empty frame,
# and the Media Library holds none of the site's pictures. The same bytes the
# page already shows are brought into the input here — BEFORE stage 1, so
# gate A (untouched copy as --original) proves nothing moved, and the images
# then take the ordinary WebP path below.
#
# The fetch is the page's own request, made once, with the pipeline's SSRF
# guard: https only, the RESOLVED address must be public (lib/net_guard.py),
# every redirect is judged again rather than followed blindly, raster types
# only (an SVG can carry script), and a size cap. Anything refused or failed
# stays hotlinked, exactly as authored, and is named in the report.
if args.remote:
    import hashlib, os, urllib.request, urllib.error
    from html import unescape
    from urllib.parse import urljoin, urlparse
    sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
    from net_guard import address_verdict  # noqa: E402

    RASTER_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
                    "image/gif": ".gif", "image/avif": ".avif"}

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None
    opener = urllib.request.build_opener(_NoRedirect)

    def fetch(url, hops=3):
        chain = [url]   # every address asked, in order: the page's, each redirect, the last
        for _ in range(hops + 1):
            if urlparse(url).scheme != "https":
                return None, f"not https: {url}"
            why = address_verdict(url)
            if why:
                return None, why
            req = urllib.request.Request(url, headers={"User-Agent": "html2wp-image-localizer/1"})
            try:
                resp = opener.open(req, timeout=30)
            except urllib.error.HTTPError as e:
                if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
                    url = urljoin(url, e.headers["Location"])
                    chain.append(url)
                    continue
                return None, f"HTTP {e.code}"
            except Exception as e:  # network trouble is reported, never fatal
                return None, f"{type(e).__name__}: {e}"
            with resp:
                ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype not in RASTER_TYPES:
                    return None, f"not a raster image ({ctype or 'no type'})"
                data = resp.read(args.remote_max_bytes + 1)
                if len(data) > args.remote_max_bytes:
                    return None, f"larger than {args.remote_max_bytes} bytes"
                return (data, RASTER_TYPES[ctype], chain), None
        return None, "too many redirects"

    # http:// is collected too, only to be refused and REPORTED: left silent it
    # stays hotlinked and becomes mixed content on an https WordPress.
    # Images only: a <script src> (the builder's analytics), an <iframe> or a
    # <video> is not ours to download, and fetching it just to refuse it opens
    # a connection to a third party for nothing.
    SRC = re.compile(r"""(<(?:img|source)\b[^>]*?\s(?:src|data-src)=)(["'])(https?://[^"'\s]+)\2""", re.I)
    SRCSET = re.compile(r"""(\ssrcset=)(["'])([^"']+)\2""", re.I)
    pages = [f for f in sorted(INPUT.rglob("*")) if f.is_file() and f.suffix.lower() in (".html", ".htm")]
    wanted = {}   # url (as unescaped) -> None until fetched
    for f in pages:
        text = f.read_text(encoding="utf-8", errors="replace")
        for m in SRC.finditer(text):
            wanted.setdefault(unescape(m.group(3)), None)
        for m in SRCSET.finditer(text):
            for cand in m.group(3).split(","):
                u = cand.strip().split(" ")[0]
                if u.startswith(("https://", "http://")):
                    wanted.setdefault(unescape(u), None)

    target_dir = INPUT / "assets" / "remote"
    local = {}    # url -> path relative to INPUT
    for url in sorted(wanted):
        got, why = fetch(url)
        if not got:
            report["remoteFailed"].append({"url": url, "why": why})
            continue
        data, ext, chain = got
        stem_name = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(urlparse(url).path).stem)[:40].strip("-") or "image"
        name = f"{stem_name}-{hashlib.sha1(url.encode()).hexdigest()[:10]}{ext}"
        rel = f"assets/remote/{name}"
        if args.apply:
            target_dir.mkdir(parents=True, exist_ok=True)
            (INPUT / rel).write_bytes(data)
        local[url] = rel
        # hops: every address the fetch asked, the page's first and finalUrl
        # (where the bytes came from) last. Gate A's --original-remote lets
        # the untouched original load exactly this chain, so it sees the
        # picture this run brought in and nothing else.
        report["localized"].append({"url": url, "finalUrl": chain[-1], "hops": chain,
                                    "file": rel, "bytes": len(data)})

    # Rewrite each reference RELATIVE to the page that holds it, in both the
    # raw and the entity-encoded spelling a static export writes.
    if local:
        for f in pages:
            text = f.read_text(encoding="utf-8", errors="replace")
            up = os.path.relpath(INPUT, f.parent).replace(os.sep, "/")
            prefix = "" if up == "." else f"{up}/"
            def swap(u):
                path = local.get(unescape(u))
                return f"{prefix}{path}" if path else u
            new = SRC.sub(lambda m: f"{m.group(1)}{m.group(2)}{swap(m.group(3))}{m.group(2)}", text)
            new = SRCSET.sub(lambda m: f"{m.group(1)}{m.group(2)}" + ", ".join(
                " ".join([swap(c.strip().split(" ")[0])] + c.strip().split(" ")[1:]) for c in m.group(3).split(",")
            ) + m.group(2), new)
            if new != text:
                report["rewrote"].append({"file": str(f.relative_to(INPUT)), "references": "remote images"})
                if args.apply:
                    f.write_text(new, encoding="utf-8")

def is_animated(path):
    try:
        with Image.open(path) as im:
            return getattr(im, "n_frames", 1) > 1
    except Exception:
        return False

candidates = [p for p in sorted(INPUT.rglob("*"))
              if p.is_file() and p.suffix.lower() in RASTER]

def encode(src, taken):
    """One candidate, start to finish. Returns what the report records;
    touches nothing outside its own destination group, so any number of
    groups can run at once. `taken` holds the destinations this group has
    already filled."""
    size = src.stat().st_size
    if size < args.min_bytes:
        return "skipped", {"file": str(src.relative_to(INPUT)), "why": "small", "bytes": size}
    if is_animated(src):
        return "skipped", {"file": str(src.relative_to(INPUT)), "why": "animated", "bytes": size}
    dst = src.with_suffix(".webp")
    # A .webp of that name is someone else's: the site's own (a <picture>
    # source) or the one a sibling a.jpg just became. Writing over it swaps
    # the picture behind every reference to it; the "not smaller" unlink then
    # deleted it outright — and the sibling's original went at the end of the
    # run, leaving the page pointing at nothing. The original stays instead.
    key = str(dst).casefold()
    if key in taken or dst.exists():
        return "skipped", {"file": str(src.relative_to(INPUT)), "why": f"{dst.name} already exists",
                           "bytes": size}
    try:
        with Image.open(src) as im:
            # Alpha is preserved; a palette image is promoted so the encoder
            # sees real channels rather than an index.
            im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
            im.save(dst, "WEBP", quality=args.quality, method=6)
    except Exception as e:  # a corrupt or exotic file is reported, never fatal
        dst.unlink(missing_ok=True)
        return "skipped", {"file": str(src.relative_to(INPUT)), "why": f"encode failed: {e}", "bytes": size}
    new_size = dst.stat().st_size
    if new_size >= size:
        dst.unlink(missing_ok=True)
        return "skipped", {"file": str(src.relative_to(INPUT)), "why": "webp was not smaller",
                           "bytes": size, "webpBytes": new_size}
    taken.add(key)
    if not args.apply:
        dst.unlink(missing_ok=True)
    return "converted", {"file": str(src.relative_to(INPUT)), "bytes": size, "webpBytes": new_size,
                         "saved": size - new_size}


# a.png and a.jpg both write a.webp, so candidates sharing a destination are
# one unit, run in sorted order in one worker — exactly the sequence a single
# worker produces. Keyed case-folded: on a case-insensitive disk A.PNG and
# a.jpg share a file too. Threads, not processes: Pillow drops the GIL while
# it encodes, and the script has no __main__ guard for a process pool.
def encode_group(group):
    taken = set()
    return [encode(src, taken) for src in group]

if args.jobs > 1:
    from concurrent.futures import ThreadPoolExecutor
    groups = {}
    for src in candidates:
        groups.setdefault(str(src.with_suffix(".webp")).casefold(), []).append(src)
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        done = list(pool.map(encode_group, groups.values()))
    results = {src: r for g, rs in zip(groups.values(), done) for src, r in zip(g, rs)}
else:
    taken = set()
    results = {src: encode(src, taken) for src in candidates}

# References are rewritten by BASENAME (below), so a name can only change
# when every file carrying it did: convert assets/a.png but keep a small
# icons/a.png, and the icon's references would be pointed at an icons/a.webp
# that was never written. Such a conversion is undone instead.
kept = {src.name for src in candidates if results[src][0] != "converted"}
for src in candidates:
    kind, entry = results[src]
    if kind == "converted" and src.name in kept:
        if args.apply:
            src.with_suffix(".webp").unlink(missing_ok=True)
        results[src] = "skipped", {"file": entry["file"], "why": f"another {src.name} is kept",
                                   "bytes": entry["bytes"], "webpBytes": entry["webpBytes"]}

# Recorded in candidate order, whatever order the workers finished in.
before = after = 0
renames = {}          # old path (relative, posix) -> new name
for src in candidates:
    kind, entry = results[src]
    report[kind].append(entry)
    if kind == "converted":
        before += entry["bytes"]
        after += entry["webpBytes"]
        renames[src.relative_to(INPUT).as_posix()] = src.with_suffix(".webp").name

# ---- references -----------------------------------------------------------
# Matched on the BASENAME, because a page two directories down writes
# ../../assets/x.png for the same file the stylesheet beside it writes x.png.
# Only names this run actually converted are in the map, and only as a whole
# name: `a.jpg` inside `banner-a.jpg` or `a.jpgx.png` is another file. A
# URL-encoded slash (`%2Fa.jpg`, Next's image loader) still counts as a
# boundary.
basenames = {Path(k).name: v for k, v in renames.items()}
if basenames:
    pattern = re.compile(r"(?:(?<=%2F)|(?<=%2f)|(?<![\w.-]))(?:"
                         + "|".join(re.escape(n) for n in sorted(basenames, key=len, reverse=True))
                         + r")(?![\w-])")
    for f in sorted(INPUT.rglob("*")):
        if not f.is_file() or f.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        swapped = pattern.sub(lambda m: basenames[m.group(0)], text)
        if swapped != text:
            hits = len(pattern.findall(text))
            report["rewrote"].append({"file": str(f.relative_to(INPUT)), "references": hits})
            if args.apply:
                f.write_text(swapped, encoding="utf-8")

if args.apply:
    for rel in renames:
        (INPUT / rel).unlink(missing_ok=True)

report["totals"] = {
    "converted": len(report["converted"]), "skipped": len(report["skipped"]),
    "filesRewritten": len(report["rewrote"]),
    "bytesBefore": before, "bytesAfter": after, "saved": before - after,
    "savedPercent": round((before - after) / before * 100, 1) if before else 0.0,
}
out = Path(args.out) if args.out else INPUT.parent / "optimize-images-report.json"
out.write_text(json.dumps(report, indent=1))

mb = lambda n: f"{n / 1_048_576:.1f} MB"
verb = "converted" if args.apply else "would convert"
if args.remote:
    print(f"{'brought in' if args.apply else 'would bring in'} {len(report['localized'])} remote image(s)"
          + (f"; {len(report['remoteFailed'])} left hotlinked" if report["remoteFailed"] else ""))
    for r in report["remoteFailed"][:10]:
        print(f"    left hotlinked: {r['url'][:90]} — {r['why']}")
print(f"{verb} {len(report['converted'])} image(s): {mb(before)} -> {mb(after)} "
      f"({report['totals']['savedPercent']}% smaller), "
      f"{len(report['rewrote'])} file(s) {'rewritten' if args.apply else 'to rewrite'}, "
      f"{len(report['skipped'])} left alone -> {out}")
for s in report["skipped"][:10]:
    print(f"    left alone: {s['file']} — {s['why']}")
if not args.apply:
    print("  nothing written — re-run with --apply, then rebuild from stage 1")
