#!/usr/bin/env bash
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
# Stages 2 → 2.5 → 2.7 in one call, after the LAST rebuild (the one after 2.65).
#
#   assets/scripts/stage2-gates.sh <workspace> [--original <dir>] [--original-remote <report>] [--jobs N]
#
# --jobs N is passed to gate A only: its three widths measured at once, one
# Chromium each (N at most 3), every red or doubtful pair measured again alone
# before the verdict. Left out, gate A runs as it always has — one browser,
# --jobs 1 — and that is the certifying run. Use --jobs in fix cycles.
#
#   phase 1, in parallel:  gate A   verify-static.py  --original U --dist dist --out {ws}/verify-static
#                          gate A2  verify-parity.mjs (its original stays the working copy)
#                          2.5a     chrome-groups.mjs
#   phase 2, alone:        2.5b     capture-chrome.py
#   phase 3, alone:        2.7      detect-collections.py
#
# The commands, arguments and output paths are exactly the ones the stages run
# one by one; only the waiting overlaps. Of the three in phase 1 only gate A
# opens a browser — A2 and chrome-groups read files for a few seconds and
# write different ones (parity-report.json, chrome-groups.json), and none of
# them reads what another writes.
#
# capture-chrome.py never shares the machine: it gives each page a fixed
# 700 ms to reach its resting state, and what it captures ships in the theme,
# so a loaded machine is a different byte in the product. detect-collections
# rewrites conversion-manifest.json, which A2 and chrome-groups read, so it
# runs last and alone.
#
# U defaults to {ws}/input-untouched — the copy taken before 0.5/0.6/2.6
# touched anything. Gate A against the working copy instead would prove only
# the build, and quietly stop measuring what the optimisations cost. There is
# no fallback: without U this refuses.
#
# Nothing is hidden. Every child writes its own log under {ws}/logs/stage2/;
# they are printed in this fixed order when all have finished, followed by one
# line per child — `<name>: ok`, `FAILED(<code>)`, or `skipped (<why>)`.
#
# Exit status: 0 when every child passed, otherwise the SUM of
#   1 gate A   2 gate A2   4 chrome-groups   8 capture-chrome   16 detect-collections
# (a skipped child adds nothing; the failure that caused the skip already did).
# 64 = the call itself was wrong (usage, missing workspace or U).
set -uo pipefail

S="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() { echo "usage: stage2-gates.sh <workspace> [--original <dir>] [--original-remote <report>] [--jobs N]" >&2; exit 64; }

WS="" ORIG="" REMOTE="" JOBS=""
while [ $# -gt 0 ]; do
  case "$1" in
    --original=*) ORIG="${1#--original=}" ;;
    --original) [ $# -ge 2 ] || usage; ORIG="$2"; shift ;;
    --original-remote=*) REMOTE="${1#--original-remote=}" ;;
    --original-remote) [ $# -ge 2 ] || usage; REMOTE="$2"; shift ;;
    --jobs=*) JOBS="${1#--jobs=}" ;;
    --jobs) [ $# -ge 2 ] || usage; JOBS="$2"; shift ;;
    -h|--help) usage ;;
    -*) echo "unknown flag: $1" >&2; usage ;;
    *) [ -z "$WS" ] || usage; WS="$1" ;;
  esac
  shift
done
[ -n "$WS" ] || usage
[ -d "$WS" ] || { echo "no workspace at $WS" >&2; exit 64; }
WS="$(cd "$WS" && pwd)"
MF="$WS/conversion-manifest.json"
DIST="$WS/astro-project/dist"
[ -f "$MF" ] || { echo "no conversion-manifest.json in $WS" >&2; exit 64; }
[ -f "$DIST/index.html" ] || { echo "no build at $DIST — run stage 1 first" >&2; exit 64; }
ORIG="${ORIG:-$WS/input-untouched}"
[ -d "$ORIG" ] || { echo "no untouched original at $ORIG — take it at stage 0 (cp -a <input> $WS/input-untouched) or pass --original" >&2; exit 64; }
[ -z "$REMOTE" ] || [ -f "$REMOTE" ] || { echo "no --original-remote report at $REMOTE" >&2; exit 64; }
case "$JOBS" in ""|[1-9]|[1-9][0-9]) ;; *) echo "--jobs wants a whole number from 1: $JOBS" >&2; exit 64 ;; esac

LOG="$WS/logs/stage2"
mkdir -p "$LOG"
rm -f "$LOG"/*.log "$LOG"/*.rc "$LOG"/*.secs "$LOG"/*.skip

GATE_A=(python3 "$S/verify-static.py" --original "$ORIG" --dist "$DIST" --out "$WS/verify-static")
[ -f "$WS/analysis.json" ] && GATE_A+=(--merged "$WS/analysis.json")
[ -n "$REMOTE" ] && GATE_A+=("--original-remote=$REMOTE")
[ -n "$JOBS" ] && GATE_A+=(--jobs "$JOBS")

# run <name> <command...> — the command's output to its own log; its exit code
# and wall-clock seconds to files beside it, so the report never depends on
# which of `wait`'s answers we happened to keep.
run() {
  local name="$1"; shift
  local t0 rc; t0="$(date +%s)"
  if "$@" >"$LOG/$name.log" 2>&1; then rc=0; else rc=$?; fi
  echo "$(( $(date +%s) - t0 ))" >"$LOG/$name.secs"
  echo "$rc" >"$LOG/$name.rc"
  return "$rc"
}

PIDS=""
# ctrl-C or a kill stops the children too, instead of leaving a browser
# rendering into the verdict directory after the wrapper has gone.
trap 'for p in $PIDS; do pkill -TERM -P "$p" 2>/dev/null; kill "$p" 2>/dev/null; done; exit 130' INT TERM

T0="$(date +%s)"
echo "==> stage 2: gate A ‖ gate A2 ‖ chrome-groups (logs: $LOG)"
run gate-a "${GATE_A[@]}" & PIDS="$PIDS $!"
run gate-a2 node "$S/verify-parity.mjs" --manifest="$MF" & PIDS="$PIDS $!"
run chrome-groups node "$S/chrome-groups.mjs" --manifest="$MF" & PIDS="$PIDS $!"
for p in $PIDS; do wait "$p"; done
PIDS=""

rc_of() { cat "$LOG/$1.rc" 2>/dev/null || echo 255; }

if [ "$(rc_of chrome-groups)" = 0 ]; then
  echo "==> stage 2.5b: capture-chrome (alone)"
  run capture-chrome python3 "$S/capture-chrome.py" --manifest="$MF"
else
  echo "skipped (chrome-groups FAILED — the capture is taken per design group)" >"$LOG/capture-chrome.skip"
fi

echo "==> stage 2.7: detect-collections (alone)"
run detect-collections python3 "$S/detect-collections.py" --manifest="$MF"
T1="$(date +%s)"

STATUS=0
SUMMARY=""
bit=1
for name in gate-a gate-a2 chrome-groups capture-chrome detect-collections; do
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
      STATUS=$(( STATUS + bit ))
    fi
  fi
  SUMMARY="$SUMMARY$line
"
  bit=$(( bit * 2 ))
done

echo
echo "==> stage2-gates: $(( T1 - T0 ))s wall clock"
printf '%s' "$SUMMARY"
exit "$STATUS"
