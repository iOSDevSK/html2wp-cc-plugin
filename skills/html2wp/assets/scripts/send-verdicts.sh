#!/usr/bin/env bash
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
# Tell the service what the gates said.
#
#   send-verdicts.sh <workspace> [--outcome=delivered|abandoned|in-progress] [--api=URL] [--dry-run]
#
# The service converts a site and never learns whether the result was CORRECT:
# gates A, A2, B, C, the editor smoke test and the Woo audit all run here, on
# your machine, against a WordPress it never sees. Every conversion it records
# therefore says "the transform succeeded", which is a statement about the
# service and not about the theme.
#
# This closes that loop, and it is REQUIRED — not as a checkbox, but because
# the next conversion asks for it. Run it at stage 6, when the gates have run.
#
# WHAT IT SENDS: gate names, verdicts, page counts, the worst percentage, and
# the page KEYS that failed — the short names you chose (`about`, `shop`).
#
# WHAT IT DOES NOT SEND, and the server drops if a future client ever tries:
# no URL, no markup, no copy, no screenshots, no file paths, no licence key,
# no site name. Read `verdicts.ts` on the service for the whitelist itself.
#
# --dry-run prints the payload instead of sending it. Nothing about a
# conversion changes either way; a failed send is a warning, never a stop.
set -euo pipefail

WS=""; OUTCOME=""; API="${H2WP_API:-https://api.html2wp.dev}"; DRY=0
for arg in "$@"; do
  case "$arg" in
    --outcome=*) OUTCOME="${arg#--outcome=}" ;;
    --api=*)     API="${arg#--api=}" ;;
    --dry-run)   DRY=1 ;;
    --*) echo "unknown flag: $arg" >&2; exit 2 ;;
    *) WS="$arg" ;;
  esac
done
[ -n "$WS" ] || { echo "usage: send-verdicts.sh <workspace> [--outcome=delivered] [--dry-run]" >&2; exit 2; }
WS="$(cd "$WS" && pwd)"; API="${API%/}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLIENT_VERSION="${H2WP_CLIENT_VERSION:-}"
for VERSION_FILE in "$SCRIPT_DIR/../../VERSION" "$SCRIPT_DIR/../../../../VERSION"; do
  [ -n "$CLIENT_VERSION" ] && break
  [ ! -f "$VERSION_FILE" ] || CLIENT_VERSION="$(tr -d '[:space:]' < "$VERSION_FILE")"
done

CLIENT_HOST="${H2WP_HOST:-}"
case "$CLIENT_HOST" in
  codex|claude-code) ;;
  '')
    if [ -n "${CODEX_SESSION_ID:-}${CODEX_THREAD_ID:-}${CODEX_CI:-}" ]; then
      CLIENT_HOST=codex
    elif [ -n "${CLAUDECODE:-}${CLAUDE_CODE_ENTRYPOINT:-}" ]; then
      CLIENT_HOST=claude-code
    else
      CLIENT_HOST=claude-code
    fi
    ;;
  *) echo "refusing: H2WP_HOST must be codex or claude-code" >&2; exit 2 ;;
esac

JOB_FILE="$WS/.h2wp-job.json"
[ -f "$JOB_FILE" ] || { echo "no $JOB_FILE — this workspace has not been converted by the service" >&2; exit 1; }
TOKEN="$(python3 - "$JOB_FILE" <<'PY'
import json, sys
value = json.load(open(sys.argv[1])).get("token", "")
if not isinstance(value, str) or not value:
    raise SystemExit(1)
print(value)
PY
)" || { echo "$JOB_FILE has no job token — start a fresh conversion before reporting verdicts" >&2; exit 1; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT

python3 - "$WS" "$JOB_FILE" "$OUTCOME" > "$TMP/payload.json" <<'PY'
import json, os, re, sys

ws, job_file, outcome = sys.argv[1:4]
job = json.load(open(job_file)).get("job", "")

def read(*parts):
    try:
        return json.load(open(os.path.join(ws, *parts)))
    except Exception:
        return None

KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,48}$", re.I)

def keys_of(report, *fields):
    """Page keys only. A path, a URL or a sentence is not a key and is dropped."""
    out = []
    for f in fields:
        v = (report or {}).get(f)
        if isinstance(v, dict):
            v = list(v.keys())
        if isinstance(v, list):
            out += [k for k in v if isinstance(k, str) and KEY.match(k)]
    return sorted(set(out))[:40]

def steps_verdict(report):
    """Some reports carry the answer per step rather than at the top.

    Absence is not a pass. `passed is None` used to fall through to `not
    failing`, and for a report shaped {steps: {name: {ok: bool}}} that is
    unconditionally True — so a smoke run that failed three steps was sent to
    the service as green, which is the one thing stage 6.5 exists to prevent.
    Look inside before concluding anything.
    """
    steps = report.get("steps")
    if not isinstance(steps, dict):
        return None, []
    failed = [k for k, v in steps.items() if isinstance(v, dict) and v.get("ok") is False]
    # ok=None is a step that never executed — a login that broke, a step that
    # threw. It is not a pass, and treating it as one is how a smoke with six
    # of eight steps unrun reported green to the service.
    not_run = [k for k, v in steps.items() if isinstance(v, dict) and v.get("ok") is None]
    ran = [v for v in steps.values() if isinstance(v, dict) and "ok" in v]
    if not ran:
        return None, []
    return (not failed and not not_run), sorted(set(failed + not_run))


def gate(name, report, *, pages_field="pages", worst_field="worstPct", fail_fields=("failures", "failed")):
    if report is None:
        return {"gate": name, "verdict": "not-run"}
    passed = report.get("passed")
    failing = keys_of(report, *fail_fields)
    if passed is None:
        derived, derived_failing = steps_verdict(report)
        if derived is not None:
            passed = derived
            failing = failing or sorted(set(k for k in derived_failing if KEY.match(k)))[:40]
        else:
            passed = not failing
    entry = {"gate": name, "verdict": "passed" if passed else "failed"}
    for src, dst in ((pages_field, "pages"), (worst_field, "worstPct")):
        v = report.get(src)
        if isinstance(v, (int, float)):
            entry[dst] = v
    if failing:
        entry["failedKeys"] = failing
    not_run = keys_of(report, "notRun", "not_run")
    if not_run:
        entry["notRun"] = not_run
    return entry

wp = read("verify-wp", "report.json")
gates = [
    gate("A",  read("verify-static", "report.json")),
    gate("A2", read("parity-report.json") or read("verify-parity", "report.json")),
    gate("B",  wp),
    gate("C",  wp),
    gate("smoke-editor", read("smoke-editor", "report.json")),
    gate("woo-coverage", read("woo-coverage", "report.json")),
]

payload = {"job": job, "gates": gates}
if outcome:
    payload["outcome"] = outcome
json.dump(payload, sys.stdout, indent=2)
PY

python3 - "$TMP/payload.json" <<'PY'
import json, sys
p = json.load(open(sys.argv[1]))
for g in p["gates"]:
    extra = []
    if "worstPct" in g: extra.append(f"{g['worstPct']}%")
    if g.get("failedKeys"): extra.append("failed: " + ", ".join(g["failedKeys"]))
    print("   %-14s %-8s %s" % (g["gate"], g["verdict"], "  ".join(extra)))
PY

if [ "$DRY" = "1" ]; then
  echo; echo "--dry-run: not sent. This is the whole payload:"; cat "$TMP/payload.json"; exit 0
fi

# Bounded hard: this runs AFTER the theme is in the user's hands, so it must
# never be the thing that makes a finished conversion look stuck.
HTTP="$(curl -sS --connect-timeout 10 --max-time 30 \
  -o "$TMP/reply.json" -w '%{http_code}' -X POST "$API/v1/verdicts" \
  -H "x-html2wp-client: ${CLIENT_VERSION:-unknown}" \
  -H "x-html2wp-host: $CLIENT_HOST" \
  -H 'content-type: application/json' -H "authorization: Bearer $TOKEN" \
  --data-binary @"$TMP/payload.json" || echo 000)"
if [ "$HTTP" = "200" ]; then
  echo "reported"
  # cleanup.sh refuses to delete the gate reports until this exists — the
  # service needs them, and a caller who owes verdicts is refused their NEXT
  # conversion.
  : > "$WS/.h2wp-verdicts-sent"
else
  # Never a stop: the conversion is finished either way, and the next job will
  # ask again. Say what happened so it is not a silent omission.
  echo "could not report the gates (HTTP $HTTP) — the next conversion will ask for them again" >&2
  [ -s "$TMP/reply.json" ] && head -c 400 "$TMP/reply.json" >&2 && echo >&2
fi
