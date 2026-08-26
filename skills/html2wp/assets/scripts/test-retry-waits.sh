#!/usr/bin/env bash
# Regression test: the client waits for what waiting can clear, and only that.
#
#   assets/scripts/test-retry-waits.sh
#
# Three answers the client used to treat as the end of the road:
#
#   429 already_running   A conversion of this machine is still finishing.
#        Conversions run one at a time per caller, so this is a WAIT — and on
#        a service whose transform lease outlives the run that stamped it, it
#        could mean a twenty-minute wait after a conversion that already
#        failed. The client exited; the agent driving it wrapped the whole
#        script in a retry loop of its own, which is a loop that belongs here.
#   429 + retry-after     The box is running all the conversions it can. The
#        job keeps its place and its upload, and the call is safe to repeat.
#        A 429 WITHOUT retry-after is this job's attempts spent, where asking
#        again cannot succeed — the two must not be confused.
#   409 on the last piece The finalising PUT landed and its answer did not.
#        The upload is settled; resending it is answered "this job already has
#        its upload", which the loop took as a refusal and threw the archive
#        away.
#
# Driven against a fake service that gives exactly those answers, with each
# wait step capped to a second so the whole file runs in seconds.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLIENT="${H2WP_CLIENT:-}"
[ -z "$CLIENT" ] && [ -f "$SCRIPT_DIR/convert-remote.sh" ] && CLIENT="$SCRIPT_DIR/convert-remote.sh"
[ -z "$CLIENT" ] && CLIENT="$HOME/Developer/html2wp-sub-protected/skill/skills/html2wp/assets/scripts/convert-remote.sh"
[ -f "$CLIENT" ] || { echo "SKIP — no convert-remote.sh at $CLIENT (set H2WP_CLIENT)"; exit 0; }

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
    "site": {"name": "Waits", "slug": "waits", "prefix": "waits", "home": ""},
    "input": {"dir": f"{ws}/astro-project/dist", "type": "static-html", "devHosts": []},
    "workspace": ws,
    "pages": [{"file": "index.html", "key": "front-page", "kind": "front",
               "title": "x", "chrome": "consensus"}],
}, open(f"{ws}/conversion-manifest.json", "w"))
PY

# --- a service that refuses a fixed number of times, then relents ----------
#
# `mode` picks which refusal, and the counters live in files so the count
# survives the handler being re-entered per request.
start_server() { # <mode>
  python3 - "$TMP" "$1" > "$TMP/server.log" 2>&1 &
  SRV_PID=$!
  for _ in $(seq 1 50); do
    [ -f "$TMP/port" ] && return 0
    sleep 0.1
  done
  echo "the fake service never started" >&2; exit 1
} <<'PY'
import http.server, json, os, socketserver, sys

tmp, mode = sys.argv[1], sys.argv[2]
seen = {"jobs": 0, "transform": 0}


def count(name):
    seen[name] += 1
    with open(os.path.join(tmp, f"count-{name}"), "w") as fh:
        fh.write(str(seen[name]))
    return seen[name]


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def reply(self, status, payload, headers=()):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path == "/v1/jobs":
            nth = count("jobs")
            # Two refusals, then the job opens — the shape of a machine whose
            # previous conversion is still finishing.
            if mode == "already-running" and nth <= 2:
                return self.reply(
                    429,
                    {"error": "A conversion is already running. They run one at a time.",
                     "reason": "already_running"},
                    [("retry-after", "300")],
                )
            # A refusal that waiting cannot clear must stop the client at once.
            if mode == "conversions-used":
                return self.reply(
                    429,
                    {"error": "The free tier is used up.", "reason": "free_conversions_used"},
                    [("retry-after", "0")],
                )
            return self.reply(201, {
                "job": "job_fake",
                "token": "tok",
                "edition": "lite",
                "upload": {"url": f"http://127.0.0.1:{PORT}/v1/uploads/tok"},
            })

        if self.path.endswith("/transform"):
            nth = count("transform")
            if mode == "at-capacity" and nth <= 2:
                return self.reply(
                    429,
                    {"error": "the service is running as many conversions as it can at once"},
                    [("retry-after", "1")],
                )
            if mode == "attempts-spent":
                # Deliberately NO retry-after: asking again cannot help.
                return self.reply(429, {"error": "this job has had its transform attempts"})
            return self.reply(200, {
                "ok": True,
                "slug": "waits",
                "pages": 1,
                "downloads": {"theme": f"http://127.0.0.1:{PORT}/v1/downloads/theme"},
            })
        self.send_error(404)

    def do_PUT(self):
        n = int(self.headers.get("content-length") or 0)
        while n > 0:
            n -= len(self.rfile.read(min(n, 65536)) or b"")
        if mode == "already-uploaded":
            return self.reply(409, {"error": "this job already has its upload"})
        return self.reply(200, {"received": n, "settled": True, "reRun": False})

    def do_GET(self):
        # A real theme archive, so a success path really is one: the client
        # verifies, expands and unpacks what it downloads.
        self.send_response(200)
        self.send_header("content-type", "application/octet-stream")
        self.send_header("content-length", str(len(THEME)))
        self.end_headers()
        self.wfile.write(THEME)


def build_theme():
    import io, tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        style = b"/* fixture */\n"
        info = tarfile.TarInfo("theme/waits/style.css")
        info.size = len(style)
        tar.addfile(info, io.BytesIO(style))
    return buf.getvalue()


THEME = build_theme()


with socketserver.TCPServer(("127.0.0.1", 0), Handler) as srv:
    srv.allow_reuse_address = True
    PORT = srv.server_address[1]
    with open(os.path.join(tmp, "port"), "w") as fh:
        fh.write(str(PORT))
    srv.serve_forever()
PY

run_client() { # <mode>
  rm -f "$TMP/port" "$TMP/count-jobs" "$TMP/count-transform" "$WS/.h2wp-job.json" "$WS/.h2wp-result.json"
  start_server "$1"
  PORT="$(cat "$TMP/port")"
  STARTED=$(date +%s)
  set +e
  H2WP_HOST=codex H2WP_WAIT_STEP_SECONDS=1 H2WP_JOB_WAIT_SECONDS=20 H2WP_TRANSFORM_WAIT_SECONDS=20 \
    bash "$CLIENT" "$WS" --api="http://127.0.0.1:$PORT" > "$TMP/out.log" 2>&1
  RC=$?
  set -e
  ELAPSED=$(( $(date +%s) - STARTED ))
  kill "$SRV_PID" 2>/dev/null; SRV_PID=""
}

check() { # <label> <condition-description> <0|1 ok>
  if [ "$3" = "1" ]; then
    echo "ok   — $1"
  else
    echo "FAIL — $1: $2"
    sed 's/^/    /' "$TMP/out.log" | tail -8
    fail=1
  fi
}

echo "== the client waits for what waiting can clear =="

# 1. already_running: two refusals, then through. The job must be OPENED, not
#    reported as refused, and it must have taken more than one request.
run_client already-running
ASKED="$(cat "$TMP/count-jobs" 2>/dev/null || echo 0)"
check "a conversion still running is waited out, not reported as a refusal" \
  "exit $RC after $ASKED job requests" \
  "$([ "$ASKED" -ge 3 ] && grep -q 'still running' "$TMP/out.log" && echo 1 || echo 0)"

# 2. A refusal waiting cannot clear must stop at once — one request, no sleep.
run_client conversions-used
ASKED="$(cat "$TMP/count-jobs" 2>/dev/null || echo 0)"
check "a spent allowance stops immediately instead of looping" \
  "exit $RC after $ASKED job requests, ${ELAPSED}s" \
  "$([ "$RC" != "0" ] && [ "$ASKED" = "1" ] && [ "$ELAPSED" -lt 10 ] && echo 1 || echo 0)"

# 3. The refusal is relayed ONCE. It used to be printed twice — by the script
#    and again by the recorder — which reads as two separate problems.
SAID="$(grep -c 'The free tier is used up' "$TMP/out.log" || true)"
check "the service's words are relayed once, not twice" "printed $SAID times" \
  "$([ "$SAID" = "1" ] && echo 1 || echo 0)"

# 4. A capacity 429 on the transform is repeated; the answer is the recorded run.
run_client at-capacity
ASKED="$(cat "$TMP/count-transform" 2>/dev/null || echo 0)"
check "a full service is waited out on the transform too" \
  "exit $RC after $ASKED transform requests" \
  "$([ "$RC" = "0" ] && [ "$ASKED" -ge 3 ] && echo 1 || echo 0)"

# 5. A 429 with no retry-after is the attempts spent — asking again cannot
#    help, so it must not be asked again.
run_client attempts-spent
ASKED="$(cat "$TMP/count-transform" 2>/dev/null || echo 0)"
check "a 429 that names no wait is not retried" \
  "exit $RC after $ASKED transform requests" \
  "$([ "$RC" != "0" ] && [ "$ASKED" = "1" ] && echo 1 || echo 0)"

# 6. The finalising piece whose answer was lost: settled is settled.
run_client already-uploaded
check "an upload the service already has is not thrown away" \
  "exit $RC" \
  "$([ "$RC" = "0" ] && grep -q 'already settled' "$TMP/out.log" && echo 1 || echo 0)"

[ "$fail" = 0 ] && echo "PASS" || echo "FAILED"
exit "$fail"
