#!/usr/bin/env bash
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
#
# What this machine has left, before any work starts.
#
#   allowance.sh [--api=URL] [--key=KEY]
#
# A read-only call to the service — it opens no job and spends nothing. Run it
# at the very start so the owner knows their credit up front: how many free
# conversions remain, the page limit, whether a shop is included, and where a
# licence comes from. The service composes the sentence; this just prints it.
set -uo pipefail

API="${H2WP_API:-https://api.html2wp.dev}"
KEY="${H2WP_KEY:-}"
for arg in "$@"; do
  case "$arg" in
    --api=*) API="${arg#--api=}" ;;
    --key=*) KEY="${arg#--key=}" ;;
    --*) echo "unknown flag: $arg" >&2; exit 2 ;;
  esac
done

# The key can also live in a file, so it never lands in shell history.
[ -z "$KEY" ] && [ -f "$HOME/.config/html2wp/licence" ] && KEY="$(tr -d '[:space:]' < "$HOME/.config/html2wp/licence")"

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

# The key travels in a header, not the URL, so it stays out of proxy/access logs.
HTTP="$(curl -sS --connect-timeout 15 --max-time 30 -o "$TMP" -w '%{http_code}' \
  ${KEY:+-H "x-html2wp-key: $KEY"} \
  "$API/v1/allowance" 2>/dev/null || echo 000)"

if [ "$HTTP" != "200" ]; then
  # Never fatal — not knowing the balance must not stop a conversion.
  echo "note: could not read the conversion allowance (HTTP $HTTP); the service will state it at job creation."
  exit 0
fi

python3 - "$TMP" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(0)
line = (d.get("credit") or {}).get("line")
note = d.get("note")
if line:
    print(line)
if note:
    print(note)
PY
