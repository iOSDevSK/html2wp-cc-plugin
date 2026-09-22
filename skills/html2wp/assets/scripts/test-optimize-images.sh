#!/usr/bin/env bash
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
#
# optimize-images.py rewrites references by BASENAME and deletes the originals
# it converted, so every way two files can share a name is a way to leave a
# page pointing at nothing. Pinned here, offline:
#   - a.jpg + a.png both want a.webp: the second never overwrites or deletes
#     the first's (it did — then a.jpg went too, and the page lost the image)
#   - a .webp the site already ships is never overwritten or deleted
#   - `1.jpg` converted never rewrites `img1.jpg` or `1.jpgx.png`
#   - assets/p.png converted while icons/p.png is kept: the conversion is undone
#   - after --apply every src/href in the page resolves to a file that exists
#   - --jobs 4 writes the same bytes and the same report as --jobs 1
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
fail=0; check() { if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1: got '$2', want '$3'"; fail=1; fi; }

mkdir -p "$TMP/site/assets" "$TMP/site/icons" && python3 - "$TMP/site" <<'PY'
import sys
from PIL import Image
d = sys.argv[1]
def grad(w, h):
    im = Image.new("RGB", (w, h))
    im.putdata([(x % 256, y % 256, (x + y) % 256) for y in range(h) for x in range(w)])
    return im
grad(400, 300).save(f"{d}/a.jpg", quality=95)     # converts
grad(400, 300).save(f"{d}/a.png")                 # its WebP is NOT smaller
grad(300, 300).save(f"{d}/b.jpg", quality=95)     # converts, but b.webp is the site's own
grad(16, 16).save(f"{d}/b.webp")
grad(320, 240).save(f"{d}/1.jpg", quality=95)     # converts
grad(500, 400).save(f"{d}/img1.png")              # PNG kept (not smaller) — name ends in "1.png", not "1.jpg"
grad(320, 240).save(f"{d}/img1.jpg", quality=95)  # converts on its own merit
grad(500, 400).save(f"{d}/1.jpgx.png")            # kept
def photo(w, h):  # noisy enough that PNG loses to WebP
    return Image.merge("RGB", [Image.effect_noise((w, h), 40)] * 3)
photo(320, 240).save(f"{d}/assets/p.png")         # would convert…
Image.new("RGB", (4, 4)).save(f"{d}/icons/p.png") # …but this p.png is too small to
open(f"{d}/index.html", "w").write(
    '<img src="a.jpg"><img src="a.png"><img src="b.jpg"><picture><source srcset="b.webp"></picture>'
    '<img src="1.jpg"><img src="img1.jpg"><img src="img1.png"><img src="1.jpgx.png">'
    '<img src="assets/p.png"><img src="icons/p.png"><a href="1.jpg">x</a>')
PY
cp -a "$TMP/site" "$TMP/site4"
b_before="$(shasum "$TMP/site/b.webp" | cut -d' ' -f1)"

python3 "$HERE/optimize-images.py" --input "$TMP/site" --min-bytes 100 --apply --out "$TMP/r1.json" > "$TMP/o1.txt" 2>&1
python3 "$HERE/optimize-images.py" --input "$TMP/site4" --min-bytes 100 --apply --jobs 4 --out "$TMP/r4.json" > "$TMP/o4.txt" 2>&1

missing="$(python3 - "$TMP/site" <<'PY'
import os, re, sys
d = sys.argv[1]
html = open(f"{d}/index.html").read()
print(" ".join(r for r in re.findall(r'(?:src|srcset|href)="([^"]+)"', html) if not os.path.exists(os.path.join(d, r))))
PY
)"
check "every reference resolves" "$missing" ""
check "a.jpg became a.webp and a.png stays" "$([ -f "$TMP/site/a.webp" ] && [ -f "$TMP/site/a.png" ] && [ ! -e "$TMP/site/a.jpg" ] && echo yes)" "yes"
check "the site's own b.webp is untouched" "$(shasum "$TMP/site/b.webp" | cut -d' ' -f1)" "$b_before"
check "b.jpg is kept beside it" "$([ -f "$TMP/site/b.jpg" ] && echo yes)" "yes"
check "img1.jpg converted on its own" "$(grep -o 'src="img1[^"]*"' "$TMP/site/index.html" | tr '\n' ' ')" 'src="img1.webp" src="img1.png" '
check "1.jpgx.png untouched" "$(grep -c 'src="1.jpgx.png"' "$TMP/site/index.html")" "1"
check "assets/p.png kept while icons/p.png is" "$([ -f "$TMP/site/assets/p.png" ] && [ ! -e "$TMP/site/assets/p.webp" ] && echo yes)" "yes"
check "--jobs 4 writes the same files" "$(diff -r "$TMP/site" "$TMP/site4" >/dev/null && echo same)" "same"
check "--jobs 4 writes the same report" "$(cmp -s "$TMP/r1.json" "$TMP/r4.json" && echo same)" "same"

[ "$fail" = 0 ] && echo "ALL OK" || { echo "FAILED"; cat "$TMP/o1.txt"; exit 1; }
