#!/usr/bin/env bash
# Regression test for lib/safe-path.mjs — the containment check every
# caller-supplied path goes through before it reaches the filesystem.
#
#   assets/scripts/test-safe-path.sh
#
# Why this exists: `pages[].file` in the manifest and the media refs scraped
# out of the site's own HTML both arrive from outside, and both were joined
# onto DIST unchecked. `join()` collapses `..` without complaint, so
# `../../app/core/visual-edit.zip` was a working read of the paid plugin —
# whose contents then shipped inside the theme the caller downloads. The
# entitlement model, defeated by a text field.
#
# The symlink cases are the half a string check cannot see: a link planted in
# the uploaded workspace makes an in-bounds path resolve out of bounds, and
# only realpath catches it.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "== paths from the manifest and from page markup stay under the root =="
node "$SCRIPT_DIR/test-safe-path.mjs"
