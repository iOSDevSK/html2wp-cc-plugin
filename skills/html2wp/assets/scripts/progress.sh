#!/usr/bin/env bash
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
#
# Where the conversion stands. Deterministic, so it cannot drift.
#
#   progress.sh start <stage> [note]   about to begin — say what and how long
#   progress.sh done  <stage> [note]   finished — percentage and what is next
#   progress.sh fail  <stage> <why>    a gate stopped it, WITH the position
#   progress.sh stages                 print the table
#   progress.sh summary [workspace]    where the time actually went
#
# This exists because the same thing written as an instruction does not
# survive an hour. A conversion is two to three hours, mostly the user
# watching, and silence reads as a hang — which is when people kill a run and
# lose the work. A prose rule saying "report every few minutes" competes with
# two thousand lines of other rules and loses somewhere around stage 3.
#
# A script call does not lose. The numbers live here rather than in prose for
# the same reason: a percentage nobody can invent is a percentage that cannot
# creep upward to sound better.
#
# The minutes in the table are ESTIMATES, and for a long time they were the
# only durations this pipeline had: nothing wrote down how long a stage really
# took, so "which stage is slow" was an opinion and these estimates could not
# be checked against anything. Every start/done/fail now also appends a row to
# {workspace}/.h2wp-timing.jsonl, and the plugin's hook appends one per script
# with the duration the host measured. `summary` reads them back.
set -uo pipefail

# stage | done% | minutes | label | what comes next
TABLE='
-3|1|0|prerequisites|prerender the app
-1|14|6|prerender the app|analyze and write the manifest
-1b|17|2|commerce specimen|analyze and write the manifest
0|21|4|analyze and write the manifest|optimise the images
0.5|26|4|optimise the images|give the images their loading attributes
0.6|28|2|give the images their loading attributes|HTML to Astro
1|36|6|HTML to Astro|gates A + A2
2|46|4|gates A + A2|capture the chrome per design group
2.5|51|3|capture the chrome per design group|move JS-held copy into the markup
2.6|56|3|move JS-held copy into the markup|name every form field
2.65|59|2|name every form field|record the repeating groups
2.7|62|2|record the repeating groups|the service builds the theme
3|71|3|the service builds the theme|the theme screenshot
3.5|73|1|the theme screenshot|gates B + C in a real WordPress
5|88|10|gates B + C in a real WordPress|your own page-by-page review
5.5|95|0|your own page-by-page review|the WooCommerce audit
5.6|97|2|the WooCommerce audit|package the deliverables
6|99|2|package the deliverables|report the gates
6.5|100|0|report the gates|clean up the workspace
7|100|1|clean up the workspace|—
'

TIMING_FILE='.h2wp-timing.jsonl'

row() { printf '%s\n' "$TABLE" | awk -F'|' -v s="$1" '$1==s {print; exit}'; }

remaining_minutes() { # everything still to come, by the table's own estimates
  printf '%s\n' "$TABLE" | awk -F'|' -v s="$1" '
    $1=="" {next}
    { if (seen) rest += $3; if ($1==s) seen=1 }
    END { print rest+0 }'
}

# Where the rows go: the workspace, named or stood in. Nothing else is guessed
# — "the newest directory under jobs/" files one site's minutes under another's
# name the first time two conversions share a machine. Under Claude Code the
# hook knows the workspace per session and writes these rows itself; this is
# for a host with no hooks, and for a call made with H2WP_WORKSPACE set.
timing_workspace() {
  if [ -n "${H2WP_WORKSPACE:-}" ] && [ -d "${H2WP_WORKSPACE}" ]; then
    printf '%s' "${H2WP_WORKSPACE%/}"; return
  fi
  case "$PWD/" in
    */html2wp/jobs/?*/*)
      local root="${PWD%%/html2wp/jobs/*}/html2wp/jobs/"
      local rest="${PWD#"$root"}"
      printf '%s%s' "$root" "${rest%%/*}"
      return
      ;;
  esac
  # A workspace somebody put outside the jobs/ tree is still a workspace: the
  # manifest is what makes a directory one.
  [ -f "$PWD/conversion-manifest.json" ] && printf '%s' "$PWD"
  return 0
}

# Best effort by construction: a progress line that failed because a log file
# could not be written would be this script breaking the run it reports on.
#
# The row is built by python, not by printf: `cut -c` counts BYTES on macOS,
# so a long note full of em-dashes was cut mid-character, the row was not
# valid UTF-8, and `summary` died reading it back. Sub-second `t` so the row
# orders correctly against the hook's rows for the same call.
record() { # <event> <stage> [note]
  local ws
  ws="$(timing_workspace)"
  [ -n "$ws" ] && [ -d "$ws" ] || return 0
  command -v python3 >/dev/null 2>&1 || return 0
  python3 - "$ws/$TIMING_FILE" "$1" "$2" "${3:-}" <<'PY' 2>/dev/null || true
import json, sys, time
path, event, stage, note = sys.argv[1:5]
note = "".join(ch for ch in note if ch >= " ")[:200]
with open(path, "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"t": round(time.time(), 3), "event": event, "stage": stage, "note": note},
                        ensure_ascii=False) + "\n")
PY
}

MODE="${1:-}"; STAGE="${2:-}"; NOTE="${3:-}"

if [ "$MODE" = "stages" ]; then
  printf '%s\n' "$TABLE" | awk -F'|' '$1!="" {printf "  %-5s %3s%%  %s\n", $1, $2, $4}'
  exit 0
fi

if [ "$MODE" = "summary" ]; then
  WS="${STAGE:-$(timing_workspace)}"
  [ -n "$WS" ] || { echo "usage: progress.sh summary <workspace>   (or run it from inside one)" >&2; exit 2; }
  [ -f "$WS/$TIMING_FILE" ] || { echo "progress.sh: nothing timed yet — no $TIMING_FILE in $WS" >&2; exit 1; }
  command -v python3 >/dev/null 2>&1 || { echo "progress.sh: summary needs python3" >&2; exit 2; }
  python3 - "$WS/$TIMING_FILE" <<'PY'
import json, sys
from collections import OrderedDict

rows = []
# errors="replace": a row cut mid-character by an older writer must cost that
# row, not the whole report.
for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    try:
        r = json.loads(line)
    except ValueError:
        continue
    if isinstance(r, dict) and isinstance(r.get("t"), (int, float)):
        rows.append(r)
rows.sort(key=lambda r: r["t"])
if not rows:
    sys.exit("progress.sh: no readable rows")

def clock(ms):
    s = int(round(ms / 1000.0))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"

def order(stage):
    # "-1b" sorts after "-1" and before "0"; an unknown stage goes last.
    try:
        return (0, float(stage.rstrip("b")), stage.endswith("b"))
    except (ValueError, AttributeError):
        return (1, 0.0, False)

# The hook and this script can both record the same start/done/fail, and they
# are NOT close in time: this script writes when it runs, the hook when the
# whole Bash call ends — minutes later when the progress call shares a command
# with a gate. A time window counted those twice (measured on a real run:
# every stage "done" twice). One progress call yields at most one row from
# each writer, and the script's row is always written first, so each hook mark
# pairs with the EARLIEST unpaired script mark of the same kind and stage that
# precedes it — and a hook mark with no such partner is the only record of a
# call made where this script could not find the workspace.
marks, unpaired = [], []
for r in rows:
    if r.get("event") not in ("start", "done", "fail"):
        continue
    key = (r["event"], str(r.get("stage")))
    if r.get("src") == "hook":
        twin = next((o for o in unpaired if o["key"] == key and o["t"] <= r["t"] + 1), None)
        if twin:
            unpaired.remove(twin)
        else:
            marks.append(r)
    else:
        unpaired.append({"key": key, "t": r["t"]})
        marks.append(r)

scripts = [r for r in rows if r.get("event") == "script"]
timed = [r for r in scripts if isinstance(r.get("ms"), (int, float)) and not isinstance(r.get("ms"), bool)]
# Rows with no duration: launched in the background, or written by a host too
# old to report `duration_ms`. Counted aloud, or "in scripts" reads as zero
# and every minute looks like thinking time.
untimed = [r for r in scripts if r not in timed and not r.get("bg")]
wall = (rows[-1]["t"] - rows[0]["t"]) * 1000
in_scripts = sum(r["ms"] for r in timed)

print()
print(f"  wall clock       {clock(wall):>8}   first row to last")
if wall > 0:
    # Scripts that overlapped each other can add up to more than the wall
    # clock; say so rather than print a negative remainder.
    rest = wall - in_scripts
    print(f"  in scripts       {clock(in_scripts):>8}   {round(100 * in_scripts / wall)}%")
    if rest >= 0:
        print(f"  everything else  {clock(rest):>8}   {round(100 * rest / wall)}%   reading, deciding, fixing, waiting")
    else:
        print("  everything else         —   scripts overlapped, so their sum exceeds the wall clock")
    if untimed:
        print(f"  not timed        {len(untimed):>8}   script run(s) the host reported no duration for — not in either figure")

# Every stage that left a trace: a stage closed with done/fail but with no
# script of its own (the manifest at stage 0 is written by hand) still ran.
stages = OrderedDict()
for key in sorted({str(r.get("stage") or "?") for r in scripts} | {str(m.get("stage")) for m in marks}, key=order):
    stages[key] = [r for r in scripts if str(r.get("stage") or "?") == key]

print()
print("  stage   done  failed   in scripts   what ran")
for stage, items in stages.items():
    per = OrderedDict()
    for r in items:
        e = per.setdefault(r.get("script", "?"), {"n": 0, "ms": 0, "red": 0})
        e["n"] += 1
        e["ms"] += r["ms"] if isinstance(r.get("ms"), (int, float)) and not isinstance(r.get("ms"), bool) else 0
        e["red"] += 0 if r.get("ok", True) else 1
    ran = ", ".join(
        f"{name} ×{e['n']}" + (f" ({e['red']} red)" if e["red"] else "")
        for name, e in sorted(per.items(), key=lambda kv: -kv[1]["ms"])
    )
    # One Bash call that ran several scripts is ONE duration, filed under the
    # first. The others ran too, and inside those minutes; leaving them out
    # made a stage that ran gate A four times read as twice.
    rode = OrderedDict()
    for r in items:
        for name in (r.get("also") if isinstance(r.get("also"), list) else []):
            if not isinstance(name, str):
                continue
            rode[name] = rode.get(name, 0) + 1
    if rode:
        ran += "  + inside those: " + ", ".join(f"{n} ×{c}" for n, c in rode.items())
    done = sum(1 for m in marks if m["event"] == "done" and str(m.get("stage")) == stage)
    fail = sum(1 for m in marks if m["event"] == "fail" and str(m.get("stage")) == stage)
    total = sum(e["ms"] for e in per.values())
    print(f"  {stage:<6}{done:>6}{fail:>8}   {clock(total):>10}   {ran}")

# Stage 3 is one script and at least four different waits: this machine packing,
# the wire, the service, the wire again. convert-remote.sh says which, and the
# service says where ITS share went. Not added to "in scripts" above — the
# row for convert-remote.sh already holds the same minutes.
def inside(event, key):
    total = OrderedDict()
    for r in rows:
        if r.get("event") == event and isinstance(r.get("ms"), (int, float)):
            total[r.get(key, "?")] = total.get(r.get(key, "?"), 0) + r["ms"]
    return total

phases, server = inside("phase", "phase"), inside("server", "script")
if phases:
    print("\n  inside stage 3    " + " · ".join(f"{n} {clock(ms)}" for n, ms in phases.items()))
if server:
    ranked = sorted(server.items(), key=lambda kv: -kv[1])
    print("  the service's share  " + " · ".join(f"{n} {clock(ms)}" for n, ms in ranked))

background = [r for r in scripts if r.get("bg")]
if background:
    names = ", ".join(sorted({r.get("script", "?") for r in background}))
    print(f"\n  launched in the background, so not timed here: {names}")
print()
PY
  exit $?
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
    if [ "$NEXT" = "—" ]; then
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

record "$MODE" "$STAGE" "$NOTE"
