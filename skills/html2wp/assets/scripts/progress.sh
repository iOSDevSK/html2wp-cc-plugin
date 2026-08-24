#!/usr/bin/env bash
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
#
# Where the conversion stands. Deterministic, so it cannot drift.
#
#   progress.sh start <stage> [note]   about to begin — say what and how long
#   progress.sh done  <stage> [note]   finished — percentage and what is next
#   progress.sh fail  <stage> <why>    a gate stopped it, WITH the position
#   progress.sh stages                 print the table
#
# This exists because the same thing written as an instruction does not
# survive an hour. A conversion is 30-90 minutes, mostly the user watching,
# and silence reads as a hang — which is when people kill a run and lose the
# work. A prose rule saying "report every few minutes" competes with two
# thousand lines of other rules and loses somewhere around stage 3.
#
# A script call does not lose. The numbers live here rather than in prose for
# the same reason: a percentage nobody can invent is a percentage that cannot
# creep upward to sound better.
set -uo pipefail

# stage | done% | minutes | label | what comes next
TABLE='
-3|1|0|prerequisites|prerender the app
-1|14|6|prerender the app|analyze and write the manifest
-1b|17|2|commerce specimen|analyze and write the manifest
0|21|4|analyze and write the manifest|optimise the images
0.5|26|4|optimise the images|HTML to Astro
1|36|6|HTML to Astro|gates A + A2
2|46|4|gates A + A2|capture the chrome per design group
2.5|51|3|capture the chrome per design group|move JS-held copy into the markup
2.6|56|3|move JS-held copy into the markup|the service builds the theme
3|71|3|the service builds the theme|the theme screenshot
3.5|73|1|the theme screenshot|gates B + C in a real WordPress
5|88|10|gates B + C in a real WordPress|your own page-by-page review
5.5|95|0|your own page-by-page review|the WooCommerce audit
5.6|97|2|the WooCommerce audit|package the deliverables
6|99|2|package the deliverables|report the gates
6.5|100|0|report the gates|—
'

row() { printf '%s\n' "$TABLE" | awk -F'|' -v s="$1" '$1==s {print; exit}'; }

remaining_minutes() { # everything still to come, by the table's own estimates
  printf '%s\n' "$TABLE" | awk -F'|' -v s="$1" '
    $1=="" {next}
    { if (seen) rest += $3; if ($1==s) seen=1 }
    END { print rest+0 }'
}

MODE="${1:-}"; STAGE="${2:-}"; NOTE="${3:-}"

if [ "$MODE" = "stages" ]; then
  printf '%s\n' "$TABLE" | awk -F'|' '$1!="" {printf "  %-5s %3s%%  %s\n", $1, $2, $4}'
  exit 0
fi

[ -n "$MODE" ] && [ -n "$STAGE" ] || {
  echo "usage: progress.sh start|done|fail <stage> [note]   (stages: progress.sh stages)" >&2
  exit 2
}

R="$(row "$STAGE")"
[ -n "$R" ] || { echo "progress.sh: no such stage '$STAGE' — run 'progress.sh stages'" >&2; exit 2; }

PCT="$(printf '%s' "$R" | cut -d'|' -f2)"
MINS="$(printf '%s' "$R" | cut -d'|' -f3)"
LABEL="$(printf '%s' "$R" | cut -d'|' -f4)"
NEXT="$(printf '%s' "$R" | cut -d'|' -f5)"
LEFT="$(remaining_minutes "$STAGE")"

case "$MODE" in
  start)
    if [ "$MINS" = "0" ]; then
      printf '\n  → stage %s — %s\n' "$STAGE" "$LABEL"
    else
      printf '\n  → stage %s — %s · about %s min\n' "$STAGE" "$LABEL" "$MINS"
    fi
    # The one that looks broken on a cold machine, so it gets warned about.
    [ "$STAGE" = "5" ] && printf '    first run on this machine pulls WordPress and MariaDB — several quiet minutes\n'
    [ -n "$NOTE" ] && printf '    %s\n' "$NOTE"
    ;;
  done)
    printf '\n  [ %s%% ] stage %s — %s\n' "$PCT" "$STAGE" "$LABEL"
    [ -n "$NOTE" ] && printf '          %s\n' "$NOTE"
    if [ "$STAGE" = "6.5" ]; then
      printf '          done.\n'
    elif [ "$LEFT" -gt 0 ] 2>/dev/null; then
      printf '          ~%s min left · next: %s\n' "$LEFT" "$NEXT"
    else
      printf '          next: %s\n' "$NEXT"
    fi
    # Not another step. Hand it over, and say there is no clock on it.
    [ "$STAGE" = "5" ] && printf '          the automated part is done — what is left is yours to read,\n          and there is no clock on it\n'
    ;;
  fail)
    # A refusal WITH a position is a result. The same refusal with no position
    # is a dead end, and that is the difference this branch exists for.
    printf '\n  [ %s%% ] stage %s FAILED — %s\n' "$PCT" "$STAGE" "$LABEL"
    printf '          %s\n' "${NOTE:-no reason given}"
    printf '          the pipeline stops here; nothing after this ran\n'
    ;;
  *) echo "progress.sh: unknown mode '$MODE'" >&2; exit 2 ;;
esac
