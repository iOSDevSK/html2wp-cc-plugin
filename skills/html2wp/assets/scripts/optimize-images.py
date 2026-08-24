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
args = ap.parse_args()

INPUT = Path(args.input).resolve()
if not INPUT.is_dir():
    sys.exit(f"not a directory: {INPUT}")

report = {"quality": args.quality, "applied": bool(args.apply),
          "converted": [], "skipped": [], "rewrote": [], "totals": {}}

def is_animated(path):
    try:
        with Image.open(path) as im:
            return getattr(im, "n_frames", 1) > 1
    except Exception:
        return False

candidates = [p for p in sorted(INPUT.rglob("*"))
              if p.is_file() and p.suffix.lower() in RASTER]

before = after = 0
renames = {}          # old path (relative, posix) -> new name
for src in candidates:
    size = src.stat().st_size
    if size < args.min_bytes:
        report["skipped"].append({"file": str(src.relative_to(INPUT)), "why": "small", "bytes": size})
        continue
    if is_animated(src):
        report["skipped"].append({"file": str(src.relative_to(INPUT)), "why": "animated", "bytes": size})
        continue
    dst = src.with_suffix(".webp")
    try:
        with Image.open(src) as im:
            # Alpha is preserved; a palette image is promoted so the encoder
            # sees real channels rather than an index.
            im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
            im.save(dst, "WEBP", quality=args.quality, method=6)
    except Exception as e:  # a corrupt or exotic file is reported, never fatal
        report["skipped"].append({"file": str(src.relative_to(INPUT)), "why": f"encode failed: {e}", "bytes": size})
        dst.unlink(missing_ok=True)
        continue
    new_size = dst.stat().st_size
    if new_size >= size:
        dst.unlink(missing_ok=True)
        report["skipped"].append({"file": str(src.relative_to(INPUT)), "why": "webp was not smaller",
                                  "bytes": size, "webpBytes": new_size})
        continue
    before += size
    after += new_size
    report["converted"].append({"file": str(src.relative_to(INPUT)), "bytes": size, "webpBytes": new_size,
                                "saved": size - new_size})
    renames[src.relative_to(INPUT).as_posix()] = dst.name
    if not args.apply:
        dst.unlink(missing_ok=True)

# ---- references -----------------------------------------------------------
# Matched on the BASENAME, because a page two directories down writes
# ../../assets/x.png for the same file the stylesheet beside it writes x.png.
# Only names this run actually converted are in the map, so an unrelated
# string that happens to end .png is untouched.
basenames = {Path(k).name: v for k, v in renames.items()}
if basenames:
    pattern = re.compile("|".join(re.escape(n) for n in sorted(basenames, key=len, reverse=True)))
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
print(f"{verb} {len(report['converted'])} image(s): {mb(before)} -> {mb(after)} "
      f"({report['totals']['savedPercent']}% smaller), "
      f"{len(report['rewrote'])} file(s) {'rewritten' if args.apply else 'to rewrite'}, "
      f"{len(report['skipped'])} left alone -> {out}")
for s in report["skipped"][:10]:
    print(f"    left alone: {s['file']} — {s['why']}")
if not args.apply:
    print("  nothing written — re-run with --apply, then rebuild from stage 1")
