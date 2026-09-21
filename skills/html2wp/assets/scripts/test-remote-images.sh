#!/usr/bin/env bash
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
#
# optimize-images.py --remote makes network requests to addresses a PAGE
# names. That is the SSRF shape net_guard exists for, so the refusals are
# pinned here, offline:
#   - a private address (127.0.0.1, the metadata IP) is never fetched
#   - plain http is never fetched
#   - a refused image stays exactly as authored — hotlinked, not broken
#   - measuring (no --apply) writes nothing
#   - a redirect is followed hop by hop, every hop judged again, and the chain
#     is recorded (`hops`, `finalUrl`) — gate A's --original-remote allows
#     exactly that chain, nothing else
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP="$(mktemp -d)"; SRV2=""; trap 'rm -rf "$TMP"; kill "$SRV" $SRV2 2>/dev/null' EXIT
fail=0; check() { if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1: got '$2', want '$3'"; fail=1; fi; }

# A local server that WOULD answer with a real PNG, so a refusal is the
# guard's doing and not a dead address.
mkdir -p "$TMP/srv" && python3 -c "
from PIL import Image; Image.new('RGB', (4, 4), (255, 0, 0)).save('$TMP/srv/red.png')"
(cd "$TMP/srv" && exec python3 -m http.server 18765 --bind 127.0.0.1 >/dev/null 2>&1) & SRV=$!
sleep 1

mkdir -p "$TMP/site"
cat > "$TMP/site/index.html" <<'HTML'
<!doctype html><html><body>
<img src="https://127.0.0.1:18765/red.png" alt="private https">
<img src="http://127.0.0.1:18765/red.png" alt="plain http">
<img src="https://169.254.169.254/latest/meta-data/x.png" alt="metadata">
<img srcset="https://127.0.0.1:18765/red.png 1x, http://127.0.0.1:18765/red.png 2x" alt="set">
</body></html>
HTML
cp "$TMP/site/index.html" "$TMP/before.html"

python3 "$HERE/optimize-images.py" --input "$TMP/site" --remote --out "$TMP/r.json" >/dev/null 2>&1
check "measuring writes nothing" "$(cmp -s "$TMP/site/index.html" "$TMP/before.html" && echo same)" "same"

python3 "$HERE/optimize-images.py" --input "$TMP/site" --remote --apply --out "$TMP/r.json" >/dev/null 2>&1
check "no private or http image fetched" "$(python3 -c "import json;print(len(json.load(open('$TMP/r.json'))['localized']))")" "0"
check "every refusal is reported" "$(python3 -c "import json;print(len(json.load(open('$TMP/r.json'))['remoteFailed']))")" "3"
check "refused references stay as authored" "$(cmp -s "$TMP/site/index.html" "$TMP/before.html" && echo same)" "same"
check "nothing written into the site" "$([ -d "$TMP/site/assets/remote" ] && echo dir || echo none)" "none"

# Redirects. The guard only passes https to a public address, so this part
# stands up a throwaway https server on loopback and runs the script with
# address_verdict told that loopback is public — except any URL naming
# "private", which stands in for a hop to a private address. The real
# verdict is the one the checks above exercised.
if openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj "/CN=127.0.0.1" \
     -addext "subjectAltName=IP:127.0.0.1" -keyout "$TMP/key.pem" -out "$TMP/cert.pem" >/dev/null 2>&1; then
  mkdir -p "$TMP/tls" && cp "$TMP/srv/red.png" "$TMP/tls/red.png"
  python3 - "$TMP" <<'PY' & SRV2=$!
import functools, ssl, sys
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
t = sys.argv[1]
class H(SimpleHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        hops = {"/hop": "/red.png", "/to-private": "/private/red.png", "/a": "/b", "/b": "/red.png"}
        if self.path in hops:
            self.send_response(302); self.send_header("Location", hops[self.path]); self.end_headers(); return
        return super().do_GET()
httpd = ThreadingHTTPServer(("127.0.0.1", 18766), functools.partial(H, directory=f"{t}/tls"))
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx.load_cert_chain(f"{t}/cert.pem", f"{t}/key.pem")
httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
httpd.serve_forever()
PY
  sleep 1
  mkdir -p "$TMP/site2"
  printf '%s\n' '<img src="https://127.0.0.1:18766/hop" alt="redirected">' \
                '<img src="https://127.0.0.1:18766/to-private" alt="redirected to private">' \
                '<img src="https://127.0.0.1:18766/a" alt="two redirects">' > "$TMP/site2/index.html"
  SSL_CERT_FILE="$TMP/cert.pem" python3 - "$HERE" "$TMP" <<'PY' >/dev/null 2>&1
import runpy, sys
here, t = sys.argv[1], sys.argv[2]
sys.path.insert(0, f"{here}/lib")
import net_guard
net_guard.address_verdict = lambda u: "a private address (stand-in)" if "private" in u else None
sys.argv = ["optimize-images.py", "--input", f"{t}/site2", "--remote", "--apply", "--out", f"{t}/r2.json"]
runpy.run_path(f"{here}/optimize-images.py", run_name="__main__")
PY
  check "a redirected image is brought in, with where it ended" \
    "$(python3 -c "import json;r=json.load(open('$TMP/r2.json'));print([(l['url'][-4:], l['finalUrl'][-8:]) for l in r['localized']])")" \
    "[('66/a', '/red.png'), ('/hop', '/red.png')]"
  check "an A -> B -> C redirect records the whole chain" \
    "$(python3 -c "import json;r=json.load(open('$TMP/r2.json'));from urllib.parse import urlparse;print([urlparse(h).path for h in r['localized'][0]['hops']])")" \
    "['/a', '/b', '/red.png']"
  check "a hop to a private address is refused and reported" \
    "$(python3 -c "import json;r=json.load(open('$TMP/r2.json'));print([f['url'][-11:] for f in r['remoteFailed']])")" \
    "['/to-private']"
else
  echo "  skip redirect case: openssl could not make a test certificate"
fi

[ "$fail" = 0 ] && echo "ALL OK" || { echo "FAILED"; exit 1; }
