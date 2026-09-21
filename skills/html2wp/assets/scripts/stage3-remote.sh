#!/usr/bin/env bash
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
# Stage 3 (the service) and stage 3.5 (the screenshot), with stage 5's
# WordPress brought up while the service works.
#
#   assets/scripts/stage3-remote.sh <workspace> [--env-slug=<slug>] [convert-remote.sh flags...]
#
#   in parallel:  convert-remote.sh <workspace> <flags>     stages 3–4.6
#                 test-env.sh up <slug>   (from the workspace)
#   then:         make-screenshot.py                        stage 3.5, needs the theme
#
# The two have nothing in common: the upload reads the workspace and writes
# theme/ and .h2wp-result.json into it, `up` writes .test-env-<slug>.json and
# talks to Docker. The slug is the manifest's site.slug unless --env-slug says
# otherwise; every other flag goes to convert-remote.sh unchanged.
#
# A WordPress that did not come up is said NOW, not discovered at stage 5.
# The repair is `test-env.sh up <slug>` again (it is idempotent); the transform
# is not repeated — the service returns the run it already recorded.
#
# Every child writes its own log under {ws}/logs/stage3/, printed in this
# fixed order when all have finished, then one line per child — `<name>: ok`,
# `FAILED(<code>)` or `skipped (<why>)`.
#
# Exit status: 0 when all passed, otherwise the SUM of
#   10 convert-remote   20 test-env   40 make-screenshot
# (30 = both parallel children failed; a skipped screenshot adds nothing — the
# failed upload already did). 64 = the call itself was wrong.
set -uo pipefail

S="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() { echo "usage: stage3-remote.sh <workspace> [--env-slug=<slug>] [--api=URL] [--key=KEY] [--opts=JSON]" >&2; exit 64; }

WS="" SLUG=""
PASS=()
for arg in "$@"; do
  case "$arg" in
    --env-slug=*) SLUG="${arg#--env-slug=}" ;;
    -h|--help) usage ;;
    -*) PASS+=("$arg") ;;
    *) [ -z "$WS" ] || usage; WS="$arg" ;;
  esac
done
[ -n "$WS" ] || usage
[ -d "$WS" ] || { echo "no workspace at $WS" >&2; exit 64; }
WS="$(cd "$WS" && pwd)"
MF="$WS/conversion-manifest.json"
[ -f "$MF" ] || { echo "no conversion-manifest.json in $WS" >&2; exit 64; }
if [ -z "$SLUG" ]; then
  SLUG="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["site"]["slug"])' "$MF" 2>/dev/null)" \
    || { echo "cannot read site.slug from $MF — pass --env-slug" >&2; exit 64; }
fi
[ -n "$SLUG" ] || usage

LOG="$WS/logs/stage3"
mkdir -p "$LOG"
rm -f "$LOG"/*.log "$LOG"/*.rc "$LOG"/*.secs "$LOG"/*.skip

# run <name> <command...> — output to its own log, exit code and seconds beside it.
run() {
  local name="$1"; shift
  local t0 rc; t0="$(date +%s)"
  if "$@" >"$LOG/$name.log" 2>&1; then rc=0; else rc=$?; fi
  echo "$(( $(date +%s) - t0 ))" >"$LOG/$name.secs"
  echo "$rc" >"$LOG/$name.rc"
  return "$rc"
}
rc_of() { cat "$LOG/$1.rc" 2>/dev/null || echo 255; }

PIDS=""
trap 'for p in $PIDS; do pkill -TERM -P "$p" 2>/dev/null; kill "$p" 2>/dev/null; done; exit 130' INT TERM

T0="$(date +%s)"
echo "==> stage 3: convert-remote ‖ test-env up $SLUG (logs: $LOG)"
run convert-remote bash "$S/convert-remote.sh" "$WS" ${PASS[@]+"${PASS[@]}"} & PIDS="$PIDS $!"
( cd "$WS" && run test-env bash "$S/test-env.sh" up "$SLUG" ) & PIDS="$PIDS $!"
for p in $PIDS; do wait "$p"; done
PIDS=""

SCHEMA="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("schema", ""))' "$MF" 2>/dev/null)"
if [ "$SCHEMA" = "html2wp/2" ]; then
  # The native Gutenberg theme is shot from its installed frontend, after
  # the import: gutenberg-screenshot.py (references/gutenberg.md).
  echo "skipped (Gutenberg target — gutenberg-screenshot.py runs after the install)" >"$LOG/make-screenshot.skip"
elif [ "$(rc_of convert-remote)" = 0 ]; then
  echo "==> stage 3.5: make-screenshot"
  run make-screenshot python3 "$S/make-screenshot.py" --manifest="$MF"
else
  echo "skipped (convert-remote FAILED — there is no theme to shoot)" >"$LOG/make-screenshot.skip"
fi
T1="$(date +%s)"

STATUS=0
SUMMARY=""
for pair in convert-remote:10 test-env:20 make-screenshot:40; do
  name="${pair%%:*}" weight="${pair##*:}"
  echo
  if [ -f "$LOG/$name.skip" ]; then
    line="$name: $(cat "$LOG/$name.skip")"
  else
    echo "==> $name ($(cat "$LOG/$name.secs" 2>/dev/null || echo ?)s) — $LOG/$name.log"
    cat "$LOG/$name.log" 2>/dev/null
    rc="$(rc_of "$name")"
    if [ "$rc" = 0 ]; then
      line="$name: ok"
    else
      line="$name: FAILED($rc)"
      STATUS=$(( STATUS + weight ))
    fi
  fi
  SUMMARY="$SUMMARY$line
"
done
if [ "$(rc_of test-env)" != 0 ]; then
  again="  (WordPress: re-run \`test-env.sh up $SLUG\` from $WS"
  [ "$(rc_of convert-remote)" = 0 ] && again="$again — the upload need not be repeated"
  SUMMARY="$SUMMARY$again)
"
fi

echo
echo "==> stage3-remote: $(( T1 - T0 ))s wall clock"
printf '%s' "$SUMMARY"
exit "$STATUS"
