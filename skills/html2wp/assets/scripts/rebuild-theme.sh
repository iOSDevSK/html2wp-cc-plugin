#!/usr/bin/env bash
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
# Post-handover rebuild — server stages 3 → 4 → 4.5 → 4.6, then the local
# screenshot and ZIP, in the one order that works.
#
#   assets/scripts/rebuild-theme.sh --manifest=conversion-manifest.json [--version=1.1.6]
#
# This exists because a delivered site's fixes arrive as REPORTS ("this should
# be a collection", "the canonical is wrong") and the repair is almost never a
# full conversion — it is an edit to the manifest followed by exactly this
# sequence. The service runs the generator stages in their required order (its
# ordering rules were each learned from a mistake); what stays local is what
# needs your machine: the screenshot and the archive.
#
# A re-upload of the same built site is recognised by its digest and does not
# spend a conversion — which is precisely what makes manifest-fix loops free.
#
# What this does NOT cover, on purpose: any change to the INPUT (src/). New
# images, markup attributes, different pages — the pipeline must re-run from
# stage 1 with gates A/A2, because nothing here re-verifies fidelity. If
# dist/ is older than src/, this script refuses.
set -euo pipefail

MANIFEST=""
VERSION=""
EXTRA=()
for arg in "$@"; do
  case "$arg" in
    --manifest=*) MANIFEST="${arg#--manifest=}";;
    --version=*)  VERSION="${arg#--version=}";;
    --api=*|--key=*|--opts=*) EXTRA+=("$arg");;
    *) echo "unknown argument: $arg"; exit 2;;
  esac
done
[ -n "$MANIFEST" ] || { echo "usage: rebuild-theme.sh --manifest=conversion-manifest.json [--version=X.Y.Z]"; exit 2; }
MANIFEST="$(cd "$(dirname "$MANIFEST")" && pwd)/$(basename "$MANIFEST")"
S="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

{
  IFS= read -r WS
  IFS= read -r SLUG
  IFS= read -r CUR_VERSION
  IFS= read -r SCHEMA
} <<EOF
$(node -e '
const m = JSON.parse(require("fs").readFileSync(process.argv[1], "utf8"));
// The workspace is where its manifest is (lib/manifest-paths.mjs): a copied
// workspace must not rebuild into the original it was copied from.
const P = require("path"), mp = P.resolve(process.argv[1]);
const ws = P.basename(mp) === "conversion-manifest.json" || !m.workspace ? P.dirname(mp) : P.resolve(m.workspace);
for (const v of [ws, m.site.slug, m.site.version || "1.0.0", m.schema || "html2wp/1"]) console.log(v);
' "$MANIFEST")
EOF

# Refused before anything changes while the theme carries live fixes
# (live-fix.py guard; H2WP_DISCARD_LIVE_FIXES=1 runs live-fix.py discard, keeping a copy).
if [ -f "$WS/live-fix.json" ] || [ -f "$WS/.live-fix/live-fix.json" ]; then
  ACTION=guard; [ "${H2WP_DISCARD_LIVE_FIXES:-0}" = 1 ] && ACTION=discard
  python3 "$S/live-fix.py" "$ACTION" --workspace "$WS" >&2 || exit 1
fi

# The version lives in the manifest so the service stamps it into style.css
# and readme.txt in one place, and so the next rebuild sees it.
if [ -n "$VERSION" ]; then
  node -e '
const fs = require("fs");
const m = JSON.parse(fs.readFileSync(process.argv[1], "utf8"));
m.site.version = process.argv[2];
fs.writeFileSync(process.argv[1], JSON.stringify(m, null, 2) + "\n");
' "$MANIFEST" "$VERSION"
else
  VERSION="$CUR_VERSION"
fi

DIST="$WS/astro-project/dist"
[ -d "$DIST" ] || { echo "no dist at $DIST — this is a stage-1 rebuild, not a repair"; exit 1; }
# A repair must not quietly ship a stale build: if any page in src/ is newer
# than its built copy, the input changed and the full pipeline owns that.
STALE=$(find "$WS/src" -name '*.html' -newer "$DIST/index.html" 2>/dev/null | head -1 || true)
[ -z "$STALE" ] || { echo "src/ is newer than dist/ (${STALE#"$WS"/}) — re-run from stage 1 with gates A/A2 instead"; exit 1; }

if [ "$SCHEMA" = "html2wp/2" ]; then
  for option in "${EXTRA[@]}"; do
    case "$option" in
      --opts=*) echo "Gutenberg rebuild uses the reviewed block plan; legacy --opts are not supported" >&2; exit 2 ;;
    esac
  done
  echo "==> direct Gutenberg rebuild ($SLUG $VERSION)"
  bash "$S/convert-remote.sh" "$WS" "${EXTRA[@]}"
  echo "Rebuilt theme: $WS/theme/$SLUG"
  echo "Install this build into localhost WordPress and run gutenberg-verify-local.py"
  echo "with --site and --source localhost origins, --routes covering all source pages,"
  echo "--theme-slug=$SLUG --theme-dir=$WS/theme/$SLUG --out=$WS/gutenberg-verification.json"
  echo "and the local test credentials. Use --edit-roundtrip on the disposable test database."
  echo "After every editor and visual gate passes, package with:"
  echo "python3 $S/gutenberg-package.py --theme=$WS/theme/$SLUG --report=$WS/gutenberg-verification.json --out=$WS/$SLUG-$VERSION.zip"
  exit 0
fi

echo "==> stages 3–4.6: the service ($SLUG $VERSION)"
bash "$S/convert-remote.sh" "$WS" "${EXTRA[@]}"

echo "==> stage 3.5: screenshot"
python3 "$S/make-screenshot.py" --manifest="$MANIFEST" | tail -1

echo "==> zip"
ZIP="$WS/$SLUG-$VERSION.zip"
MAKE_ZIP_MANIFEST="$MANIFEST" bash "$S/make-zip.sh" "$WS/theme/$SLUG" "$ZIP"
echo "rebuilt: $ZIP"
