#!/usr/bin/env bash
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
#
# What am I looking at, and therefore what should happen next?
#
#   whats-here.sh <dir>
#
# Run this FIRST, before anything else, every time.
#
# Somebody comes back a week later wanting one page changed. If the pipeline
# starts at stage -1 it rebuilds the site, produces a different digest, and
# spends a conversion out of their allowance — for a change that costs nothing
# when it goes through the manifest instead. The difference between a free
# repair and a spent conversion is entirely whether anyone looked in the
# directory first, and "remember to look" is not a mechanism.
#
# Exit codes are the route, so they can be branched on:
#   0  fresh input — convert it
#   3  converted, and repairable — go to the repair table, not to stage -1
#   4  converted but stripped — deliverables only; repairs need the original
#   5  a conversion is part-finished — resume it, do not restart
set -uo pipefail

D="${1:-.}"
[ -d "$D" ] || { echo "whats-here.sh: no such directory: $D" >&2; exit 2; }
D="$(cd "$D" && pwd)"

# A workspace may be the directory itself or `conversion/` inside it — the
# skill writes the second shape, and people point at the first.
WS="$D"
[ -d "$D/conversion" ] && [ ! -f "$D/conversion-manifest.json" ] && WS="$D/conversion"

has() { [ -e "$WS/$1" ]; }

REPORT=0;  has CONVERSION-REPORT.md && REPORT=1
JOB=0;     has .h2wp-job.json && JOB=1
MANIFEST=0; has conversion-manifest.json && MANIFEST=1
KIT=0;     has conversion-manifest.json && has astro-report.json && has astro-project/dist && KIT=1
SENT=0;    has .h2wp-verdicts-sent && SENT=1
ZIP="$(ls "$WS"/*.zip 2>/dev/null | grep -v visual-edit | head -1)"
EDITOR=0;  has visual-edit.zip && EDITOR=1

echo
echo "  $WS"

# ---------------------------------------------------------------- fresh
if [ "$JOB" = "0" ] && [ "$MANIFEST" = "0" ] && [ "$REPORT" = "0" ]; then
  echo "  nothing has been converted here."
  echo
  echo "  → convert it: start at stage -3, then the pipeline from the top."
  exit 0
fi

# ------------------------------------------------------- part-finished
if [ "$REPORT" = "0" ]; then
  echo "  a conversion was started here and did not finish."
  [ "$MANIFEST" = "1" ] && echo "    the manifest exists — stage 0 got that far"
  [ "$KIT" = "1" ] && echo "    the site is built — the service stages can run"
  echo
  echo "  → RESUME. Do not start over: convert-remote.sh reuses the open job"
  echo "    and an upload picks up where it stopped."
  exit 5
fi

# ------------------------------------------------------------ finished
echo "  a finished conversion."
[ -n "$ZIP" ] && echo "    theme:  $(basename "$ZIP")"
[ "$EDITOR" = "1" ] && echo "    editor: visual-edit.zip (licensed conversion)"
[ "$SENT" = "0" ] && [ "$JOB" = "1" ] && {
  echo
  echo "  ⚠ the gates have NOT been reported. The next conversion — this site or"
  echo "    any other — is refused until they are:"
  echo "        assets/scripts/send-verdicts.sh $WS --outcome=delivered"
}

echo
if [ "$KIT" = "1" ]; then
  echo "  → REPAIRS ARE FREE HERE. The re-run kit is intact, so a fix that lives"
  echo "    in the manifest or the bundle costs no conversion:"
  echo
  echo "        edit conversion-manifest.json"
  echo "        assets/scripts/rebuild-theme.sh --manifest=conversion-manifest.json"
  echo
  echo "    DO NOT re-run the pipeline from stage -1 for this. Rebuilding the"
  echo "    site changes the digest, and a changed digest is a new conversion"
  echo "    out of the allowance."
  echo
  echo "    Classify the fix first — SKILL.md, 'Post-handover repairs'. Only a"
  echo "    change to the INPUT (markup, images) needs the pipeline from the top,"
  echo "    and that one genuinely is a new conversion."
  exit 3
fi

echo "  → the re-run kit is gone (cleanup --minimal, or it was never here)."
echo "    Manifest repairs are not possible from this directory: rebuild-theme.sh"
echo "    needs conversion-manifest.json, astro-report.json and"
echo "    astro-project/dist."
echo
echo "    A fix from here means converting again from the original project,"
echo "    which spends a conversion. Say that before doing it, not after."
exit 4
