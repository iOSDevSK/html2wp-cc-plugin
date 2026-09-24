#!/usr/bin/env bash
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
#
# The test WordPress holds still: a fresh `test-env.sh up` runs the pinned
# WordPress, and WordPress's own update checks (the wp-cron events that
# installed 7.1.2 over an idle 7.0.2 preview, maintenance mode included)
# change nothing: same version afterwards, no .maintenance file, and the
# updater reports itself disabled.
# Needs Docker. Uses its own throwaway env and removes it.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v docker >/dev/null || { echo "SKIP: docker not available"; exit 0; }
TMP="$(mktemp -d)"; SLUG="p-pinned-$$"
cleanup() { (cd "$TMP" && bash "$HERE/test-env.sh" down "$SLUG" >/dev/null 2>&1); rm -rf "$TMP"; }
trap cleanup EXIT
fail=0; check() { if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1: got '$2', want '$3'"; fail=1; fi; }

cd "$TMP"
bash "$HERE/test-env.sh" up "$SLUG" > up.log 2>&1 || { echo "up failed:"; tail -20 up.log; exit 1; }
WP="$(python3 -c "import json;print(json.load(open('.test-env-$SLUG.json'))['wpCli'])")"
# The version the image ships, read from the image's own copy: the pin is the
# digest, so the test never hardcodes a WordPress release.
IMG="$($WP eval 'include "/usr/src/wordpress/wp-includes/version.php"; echo $wp_version;' 2>/dev/null)"
before="$($WP core version)"
check "a fresh up runs the image's WordPress" "$before" "$IMG"
check "the updater is off" "$($WP eval 'require_once ABSPATH."wp-admin/includes/class-wp-automatic-updater.php"; echo (new WP_Automatic_Updater())->is_disabled() ? "off" : "on";')" "off"
$WP cron event run wp_version_check >/dev/null 2>&1
$WP cron event run wp_maybe_auto_update >/dev/null 2>&1
check "after WordPress's own update run: same version" "$($WP core version)" "$before"
check "no maintenance mode left behind" "$($WP eval 'echo file_exists(ABSPATH.".maintenance") ? "present" : "absent";')" "absent"

[ "$fail" = 0 ] && echo "ALL OK" || { echo "FAILED"; exit 1; }
