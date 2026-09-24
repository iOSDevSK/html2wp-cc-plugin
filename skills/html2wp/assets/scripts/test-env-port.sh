#!/usr/bin/env bash
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
#
# test-env.sh reads the wp service's published port until Docker has really
# published one: right after a container (re)start `docker compose port` can
# print nothing or `0.0.0.0:0`, and `up` once took that 0 as the port and
# wrote http://localhost:0 into the state file and WordPress's siteurl.
# Runs read_port itself against a stand-in `compose`; no Docker needed.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fail=0; check() { if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1: got '$2', want '$3'"; fail=1; fi; }

# test-env.sh's own read_port, not a copy.
eval "$(sed -n '/^read_port() {/,/^}/p' "$HERE/test-env.sh")"
declare -F read_port >/dev/null || { echo "FAIL: read_port not found in test-env.sh"; exit 1; }
PROJECT="h2wp-port-test"
sleep() { :; }
# The stand-in prints the next of READS (one line each, `-` = nothing), then
# keeps repeating the last. read_port calls it in a subshell: the count of
# calls so far lives in a file.
READS=()
COUNT="$(mktemp)"; trap 'rm -f "$COUNT" "$COUNT.err"' EXIT
compose() {
  local calls i; calls="$(cat "$COUNT")"; i=$calls
  (( i >= ${#READS[@]} )) && i=$(( ${#READS[@]} - 1 ))
  printf '%s\n' "$((calls + 1))" > "$COUNT"
  [ "${READS[$i]}" = "-" ] || printf '%s\n' "${READS[$i]}"
}
run() { READS=("$@"); echo 0 > "$COUNT"; OUT="$(read_port 2>"$COUNT.err")"; STATUS=$?; ERR="$(cat "$COUNT.err")"; }

run "0.0.0.0:61447"
check "a published port is read at once" "$OUT/$STATUS" "61447/0"
run "0.0.0.0:0" "0.0.0.0:0" "0.0.0.0:61447"
check "0.0.0.0:0 (not published yet) is waited out" "$OUT/$STATUS" "61447/0"
check "...reading again until the port is real" "$(cat "$COUNT")" "3"
run "-" "-" "[::]:52001"
check "no output yet is waited out; an IPv6 address gives its port" "$OUT/$STATUS" "52001/0"
H2WP_PORT_WAIT=3 run "0.0.0.0:0"
check "a port that is never published fails" "$STATUS" "1"
check "...with nothing on stdout" "$OUT" ""
case "$ERR" in *"published no port for wp:80 within 3s (last read: '0.0.0.0:0')"*) check "...naming what it read" ok ok ;; *) check "...naming what it read" "$ERR" "published no port … (last read: '0.0.0.0:0')" ;; esac
H2WP_PORT_WAIT=2 run "garbage"
check "text that is no port fails too" "$STATUS" "1"

[ "$fail" = 0 ] && echo "ALL OK" || { echo "FAILED"; exit 1; }
