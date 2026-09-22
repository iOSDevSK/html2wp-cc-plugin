#!/usr/bin/env bash
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
#
# Fires after every Bash call — the ones that succeed (PostToolUse) and the
# ones that exit non-zero (PostToolUseFailure) — and stays silent for all but
# the few that end a stage.
#
# progress.sh made the NUMBERS reliable. It did not make the CALLING reliable:
# something still has to remember, for an hour, at every boundary. This is the
# part that does not forget — the hook runs whether or not anything remembered
# it, and reminds the model that a boundary was just crossed. On the same pass
# it writes down how long the script took, which nothing did before.
#
# It deliberately does NOT report the stage as passed. It cannot see whether
# the gate went green, and a hook that cheerfully prints "46%" after a failed
# gate would be worse than no hook. It says which boundary was reached and
# leaves the verdict to whoever watched the output.
#
# The work is in stage-progress.py. This wrapper stays because hooks.json in
# every installed copy names it, and because one rule outlives any rewrite:
# a host reading stdout as JSON must GET JSON, including when there is nothing
# to say — which is almost every call. Printing nothing was read as malformed
# output rather than as silence.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

command -v python3 >/dev/null 2>&1 || { printf '{}\n'; exit 0; }

OUT="$(python3 "$HERE/stage-progress.py" 2>/dev/null)" || OUT=''
case "$OUT" in
  '{'*) printf '%s\n' "$OUT" ;;
  *)    printf '{}\n' ;;
esac
exit 0
