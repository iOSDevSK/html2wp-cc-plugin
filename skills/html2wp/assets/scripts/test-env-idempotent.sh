#!/usr/bin/env bash
# test-env.sh `up`, run twice on a shop workspace, must leave the site as the
# first run left it: WordPress's sample content gone, WooCommerce's shop, cart,
# checkout and account pages there. Needs Docker; not part of the offline CI.
#
#   test-env-idempotent.sh <workspace-with-a-shop-manifest>
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS="${1:?usage: test-env-idempotent.sh <workspace>}"
SLUG="idem-$$"
cd "$WS"
export H2WP_WORKSPACE="$(pwd -P)"
trap '"$HERE/test-env.sh" down "$SLUG" >/dev/null 2>&1 || true' EXIT
"$HERE/test-env.sh" up "$SLUG" >/dev/null
WPCLI="$(jq -r .wpCli ".test-env-$SLUG.json")"
pages() { $WPCLI option get "woocommerce_${1}_page_id" 2>/dev/null | xargs -I{} $WPCLI post get {} --field=post_status 2>/dev/null || echo missing; }
before="$(for p in shop cart checkout myaccount; do pages $p; done | tr '\n' ' ')"
"$HERE/test-env.sh" up "$SLUG" >/dev/null
after="$(for p in shop cart checkout myaccount; do pages $p; done | tr '\n' ' ')"
echo "first up:  $before"
echo "second up: $after"
[[ "$before" == "publish publish publish publish " && "$after" == "$before" ]] && echo "ok — a second up keeps WooCommerce's pages" && exit 0
echo "FAIL — a second up changed the site" >&2
exit 1
