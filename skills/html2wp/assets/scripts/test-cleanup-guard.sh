#!/usr/bin/env bash
# cleanup.sh deletes directory contents. The only thing it used to check was
# that its argument was a directory that EXISTS — so `cleanup.sh ~`, a wrong
# path from an agent, or a typo that happened to resolve, all wiped whatever
# was not on the keep list.
#
# These are the refusals that make that unreachable, and the working case that
# must keep working alongside them.
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLEANUP="$HERE/cleanup.sh"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

FAILED=0
check() { # <label> <condition-exit-code>
  if [ "$2" = "0" ]; then echo "ok   — $1"; else echo "FAIL — $1"; FAILED=1; fi
}

echo "== refusals that no flag gets past =="

# A home directory, even with --force. There is no argument for this.
mkdir -p "$TMP/fakehome"
: > "$TMP/fakehome/precious.txt"
HOME="$TMP/fakehome" bash "$CLEANUP" "$TMP/fakehome" --force >/dev/null 2>&1
[ -f "$TMP/fakehome/precious.txt" ]
check "a home directory is refused even with --force" $?

# A git checkout root: wiping a repo's untracked files is not a cleanup.
mkdir -p "$TMP/repo/.git"
: > "$TMP/repo/untracked.txt"
bash "$CLEANUP" "$TMP/repo" --force >/dev/null 2>&1
[ -f "$TMP/repo/untracked.txt" ]
check "a git repository root is refused even with --force" $?

# The filesystem root, without going anywhere near it.
out="$(bash "$CLEANUP" / --force 2>&1)"
printf '%s' "$out" | grep -q "filesystem root"
check "the filesystem root is refused" $?

echo
echo "== a directory that is not a conversion =="

mkdir -p "$TMP/random"
: > "$TMP/random/somebodys-work.txt"
bash "$CLEANUP" "$TMP/random" >/dev/null 2>&1
[ -f "$TMP/random/somebodys-work.txt" ]
check "an unrecognised directory is refused, and nothing is deleted" $?

echo
echo "== a real workspace still cleans =="

ws="$TMP/ws"
mkdir -p "$ws/astro-project/dist" "$ws/node_modules"
echo '{}' > "$ws/conversion-manifest.json"
echo '{}' > "$ws/astro-report.json"
: > "$ws/site.zip"
: > "$ws/scaffolding.log"
: > "$ws/node_modules/big.bin"
: > "$ws/.h2wp-verdicts-sent"
echo '{}' > "$ws/.h2wp-job.json"

bash "$CLEANUP" "$ws" --dry-run >/dev/null 2>&1
[ -f "$ws/scaffolding.log" ]
check "--dry-run deletes nothing" $?

bash "$CLEANUP" "$ws" >/dev/null 2>&1
[ ! -f "$ws/scaffolding.log" ] && [ ! -d "$ws/node_modules" ]
check "scaffolding and node_modules are removed" $?

[ -f "$ws/conversion-manifest.json" ] && [ -f "$ws/site.zip" ] \
  && [ -d "$ws/astro-project/dist" ]
check "the deliverable and the re-run kit survive" $?

# Running it a second time must not abort — an already-clean workspace still
# carries .h2wp-* and the manifest, so the marker check still passes.
bash "$CLEANUP" "$ws" >/dev/null 2>&1
check "a second run on an already-clean workspace still succeeds" $?

echo
if [ "$FAILED" = "0" ]; then echo "ALL OK"; else echo "FAILED"; exit 1; fi
