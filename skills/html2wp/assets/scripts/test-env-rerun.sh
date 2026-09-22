#!/usr/bin/env bash
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
#
# `test-env.sh up` run AGAIN on a WordPress a site was already imported into —
# stage3-remote runs `up` on every pass — must leave that site alone:
#   - WordPress's stock sample (hello-world, sample-page, draft privacy-policy)
#     is deleted, on the first run and on a re-run, while nothing claims it;
#   - imported pages/posts (importer meta), an owner's own page and an
#     imported page that happens to be called privacy-policy all survive;
#   - the permalink structure the import set is kept, not reset to the
#     WordPress default.
# Needs Docker. Uses its own throwaway env and removes it.
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
command -v docker >/dev/null || { echo "SKIP: docker not available"; exit 0; }
TMP="$(mktemp -d)"; SLUG="p-rerun-$$"
cleanup() { (cd "$TMP" && bash "$HERE/test-env.sh" down "$SLUG" >/dev/null 2>&1); rm -rf "$TMP"; }
trap cleanup EXIT
fail=0; check() { if [ "$2" = "$3" ]; then echo "  ok   $1"; else echo "  FAIL $1: got '$2', want '$3'"; fail=1; fi; }

cd "$TMP"
bash "$HERE/test-env.sh" up "$SLUG" > up1.log 2>&1 || { echo "first up failed:"; tail -20 up1.log; exit 1; }
WP="$(python3 -c "import json;print(json.load(open('.test-env-$SLUG.json'))['wpCli'])")"
check "first up: stock sample gone" "$($WP post list --post_type=post,page --post_status=any --format=count)" "0"
check "first up: sample comment gone" "$($WP comment list --format=count)" "0"

# What an import leaves: pages and a post claimed by importer meta, an owner's
# own page, an IMPORTED privacy-policy page, a stray unclaimed sample page,
# and the bundle's own permalink structure.
imp="$($WP post create --post_type=page --post_status=publish --post_title=Home --post_name=home --porcelain)"
$WP post meta add "$imp" _clara_ve_key front-page >/dev/null
gb="$($WP post create --post_type=page --post_status=publish --post_title=About --post_name=about --porcelain)"
$WP post meta add "$gb" _h2wp_gb_source "x:page:about" >/dev/null
art="$($WP post create --post_type=post --post_status=publish --post_title=Article --post_name=an-article --porcelain)"
$WP post meta add "$art" _html2wp_bundle_file "blog/an-article.html" >/dev/null
own="$($WP post create --post_type=page --post_status=publish --post_title=Mine --post_name=mine --porcelain)"
priv="$($WP post create --post_type=page --post_status=publish --post_title=Privacy --post_name=privacy-policy --porcelain)"
$WP post meta add "$priv" _clara_ve_theme site >/dev/null
stray="$($WP post create --post_type=page --post_status=publish --post_title='Sample Page' --post_name=sample-page --porcelain)"
$WP rewrite structure '/blog/%postname%/' --hard >/dev/null 2>&1

bash "$HERE/test-env.sh" up "$SLUG" > up2.log 2>&1 || { echo "second up failed:"; tail -20 up2.log; exit 1; }
alive() { $WP post get "$1" --field=post_status 2>/dev/null || echo gone; }
check "re-run keeps an imported page (HTML theme meta)" "$(alive "$imp")" "publish"
check "re-run keeps an imported page (Gutenberg meta)" "$(alive "$gb")" "publish"
check "re-run keeps an imported post" "$(alive "$art")" "publish"
check "re-run keeps the owner's own page" "$(alive "$own")" "publish"
check "re-run keeps an imported privacy-policy page" "$(alive "$priv")" "publish"
check "re-run deletes an unclaimed stock sample page" "$(alive "$stray")" "gone"
check "re-run keeps the permalink structure" "$($WP option get permalink_structure)" "/blog/%postname%/"

[ "$fail" = 0 ] && echo "ALL OK" || { echo "FAILED"; exit 1; }
