#!/usr/bin/env bash
# Regression test: where the mirror is allowed to fetch from.
#
#   assets/scripts/test-ssrf-guard.sh
#
# T07 of the adversarial set, and the half that was missing for longer.
#
# The URL a person types was checked. Nothing else was. A mirror drives a real
# browser at a real page, and the PAGE decides what else to load — script
# tags, images, XHR, iframes. So a site on an ordinary public host could ask
# for
#
#     <img src="http://169.254.169.254/latest/meta-data/...">
#     fetch('http://192.168.1.1/admin/config')
#
# and the mirror fetched it, wrote it into the output directory and carried it
# on into the conversion. The route handler that should have caught this
# looked only at the HTTP METHOD, and a GET to the metadata endpoint is a GET.
#
# The check now runs per request, which is why the logic had to leave
# mirror-live.py: that script parses argv at import, so nothing could load it,
# so the check could never be tested. A security check nobody can run is a
# security check nobody has verified.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "== the mirror refuses private addresses, however they are reached =="
python3 "$SCRIPT_DIR/test-ssrf-guard.py"

# The wiring, separately: a correct predicate the route handler never calls is
# the same as no predicate. Asserted against the source because driving a real
# browser at a real metadata endpoint is not something a test suite should do.
echo
echo "== every live browser/API path uses the reusable guard =="
fail=0
guard_calls="$(grep -c "attach_network_guard(" "$SCRIPT_DIR/mirror-live.py" || true)"
if [ "$guard_calls" -ge 3 ]; then
  echo "ok   — mirror capture, redirect adoption and live gate contexts are guarded"
else
  echo "FAIL — expected at least 3 live context guards, found $guard_calls"; fail=1
fi

if grep -q "guarded_api_get(" "$SCRIPT_DIR/mirror-live.py"; then
  echo "ok   — referenced-asset redirects are checked hop by hop"
else
  echo "FAIL — the API asset sweep can follow redirects without the guard"; fail=1
fi

if grep -q "private = request_is_private(req.url)" "$SCRIPT_DIR/mirror-live.py"; then
  echo "ok   — the offline iframe exception still refuses private targets"
else
  echo "FAIL — the mirror gate's iframe path has no private-address check"; fail=1
fi

if grep -q "guard_context(ctx, base_url)" "$SCRIPT_DIR/prerender-spa.py"; then
  echo "ok   — local prerender pages cannot use Chromium to reach other private hosts"
else
  echo "FAIL — prerender's untrusted browser context is unguarded"; fail=1
fi

if grep -q "blockedPrivateRequests" "$SCRIPT_DIR/mirror-live.py"; then
  echo "ok   — refusals are reported, not silently dropped"
else
  echo "FAIL — nothing surfaces what was refused"; fail=1
fi

echo
if [ "$fail" = 0 ]; then echo "ALL OK"; else echo "$fail failing check(s)"; exit 1; fi
