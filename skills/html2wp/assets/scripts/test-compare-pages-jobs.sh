#!/usr/bin/env bash
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
#
# compare-pages.py --jobs N splits the pages over N processes, one Chromium
# each. What the reviewer gets must not depend on N. Pinned here, with no
# WordPress: a stand-in server answers the URLs compare-pages asks WordPress
# for (/, /{key}/, the posts REST route) from the same static files.
#   - --jobs 3 writes the same review-manifest.json and the same composite
#     bytes as --jobs 1 (pages in manifest order, articles found by <h1>)
#   - a page whose heights differ is captured again, alone, and marked
#     "recaptured"; everything else is still identical
#   - every composite is also cut into viewport-height tiles that, stacked,
#     are exactly the composite, and the manifest lists them
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP="$(mktemp -d)"; SRV=""
trap '[ -n "$SRV" ] && kill "$SRV" 2>/dev/null; rm -rf "$TMP"' EXIT
fail=0; check() { if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1: got '$2', want '$3'"; fail=1; fi; }

python3 - "$TMP" <<'PY'
import json, sys
from pathlib import Path
t = Path(sys.argv[1]); site = t / "site"; (site / "blog").mkdir(parents=True)
css = "<style>body{margin:0;font:18px/1.5 sans-serif} section{padding:60px 40px} .tall{height:%dpx;background:%s}</style>"
def page(title, h1, tall, color):
    return (f"<!doctype html><html><head><title>{title}</title>{css % (tall, color)}</head><body>"
            f"<section><h1>{h1}</h1><p>{'Lorem ipsum dolor sit amet. ' * 30}</p></section>"
            f"<div class=tall></div><section><p>footer</p></section></body></html>")
(site / "index.html").write_text(page("Home", "Home", 1400, "#2a6"))
(site / "about.html").write_text(page("About", "About us", 900, "#a62"))
(site / "work.html").write_text(page("Work", "Work", 2200, "#26a"))
(site / "blog.html").write_text(page("Blog", "Journal", 600, "#666"))
(site / "blog" / "first-post.html").write_text(page("First | Site", "The First Post &amp; More", 1200, "#a2a"))
(t / "manifest.json").write_text(json.dumps({
    "workspace": str(t), "input": {"dir": str(site)}, "blog": {"present": True},
    "pages": [
        {"file": "index.html", "key": "front-page", "kind": "front"},
        {"file": "about.html", "key": "about", "kind": "page"},
        {"file": "work.html", "key": "work", "kind": "page"},
        {"file": "blog.html", "key": "blog", "kind": "listing"},
        {"file": "blog/first-post.html", "key": "blog-first-post", "kind": "article", "title": "First | Site"},
        {"file": "missing.html", "key": "missing", "kind": "page"},
    ]}))
PY

# The stand-in WordPress: serves <root>, redirects /{key}/ to the page file
# (so relative URLs resolve as they do in the original), answers the posts
# REST route with each article's <h1>. Writes its port to <portfile>.
cat > "$TMP/fakewp.py" <<'PY'
import functools, json, re, sys
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
mf, root, portfile = json.loads(Path(sys.argv[1]).read_text()), Path(sys.argv[2]), sys.argv[3]
route, posts = {}, []
class H(SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        p = self.path.split("?")[0]
        if p.startswith("/wp-json/wp/v2/posts"):
            b = json.dumps(posts).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b))); self.end_headers(); self.wfile.write(b); return
        if p == "/":
            self.path = "/index.html"
        elif p in route:
            self.send_response(302); self.send_header("Location", route[p]); self.end_headers(); return
        return super().do_GET()
httpd = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(H, directory=str(root)))
base = f"http://127.0.0.1:{httpd.server_port}"
for e in mf["pages"]:
    route[f"/{e['key']}/"] = "/" + e["file"]
    if e.get("kind") == "article":
        m = re.search(r"<h1[^>]*>(.*?)</h1>", (root / e["file"]).read_text(), re.S)
        posts.append({"title": {"rendered": m.group(1).replace("&amp;", "&#038;")}, "link": f"{base}/{e['key']}/"})
Path(portfile).write_text(str(httpd.server_port))
httpd.serve_forever()
PY

serve() {  # serve <root>: starts the stand-in, sets SRV and WP
  [ -n "$SRV" ] && kill "$SRV" 2>/dev/null; rm -f "$TMP/port"
  python3 "$TMP/fakewp.py" "$TMP/manifest.json" "$1" "$TMP/port" & SRV=$!
  for _ in $(seq 50); do [ -s "$TMP/port" ] && break; sleep 0.1; done
  WP="http://127.0.0.1:$(cat "$TMP/port")"
}
compare() {  # compare <out> <jobs>
  python3 -W ignore::SyntaxWarning "$HERE/compare-pages.py" --manifest "$TMP/manifest.json" --wp "$WP" \
    --out "$TMP/$1" --jobs "$2" > "$TMP/$1.txt" 2>&1 || { echo "  compare-pages failed:"; cat "$TMP/$1.txt"; }
}
same_pngs() {  # same_pngs <dirA> <dirB>: every PNG byte-identical, same set
  a="$(cd "$TMP/$1" && ls *.png | sort)"; b="$(cd "$TMP/$2" && ls *.png | sort)"
  [ "$a" = "$b" ] || { echo "file sets differ"; return; }
  for f in $a; do cmp -s "$TMP/$1/$f" "$TMP/$2/$f" || { echo "differ: $f"; return; }; done
  echo same
}

serve "$TMP/site"
compare j1 1
compare j3 3
check "--jobs 1 made a composite for every capturable page" \
  "$(python3 -c "import json;print(sum('composite' in p for p in json.load(open('$TMP/j1/review-manifest.json'))['pairs']))")" "5"
check "--jobs 3: same review-manifest.json" "$(cmp -s "$TMP/j1/review-manifest.json" "$TMP/j3/review-manifest.json" && echo same)" "same"
check "--jobs 3: same composite bytes" "$(same_pngs j1 j3)" "same"
check "tiles stack back into each composite" "$(python3 - "$TMP/j1" <<'PY'
import json, sys
from pathlib import Path
from PIL import Image, ImageChops
out = Path(sys.argv[1]); bad = []
for p in json.load(open(out / "review-manifest.json"))["pairs"]:
    if "composite" not in p:
        continue
    board = Image.open(out / p["composite"]).convert("RGB"); y = 44
    for t in p["tiles"]:
        tile = Image.open(out / t).convert("RGB"); h = tile.height - 44
        if ImageChops.difference(tile.crop((0, 44, tile.width, tile.height)), board.crop((0, y, board.width, y + h))).getbbox():
            bad.append(t)
        y += h
    if y != board.height or not p["tiles"]:
        bad.append(p["composite"])
print(" ".join(bad) or "ok")
PY
)" "ok"
check "--jobs 3: same stdout" "$(diff <(sed "s#$TMP/j1#OUT#" "$TMP/j1.txt") <(sed "s#$TMP/j3#OUT#" "$TMP/j3.txt") >/dev/null && echo same)" "same"

# The converted side of one page grows: a real height mismatch.
cp -a "$TMP/site" "$TMP/wp"
python3 -c "
p='$TMP/wp/about.html'; s=open(p).read(); open(p,'w').write(s.replace('</body>','<div style=\"height:700px\"></div></body>'))"
serve "$TMP/wp"
compare m1 1
compare m3 3
check "mismatched page is re-captured, and only it" \
  "$(python3 -c "import json;print([p['page'] for p in json.load(open('$TMP/m3/review-manifest.json'))['pairs'] if p.get('recaptured')])")" "['about.html']"
check "otherwise the same manifest as --jobs 1" "$(python3 -c "
import json
a = json.load(open('$TMP/m1/review-manifest.json')); b = json.load(open('$TMP/m3/review-manifest.json'))
for p in b['pairs']: p.pop('recaptured', None)
print(a == b)")" "True"
check "and the same composite bytes" "$(same_pngs m1 m3)" "same"

[ "$fail" = 0 ] && echo "ALL OK" || { echo "FAILED"; exit 1; }
