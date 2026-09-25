#!/usr/bin/env bash
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
# Flash stage 3.5 — the theme ZIP stage 5 installs and the run delivers.
#
#   assets/scripts/theme-zip.sh <workspace>
#
# With a blog, the article layout first (article-part.py: the service's own
# when it is the site's, else derived from the site's article page); with a
# shop, the cart behaviours a shopper watches (woo-shims.py --all: the header
# count, its sync with the block cart, a chosen option as its own cart line);
# then make-zip.sh with the manifest, into <workspace>/<slug>-<version>.zip —
# the path stage 5 and write-result.py read. Stage 3 (stage3-remote.sh) builds
# the theme and its screenshot; it never packs the ZIP.
#
# Exit 0 = packed (the path on the last line); otherwise make-zip's or
# article-part's refusal, with its `h2wp-signature:` line for Flash's repair
# budget (SKILL.md, "Flash repairs"). 64 = the call itself was wrong.
set -uo pipefail

S="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WS="${1:-}"
[ -n "$WS" ] && [ -d "$WS" ] || { echo "usage: theme-zip.sh <workspace>" >&2; exit 64; }
WS="$(cd "$WS" && pwd)"
MF="$WS/conversion-manifest.json"
[ -f "$MF" ] || { echo "no conversion-manifest.json in $WS" >&2; exit 64; }
read -r SLUG VERSION BLOG SHOP < <(python3 -c '
import json, sys
m = json.load(open(sys.argv[1]))
s = m.get("site") or {}
print(s.get("slug") or "-", s.get("version") or "1.0.0", "yes" if (m.get("blog") or {}).get("present") else "no",
      "yes" if (m.get("shop") or {}).get("present") else "no")
' "$MF") || { echo "cannot read $MF" >&2; exit 64; }
[ "$SLUG" != "-" ] && [ -d "$WS/theme/$SLUG" ] || { echo "no theme at $WS/theme/$SLUG — stage 3 builds it" >&2; exit 64; }

if [ "$BLOG" = "yes" ]; then
  echo "==> the article layout (article-part.py)"
  python3 "$S/article-part.py" "$WS" || exit $?
fi
if [ "$SHOP" = "yes" ]; then
  echo "==> the shop's cart behaviours (woo-shims.py --all)"
  python3 "$S/woo-shims.py" "$WS" --all || exit $?
fi
echo "==> make-zip"
MAKE_ZIP_MANIFEST="$MF" bash "$S/make-zip.sh" "$WS/theme/$SLUG" "$WS/$SLUG-$VERSION.zip" || exit $?
echo "$WS/$SLUG-$VERSION.zip"
