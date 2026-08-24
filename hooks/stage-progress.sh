#!/usr/bin/env bash
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
#
# Fires after every Bash call and stays silent for all but sixteen of them.
#
# progress.sh made the NUMBERS reliable. It did not make the CALLING reliable:
# something still has to remember, for an hour, at every boundary. This is the
# part that does not forget — the hook runs whether or not anything remembered
# it, and reminds the model that a boundary was just crossed.
#
# It deliberately does NOT report the stage as passed. It cannot see whether
# the gate went green, and a hook that cheerfully prints "46%" after a failed
# gate would be worse than no hook. It says which boundary was reached and
# leaves the verdict to whoever watched the output.
set -uo pipefail

IN="$(cat)"

# Any host reading stdout as JSON must get JSON, including when this hook
# has nothing to say — which is almost every call. Printing nothing was read
# as malformed output rather than as silence.
quiet() { printf '{}\n'; exit 0; }

CMD="$(printf '%s' "$IN" | python3 -c '
import json,sys
try: print(json.load(sys.stdin).get("tool_input",{}).get("command",""))
except Exception: print("")
' 2>/dev/null)"
[ -n "$CMD" ] || quiet

# script fragment -> the stage its completion ends
stage_for() {
  case "$1" in
    *prerender-spa.py*)             echo "-1"  ;;
    *capture-commerce-specimen.py*) echo "-1b" ;;
    *analyze-input.mjs*)            echo "0"   ;;
    *optimize-images.py*)           echo "0.5" ;;
    *verify-static.py*|*verify-parity.mjs*) echo "2" ;;
    *chrome-groups.mjs*|*capture-chrome.py*) echo "2.5" ;;
    *convert-remote.sh*)            echo "3"   ;;
    *make-screenshot.py*)           echo "3.5" ;;
    *verify-wp.py*|*smoke-editor.py*) echo "5" ;;
    *audit-woo-coverage.py*)        echo "5.6" ;;
    *make-zip.sh*)                  echo "6"   ;;
    *send-verdicts.sh*)             echo "6.5" ;;
    *) echo "" ;;
  esac
}

STAGE="$(stage_for "$CMD")"
[ -n "$STAGE" ] || quiet

# Do not fire on the progress call itself, or it reminds you to do what you
# just did.
case "$CMD" in *progress.sh*) quiet ;; esac

python3 - "$STAGE" <<'PY'
import json, sys
stage = sys.argv[1]
print(json.dumps({"hookSpecificOutput": {
    "hookEventName": "PostToolUse",
    "systemMessage": (
        f"html2wp: stage {stage} boundary reached. Report it — "
        f"`assets/scripts/progress.sh done {stage}` if it passed, "
        f"or `assets/scripts/progress.sh fail {stage} \"<why>\"` if it did not. "
        "Do not compose the line yourself."
    ),
}}))
PY
exit 0
