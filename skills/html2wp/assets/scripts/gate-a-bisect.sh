#!/usr/bin/env bash
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
# Gate A went red: WHICH step moved the pixels?
#
#   assets/scripts/gate-a-bisect.sh <workspace> [--pages a.html,b.html] [--original-remote <report>]
#                                   [--original <U>] [--working <W>]
#
# Gate A runs once, after the last rebuild, against the untouched original U
# ({ws}/input-untouched). A red verdict there covers four steps at once — 0.5/0.6
# on the working copy W, the stage-1 build, 2.6 and 2.65 — so this re-runs the
# gate on ONLY the failed pages against each snapshot, in pipeline order, and
# stops at the first that is red:
#
#   1  U ↔ W                   red → 0.5/0.6 (separate 0.5 by re-running it on a fresh copy of U)
#   2  U ↔ bisect/dist-s1      red → stage 1, the build
#   3  U ↔ bisect/dist-s26     red → 2.6 (materialize-js-text --apply)
#   4  U ↔ astro-project/dist  red → 2.65 (normalize-form-fields --apply)
#                              green → the red did not reproduce on these pages
#
# Step 4 runs only when 1-3 are green: it is what tells "2.65 did it" apart
# from "the full run was red and this one is not".
#
# This is a DIAGNOSIS, never a verdict. Every run writes under
# {ws}/diag/gate-a-bisect/<step>/ — never {ws}/verify-static, which is the
# path the verdict is read from (send-verdicts.sh). A partial page list must
# not be mistaken for the gate: `--pages` switches off the inventory check.
# After the fix, the whole chain runs again and gate A runs once, in full.
#
# Every step is measured against U, so drift ACCUMULATES along the chain and
# shares one 0.6% budget. "Each step alone would be fine" is not a defence: if
# step 4 is red, the delivered build is red.
#
# Pages default to the ones that failed in {ws}/verify-static/report.json.
# A missing snapshot is reported as skipped, and the verdict then names the
# range it could not split (e.g. "stage 1 or 2.6").
#
# Exit status: 0 = a step went red (named on the last line), 1 = nothing
# reproduced, 64 = the call itself was wrong.
set -uo pipefail

S="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
  echo "usage: gate-a-bisect.sh <workspace> [--pages a.html,b.html] [--original-remote <report>] [--original <U>] [--working <W>]" >&2
  exit 64
}

WS="" PAGES="" REMOTE="" U="" W=""
while [ $# -gt 0 ]; do
  case "$1" in
    --pages=*) PAGES="${1#--pages=}" ;;
    --pages) [ $# -ge 2 ] || usage; PAGES="$2"; shift ;;
    --original-remote=*) REMOTE="${1#--original-remote=}" ;;
    --original-remote) [ $# -ge 2 ] || usage; REMOTE="$2"; shift ;;
    --original=*) U="${1#--original=}" ;;
    --original) [ $# -ge 2 ] || usage; U="$2"; shift ;;
    --working=*) W="${1#--working=}" ;;
    --working) [ $# -ge 2 ] || usage; W="$2"; shift ;;
    -h|--help) usage ;;
    -*) echo "unknown flag: $1" >&2; usage ;;
    *) [ -z "$WS" ] || usage; WS="$1" ;;
  esac
  shift
done
[ -n "$WS" ] && [ -d "$WS" ] || usage
WS="$(cd "$WS" && pwd)"
MF="$WS/conversion-manifest.json"
[ -f "$MF" ] || { echo "no conversion-manifest.json in $WS" >&2; exit 64; }

U="${U:-$WS/input-untouched}"
[ -d "$U" ] || { echo "no untouched original at $U" >&2; exit 64; }
if [ -z "$W" ]; then
  W="$(python3 -c 'import json,sys; sys.path.insert(0, sys.argv[2]); from manifest_paths import input_dir_of; print(input_dir_of(json.load(open(sys.argv[1])), sys.argv[1]))' "$MF" "$(dirname "$0")/lib")" \
    || { echo "cannot read input.dir from $MF — pass --working" >&2; exit 64; }
fi
[ -d "$W" ] || { echo "no working copy at $W" >&2; exit 64; }
[ -z "$REMOTE" ] || [ -f "$REMOTE" ] || { echo "no --original-remote report at $REMOTE" >&2; exit 64; }

if [ -z "$PAGES" ]; then
  REPORT="$WS/verify-static/report.json"
  [ -f "$REPORT" ] || { echo "no $REPORT — pass --pages" >&2; exit 64; }
  PAGES="$(python3 - "$REPORT" <<'PY'
import json, sys
r = json.load(open(sys.argv[1]))
bad = [p for p, v in r.get("pages", {}).items()
       if isinstance(v, dict) and any(isinstance(w, dict) and w.get("ok") is False for w in v.values())]
print(",".join(sorted(bad)))
for k in ("missingFromDist", "notInOriginal"):
    if r.get(k):
        print(f"note: the report also has {k} — an inventory failure, which no pixel bisection explains",
              file=sys.stderr)
PY
)"
  [ -n "$PAGES" ] || { echo "no page failed on pixels in $REPORT — nothing to bisect" >&2; exit 64; }
fi

DIAG="$WS/diag/gate-a-bisect"
# The verdict path is read by send-verdicts.sh; a diagnosis must never land there.
case "$DIAG/" in "$WS/verify-static/"*) echo "refusing: $DIAG is inside the verdict path" >&2; exit 64 ;; esac
# Last bisection's step reports go: a step this run does not reach must not
# leave an older answer lying beside the new one.
rm -rf "$DIAG"
mkdir -p "$DIAG"

echo "==> gate A bisection on: $PAGES"
echo "    U = $U"
FIRST="" SKIPPED=""
# step <dir-name> <dist> <culprit if red>
step() {
  local name="$1" dist="$2" culprit="$3" out="$DIAG/$1" rc
  if [ ! -d "$dist" ]; then
    echo "--- $name: skipped (no snapshot at $dist)"
    SKIPPED="$SKIPPED $culprit;"
    return 1
  fi
  rm -rf "$out"
  local cmd=(python3 "$S/verify-static.py" --original "$U" --dist "$dist" --pages "$PAGES" --out "$out")
  [ -n "$REMOTE" ] && cmd+=("--original-remote=$REMOTE")
  echo "--- $name: U ↔ $dist"
  "${cmd[@]}" >"$out.log" 2>&1; rc=$?
  tail -1 "$out.log" | sed 's/^/    /'
  if [ "$rc" = 0 ]; then
    SKIPPED=""  # green here: everything before it is cleared, the range restarts
    return 1
  fi
  if [ "$rc" != 1 ]; then
    echo "    the gate itself failed to run (exit $rc) — see $out.log" >&2
    exit 64
  fi
  FIRST="$name"
  # A skipped snapshot before this one means this red covers that step too.
  VERDICT="${SKIPPED:+$(echo "$SKIPPED" | sed 's/;$//; s/^ //; s/; / or /g') or }$culprit"
  return 0
}

step 1-u-vs-working "$W" "0.5/0.6 on the working copy" \
  || step 2-u-vs-dist-s1 "$WS/bisect/dist-s1" "stage 1 (the build)" \
  || step 3-u-vs-dist-s26 "$WS/bisect/dist-s26" "2.6 (materialize-js-text --apply)" \
  || step 4-u-vs-dist "$WS/astro-project/dist" "2.65 (normalize-form-fields --apply)"

python3 - "$DIAG/summary.json" "$PAGES" "$FIRST" "${VERDICT:-}" <<'PY'
import json, sys
path, pages, first, verdict = sys.argv[1:5]
json.dump({"pages": pages.split(","), "firstRed": first or None, "culprit": verdict or None},
          open(path, "w"), indent=2)
PY

echo
if [ -n "$FIRST" ]; then
  echo "first red: $FIRST → $VERDICT"
  echo "(diagnosis only — reports under $DIAG; after the fix re-run the chain and gate A in full)"
  exit 0
fi
echo "not reproduced: every step is green on these pages. Re-run gate A in full; a red that comes and goes is itself the finding."
exit 1
