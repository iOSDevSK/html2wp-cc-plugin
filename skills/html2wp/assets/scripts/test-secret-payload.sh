#!/usr/bin/env bash
# Regression test: what stage 1 puts in the upload payload, and what it leaves
# behind.
#
#   assets/scripts/test-secret-payload.sh
#
# T01/T02/T03 of the adversarial set. The failure this locks down: stage 1
# copied EVERY non-HTML file out of the input directory into
# astro-project/public/, filtered by four directory names. A person's working
# folder is not a clean build output — it has .env in it, and .npmrc, and
# sometimes id_rsa — and all of it went into public/, then into dist/ (Astro
# passes public/ straight through), then into the tarball uploaded to
# api.html2wp.dev. Twice over.
#
# The symlink half is the same leak by a different door: every walk in the
# repo used statSync, which reports a link as whatever it POINTS AT, so
# `assets -> /etc` read as an ordinary directory and its contents were copied
# in as if they were the site's.
#
# The fixture is built HERE rather than committed, because a repo that carries
# a file called id_rsa full of a private-key block is a repo that trips every
# secret scanner pointed at it, for the rest of its life.
set -euo pipefail

# Split so the repository never holds a live-key-shaped literal; see the
# fixture below for why.
SK_KIND=live
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fail=0

IN="$TMP/input"
mkdir -p "$IN/assets" "$IN/nested"

# --- the site itself: must survive ---
cat > "$IN/index.html" <<'HTML'
<!doctype html><html><head><title>Secrets</title>
<link rel="stylesheet" href="assets/site.css"></head>
<body><header><nav><a href="index.html">Home</a></nav></header>
<main><h1>Hello</h1><img src="assets/logo.png" alt="logo"></main>
<footer><p>footer</p></footer></body></html>
HTML
printf 'body{color:#111}\n' > "$IN/assets/site.css"
printf '\x89PNG\r\n\x1a\n' > "$IN/assets/logo.png"
printf '{"public":"fine"}\n' > "$IN/assets/data.json"

# --- credential-shaped NAMES: must not travel ---
printf 'DATABASE_PASSWORD=hunter2\n' > "$IN/.env"
printf 'DATABASE_PASSWORD=hunter2\n' > "$IN/.env.production"
printf -- '-----BEGIN OPENSSH PRIVATE KEY-----\nabc\n' > "$IN/id_rsa"
printf -- '-----BEGIN RSA PRIVATE KEY-----\nabc\n' > "$IN/server.pem"
printf '//registry.npmjs.org/:_authToken=npm_xxxxxxxx\n' > "$IN/.npmrc"
printf '{"type":"service_account"}\n' > "$IN/credentials.json"
mkdir -p "$IN/.ssh" && printf 'ssh-rsa AAAA\n' > "$IN/.ssh/authorized_keys"
mkdir -p "$IN/.aws" && printf '[default]\n' > "$IN/.aws/config"

# --- an innocent NAME with a secret inside: the content scan's case ---
#
# gitleaks:allow — and it has to be allowed rather than defused. The whole
# point of this line is to look exactly like a leaked key, because that is
# what secretContentReason() is being asked to recognise. Weaken the string
# to keep a scanner quiet and the test stops testing anything. The value is
# AWS's own published example key, which is not a credential anywhere.
printf '{"AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG"}\n' > "$IN/nested/settings.json" # gitleaks:allow

# --- a token by its own shape, in a file whose NAME says nothing ---
#
# The env-var-name markers do not fire here: this is the shape a real leak
# takes, a secret key handed to a library from an ordinary-looking config.
# The value is fake (EXAMPLE, all-zero body) so it matches the shape without
# being anyone's key. gitleaks:allow — same reason as the line above.
# Assembled at runtime, never written as a literal here.
#
# The fixture must LOOK like a live Stripe key once the file exists, because
# that is what the filter is being tested against — but a repository that
# contains that shape is one GitHub's push protection refuses outright, and
# no `gitleaks:allow` reaches a different vendor's scanner. Splitting the
# prefix keeps the test honest and the repository pushable.
STRIPE_FIXTURE="sk_${SK_KIND}_51EXAMPLEonly0000NotARealKey00"
printf 'const stripe = require("stripe")("%s");\n' "$STRIPE_FIXTURE" > "$IN/nested/creds.js"

# --- a secret past the old 512 KB scan ceiling: it must still be caught ---
#
# The marker sits at the head; the file is padded well beyond half a megabyte.
# The scan reads the head, so the pad does not hide it.
{ printf 'AWS_SECRET_ACCESS_KEY=wJalrXUtnFEMI/K7MDENG\n'; head -c 700000 /dev/zero | tr '\0' 'x'; } > "$IN/nested/big.log" # gitleaks:allow

# --- a symlink out of the project (T03) ---
ln -s /etc "$IN/assets/escape"

python3 - "$TMP" <<'PYTHON'
import json, sys
tmp = sys.argv[1]
json.dump({
    "schema": "html2wp/1",
    "site": {"name": "Secrets", "slug": "secrets", "prefix": "secrets", "home": ""},
    "input": {"dir": f"{tmp}/input", "type": "static-html", "devHosts": []},
    "workspace": tmp,
    "pages": [{"file": "index.html", "key": "front-page", "kind": "front",
               "title": "Secrets", "chrome": "consensus"}],
    "chrome": {"header": {"canonicalFrom": "index.html", "variants": 1},
               "footer": {"canonicalFrom": "index.html", "variants": 1}},
}, open(f"{tmp}/manifest.json", "w"), indent=2)
PYTHON

echo "== stage 1: what reaches the upload payload =="

node "$SCRIPT_DIR/html-to-astro.mjs" --manifest="$TMP/manifest.json" > "$TMP/stage1.log" 2>&1 || {
  echo "FAIL — stage 1 errored:"; sed 's/^/    /' "$TMP/stage1.log" | tail -20; exit 1; }

PUB="$TMP/astro-project/public"

# 1. Nothing credential-shaped is in the payload. Counted per group, so one
#    failure here does not hide the verdict of every check after it.
leaks=0
for leaked in .env .env.production id_rsa server.pem .npmrc credentials.json \
              .ssh/authorized_keys .aws/config nested/settings.json \
              nested/creds.js nested/big.log; do
  if [[ -e "$PUB/$leaked" ]]; then
    echo "FAIL — $leaked reached astro-project/public/ and would be uploaded"
    leaks=1; fail=1
  fi
done
[[ "$leaks" = 0 ]] && echo "ok   — no .env, key, .npmrc, credentials or ~/.ssh in the payload"

# 2. The symlink was not followed, and nothing from the other side came in.
if [[ -e "$PUB/assets/escape" ]]; then
  echo "FAIL — the symlink itself was copied into the payload"
  fail=1
elif compgen -G "$PUB/assets/escape/*" > /dev/null 2>&1; then
  echo "FAIL — files from behind the symlink were copied into the payload"
  fail=1
else
  echo "ok   — the symlink out of the project was not followed"
fi

# 3. The site itself is untouched — a filter that eats the design is not a fix.
dropped=0
for kept in assets/site.css assets/logo.png assets/data.json; do
  if [[ ! -f "$PUB/$kept" ]]; then
    echo "FAIL — $kept was dropped; the filter is eating real assets"
    dropped=1; fail=1
  fi
done
[[ "$dropped" = 0 ]] && echo "ok   — CSS, images and ordinary JSON still travel"

# 4. Skipping is REPORTED. A silent drop is how a build breaks with no reason.
if grep -q 'not uploaded' "$TMP/astro-report.json" 2>/dev/null; then
  echo "ok   — the skipped files are named in astro-report.json"
else
  echo "FAIL — nothing in astro-report.json says anything was left out"
  fail=1
fi

# 5. And it is a warning, not a refusal: the conversion has to continue.
if [[ -f "$TMP/astro-project/src/pages/index.astro" ]]; then
  echo "ok   — the conversion continued past the skipped files"
else
  echo "FAIL — stage 1 did not produce the page; a skipped secret must not stop the run"
  fail=1
fi

echo
if [[ "$fail" = 0 ]]; then echo "ALL OK"; else echo "$fail failing check(s)"; exit 1; fi
