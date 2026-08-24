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
} <<EOF
$(node -e '
const m = JSON.parse(require("fs").readFileSync(process.argv[1], "utf8"));
for (const v of [m.workspace, m.site.slug, m.site.version || "1.0.0"]) console.log(v);
' "$MANIFEST")
EOF

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

echo "==> stages 3–4.6: the service ($SLUG $VERSION)"
bash "$S/convert-remote.sh" "$WS" "${EXTRA[@]}"

echo "==> stage 3.5: screenshot"
python3 "$S/make-screenshot.py" --manifest="$MANIFEST" | tail -1

echo "==> zip"
ZIP="$WS/$SLUG-$VERSION.zip"
MAKE_ZIP_MANIFEST="$MANIFEST" bash "$S/make-zip.sh" "$WS/theme/$SLUG" "$ZIP"
echo "rebuilt: $ZIP"
