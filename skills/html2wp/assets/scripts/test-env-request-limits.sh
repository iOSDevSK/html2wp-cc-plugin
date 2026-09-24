#!/usr/bin/env bash
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
#
# A preview's wp-admin takes the cookies of every preview on localhost.
#
# All previews live on localhost, whatever their port, and a browser sends
# localhost's cookies to each of them. After a few, the Cookie header passes
# Apache's 8190-byte default and wp-admin answers 400 "Size of a request header
# field exceeds server limit" — the owner's first run in the app hit it.
# test-env-compose.yml raises the limits before Apache starts. This runs the
# pinned WordPress image with the compose file's own command and sends
# /wp-admin/ a 20 KB and a ~200 KB Cookie header: not 400 (the limits are 256
# KB). The stock command is run too, to show the 20 KB request really is one
# the default refuses.
#
# Needs Docker; skipped without it (a failure under GitHub Actions).
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE="$HERE/test-env-compose.yml"

if ! docker info >/dev/null 2>&1; then
  [ "${GITHUB_ACTIONS:-}" = "true" ] && { echo "FAIL: Docker is not available in CI"; exit 1; }
  echo "SKIP: Docker is not running"; exit 0
fi

# The compose file as Compose itself reads it (no YAML parser needed).
CONFIG="$(docker compose -p h2wp-limits-probe -f "$COMPOSE" config --format json)" || { echo "FAIL: docker compose cannot read $COMPOSE"; exit 1; }
IMAGE="$(printf '%s' "$CONFIG" | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["wp"]["image"])')"
SCRIPT="$(printf '%s' "$CONFIG" | python3 -c 'import json,sys; c=json.load(sys.stdin)["services"]["wp"]["command"]; assert c[:2]==["bash","-c"], c; print(c[2])')" \
  || { echo "FAIL: the wp service has no bash -c command"; exit 1; }
cookie_of() { python3 -c 'import sys; print("; ".join(f"wp-preview-{i}=" + "x" * 190 for i in range(int(sys.argv[1]))))' "$1"; }
COOKIE="$(cookie_of 100)"      # ~20 KB
BIG_COOKIE="$(cookie_of 1000)" # ~200 KB
fail=0
cleanup() { docker rm -f h2wp-limits-probe h2wp-limits-stock >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup

status_of() { # <container> [cookie] -> the HTTP status of /wp-admin/ with that cookie, once Apache answers
  local port code i cookie="${2:-$COOKIE}"
  port="$(docker port "$1" 80/tcp | head -1 | sed 's/.*://')"
  for i in $(seq 1 60); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:$port/wp-login.php" || echo 000)"
    [ "$code" != "000" ] && break
    sleep 1
  done
  curl -s -o /dev/null -w '%{http_code}' --max-time 10 -H "Cookie: $cookie" "http://127.0.0.1:$port/wp-admin/" || echo 000
}

# No database: WordPress itself cannot answer, but the 400 is Apache's, before PHP.
docker run -d --name h2wp-limits-probe -p 127.0.0.1::80 "$IMAGE" bash -c "$SCRIPT" >/dev/null
docker run -d --name h2wp-limits-stock -p 127.0.0.1::80 "$IMAGE" >/dev/null
with="$(status_of h2wp-limits-probe)"
big="$(status_of h2wp-limits-probe "$BIG_COOKIE")"
stock="$(status_of h2wp-limits-stock)"
if [ "$with" != "400" ] && [ "$with" != "000" ]; then echo "  ok   the compose command: a 20 KB cookie gets $with, not 400"
else echo "  FAIL the compose command: a 20 KB cookie gets $with"; fail=1; fi
if [ "$big" != "400" ] && [ "$big" != "000" ]; then echo "  ok   the compose command: a ~200 KB cookie gets $big, not 400"
else echo "  FAIL the compose command: a ~200 KB cookie gets $big"; fail=1; fi
if [ "$stock" = "400" ]; then echo "  ok   the stock image refuses the same request (400): the probe measures the limit"
else echo "  FAIL the stock image answered $stock — the probe no longer shows the limit it guards"; fail=1; fi
docker exec h2wp-limits-probe test -f /var/www/html/wp-includes/version.php \
  && echo "  ok   the image's entrypoint still set WordPress up" \
  || { echo "  FAIL the image's entrypoint did not run: no WordPress in /var/www/html"; fail=1; }
[ "$fail" = 0 ] && echo "ALL OK" || exit 1
