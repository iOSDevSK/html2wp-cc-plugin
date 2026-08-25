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
#   visual-edit.zip          (licensed conversions only) the paid editor
#
# Every call here is safe to repeat — a dropped connection is repaired by
# running this script again. The service records the transform result, so a
# retry returns the run already done rather than spending another attempt.
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

write_result() {
  python3 - "$RESULT" "$H2WP_STATUS" "$H2WP_CODE" "$H2WP_STAGE" "$H2WP_MESSAGE" "$H2WP_ACTION" \
    "${JOB:-}" "${EDITION:-}" "${SLUG:-}" <<'PY'
import json, sys
path, status, code, stage, message, action, job, edition, slug = sys.argv[1:10]
out = {"status": status, "code": code, "stage": stage, "message": message}
if action: out["action"] = action
if job: out["jobId"] = job
if edition: out["edition"] = edition
if slug: out["slug"] = slug
with open(path, "w") as fh:
    json.dump(out, fh, indent=2)
    fh.write("\n")
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
MEMBERS=(conversion-manifest.json astro-report.json astro-project)
for extra in chrome-at-rest style-specimens chrome-groups.json; do
  [ -e "$WS/$extra" ] && MEMBERS+=("$extra")
done
# COPYFILE_DISABLE: without it macOS bsdtar writes AppleDouble `._*` entries,
# which materialise as real files on the Linux side and read as pages with no
# <body>. The server also discards them, but not shipping junk beats relying
# on the janitor.
COPYFILE_DISABLE=1 tar -czf "$TMP/upload.tar.gz" -C "$WS" \
  --exclude node_modules --exclude .astro --exclude '._*' --exclude .DS_Store \
  "${MEMBERS[@]}"
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
# An unchanged input reuses its job: the upload is already settled, so a
# retry with different --opts goes straight to the transform — no second
# upload, no second job. The state file is invalidated the moment the input
# changes (the sha differs) or the service no longer recognises the token.
STATE_FILE="$WS/.h2wp-job.json"
JOB=""
TOKEN=""
REUSED=0
if [ -f "$STATE_FILE" ] && [ "$(json_field "$STATE_FILE" sha)" = "$SHA" ]; then
  JOB="$(json_field "$STATE_FILE" job)"
  TOKEN="$(json_field "$STATE_FILE" token)"
  REUSED=1
  echo "job $JOB reused (input unchanged — straight to the transform)"
fi

if [ -z "$JOB" ]; then
  BODY="$(python3 - "$PAGES" "$KEY" <<'PY'
import json, sys
body = {'pages': int(sys.argv[1])}
if sys.argv[2].strip(): body['key'] = sys.argv[2].strip()
print(json.dumps(body))
PY
)"
  # `|| echo 000` and not a bare curl: without it a service that cannot be
  # reached at all kills the script under `set -e` before anything can record
  # WHY, and the outcome file reads "interrupted" for what is really "the
  # service did not answer". Normalised to the last three digits for the same
  # reason as the upload loop — curl's own -w already prints 000 on failure.
  RAW="$(curl -sS --connect-timeout 20 --max-time 60 \
    -o "$TMP/job.json" -w '%{http_code}' -X POST "$API/v1/jobs" \
    -H "x-html2wp-client: ${CLIENT_VERSION:-unknown}" \
    -H "x-html2wp-host: $CLIENT_HOST" \
    -H 'content-type: application/json' -d "$BODY" || echo 000)"
  HTTP="${RAW: -3}"
  if [ "$HTTP" = "000" ]; then
    fail_with SERVER_UNREACHABLE job \
      "could not reach $API — no answer to the request to open a conversion" \
      "check the address and your connection, then run this again; nothing was uploaded"
  fi
  if [ "$HTTP" != "201" ]; then
    echo "the service refused to open a conversion (HTTP $HTTP):" >&2
    json_field "$TMP/job.json" error >&2
    # The service's own reason code, relayed rather than flattened — it is the
    # difference between "wait a day" and "buy a licence".
    REASON="$(json_field "$TMP/job.json" reason)"
    fail_with "${REASON:-JOB_REFUSED}" job \
      "$(json_field "$TMP/job.json" error)" \
      "the service explained why above; nothing was uploaded"
  fi
  JOB="$(json_field "$TMP/job.json" job)"
  TOKEN="$(json_field "$TMP/job.json" token)"
  UPLOAD_URL="$(json_field "$TMP/job.json" upload.url)"
  EDITION="$(json_field "$TMP/job.json" edition)"
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
  python3 - "$JOB" "$TOKEN" "$SHA" "$STATE_FILE" <<'PY'
import json, sys
job, token, sha, state_file = sys.argv[1:5]
json.dump({'job': job, 'token': token, 'sha': sha}, open(state_file, 'w'))
PY
  chmod 600 "$STATE_FILE"
fi

# ---- transform -----------------------------------------------------------
# Retried on transport failure only: the same request returns the recorded
# result of the run already done, so retrying never spends another attempt.
for attempt in 1 2 3 4 5; do
  HTTP="$(curl -sS --connect-timeout 20 -m 900 \
    -o "$TMP/result.json" -w '%{http_code}' -X POST "$API/v1/jobs/$JOB/transform" \
    -H "x-html2wp-client: ${CLIENT_VERSION:-unknown}" \
    -H "x-html2wp-host: $CLIENT_HOST" \
    -H "authorization: Bearer $TOKEN" -H 'content-type: application/json' -d "$OPTS" || echo 000)"
  [ "$HTTP" != "000" ] && break
  echo "the answer did not arrive (attempt $attempt) — asking for the recorded result..."
  sleep $((attempt * 5))
done
# A reused job the service no longer recognises (expired token, wiped state)
# starts over cleanly, once.
if [ "$HTTP" = "403" ] && [ "$REUSED" = "1" ] && [ -z "${H2WP_RETRIED:-}" ]; then
  echo "the saved job is no longer valid — starting a fresh one"
  rm -f "$STATE_FILE"
  export H2WP_RETRIED=1
  # The key travels in the environment, never in argv: it is readable in
  # `ps` there, which would undo the reason it lives in a file at all.
  H2WP_KEY="$KEY" exec bash "$0" "$WS" --api="$API" --opts="$OPTS"
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

# The editor, either edition. Pro travels with the job as a signed download;
# the free edition is public, so it is a link and stays current on its own.
EDITOR_EDITION="$(json_field "$TMP/result.json" editor.edition)"
EDITOR_INSTALL="$(json_field "$TMP/result.json" editor.install)"
if [ "$EDITOR_EDITION" = "pro" ]; then
  # Same reasoning as the theme download: bounded, and only from the service
  # we were already talking to. This one is a plugin that gets installed.
  EDITOR_HOST="$(printf '%s' "$EDITOR_INSTALL" | sed -E 's#^[a-z]+://##; s#/.*##')"
  [ "$EDITOR_HOST" = "$API_HOST" ] || fail_with UNSAFE_ARTIFACT download \
    "refusing the editor download: it points at $EDITOR_HOST, not the service at $API_HOST" \
    "the service that answered is not one this client will install from"
  curl -sSf --connect-timeout 20 --max-time 600 \
    --max-filesize $((128 * 1024 * 1024)) \
    "$EDITOR_INSTALL" -o "$WS/visual-edit.zip" || fail_with UNSAFE_ARTIFACT download \
    "the editor download failed or was larger than a plugin should be" \
    "retry; every call in this script is safe to repeat"
  echo "editor (Pro, licensed): $WS/visual-edit.zip"
elif [ -n "$EDITOR_INSTALL" ]; then
  echo "editor (free edition, optional): $EDITOR_INSTALL"
  echo "  The theme is standalone and needs no plugin. Install the free editor"
  echo "  only if the owner wants click-to-edit authoring."
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
