#!/usr/bin/env bash
# Regression test for optimize-markup.py (stage 0.6): width/height go only on
# images whose drawn box they do not change.
#
#   assets/scripts/test-optimize-markup.sh
#
# Why this exists: the attributes are presentational hints for the CSS
# width/height properties, not just an aspect ratio. A design that sets
# `width:100%; aspect-ratio:3/4` and no `height:auto` drew every portrait at the
# file's own 2:3 once `height="1536"` was added — gate A red on six pages of a
# hand-written site, 43-91% of pixels. Each case below is one CSS shape.
# Needs Pillow and Playwright's chromium, like the script itself.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
T="$(mktemp -d "${TMPDIR:-/tmp}/h2wp-optimize-markup.XXXXXX")"
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/site/assets"
python3 - "$T/site/assets" <<'PY'
import sys
from PIL import Image
Image.new("RGB", (400, 600), (180, 120, 90)).save(f"{sys.argv[1]}/portrait.png")
Image.new("RGB", (300, 200), (90, 120, 180)).save(f"{sys.argv[1]}/wide.png")
PY
cat > "$T/site/index.html" <<'HTML'
<!doctype html><html><head><meta charset="utf-8"><style>
body{margin:0;width:800px}
img{display:block;max-width:100%}
.auto img{width:100%;height:auto}
.ratio img{width:100%;aspect-ratio:3/4;object-fit:cover}
.fixed img{width:120px;height:90px;object-fit:cover}
.half img{width:50%}
.frame{height:240px}.frame img{width:100%;height:100%;object-fit:cover}
.grid{display:grid;grid-template-columns:1fr 1fr 1fr}.grid img{width:100%}
</style></head><body>
<div class="auto"><img id="auto" src="assets/portrait.png" alt=""></div>
<div class="ratio"><img id="ratio" src="assets/portrait.png" alt=""></div>
<div class="fixed"><img id="fixed" src="assets/wide.png" alt=""></div>
<div><img id="natural" src="assets/wide.png" alt=""></div>
<div class="half"><img id="half" src="assets/wide.png" alt=""></div>
<div class="frame"><img id="frame" src="assets/portrait.png" alt=""></div>
<div class="grid"><img id="grid" src="assets/portrait.png" alt=""><span></span><span></span></div>
<div><img id="declared" src="assets/wide.png" width="30" alt=""></div>
</body></html>
HTML
echo "== stage 0.6 sizes an image only when that leaves its box where the design drew it =="
python3 -W ignore "$SCRIPT_DIR/optimize-markup.py" --input "$T/site" --apply --out "$T/report.json" >/dev/null
python3 - "$T/site/index.html" <<'PY'
import re, sys
html = open(sys.argv[1]).read()
want = {  # id -> gets width/height from the file?
    "auto": True,       # height:auto overrides the hint
    "ratio": False,     # aspect-ratio without height:auto: the hint pins the height
    "fixed": True,      # both dimensions set in CSS
    "natural": True,    # drawn at its own size either way
    "half": False,      # a CSS width alone: the hint keeps the file's height
    "frame": True,      # height:100% of a sized frame
    "grid": False,      # a grid cell narrower than the file, no height rule
    "declared": None,   # the author's own width stays, nothing is added
}
fail = 0
for key, expect in want.items():
    tag = re.search(rf'<img id="{key}"[^>]*>', html).group(0)
    sized = bool(re.search(r'\bheight="\d+"', tag))
    ok = (not sized and 'width="30"' in tag) if expect is None else sized == expect
    fail += not ok
    print(f"  {'ok  ' if ok else 'FAIL'} {key}: {'sized' if sized else 'unsized'}  {tag}")
if fail:
    sys.exit(f"{fail} case(s) wrong")
print("optimize-markup: all cases passed")
PY
