#!/usr/bin/env bash
# Regression test for lib/page-key.mjs — the one derivation of a page's key,
# shared by stage 0 (analyze-input.mjs) and the service's stage 4.
#
#   assets/scripts/test-page-key.sh
#
# Why this exists: the derivation kept "_" while the service's validator
# (transform.ts PAGE_KEY) refuses it, so a site with index_v2.html or
# about_us.html was refused at stage 0 — with a message blaming the length.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
echo "== every derived page key is one the service stores =="
node "$SCRIPT_DIR/test-page-key.mjs"

echo "== two files that normalise to one key are caught at stage 0 =="
T="$(mktemp -d "${TMPDIR:-/tmp}/h2wp-page-key.XXXXXX")"
trap 'rm -rf "$T"' EXIT
page() { printf '<!doctype html><html><head><title>%s</title></head><body><header><a href="index.html">Home</a></header><main><h1>%s</h1><p>%s</p></main></body></html>\n' "$1" "$1" "$2"; }
mkdir -p "$T/diff" "$T/same"
page Home "front" > "$T/diff/index.html"; page "About us" "one team" > "$T/diff/about_us.html"; page "About" "a different page" > "$T/diff/about-us.html"
set +e
node "$SCRIPT_DIR/analyze-input.mjs" "$T/diff" --out="$T/diff.json" > "$T/diff.log" 2>&1; rc=$?
set -e
[ "$rc" = 2 ] || { echo "FAIL different pages sharing a key: exit $rc, want 2"; cat "$T/diff.log"; exit 1; }
node -e '
const a = require(process.argv[1]); const e = a.errors.join("\n");
if (!/about_us\.html/.test(e) || !/about-us\.html/.test(e) || !/"about-us"/.test(e) || !/rename one of the two FILES/.test(e)) { console.error("FAIL collision message:\n" + e); process.exit(1); }
console.log("  ok   about_us.html + about-us.html (different pages) → refused, both files and the fix named");' "$T/diff.json"
page Home "front" > "$T/same/index.html"; page "About" "same" > "$T/same/about_us.html"; cp "$T/same/about_us.html" "$T/same/about-us.html"
node "$SCRIPT_DIR/analyze-input.mjs" "$T/same" --out="$T/same.json" > "$T/same.log" 2>&1
node -e '
const a = require(process.argv[1]);
const d = a.duplicatePages || [];
if (a.errors.length || d.length !== 1 || d[0].key !== "about-us" || a.pages.filter((p) => p.key === "about-us").length !== 1) { console.error("FAIL identical pair:", JSON.stringify({ errors: a.errors, d })); process.exit(1); }
console.log("  ok   about_us.html + about-us.html (identical) → one page, " + d[0].dropped + " recorded for a redirect");' "$T/same.json"
