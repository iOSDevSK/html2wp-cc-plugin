#!/usr/bin/env bash
# Regression test: the upload loop always ends.
#
#   assets/scripts/test-upload-bounded.sh
#
# T10 of the adversarial set. The failure: the loop that pushes the workspace
# to the service had no bound of any kind.
#
#   000  curl could not finish (no route, reset, DNS gone). The loop slept two
#        seconds and tried the same piece again — with no counter, no ceiling
#        and no deadline. An unreachable service meant a conversion that spun
#        in silence until somebody noticed and pressed ctrl-C.
#   416  "resume from expectedOffset". The loop assigned that straight to its
#        own loop variable without checking it was a number or that it moved
#        FORWARD. A server answering with a constant re-uploaded the same
#        bytes indefinitely; one answering with nothing set OFFSET to "" and
#        crashed inside [ ].
#
# Both are driven here against a fake service that misbehaves on purpose. The
# assertion is not "it fails" — it is "it STOPS, and says why".
#
# convert-remote.sh is the public edition's client and has no counterpart in
# this repo, so the test reaches for the skill tree. H2WP_CLIENT overrides it.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Next to this script when it ships inside the skill; the protected checkout
# when it runs from the R&D repo, which has no convert-remote.sh of its own.
CLIENT="${H2WP_CLIENT:-}"
[ -z "$CLIENT" ] && [ -f "$SCRIPT_DIR/convert-remote.sh" ] && CLIENT="$SCRIPT_DIR/convert-remote.sh"
[ -z "$CLIENT" ] && CLIENT="$HOME/Developer/html2wp-sub-protected/skill/skills/html2wp/assets/scripts/convert-remote.sh"
[ -f "$CLIENT" ] || { echo "SKIP — no convert-remote.sh at $CLIENT (set H2WP_CLIENT)"; exit 0; }
CLIENT_VERSION_FILE="$(cd "$(dirname "$CLIENT")/../.." && pwd)/VERSION"
[ -f "$CLIENT_VERSION_FILE" ] || { echo "FAIL — client has no shared VERSION at $CLIENT_VERSION_FILE"; exit 1; }

TMP="$(mktemp -d)"
SRV_PID=""
cleanup() { [ -n "$SRV_PID" ] && kill "$SRV_PID" 2>/dev/null; rm -rf "$TMP"; }
trap cleanup EXIT
fail=0

# --- a workspace the client will agree to upload ---------------------------
WS="$TMP/ws"
mkdir -p "$WS/astro-project/dist"
printf '<!doctype html><title>x</title><body>x</body>\n' > "$WS/astro-project/dist/index.html"
printf '{}\n' > "$WS/astro-report.json"
python3 - "$WS" <<'PY'
import json, sys
ws = sys.argv[1]
json.dump({
    "schema": "html2wp/1",
    "site": {"name": "Bound", "slug": "bound", "prefix": "bound", "home": ""},
    "input": {"dir": f"{ws}/astro-project/dist", "type": "static-html", "devHosts": []},
    "workspace": ws,
    "pages": [{"file": "index.html", "key": "front-page", "kind": "front",
               "title": "x", "chrome": "consensus"}],
}, open(f"{ws}/conversion-manifest.json", "w"))
PY

# --- a service that opens a job and then misbehaves on the upload ----------
start_server() { # <mode>
  python3 - "$TMP" "$1" > "$TMP/server.log" 2>&1 &
  SRV_PID=$!
  for _ in $(seq 1 50); do
    [ -f "$TMP/port" ] && return 0
    sleep 0.1
  done
  echo "the fake service never started" >&2; exit 1
} <<'PY'
import http.server, json, os, socketserver, sys, threading

tmp, mode = sys.argv[1], sys.argv[2]

class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        if self.path == "/v1/jobs":
            with open(os.path.join(tmp, "client-headers"), "a") as fh:
                fh.write(json.dumps({
                    "version": self.headers.get("x-html2wp-client"),
                    "host": self.headers.get("x-html2wp-host"),
                }) + "\n")
            body = json.dumps({
                "job": "job_fake",
                "token": "tok",
                "edition": "lite",
                "upload": {"url": f"http://127.0.0.1:{PORT}/v1/uploads/tok"},
            }).encode()
            self.send_response(201)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_PUT(self):
        # Drain the piece so curl is not blocked writing.
        n = int(self.headers.get("content-length") or 0)
        while n > 0:
            n -= len(self.rfile.read(min(n, 65536)) or b"")
        if mode == "drop":
            # No reply at all: curl reports an empty reply, the client sees 000.
            self.close_connection = True
            try:
                self.connection.close()
            except OSError:
                pass
            return
        # mode == "stuck": always answer "resume from 0", forever.
        body = json.dumps({"expectedOffset": 0}).encode()
        self.send_response(416)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

with socketserver.TCPServer(("127.0.0.1", 0), Handler) as srv:
    PORT = srv.server_address[1]
    with open(os.path.join(tmp, "port"), "w") as fh:
        fh.write(str(PORT))
    srv.serve_forever()
PY

run_client() { # <mode> <label>
  rm -f "$TMP/port" "$WS/.h2wp-job.json"
  start_server "$1"
  PORT="$(cat "$TMP/port")"
  started=$(date +%s)
  set +e
  H2WP_HOST=codex H2WP_UPLOAD_MAX_FAILS=3 H2WP_UPLOAD_TOTAL_SECONDS=60 \
    bash "$CLIENT" "$WS" --api="http://127.0.0.1:$PORT" > "$TMP/out.log" 2>&1
  rc=$?
  set -e
  elapsed=$(( $(date +%s) - started ))
  kill "$SRV_PID" 2>/dev/null; SRV_PID=""

  if [ "$rc" = 0 ]; then
    echo "FAIL — $2: the client reported success against a service that never accepted the upload"
    fail=1
  elif [ "$elapsed" -ge 90 ]; then
    echo "FAIL — $2: took ${elapsed}s; the loop is not bounded"
    fail=1
  else
    echo "ok   — $2: stopped after ${elapsed}s with exit $rc"
  fi
}

echo "== the upload loop ends when the service does not =="

# 1. Transport keeps failing: bounded by the consecutive-failure counter.
run_client drop "connection dropped every time"
if grep -q 'failed .* times in a row' "$TMP/out.log"; then
  echo "ok   — it says how it gave up, and that nothing is lost"
else
  echo "FAIL — it stopped without explaining why:"; sed 's/^/    /' "$TMP/out.log" | tail -5
  fail=1
fi

# 2. Server keeps saying "resume from 0": bounded by the no-progress check.
run_client stuck "service always answers resume-from-0"
if grep -q 'not progressing' "$TMP/out.log"; then
  echo "ok   — a resume offset that never advances is detected"
else
  echo "FAIL — it did not notice the upload standing still:"; sed 's/^/    /' "$TMP/out.log" | tail -5
  fail=1
fi

# 3. The same bundle serves Codex and Claude Code. The service needs both the
# shared release version and the host in order to return the right update
# instruction; neither may silently disappear from a future curl edit.
if python3 - "$TMP/client-headers" "$CLIENT_VERSION_FILE" <<'PY'
import json, pathlib, sys
rows = [json.loads(line) for line in pathlib.Path(sys.argv[1]).read_text().splitlines()]
version = pathlib.Path(sys.argv[2]).read_text().strip()
raise SystemExit(0 if rows and all(r == {"version": version, "host": "codex"} for r in rows) else 1)
PY
then
  echo "ok   — every job request identifies the shared version and Codex host"
else
  echo "FAIL — client metadata headers are missing or disagree with VERSION"
  fail=1
fi

echo
if [ "$fail" = 0 ]; then echo "ALL OK"; else echo "$fail failing check(s)"; exit 1; fi
