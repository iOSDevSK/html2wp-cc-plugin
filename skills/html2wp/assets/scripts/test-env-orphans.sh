#!/usr/bin/env bash
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
#
# `test-env.sh up` from a workspace whose state file is gone (deleted by a
# script clearing the workspace, or the workspace copied in afresh) must not
# leave the earlier run of that slug running beside the new one:
#   - the earlier run FROM THIS WORKSPACE is removed before the new one starts;
#   - a run of the same slug from ANOTHER workspace is never touched.
# Needs Docker. Uses throwaway envs and removes them.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v docker >/dev/null || { echo "SKIP: docker not available"; exit 0; }
A="$(mktemp -d)"; B="$(mktemp -d)"; SLUG="p-orphan-$$"
cleanup() { for d in "$A" "$B"; do (cd "$d" && bash "$HERE/test-env.sh" down "$SLUG" >/dev/null 2>&1); done; rm -rf "$A" "$B"; }
trap cleanup EXIT
fail=0; check() { if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1: got '$2', want '$3'"; fail=1; fi; }
projects() { docker ps -a --filter "label=com.docker.compose.project" --format '{{.Label "com.docker.compose.project"}}' | sort -u | grep -E "^h2wp-${SLUG}-[0-9a-f]{6}$" || true; }
project_of() { python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['project'])" "$1/.test-env-$SLUG.json"; }

(cd "$A" && bash "$HERE/test-env.sh" up "$SLUG" > up1.log 2>&1) || { echo "up in A failed"; tail -20 "$A/up1.log"; exit 1; }
(cd "$B" && bash "$HERE/test-env.sh" up "$SLUG" > up1.log 2>&1) || { echo "up in B failed"; tail -20 "$B/up1.log"; exit 1; }
first_a="$(project_of "$A")"; b="$(project_of "$B")"
check "two workspaces, one slug: two runs" "$(projects | wc -l | tr -d ' ')" "2"

rm -f "$A/.test-env-$SLUG.json"     # what a workspace-clearing script does
(cd "$A" && bash "$HERE/test-env.sh" up "$SLUG" > up2.log 2>&1) || { echo "second up in A failed"; tail -20 "$A/up2.log"; exit 1; }
second_a="$(project_of "$A")"
check "A's earlier run was removed, not left running" "$(projects | grep -c "^$first_a$")" "0"
check "A has exactly its new run" "$(projects | grep -c "^$second_a$")" "1"
check "B's run of the same slug is untouched" "$(projects | grep -c "^$b$")" "1"
check "still two runs in all" "$(projects | wc -l | tr -d ' ')" "2"
check "the up said what it removed" "$(grep -c "lost its state file — removing $first_a" "$A/up2.log")" "1"

[ "$fail" = 0 ] && echo "ALL OK" || { echo "FAILED"; exit 1; }
