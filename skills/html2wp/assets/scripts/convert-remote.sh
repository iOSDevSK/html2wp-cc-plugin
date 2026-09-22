#!/usr/bin/env bash
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
# Stages 3–4.6, performed by the html2wp service.
#
#   convert-remote.sh <workspace> [--api=URL] [--key=KEY] [--opts=JSON]
#
# The workspace is the directory conversion-manifest.json lives in, after the
# local stages have run: astro-report.json and astro-project/dist must exist,
# chrome-at-rest/ and style-specimens/ ride along when present.
#
# What arrives back, unpacked INTO the workspace:
#   theme/{slug}/            the generated theme, content bundle included
#   theme-report.json        the generator's warnings — READ THEM
#   chrome-groups.json       the chrome partition the theme was built from
#
# Every call here is safe to repeat — a dropped connection is repaired by
# running this script again. The service records the transform result, so a
# retry returns the run already done rather than spending another attempt.
#
# A manifest of schema html2wp/2 (prepare-block-plan.mjs) is the native
# Gutenberg target: the reviewed block plan is finalized and rides along, and
# the service compiles a block theme instead of generating the HTML one.
#
# Environment, beyond H2WP_API / H2WP_KEY:
#   H2WP_JOB_STATE    where the job state lives (default {workspace}/.h2wp-job.json);
#                     the last answer is kept beside it as {state}.result
#   H2WP_STRICT_JOBS  1 = never open a replacement job on the caller's behalf
#
# `--opts` carries dist-to-bundle's judgment flags as JSON, e.g.
#   --opts='{"stripInFront":["footer.site-footer"]}'
#   --opts='{"keepArticlePages":true,"excludePages":["draft"]}'
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

WS=""
API="${H2WP_API:-https://api.html2wp.dev}"
KEY="${H2WP_KEY:-}"
OPTS="{}"
for arg in "$@"; do
  case "$arg" in
    --api=*) API="${arg#--api=}" ;;
    --key=*) KEY="${arg#--key=}" ;;
    --opts=*) OPTS="${arg#--opts=}" ;;
    --*) echo "unknown flag: $arg" >&2; exit 2 ;;
    *) WS="$arg" ;;
  esac
done
[ -n "$WS" ] || { echo "usage: convert-remote.sh <workspace> [--api=URL] [--key=KEY] [--opts=JSON]" >&2; exit 2; }
WS="$(cd "$WS" && pwd)"
API="${API%/}"

# https, unless you say out loud that you meant otherwise.
#
# The licence key and the job bearer token both travel on this URL, and
# --api / $H2WP_API accept whatever they are given — so a mistyped or
# copy-pasted `http://` sent both in clear text, and the answers coming back
# steer this script. Plain http stays possible because the documented way to
# run a staging endpoint is `H2WP_API=http://127.0.0.1:8080`, but it has to be
# either a loopback address or an explicit opt-in.
case "$API" in
  https://*) ;;
  http://127.0.0.1[:/]*|http://127.0.0.1|http://localhost[:/]*|http://localhost|http://\[::1\][:/]*)
    echo "note: talking to a local endpoint over http ($API)" ;;
  http://*)
    if [ "${H2WP_ALLOW_INSECURE_API:-}" = "1" ]; then
      echo "warning: --api is plain http; the licence key and job token travel in clear text" >&2
    else
      echo "refusing: --api must be https (got $API)." >&2
      echo "  The licence key and the job token travel on this URL." >&2
      echo "  For a non-loopback http endpoint, set H2WP_ALLOW_INSECURE_API=1 deliberately." >&2
      exit 2
    fi ;;
  *)
    echo "refusing: --api must be an http(s) URL (got $API)" >&2; exit 2 ;;
esac

# Say which client this is, and which host is running it, on every call.
#
# The service can refuse a client too old to be safe (H2WP_MIN_CLIENT), and
# it records the version on every conversion so a failure rate that moves can
# be attributed. Both were reading a header nothing sent: the refusal would
# have rejected EVERY caller the moment it was switched on, and the learning
# rows all said clientVersion: null.
#
# VERSION is mirrored at plugin and standalone-skill roots and checked against
# both host manifests. The inner copy survives `skills/html2wp/`-only installs;
# the outer copy remains the release source of truth for the full plugin.
CLIENT_VERSION="${H2WP_CLIENT_VERSION:-}"
for VERSION_FILE in "$SCRIPT_DIR/../../VERSION" "$SCRIPT_DIR/../../../../VERSION"; do
  [ -n "$CLIENT_VERSION" ] && break
  [ ! -f "$VERSION_FILE" ] || CLIENT_VERSION="$(tr -d '[:space:]' < "$VERSION_FILE")"
done

# Both hosts execute the same scripts. Prefer an explicit override, then the
# stable process markers each host exports. Claude Code is the compatibility
# fallback for old/plain installations; values outside this two-value
# protocol are rejected instead of becoming unbounded telemetry labels.
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

# The licence key can also live in a file, so it never lands in shell history.
[ -z "$KEY" ] && [ -f "$HOME/.config/html2wp/licence" ] && KEY="$(tr -d '[:space:]' < "$HOME/.config/html2wp/licence")"

for f in conversion-manifest.json astro-report.json astro-project/dist; do
  [ -e "$WS/$f" ] || { echo "missing $WS/$f — run the local stages first" >&2; exit 1; }
done

# What rides along must describe THIS build. The chrome partition, the at-rest
# captures and the collections are all read off dist/, and a rebuild after
# them (a 2.6/2.65 fix, a re-run of stage 1) leaves them describing a site
# that no longer exists — the captures are paired with groups by index, so a
# stale file ships the wrong chrome without a single error anywhere. Each is
# checked only when present; its absence is the stage's own business.
#
# Only the captures the generator will READ are checked: make-theme opens
# chrome-at-rest/{region}-g{index}.html for each group of the partition it
# computes, which is the one chrome-groups.json records. A capture for a group
# the partition no longer has is never opened, and capture-chrome.py does not
# delete it — so every change of partition would leave one behind, and a
# guard that counted it would refuse a correct upload. Without a readable
# chrome-groups.json there is no partition to go by, and every capture counts.
BUILT="$WS/astro-project/dist/index.html"
if [ -f "$BUILT" ]; then
  CHECK=("$WS/chrome-groups.json" "$WS/collections-report.json")
  ORPHANS=()
  if [ -d "$WS/chrome-at-rest" ]; then
    READ_BY_GENERATOR="$(python3 - "$WS/chrome-groups.json" <<'PY'
import json, sys
try:
    regions = json.load(open(sys.argv[1]))["regions"]
    for region, groups in regions.items():
        for g in groups:
            print("%s-g%d.html" % (region, int(g["index"])))
except Exception:
    print("*")
PY
)"
    for f in "$WS"/chrome-at-rest/*; do
      [ -f "$f" ] || continue
      if [ "$READ_BY_GENERATOR" = "*" ] || printf '%s\n' "$READ_BY_GENERATOR" | grep -qxF "$(basename "$f")"; then
        CHECK+=("$f")
      else
        ORPHANS+=("${f#"$WS"/}")
      fi
    done
  fi
  STALE=()
  for f in "${CHECK[@]}"; do
    if [ -f "$f" ] && [ "$f" -ot "$BUILT" ]; then STALE+=("${f#"$WS"/}"); fi
  done
  if [ "${#ORPHANS[@]}" -gt 0 ]; then
    echo "note: not in the current chrome partition, so the generator ignores them (they still travel):"
    printf '  %s\n' "${ORPHANS[@]}"
  fi
  if [ "${#STALE[@]}" -gt 0 ]; then
    echo "refusing: older than the build (astro-project/dist/index.html):" >&2
    printf '  %s\n' "${STALE[@]}" >&2
    echo "  re-run stages 2.5 and 2.7 on this build (stage2-gates.sh does both), then upload." >&2
    exit 1
  fi
fi

# Values reach python through ARGV, never through the source text.
#
# Every python3 -c in this file used to interpolate shell variables straight
# into the program, which made each of them an injection site with a different
# author: $WS is a path the user chose (an apostrophe in a directory name is
# enough to break it), $KEY is read from a file, and $JOB/$TOKEN come back from
# the SERVER — and --api can point the client at any server. send-verdicts.sh
# already did this the safe way; this is the same shape.
PAGES="$(python3 - "$WS/conversion-manifest.json" <<'PY'
import json, sys
mf = json.load(open(sys.argv[1]))
print(len([p for p in mf.get('pages', []) if p.get('kind') != 'fragment']))
PY
)"

TMP="$(mktemp -d)"

# One machine-readable outcome per run, written where the run happened.
#
# The service answers a refusal with a stable `reason` code (site_too_large,
# free_conversions_used, licence_used_this_period, …) and this script used to
# print the prose and throw the code away — `$TMP/result.json` was deleted by
# the trap, so nothing survived that another tool, or the agent on the next
# turn, could branch on. Every gate in this pipeline writes a JSON report;
# stage 3 was the one that did not.
#
# Written on EVERY exit, success or failure, by the trap. A run that ends with
# no result.json is a run that was killed, which is itself worth being able to
# tell apart from one that failed.
RESULT="$WS/.h2wp-result.json"
H2WP_STATUS="FAILED_CLIENT"
H2WP_CODE="INTERRUPTED"
H2WP_STAGE="startup"
H2WP_MESSAGE="the client exited before it recorded an outcome"
H2WP_ACTION="re-run convert-remote.sh; the job resumes from where it stopped"

# Where this script's minutes go.
#
# Stage 3 is one line in every progress report and at least four different
# waits behind it: packing the archive on this machine, the upload, the
# service's own work, the download. Which of them a slow stage 3 was spent in
# decides whether the fix is here, on the wire or on the server — and nothing
# recorded it. `phase` closes the phase that was open and opens the named one;
# the trap closes the last. Whole seconds, because that is what `date` gives
# everywhere this runs and the phases worth noticing are minutes long.
PHASE=""; PHASE_AT=0; PHASES=""; RESULT_TARGET=""
phase() { # <name> — or nothing, to close the one that is open
  local now; now="$(date +%s)"
  [ -n "$PHASE" ] && PHASES="$PHASES$PHASE=$((now - PHASE_AT)) "
  PHASE="${1:-}"; PHASE_AT="$now"
}

write_result() {
  phase
  python3 - "$RESULT" "$H2WP_STATUS" "$H2WP_CODE" "$H2WP_STAGE" "$H2WP_MESSAGE" "$H2WP_ACTION" \
    "${JOB:-}" "${EDITION:-}" "${SLUG:-}" "$PHASES" "$TMP/result.json" "$WS/.h2wp-timing.jsonl" "$RESULT_TARGET" <<'PY'
import json, sys, time
path, status, code, stage, message, action, job, edition, slug, phases, answer, timing_log, target = sys.argv[1:14]
out = {"status": status, "code": code, "stage": stage, "message": message}
if target: out["target"] = target
if action: out["action"] = action
if job: out["jobId"] = job
if edition: out["edition"] = edition
if slug: out["slug"] = slug

# A phase that ran twice (a job request retried after a wait) is summed: the
# question is where the time went, not how many attempts it took.
seconds = {}
for pair in phases.split():
    name, _, value = pair.partition("=")
    if value.isdigit():
        seconds[name] = seconds.get(name, 0) + int(value)
if seconds:
    out["timing"] = {"seconds": seconds}

# What the service said about its own half, when it answered at all.
#
# --api can name any server, and these keys end up in a file whose summary the
# conversion report quotes. So only the documented shape is kept: numbers of
# milliseconds under short script-shaped names, at most sixteen of them.
import re
server = None
try:
    raw = json.load(open(answer)).get("timings")
    if isinstance(raw, dict) and isinstance(raw.get("scripts"), dict):
        ms = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v < 86_400_000
        scripts = {k: int(v) for k, v in list(raw["scripts"].items())[:16]
                   if isinstance(k, str) and re.fullmatch(r"[a-z0-9-]{1,32}", k) and ms(v)}
        server = {"scripts": scripts}
        if ms(raw.get("totalMs")):
            server["totalMs"] = int(raw["totalMs"])
except Exception:
    server = None
if server:
    out.setdefault("timing", {})["server"] = server

with open(path, "w") as fh:
    json.dump(out, fh, indent=2)
    fh.write("\n")

# The same numbers, as rows beside every other stage's. Never worth failing
# for: the outcome above is already on disk.
try:
    now = round(time.time(), 3)
    rows = [{"t": now, "event": "phase", "stage": "3", "phase": n, "ms": s * 1000} for n, s in seconds.items()]
    if server:
        rows += [{"t": now, "event": "server", "stage": "3", "script": k, "ms": v}
                 for k, v in server["scripts"].items()]
    if rows:
        with open(timing_log, "a") as fh:
            fh.write("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows))
except Exception:
    pass
PY
}
fail_with() { # <code> <stage> <message> [action]
  H2WP_STATUS=FAILED_WITH_ACTION; H2WP_CODE="$1"; H2WP_STAGE="$2"; H2WP_MESSAGE="$3"
  H2WP_ACTION="${4:-}"
  echo "$3" >&2
  exit 1
}
trap 'write_result; rm -rf "$TMP"' EXIT

# ---- pack ----------------------------------------------------------------
phase pack
MEMBERS=(conversion-manifest.json astro-report.json astro-project)
# The output target is the manifest's: html2wp/2 is the native Gutenberg theme
# (prepare-block-plan.mjs set it), anything else the HTML theme.
#
# v2 ships reviewed data only. Worker checkpoints, findings, logs and arbitrary
# files under block-plan are local; never package the directory wholesale.
MANIFEST_SCHEMA="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("schema", ""))' "$WS/conversion-manifest.json")"
# Which theme the service will build, by the service's own rule (the
# v2 schema or an explicit gutenberg target), for the result record.
RESULT_TARGET="$(python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); print("gutenberg" if m.get("schema")=="html2wp/2" or m.get("target")=="gutenberg" else "html")' "$WS/conversion-manifest.json" 2>/dev/null || true)"
if [ "$MANIFEST_SCHEMA" = "html2wp/2" ]; then
  if ! node "$SCRIPT_DIR/prepare-block-plan.mjs" finalize --manifest="$WS/conversion-manifest.json"; then
    fail_with INVALID_BLOCK_PLAN pack "Gutenberg plan is incomplete or stale" \
      "resolve .gutenberg/check-report.json and complete worker checkpoints before uploading"
  fi
  MEMBERS+=(block-plan/contract.json)
  while IFS= read -r page_file; do
    MEMBERS+=("$page_file")
  done < <(python3 - "$WS/conversion-manifest.json" <<'PYBLOCK'
import json, sys
for page in json.load(open(sys.argv[1])).get('pages', []):
    if page.get('kind') != 'fragment':
        print('block-plan/pages/' + page['key'] + '.json')
PYBLOCK
)
fi
for extra in chrome-at-rest style-specimens chrome-groups.json; do
  [ -e "$WS/$extra" ] && MEMBERS+=("$extra")
done
# Stable sorted archive bytes, for both targets: an unchanged input packs to
# the same sha on macOS and Linux, so a retry reuses its saved job instead of
# opening another. The packer leaves out what tar was told to exclude
# (node_modules, .astro, AppleDouble `._*` — which materialise as real files
# on the Linux side and read as pages with no <body> — and .DS_Store), zeroes
# the tar and gzip timestamps, and refuses a symlink rather than follow it.
python3 "$SCRIPT_DIR/gutenberg-pack-upload.py" "$WS" "$TMP/upload.tar.gz" "${MEMBERS[@]}" \
  || fail_with PACK_FAILED pack "the upload could not be packed (see the line above)" \
       "fix what the packer named and run again; nothing was uploaded"
SIZE="$(wc -c < "$TMP/upload.tar.gz" | tr -d ' ')"
SHA="$(python3 - "$TMP/upload.tar.gz" <<'PY'
import hashlib, sys
h = hashlib.sha256()
# Streamed: reading a 500MB tarball whole to hash it is a needless half-gig
# of resident memory on a laptop.
#
# NOTE, and it cost an hour: no apostrophes anywhere in a heredoc that sits
# inside $( ). bash 3.2 — the bash macOS still ships — tracks single quotes
# while it hunts for the closing paren, so one "user's" in a COMMENT here
# opens a quote that never closes and the rest of the file stops parsing.
with open(sys.argv[1], 'rb') as fh:
    for chunk in iter(lambda: fh.read(1 << 20), b''):
        h.update(chunk)
print(h.hexdigest())
PY
)"

# The wait the service asked for, in seconds, or empty when it named none.
#
# Read from the headers rather than the body, because that is where the
# service puts it, and the DIFFERENCE matters: a 429 that carries retry-after
# is one that waiting can clear (a conversion of this machine is still
# running, or the box is at capacity), and a 429 without it is one that
# waiting cannot (this job has spent its attempts). Retrying the second is a
# loop that cannot succeed.
retry_after() { # <header-dump>
  [ -f "$1" ] || return 0
  # grep -i, not awk IGNORECASE: the awk macOS ships does not have it.
  tr -d '\r' < "$1" | grep -i '^retry-after:' | tail -1 | sed 's/^[^:]*: *//'
}

# The seconds to wait before asking again, clamped into something sane.
#
# The service answers `already_running` with a flat 300 that is not the
# remaining lease, so honouring it literally waits five minutes for something
# that often clears in one. A minute is the longest useful step.
#
# The longest step is env-overridable for the same reason the upload loop's
# limits are: the regression test drives these paths in seconds rather than
# minutes. It caps the step from BOTH sides — a five-second floor under a
# one-second ceiling would put the test back where it started.
WAIT_STEP_MAX="${H2WP_WAIT_STEP_SECONDS:-60}"
wait_seconds() { # <asked> <fallback>
  local asked="$1"
  local floor=5
  [ "$WAIT_STEP_MAX" -lt "$floor" ] && floor="$WAIT_STEP_MAX"
  case "$asked" in ''|*[!0-9]*) asked="$2" ;; esac
  [ "$asked" -gt "$WAIT_STEP_MAX" ] && asked="$WAIT_STEP_MAX"
  [ "$asked" -lt "$floor" ] && asked="$floor"
  printf '%s' "$asked"
}

json_field() { python3 - "$1" "$2" <<'PY'
import json, sys
try:
    value = json.load(open(sys.argv[1]))
except Exception:
    value = None
for part in sys.argv[2].split('.'):
    value = value.get(part) if isinstance(value, dict) else None
print('' if value is None else value)
PY
}

# ---- job -----------------------------------------------------------------
phase job
# An unchanged input reuses its job: the upload is already settled, so a
# retry with different --opts goes straight to the transform — no second
# upload, no second job. The state file is invalidated the moment the input
# changes (the sha differs) or the service no longer recognises the token.
#
# The state is bound to the service it came from (`api`): a job of the local
# test service is not a job of the real one. A state written before that field
# existed is not reused. H2WP_JOB_STATE moves it out of the workspace — the
# desktop app keeps it in its own private job directory.
#
# H2WP_STRICT_JOBS=1 is for a caller that bills or reconciles jobs itself (the
# desktop app): a saved job the service no longer accepts, or a job request
# whose answer never arrived, stops the run with a code instead of opening a
# replacement on its own. Without it the client repairs both, once.
STATE_FILE="${H2WP_JOB_STATE:-$WS/.h2wp-job.json}"
STRICT_JOBS="${H2WP_STRICT_JOBS:-0}"
JOB=""
TOKEN=""
REUSED=0
RESUMED_UPLOAD=0

# Written whole or not at all (a temp file renamed over it), owner-only, and
# the token reaches python through the environment rather than argv.
save_state() { # <phase: uploading|settled>
  H2WP_PRIVATE_TOKEN="$TOKEN" python3 - "$STATE_FILE" "$1" "$JOB" "$SHA" "$API" \
    "${UPLOAD_URL:-}" "${EDITION:-}" <<'PY'
import json, os, sys
path, phase, job, sha, api, upload_url, edition = sys.argv[1:8]
state = {'job': job, 'token': os.environ['H2WP_PRIVATE_TOKEN'], 'sha': sha, 'api': api}
if phase == 'uploading':
    state.update(phase='uploading', uploadUrl=upload_url, edition=edition or None)
fd = os.open(path + '.tmp', os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, 'w') as fh:
    json.dump(state, fh)
    fh.flush()
    os.fsync(fh.fileno())
os.replace(path + '.tmp', path)
PY
}

# The one way a saved job that stopped being valid is handled.
renew_job() { # <stage> <why>
  if [ "$STRICT_JOBS" = "1" ]; then
    fail_with JOB_EXPIRED "$1" \
      "the saved job is no longer valid ($2); no replacement conversion was started" \
      "authorize a new conversion only after reconciling this job"
  fi
  echo "the saved job is no longer valid ($2) — starting a fresh one"
  rm -f "$STATE_FILE" "${STATE_FILE}.result"
  export H2WP_RETRIED=1
  # The key travels in the environment, never in argv: it is readable in
  # `ps` there, which would undo the reason it lives in a file at all.
  H2WP_KEY="$KEY" exec bash "$0" "$WS" --api="$API" --opts="$OPTS"
}

# A request to open a job that never got its answer. The service may have
# opened one — this client cannot know, and the marker says so.
if [ -f "${STATE_FILE}.pending" ]; then
  if [ "$STRICT_JOBS" = "1" ]; then
    fail_with JOB_RECOVERY_REQUIRED job \
      "the service did not confirm job creation. The request has been preserved; no duplicate will be opened." \
      "reconcile the pending job with html2wp support before starting a new conversion"
  fi
  echo "note: an earlier request to open a job got no answer; asking for a new one (the service supersedes an unfinished one)"
  rm -f "${STATE_FILE}.pending"
fi

if [ -f "$STATE_FILE" ] && [ "$(json_field "$STATE_FILE" sha)" = "$SHA" ] && [ "$(json_field "$STATE_FILE" api)" = "$API" ]; then
  JOB="$(json_field "$STATE_FILE" job)"
  TOKEN="$(json_field "$STATE_FILE" token)"
  REUSED=1
  # Opened, but the upload never settled (the run was killed, the machine
  # slept): resume the upload into the same job rather than open another. The
  # service answers the first piece with the offset it already holds.
  if [ "$(json_field "$STATE_FILE" phase)" = "uploading" ]; then
    REUSED=0
    RESUMED_UPLOAD=1
    UPLOAD_URL="$(json_field "$STATE_FILE" uploadUrl)"
    EDITION="$(json_field "$STATE_FILE" edition)"
    [ -n "$UPLOAD_URL" ] || fail_with UPLOAD_RECOVERY_REQUIRED upload \
      "the saved job has no upload URL" "keep this job and contact support"
    echo "job $JOB reused (its upload did not finish — resuming it)"
  else
    echo "job $JOB reused (input unchanged — straight to the transform)"
  fi
fi

if [ -z "$JOB" ]; then
  # The key reaches python through the environment and curl through a file:
  # argv of either is readable in `ps` by anyone on the machine.
  BODY="$(H2WP_PRIVATE_KEY="$KEY" python3 - "$PAGES" <<'PY'
import json, os, sys
body = {'pages': int(sys.argv[1])}
key = os.environ.get('H2WP_PRIVATE_KEY', '').strip()
if key: body['key'] = key
print(json.dumps(body))
PY
)"
  (umask 077; printf '%s' "$BODY" > "$TMP/request.json")
  # Conversions run one at a time per machine, so an earlier one that is still
  # finishing is a WAIT, not a refusal — and it was being reported as a
  # refusal. The agent driving this script then wrapped it in a retry loop of
  # its own, which is a loop this script should own: it knows what the service
  # asked for, and it is the thing holding the packed upload.
  #
  # Only `already_running` waits. Every other refusal — the day's jobs, the
  # licence period, a site too large, verdicts owed — is answered by doing
  # something, not by asking again.
  JOB_WAITED=0
  # Past the service's own transform lease (20 min), so the wait outlives the
  # longest thing that can legitimately hold the door.
  JOB_WAIT_MAX="${H2WP_JOB_WAIT_SECONDS:-1500}"
  while :; do
    # `|| echo 000` and not a bare curl: without it a service that cannot be
    # reached at all kills the script under `set -e` before anything can record
    # WHY, and the outcome file reads "interrupted" for what is really "the
    # service did not answer". Normalised to the last three digits for the same
    # reason as the upload loop — curl's own -w already prints 000 on failure.
    # Set before asking and cleared on any answer: a marker that survives means
    # the request left and its answer never came back.
    (umask 077; printf '%s' "$SHA" > "${STATE_FILE}.pending")
    RAW="$(curl -sS --connect-timeout 20 --max-time 60 \
      -o "$TMP/job.json" -D "$TMP/job.head" -w '%{http_code}' -X POST "$API/v1/jobs" \
      -H "x-html2wp-client: ${CLIENT_VERSION:-unknown}" \
      -H "x-html2wp-host: $CLIENT_HOST" \
      -H 'content-type: application/json' --data-binary @"$TMP/request.json" || echo 000)"
    HTTP="${RAW: -3}"
    if [ "$HTTP" = "000" ]; then
      fail_with SERVER_UNREACHABLE job \
        "could not reach $API — no answer to the request to open a conversion" \
        "check the address and your connection, then run this again; nothing was uploaded"
    fi
    [ "$HTTP" = "201" ] && break
    rm -f "${STATE_FILE}.pending"
    # The service's own reason code, relayed rather than flattened — it is the
    # difference between "wait a day" and "buy a licence".
    REASON="$(json_field "$TMP/job.json" reason)"
    if [ "$HTTP" = "429" ] && [ "$REASON" = "already_running" ] && [ "$JOB_WAITED" -lt "$JOB_WAIT_MAX" ]; then
      WAIT="$(wait_seconds "$(retry_after "$TMP/job.head")" 45)"
      JOB_WAITED=$((JOB_WAITED + WAIT))
      echo "a conversion of this machine is still running; asking again in ${WAIT}s (waited ${JOB_WAITED}s of ${JOB_WAIT_MAX}s)"
      sleep "$WAIT"
      continue
    fi
    # The service is draining for a redeploy: the process answering now takes
    # no new work, the one after it will. Its Retry-After spans the restart.
    if [ "$HTTP" = "429" ] && [ "$REASON" = "service_restarting" ] && [ "$JOB_WAITED" -lt "$JOB_WAIT_MAX" ]; then
      WAIT="$(wait_seconds "$(retry_after "$TMP/job.head")" 60)"
      JOB_WAITED=$((JOB_WAITED + WAIT))
      echo "the service is restarting for an update; asking again in ${WAIT}s (waited ${JOB_WAITED}s of ${JOB_WAIT_MAX}s)"
      sleep "$WAIT"
      continue
    fi
    echo "the service refused to open a conversion (HTTP $HTTP):" >&2
    fail_with "${REASON:-JOB_REFUSED}" job \
      "$(json_field "$TMP/job.json" error)" \
      "the service explained why above; nothing was uploaded"
  done
  JOB="$(json_field "$TMP/job.json" job)"
  TOKEN="$(json_field "$TMP/job.json" token)"
  UPLOAD_URL="$(json_field "$TMP/job.json" upload.url)"
  EDITION="$(json_field "$TMP/job.json" edition)"
  # Saved before the first byte goes up, so an interrupted upload resumes into
  # this job; the marker goes only once the job is on disk.
  save_state uploading
  rm -f "${STATE_FILE}.pending"
  NOTE="$(json_field "$TMP/job.json" note)"
  [ -n "$NOTE" ] && echo "note: $NOTE"

  # A newer client exists. Said here, at the START, because the end of a
  # conversion is a wall of gate output and a warning there is a warning
  # nobody reads. Never a stop — the conversion runs on this version.
  UPD_LATEST="$(json_field "$TMP/job.json" update.latest)"
  if [ -n "$UPD_LATEST" ]; then
    UPD_RUN="$(json_field "$TMP/job.json" update.run)"
    UPD_WHY="$(json_field "$TMP/job.json" update.why)"
    echo
    echo "  ┌─ html2wp $UPD_LATEST is out. You are on ${CLIENT_VERSION:-an unidentified version}."
    echo "  │  Update:  $UPD_RUN"
    [ -n "$UPD_WHY" ] && echo "  │  $UPD_WHY" | fold -s -w 74 | sed '2,$s/^/  │  /'
    echo "  └─ This conversion continues on your current version."
    echo
  fi
  echo "job $JOB open (edition: $EDITION, $PAGES pages, upload $SIZE bytes)"
  # The service's own words on what this machine has left — relay verbatim.
  CREDIT="$(json_field "$TMP/job.json" credit.line)"
  [ -n "$CREDIT" ] && echo "credit: $CREDIT"
fi

# ---- upload, in pieces ---------------------------------------------------
phase upload
# 48MB stays under every proxy's request cap. On any failure the loop asks
# the service where it stands (the 416 answer carries expectedOffset) and
# resumes from there.
PIECE=$((48 * 1024 * 1024))
OFFSET=0
[ "$REUSED" = "1" ] && OFFSET="$SIZE"

# Bounded, because the loop below was not.
#
# The real defect was the `416` arm: it assigned the SERVER's `expectedOffset`
# straight to its own loop variable, with no check that it was a number or
# that it moved FORWARD. A service answering "resume from 0" every time —
# broken, or hostile, and `--api` points wherever you like — re-uploaded the
# same bytes for as long as anyone let it. Reproduced in
# test-upload-bounded.sh, which is where the deadline below comes from.
#
# The `000` arm was the opposite of what it looked like: `curl -w
# '%{http_code}'` ALREADY prints 000 when the transport fails, so the
# `|| echo 000` after it made the value `000000`, which matched no arm and
# fell through to `*)`. Transport failure therefore exited immediately with
# "upload refused (HTTP 000000)" instead of retrying at all — the retry the
# arm was written for never happened once. Normalised below so the arm works
# and a dropped connection is retried a bounded number of times.
#
# Both limits are env-overridable so the regression test can drive them in
# seconds instead of an hour, and so an operator on a bad line can raise them
# without editing the script.
UPLOAD_MAX_FAILS="${H2WP_UPLOAD_MAX_FAILS:-8}"
UPLOAD_TOTAL_SECONDS="${H2WP_UPLOAD_TOTAL_SECONDS:-3600}"
UPLOAD_DEADLINE=$(( $(date +%s) + UPLOAD_TOTAL_SECONDS ))
fails=0

while [ "$OFFSET" -lt "$SIZE" ]; do
  if [ "$(date +%s)" -gt "$UPLOAD_DEADLINE" ]; then
    fail_with UPLOAD_TIMEOUT upload \
      "the upload did not finish within $UPLOAD_TOTAL_SECONDS seconds" \
      "re-run this script; the upload resumes from where it stopped"
  fi
  REMAIN=$((SIZE - OFFSET))
  LEN=$((REMAIN < PIECE ? REMAIN : PIECE))
  FINAL=""
  [ $((OFFSET + LEN)) -eq "$SIZE" ] && FINAL="&final=1&sha256=$SHA"
  tail -c "+$((OFFSET + 1))" "$TMP/upload.tar.gz" | head -c "$LEN" > "$TMP/piece"
  RAW="$(curl -sS --connect-timeout 20 --max-time 600 \
    -o "$TMP/up.json" -w '%{http_code}' -X PUT "$UPLOAD_URL?offset=$OFFSET$FINAL" \
    -H "x-html2wp-client: ${CLIENT_VERSION:-unknown}" \
    -H "x-html2wp-host: $CLIENT_HOST" \
    -H 'content-type: application/octet-stream' --data-binary @"$TMP/piece" || echo 000)"
  # Last three digits: curl's own -w output may already be there, so the
  # fallback can double it (see the note above).
  HTTP="${RAW: -3}"
  case "$HTTP" in
    200)
      OFFSET=$((OFFSET + LEN))
      fails=0
      ;;
    416)
      NEXT="$(json_field "$TMP/up.json" expectedOffset)"
      # Must be a number, and must not send us backwards or nowhere.
      case "$NEXT" in
        ''|*[!0-9]*)
          fail_with UPLOAD_BAD_OFFSET upload \
            "the service asked to resume from $NEXT, which is not an offset" \
            "check --api points at the real service" ;;
      esac
      # No exemption for 0. "Start over" is legitimate ONCE, and the counter
      # allows that; a service that says it every time is the loop this whole
      # block exists to stop, and 0 is the value it says it with.
      if [ "$NEXT" -le "$OFFSET" ]; then
        fails=$((fails + 1))
        if [ "$fails" -ge "$UPLOAD_MAX_FAILS" ]; then
          fail_with UPLOAD_STALLED upload \
            "the service asked $fails times to resume from $NEXT — the upload is not progressing" \
            "nothing is lost; re-run this script to try again"
        fi
      else
        fails=0
      fi
      OFFSET="$NEXT"
      ;;
    000)
      fails=$((fails + 1))
      if [ "$fails" -ge "$UPLOAD_MAX_FAILS" ]; then
        fail_with SERVER_UNREACHABLE upload \
          "the upload failed $fails times in a row at offset $OFFSET — the service is unreachable" \
          "nothing is lost; re-run this script and it resumes from here"
      fi
      echo "piece at $OFFSET dropped mid-flight ($fails/$UPLOAD_MAX_FAILS); asking again..."
      sleep $((fails * 2))
      ;;
    409)
      # The service already has this upload. The finalising piece verifies,
      # unpacks and settles the workspace, which takes long enough that a
      # dropped answer is a real outcome — and a resend of a piece whose work
      # is already done is answered "this job already has its upload", which
      # this loop used to treat as a fatal refusal and throw the archive away.
      # Settled is settled; go on to the transform.
      echo "the upload was already settled (the answer to the last piece never arrived)"
      OFFSET="$SIZE"
      ;;
    401|403|404|410)
      # A resumed upload whose job the service no longer knows is the same
      # case as a reused job the transform refuses, handled the same way.
      if [ "$RESUMED_UPLOAD" = "1" ] && [ -z "${H2WP_RETRIED:-}" ]; then
        renew_job upload "the upload was refused with HTTP $HTTP"
      fi
      fail_with "UPLOAD_REFUSED_$HTTP" upload \
        "upload refused (HTTP $HTTP): $(json_field "$TMP/up.json" error)" \
        "the archive was discarded; fix what the service named and run again" ;;
    *)
      fail_with "UPLOAD_REFUSED_$HTTP" upload \
        "upload refused (HTTP $HTTP): $(json_field "$TMP/up.json" error)" \
        "the archive was discarded; fix what the service named and run again" ;;
  esac
done
if [ "$REUSED" != "1" ]; then
  RERUN="$(json_field "$TMP/up.json" reRun)"
  [ "$RERUN" = "True" ] && echo "the service recognised this site — a re-run, not a new conversion"
  echo "upload settled"
  save_state settled
fi

# ---- transform -----------------------------------------------------------
phase transform
# Retried on transport failure, and on a "not now" the service asked us to
# repeat: the same request returns the recorded result of the run already
# done, so retrying never spends another attempt.
#
# The bearer token goes to curl in a header file, not on its command line.
(umask 077; printf 'authorization: Bearer %s\n' "$TOKEN" > "$TMP/auth.headers")
DROPS=0
TRANSFORM_WAITED=0
TRANSFORM_WAIT_MAX="${H2WP_TRANSFORM_WAIT_SECONDS:-900}"
while :; do
  # Last three digits, as in the upload loop and the job request: curl's own
  # -w ALREADY prints 000 when the transport fails, so the `|| echo 000` after
  # it makes the value `000000` — which is not `000`, so this loop used to
  # treat a dropped connection as a real answer and never retried once.
  RAW="$(curl -sS --connect-timeout 20 -m 900 \
    -o "$TMP/result.json" -D "$TMP/result.head" -w '%{http_code}' -X POST "$API/v1/jobs/$JOB/transform" \
    -H "x-html2wp-client: ${CLIENT_VERSION:-unknown}" \
    -H "x-html2wp-host: $CLIENT_HOST" \
    -H @"$TMP/auth.headers" -H 'content-type: application/json' -d "$OPTS" || echo 000)"
  HTTP="${RAW: -3}"
  if [ "$HTTP" = "000" ]; then
    DROPS=$((DROPS + 1))
    [ "$DROPS" -ge 5 ] && break
    echo "the answer did not arrive (attempt $DROPS) — asking for the recorded result..."
    sleep $((DROPS * 5))
    continue
  fi
  # A 429 that names a wait is the box being full — the job keeps its place
  # and its upload, and the call is safe to repeat. A 429 that names none is
  # this job's attempts spent, where asking again cannot help.
  if [ "$HTTP" = "429" ] && [ -n "$(retry_after "$TMP/result.head")" ] && [ "$TRANSFORM_WAITED" -lt "$TRANSFORM_WAIT_MAX" ]; then
    WAIT="$(wait_seconds "$(retry_after "$TMP/result.head")" 60)"
    TRANSFORM_WAITED=$((TRANSFORM_WAITED + WAIT))
    echo "the service is running as many conversions as it can; asking again in ${WAIT}s (waited ${TRANSFORM_WAITED}s of ${TRANSFORM_WAIT_MAX}s)"
    sleep "$WAIT"
    continue
  fi
  break
done
# The answer, kept beside the job state: $TMP goes with the trap, and a
# recorded failure is worth reading after the run.
[ ! -f "$TMP/result.json" ] || (umask 077; cp "$TMP/result.json" "${STATE_FILE}.result")
# A reused job the service no longer recognises (expired token, wiped state)
# or one a newer job of this caller superseded starts over cleanly, once —
# or, under H2WP_STRICT_JOBS, stops and says so.
if [ "$REUSED" = "1" ] && [ -z "${H2WP_RETRIED:-}" ]; then
  if [ "$HTTP" = "403" ]; then
    renew_job transform "HTTP 403"
  elif [ "$HTTP" = "409" ] && [ "$(json_field "$TMP/result.json" reason)" = "superseded" ]; then
    renew_job transform "superseded by a newer job"
  fi
fi
# A recorded transform failure can come back as HTTP 200 with no theme to
# download. It is a failure, with the generator's own words, not an unsafe
# download URL.
if [ "$HTTP" = "200" ] && [ -z "$(json_field "$TMP/result.json" downloads.theme)" ]; then
  HTTP=422
fi
if [ "$HTTP" != "200" ]; then
  H2WP_STAGE="$(json_field "$TMP/result.json" stage)"
  H2WP_CODE="TRANSFORM_FAILED"
  [ "$HTTP" = "000" ] && H2WP_CODE=SERVER_UNREACHABLE
  [ "$HTTP" = "429" ] && H2WP_CODE=RATE_LIMITED
  [ "$HTTP" = "400" ] && H2WP_CODE=BAD_REQUEST
  H2WP_STATUS=FAILED_WITH_ACTION
  H2WP_MESSAGE="$(json_field "$TMP/result.json" message)"
  [ -z "$H2WP_MESSAGE" ] && H2WP_MESSAGE="the transform did not succeed (HTTP $HTTP)"
  H2WP_ACTION="read the stage and message above; every call here is safe to repeat"
  echo "the transform did not succeed (HTTP $HTTP):" >&2
  python3 - "$TMP/result.json" <<'PY' >&2
import json, sys
try:
    r = json.load(open(sys.argv[1]))
except Exception:
    r = {}
print('stage:', r.get('stage'), '—', r.get('message') or r.get('error'))
tail = r.get('logTail')
if tail: print('--- log tail ---'); print(tail)
PY
  exit 1
fi

# ---- download + unpack ---------------------------------------------------
phase download
THEME_URL="$(json_field "$TMP/result.json" downloads.theme)"

# The URL comes out of the answer, and --api can point anywhere, so it is an
# untrusted string that this script is about to fetch and unpack. It must at
# least be https and live on the host we were already talking to — otherwise
# a hostile (or merely misconfigured) service can redirect the download of the
# thing that gets installed on somebody's WordPress.
API_HOST="$(printf '%s' "$API" | sed -E 's#^[a-z]+://##; s#/.*##')"
THEME_HOST="$(printf '%s' "$THEME_URL" | sed -E 's#^[a-z]+://##; s#/.*##')"
case "$THEME_URL" in
  https://*) ;;
  # A plain-http API is a local dev gate; allow it only when that is what was
  # asked for, never as a downgrade from an https --api.
  http://*) case "$API" in http://*) ;; *) fail_with UNSAFE_ARTIFACT download \
      "refusing the download: an https service answered with an http download URL" \
      "the service that answered is not one this client will unpack from" ;; esac ;;
  *) fail_with UNSAFE_ARTIFACT download \
      "refusing the download: the URL is not http(s)" \
      "the service that answered is not one this client will unpack from" ;;
esac
[ "$THEME_HOST" = "$API_HOST" ] || fail_with UNSAFE_ARTIFACT download \
  "refusing the download: it points at $THEME_HOST, not the service at $API_HOST" \
  "the service that answered is not one this client will unpack from"

# --max-time, or a stalled download hangs the conversion with no way out but
# ctrl-C. Generous: a big theme over a slow line is a real case.
#
# --max-filesize bounds what an answer can make this write to disk. Without it
# the only limit on the download was the caller's free space, and the thing
# being downloaded is chosen by whatever service --api names.
THEME_MAX_BYTES=$((512 * 1024 * 1024))
curl -sSf --connect-timeout 20 --max-time 900 \
  --max-filesize "$THEME_MAX_BYTES" \
  -H "x-html2wp-client: ${CLIENT_VERSION:-unknown}" \
  -H "x-html2wp-host: $CLIENT_HOST" \
  "$THEME_URL" -o "$TMP/theme.tar.gz" || fail_with UNSAFE_ARTIFACT download \
  "the theme download failed or exceeded $THEME_MAX_BYTES bytes" \
  "retry; every call in this script is safe to repeat"

# The digest the service said this archive has. Verifying it turns "some bytes
# arrived" into "the bytes that run produced arrived", which is the difference
# between trusting the connection and trusting the answer.
#
# Absent on an older gate, and that must not break the conversion — so a
# missing digest is a warning, a MISMATCHED one is fatal.
EXPECT_SHA="$(json_field "$TMP/result.json" checksums.theme.sha256)"
if [ -n "$EXPECT_SHA" ]; then
  GOT_SHA="$(shasum -a 256 "$TMP/theme.tar.gz" 2>/dev/null | cut -d' ' -f1)"
  [ -n "$GOT_SHA" ] || GOT_SHA="$(sha256sum "$TMP/theme.tar.gz" 2>/dev/null | cut -d' ' -f1)"
  if [ -n "$GOT_SHA" ] && [ "$GOT_SHA" != "$EXPECT_SHA" ]; then
    fail_with UNSAFE_ARTIFACT download \
      "refusing the archive: its digest is not the one the service reported" \
      "the download was altered or truncated in transit — retry, and if it repeats do not install it"
  fi
  echo "theme digest verified (sha256 ${EXPECT_SHA%"${EXPECT_SHA#??????????}"}…)"
else
  echo "note: this gate reported no theme digest, so the download could not be verified against one."
fi

# What it expands to, counted before anything is unpacked. A small archive can
# expand without bound, and this one is unpacked into the caller's own
# workspace.
UNPACK_MAX_BYTES=$((1500 * 1024 * 1024))
EXPANDED="$(gzip -dc "$TMP/theme.tar.gz" 2>/dev/null | head -c "$((UNPACK_MAX_BYTES + 1))" | wc -c | tr -d ' ')"
if [ "${EXPANDED:-0}" -gt "$UNPACK_MAX_BYTES" ]; then
  fail_with UNSAFE_ARTIFACT download \
    "refusing the archive: it expands to more than $UNPACK_MAX_BYTES bytes, which a theme does not" \
    "the service that answered is not one this client will unpack from"
fi
ENTRIES="$(tar -tzf "$TMP/theme.tar.gz" 2>/dev/null | wc -l | tr -d ' ')"
if [ "${ENTRIES:-0}" -gt 20000 ]; then
  fail_with UNSAFE_ARTIFACT download \
    "refusing the archive: it holds $ENTRIES entries, which a theme does not" \
    "the service that answered is not one this client will unpack from"
fi
# --api can point anywhere, so the answer is not automatically trusted: an
# entry climbing out of the workspace would write wherever you can write.
if tar -tzf "$TMP/theme.tar.gz" | grep -qE '^/|(^|/)\.\.(/|$)'; then
  fail_with UNSAFE_ARTIFACT download \
    "refusing the archive: it contains absolute or parent paths" \
    "the service that answered is not one this client will unpack from"
fi

# That check reads NAMES, and `tar -tzf` prints nothing else — the member type
# is in the header and never appears. So a symlink with a perfectly ordinary
# name passed it, and once unpacked into the workspace it is a door out:
# `theme/x -> /Users/you/.ssh` followed by a regular entry `theme/x/authorized_keys`
# is two innocent-looking names and one overwritten file.
#
# Unpacked into a staging directory first and inspected with lstat, which
# cannot be misread the way a `tar -tvzf` parser can (its format differs
# between GNU tar and bsdtar). Only then moved into the workspace.
STAGE="$TMP/unpack"
mkdir -p "$STAGE"
tar -xzf "$TMP/theme.tar.gz" -C "$STAGE" --no-same-owner --no-same-permissions
BAD="$(find "$STAGE" \( -type l -o -type b -o -type c -o -type p -o -type s \) -print 2>/dev/null | head -5)"
if [ -n "$BAD" ]; then
  echo "$BAD" | sed "s|$STAGE/|  |" >&2
  fail_with UNSAFE_ARTIFACT download \
    "refusing the archive: it contains links or special files, which a theme does not" \
    "the service that answered is not one this client will unpack from"
fi
# -R and not `mv`, so an existing theme directory is updated rather than
# replaced wholesale — a re-run must not delete anything the owner added.
cp -R "$STAGE"/. "$WS"/
SLUG="$(json_field "$TMP/result.json" slug)"
echo "theme unpacked: $WS/theme/$SLUG"

# No editor is bundled with a conversion. Every job is pointed at the public
# Visual Edit Lite release — a link, which stays current on its own. Visual
# Edit Pro is a separate purchase, activated on the site, never shipped here.
EDITOR_INSTALL="$(json_field "$TMP/result.json" editor.install)"
if [ -n "$EDITOR_INSTALL" ]; then
  echo "editor (Visual Edit Lite, optional): $EDITOR_INSTALL"
  echo "  The theme is standalone and needs no plugin. Install the free editor"
  echo "  only if the owner wants click-to-edit authoring; Visual Edit Pro is a"
  echo "  separate purchase, activated on the site."
fi

python3 - "$TMP/result.json" <<'PY'
import json, sys
try:
    r = json.load(open(sys.argv[1]))
except Exception:
    r = {}
report = r.get('themeReport') or {}
warnings = report.get('warnings') or []
if warnings:
    print('--- theme-report warnings: read every one, wire what the generator could not ---')
    for w in warnings: print(' *', w)
PY
# The run reached the end with a theme on disk. Recorded before the trap runs
# so a reader can tell this apart from a client that was killed mid-flight.
H2WP_STATUS=SUCCESS
H2WP_CODE=OK
H2WP_STAGE=done
H2WP_MESSAGE="theme unpacked into $WS/theme/$SLUG"
H2WP_ACTION="continue with stage 3.5 (screenshot) and stage 5 (WordPress gates)"

echo "server stages done — continue with the screenshot (stage 3.5) and the WordPress gates (stage 5)"
# Credit AFTER this conversion — the service counted it, so this is what is
# left now. Relay verbatim; do not compute a number yourself.
CREDIT="$(json_field "$TMP/result.json" credit.line)"
[ -n "$CREDIT" ] && echo "credit: $CREDIT"
echo "outcome recorded in $RESULT"
