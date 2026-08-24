#!/usr/bin/env bash
# Regression: hostile build code cannot mutate the source, use the network,
# fall back to the host, or smuggle a host file through an output symlink.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fail=0

echo "== the dependency filter refuses hosts, not versions =="
python3 - "$SCRIPT_DIR" <<'PY'
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(sys.argv[1]) / "lib"))
import sandbox
# `~5.4.0` is npm's ordinary tilde range and appears in a large share of real
# package.json files. An earlier filter forbade the CHARACTER rather than the
# "~/" prefix, so those projects were refused outright with
# UNSAFE_DEPENDENCY_SOURCE — a false positive that costs more users than the
# home-relative path it guards against would ever have cost. A security check
# that refuses ordinary input gets switched off, and then it guards nothing.
cases = [("^18.3.1", True), ("~5.4.0", True), (">=1.2 <2", True), ("1.x", True),
         ("latest", True), ("npm:react@18", True),
         ("github:vercel/swr", False), ("git+https://g/x.git", False),
         ("file:../lib", False), ("https://e.com/p.tgz", False),
         ("~/secrets/pkg", False), ("./local", False)]
bad = [c for c, want in cases if bool(sandbox._dependency_spec_allowed(c)) is not want]
if bad:
    print("  FAIL, judged wrongly:", bad)
    raise SystemExit(1)
print(f"  ok   — {len(cases)} dependency specs judged correctly")
PY
echo

echo "== sandbox boundary =="
if ! python3 -c "
import sys; sys.path.insert(0, '$SCRIPT_DIR/lib'); import sandbox
raise SystemExit(0 if sandbox.available() else 1)" 2>/dev/null; then
  if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
    echo "FAIL — Docker is required for this CI security regression"; fail=1
  else
    echo "SKIP — no usable Docker; static boundary checks still run below"
  fi
else
  DECOY="$TMP/home"; mkdir -p "$DECOY/.ssh"
  printf 'DECOY-KEY-%s\n' "$RANDOM$RANDOM" > "$DECOY/.ssh/id_rsa"
  PROJ="$TMP/project"; mkdir -p "$PROJ"
  printf '{"scripts":{"build":"true"}}\n' > "$PROJ/package.json"
  printf 'ORIGINAL\n' > "$PROJ/must-survive.txt"

  python3 - "$SCRIPT_DIR" "$PROJ" "$DECOY" <<'PY' > "$TMP/out.txt" 2>&1
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(sys.argv[1]) / "lib"))
import sandbox
work, _deps = sandbox.prepare_workspace(sys.argv[2])
probe = (
    "node -e \"const fs=require('fs'),os=require('os');"
    "for (const p of [os.homedir()+'/.ssh/id_rsa', %r+'/.ssh/id_rsa']) {"
    "try { console.log(fs.readFileSync(p,'utf8')) } catch(e) { console.log('NO-ACCESS-'+e.code) }};"
    "console.log('HOME='+os.homedir())\""
) % sys.argv[3]
r = sandbox.run_in_sandbox(probe, work, 300, "secret probe", network=False)
if r.returncode: raise SystemExit(r.returncode)
r = sandbox.run_in_sandbox(
    "node -e \"fetch('https://registry.npmjs.org/').then(()=>process.exit(9)).catch(()=>process.exit(0))\"",
    work, 300, "network probe", network=False,
)
if r.returncode: raise SystemExit(r.returncode)
r = sandbox.run_in_sandbox("rm -rf /work/*", work, 300, "mutation probe", network=False)
raise SystemExit(r.returncode)
PY
  sed 's/^/    /' "$TMP/out.txt"

  CANARY="$(cat "$DECOY/.ssh/id_rsa")"
  if grep -qF "$CANARY" "$TMP/out.txt"; then
    echo "  FAIL — sandbox read the host decoy key"; fail=1
  else
    echo "  ok   — host credentials are outside the mount"
  fi
  if grep -q "HOME=/home/build" "$TMP/out.txt"; then
    echo "  ok   — container got a throwaway home"
  else
    echo "  FAIL — HOME was not replaced"; fail=1
  fi
  if [ "$(cat "$PROJ/must-survive.txt")" = "ORIGINAL" ]; then
    echo "  ok   — deleting /work did not touch the original project"
  else
    echo "  FAIL — disposable build mutated the original project"; fail=1
  fi
  echo "  ok   — build/lifecycle phase could not reach the network"
fi

echo
echo "== trusted output copy =="
mkdir -p "$TMP/output"
printf '<html>ok</html>\n' > "$TMP/output/index.html"
ln -s "$TMP/home/.ssh/id_rsa" "$TMP/output/leak.txt"
if python3 - "$SCRIPT_DIR" "$TMP/output" "$TMP/copied" <<'PY'
import pathlib, sys
sys.path.insert(0, str(pathlib.Path(sys.argv[1]) / "lib"))
import sandbox
try:
    sandbox.copy_build_output(sys.argv[2], sys.argv[3])
except ValueError:
    raise SystemExit(1)
PY
then
  echo "FAIL — output symlink was followed"; fail=1
else
  echo "ok   — output symlinks are refused and partial output is removed"
fi
[ ! -e "$TMP/copied" ] || { echo "FAIL — rejected partial output remains"; fail=1; }

echo
echo "== prerender wiring =="
SOURCE="$SCRIPT_DIR/prerender-spa.py"
for want in "sandbox.unsafe_override" "SANDBOX_UNAVAILABLE" "sandbox.prepare_workspace" \
            "network=False" "sandbox.copy_build_output"; do
  if grep -q "$want" "$SOURCE"; then
    echo "ok   — build boundary contains $want"
  else
    echo "FAIL — build boundary is missing $want"; fail=1
  fi
done
if grep -q 'node:22-bookworm-slim@sha256:' "$SCRIPT_DIR/lib/sandbox.py"; then
  echo "ok   — build image is digest-pinned"
else
  echo "FAIL — build image is a moving tag"; fail=1
fi
if grep -q 'shell=True, cwd=PROJECT' "$SOURCE" && ! grep -q 'H2WP_NO_SANDBOX=1' "$SOURCE"; then
  echo "FAIL — direct host execution is not tied to the explicit override"; fail=1
else
  echo "ok   — host execution exists only behind H2WP_NO_SANDBOX=1"
fi

echo
if [ "$fail" = 0 ]; then echo "ALL OK"; else echo "$fail failing check(s)"; exit 1; fi
