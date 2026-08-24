#!/usr/bin/env bash
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
#
# Throw away the scaffolding, keep what was delivered.
#
#   cleanup.sh <workspace> [--minimal] [--force] [--dry-run]
#
# A finished conversion leaves ~225 MB of workspace behind, of which the three
# things anyone wanted are about 3 MB: the theme ZIP, the conversion report,
# and the editor. The rest is scaffolding that was worth keeping while the
# pipeline ran and is worth nothing now.
#
# What it keeps by default, and why each one:
#
#   <slug>-<version>.zip      the deliverable
#   CONVERSION-REPORT.md      what was done, what was found, what is left
#   visual-edit.zip           the editor, on a licensed conversion
#   FINDINGS.md               the page-by-page review — the part a person wrote
#   visual-review/            side-by-side images; the evidence behind FINDINGS
#   conversion-manifest.json  \
#   astro-report.json          > THE RE-RUN KIT — see below
#   astro-project/dist/       /
#   .h2wp-job.json            the job a re-run reuses
#
# THE RE-RUN KIT IS THE POINT. A re-run is the same built dist uploaded again,
# matched by digest, and it does NOT spend a conversion — so fixing how a page
# was described and going again is free. Delete `astro-project/dist` and that
# stops being true: the next attempt is a new conversion out of the allowance.
# It costs 1.1 MB to keep. Nearly all the weight is `node_modules`, which is
# 150 MB and reinstalls itself with `npm install`.
#
# --minimal drops the re-run kit and the review too, leaving only the three
# deliverables. It also ends the ability to REPAIR this site: a repair is an
# edit to the manifest followed by rebuild-theme.sh, which needs exactly
# conversion-manifest.json, astro-report.json and astro-project/dist. Use it
# only when the site is signed off and will not be touched.
set -uo pipefail

WS=""; MINIMAL=0; DRY=0; FORCE=0
for a in "$@"; do
  case "$a" in
    --minimal) MINIMAL=1 ;;
    --dry-run) DRY=1 ;;
    --force)   FORCE=1 ;;
    --*) echo "unknown flag: $a" >&2; exit 2 ;;
    *) WS="$a" ;;
  esac
done
[ -n "$WS" ] || { echo "usage: cleanup.sh <workspace> [--minimal] [--force] [--dry-run]" >&2; exit 2; }
[ -d "$WS" ] || { echo "cleanup.sh: no such workspace: $WS" >&2; exit 2; }
WS="$(cd "$WS" && pwd)"

# Everything below this line exists because the only thing this script checked
# was that its argument was a directory that EXISTS — and then it deleted
# every entry in it that was not on the keep list. `cleanup.sh ~` was a
# working command. So was a manifest-supplied path, or an agent that passed
# the project root instead of the workspace, or a typo that resolved.
#
# Two gates, deliberately different in strength:
#
#   A path that must never be cleaned is refused OUTRIGHT — no flag gets past
#   it, because there is no argument for pointing this at a home directory.
#
#   A directory that simply does not look like a conversion is refused with a
#   way out (--force), because an unusual-but-real workspace should not be
#   unrecoverable.

# Somewhere whose contents are not this script's to delete, whatever else is
# true about it.
refuse_path() {
  local dir="$1" reason="$2"
  echo "cleanup.sh: refusing to clean $dir" >&2
  echo "  $reason" >&2
  echo "  This deletes directory contents; point it at a conversion workspace." >&2
  exit 2
}

case "$WS" in
  /) refuse_path "$WS" "that is the filesystem root." ;;
esac
[ "$WS" != "${HOME:-/nonexistent}" ] || refuse_path "$WS" "that is your home directory."
# One level under / (/Users, /home, /opt) is somebody's parent directory, never
# a workspace.
[ "$(echo "${WS#/}" | tr -cd '/' | wc -c)" -ge 1 ] 2>/dev/null \
  || refuse_path "$WS" "that is a top-level directory."
# A checkout root. A workspace lives inside a project or beside it; it is not
# the project, and wiping a repo's untracked files is not a cleanup.
[ ! -e "$WS/.git" ] || refuse_path "$WS" "that is a git repository root (.git is there)."

# What only a conversion leaves behind. `.h2wp-*` survives --minimal, so a
# second run of this script on an already-cleaned workspace still passes.
has_marker=0
for marker in conversion-manifest.json astro-report.json astro-project .h2wp-job.json; do
  [ -e "$WS/$marker" ] && { has_marker=1; break; }
done
if [ "$has_marker" = "0" ]; then
  for stamp in "$WS"/.h2wp-*; do
    [ -e "$stamp" ] && { has_marker=1; break; }
  done
fi
if [ "$has_marker" = "0" ] && [ "$FORCE" != "1" ]; then
  echo "cleanup.sh: $WS does not look like a conversion workspace." >&2
  echo "  Nothing here is named conversion-manifest.json, astro-report.json," >&2
  echo "  astro-project/ or .h2wp-*, so this is probably not the directory you" >&2
  echo "  meant — and this script deletes what it does not recognise." >&2
  echo "  If it really is one, re-run with --force." >&2
  exit 2
fi

# Refuse to delete the evidence before the service has been told what the
# gates said. Stage 6.5 needs the reports that are about to go, and a caller
# who owes verdicts is refused their NEXT conversion — so cleaning up first
# turns a finished job into a blocked account.
if [ -f "$WS/.h2wp-job.json" ] && [ ! -f "$WS/.h2wp-verdicts-sent" ] && [ "$FORCE" != "1" ]; then
  echo "refusing: the gates have not been reported yet." >&2
  echo "  run:  assets/scripts/send-verdicts.sh $WS --outcome=delivered" >&2
  echo "  then: assets/scripts/cleanup.sh $WS" >&2
  echo "(--force skips this, and the next conversion will be refused until the" >&2
  echo " verdicts arrive some other way.)" >&2
  exit 1
fi

keep() { # a name is kept if it matches anything in the keep list
  case "$1" in
    *.zip|CONVERSION-REPORT.md|.h2wp-*) return 0 ;;
  esac
  [ "$MINIMAL" = "1" ] && return 1
  case "$1" in
    conversion-manifest.json|astro-report.json|FINDINGS.md|visual-review) return 0 ;;
    astro-project) return 0 ;;   # pruned rather than kept whole — see below
  esac
  return 1
}

BEFORE="$(du -sk "$WS" 2>/dev/null | cut -f1)"
GOING=(); STAYING=(); PRUNE=()

for path in "$WS"/* "$WS"/.[!.]*; do
  [ -e "$path" ] || continue
  n="$(basename "$path")"
  if keep "$n"; then STAYING+=("$n"); else GOING+=("$n"); fi
done

# astro-project is kept only for dist/ — node_modules is 98% of the weight and
# `npm install` puts it back.
if [ "$MINIMAL" != "1" ] && [ -d "$WS/astro-project" ]; then
  for sub in "$WS/astro-project"/*; do
    [ -e "$sub" ] || continue
    case "$(basename "$sub")" in dist) ;; *) PRUNE+=("astro-project/$(basename "$sub")") ;; esac
  done
fi

# bash 3.2 — which is what macOS ships — treats an empty array as unset under
# `set -u`, so every one of these needs its length checked first. Running this
# twice used to abort on the second pass, which is exactly when it must not.
list() { # <heading> <name...>
  local heading="$1"; shift
  [ "$#" -gt 0 ] || return 0
  echo "$heading"
  for n in "$@"; do echo "    $n"; done
}

echo
list "keeping:" ${STAYING[@]+"${STAYING[@]}"}
[ "${#PRUNE[@]}" -eq 0 ] 2>/dev/null || \
  list "  pruning inside astro-project (keeping dist/ for free re-runs):" ${PRUNE[@]+"${PRUNE[@]}"}
if [ "${#GOING[@]}" -eq 0 ] 2>/dev/null; then
  echo "removing: nothing — this workspace is already clean."
else
  list "removing:" ${GOING[@]+"${GOING[@]}"}
fi

if [ "$DRY" = "1" ]; then
  echo; echo "--dry-run: nothing was deleted."
  exit 0
fi

for n in ${GOING[@]+"${GOING[@]}"}; do rm -rf -- "$WS/${n:?}"; done
for n in ${PRUNE[@]+"${PRUNE[@]}"}; do rm -rf -- "$WS/${n:?}"; done

AFTER="$(du -sk "$WS" 2>/dev/null | cut -f1)"
echo
printf 'was %s MB, now %s MB.\n' "$((BEFORE / 1024))" "$((AFTER / 1024))"
if [ "$MINIMAL" = "1" ]; then
  echo "The re-run kit is gone. Two consequences, not one:"
  echo "  - converting this site again spends a conversion;"
  echo "  - manifest repairs are no longer possible from here. rebuild-theme.sh"
  echo "    needs conversion-manifest.json, astro-report.json and"
  echo "    astro-project/dist, and none of them survived."
  echo "Only right for a site that is signed off and finished."
else
  echo "Re-runs and manifest repairs both still work: rebuild-theme.sh has the"
  echo "three files it needs, and a re-run of the same dist costs no conversion."
fi
