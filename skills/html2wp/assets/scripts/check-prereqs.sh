#!/usr/bin/env bash
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
#
# Is this machine able to run a conversion? Answer before stage 0, not at
# stage 5.
#
#   check-prereqs.sh            report, exit 1 if anything is missing
#   check-prereqs.sh --commands print only the install commands, one per line
#
# A conversion is 30–90 minutes of work and touches six different tools. The
# expensive failure is not "jq is missing" — it is discovering that at stage 6,
# after everything else has already been paid for. So this runs first.
#
# It does NOT install anything. It prints exactly what to run, split by who can
# run it: the assistant can install the user-local things, and Docker Desktop
# and Node are the user's own decision because they change the machine.
set -uo pipefail

FORMAT="report"
[ "${1:-}" = "--commands" ] && FORMAT="commands"

case "$(uname -s)" in
  Darwin) PLATFORM="macos" ;;
  Linux)  PLATFORM="linux" ;;
  *)      PLATFORM="other" ;;
esac

# The package manager decides what every suggestion below looks like, so find
# it once. On Linux the family matters more than the distribution name.
PM=""
case "$PLATFORM" in
  macos) command -v brew >/dev/null 2>&1 && PM="brew" ;;
  linux)
    for c in apt-get dnf pacman zypper; do
      command -v "$c" >/dev/null 2>&1 && { PM="$c"; break; }
    done ;;
esac

pkg_cmd() { # <brew-name> <apt-name> — most packages differ only in name
  case "$PM" in
    brew)    echo "brew install $1" ;;
    apt-get) echo "sudo apt-get install -y $2" ;;
    dnf)     echo "sudo dnf install -y $2" ;;
    pacman)  echo "sudo pacman -S --needed $2" ;;
    zypper)  echo "sudo zypper install -y $2" ;;
    *)       echo "" ;;
  esac
}

MISSING_AGENT=()   # the assistant may install these, with the user's approval
MISSING_USER=()    # the user installs these; they change the machine
ROWS=()

row() { ROWS+=("$(printf '%-22s %-9s %s' "$1" "$2" "$3")"); }

# ---------------------------------------------------------------- Node
if command -v node >/dev/null 2>&1; then
  NODE_MAJOR="$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)"
  if [ "$NODE_MAJOR" -ge 20 ] 2>/dev/null; then
    row "Node.js" "ok" "$(node -v)"
  else
    row "Node.js" "TOO OLD" "$(node -v) — 20 or newer is required"
    MISSING_USER+=("Node 20+ — $( [ "$PM" = brew ] && echo 'brew upgrade node' || echo 'https://nodejs.org/ or your version manager (nvm, fnm, volta)')")
  fi
else
  row "Node.js" "MISSING" "the Astro build and four .mjs scripts need it"
  MISSING_USER+=("Node 20+ — $( [ -n "$PM" ] && pkg_cmd nodejs nodejs || echo 'https://nodejs.org/')")
fi

# ---------------------------------------------------------------- Python
PY=""
for c in python3 python; do
  command -v "$c" >/dev/null 2>&1 && { "$c" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,9) else 1)' 2>/dev/null && { PY="$c"; break; }; }
done
if [ -n "$PY" ]; then
  row "Python" "ok" "$("$PY" -V 2>&1 | cut -d' ' -f2)"
else
  row "Python" "MISSING" "3.9 or newer; fifteen scripts are Python"
  MISSING_USER+=("Python 3.9+ — $( [ -n "$PM" ] && pkg_cmd python python3 || echo 'https://www.python.org/downloads/')")
  PY="python3"
fi

# ------------------------------------------------- Python packages + chromium
# These are user-local: a pip --user install and a browser download into the
# user's cache. Nothing here needs root, which is why the assistant may run them.
if "$PY" -c 'import playwright' >/dev/null 2>&1; then
  # Installed is not the same as usable — the browser binary is a separate
  # download, and its absence is the single most common failure on a fresh
  # machine. Ask playwright itself rather than guessing at cache paths.
  if "$PY" -c '
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    p.chromium.launch(headless=True).close()
' >/dev/null 2>&1; then
    row "Playwright" "ok" "chromium launches"
  else
    row "Playwright" "NO BROWSER" "the package is there, chromium is not"
    MISSING_AGENT+=("$PY -m playwright install chromium")
  fi
else
  row "Playwright" "MISSING" "prerendering and every screenshot"
  MISSING_AGENT+=("$PY -m pip install --user 'playwright==1.60.0'")
  MISSING_AGENT+=("$PY -m playwright install chromium")
fi

if "$PY" -c 'import PIL' >/dev/null 2>&1; then
  row "Pillow" "ok" ""
else
  row "Pillow" "MISSING" "image comparison and the theme screenshot"
  MISSING_AGENT+=("$PY -m pip install --user 'pillow==11.3.0'")
fi

# ---------------------------------------------------------------- Docker
# Three different failures with three different fixes, so do not collapse them
# into one "docker missing" line.
if command -v docker >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then
    if docker compose version >/dev/null 2>&1; then
      row "Docker" "ok" "daemon up, compose v2"
    else
      row "Docker" "NO COMPOSE" "'docker compose' v2 is required, not docker-compose"
      MISSING_USER+=("Docker Compose v2 — update Docker; the v1 'docker-compose' binary is not used")
    fi
  else
    row "Docker" "NOT RUNNING" "installed, but the daemon is not up"
    # Starting a daemon that is ALREADY INSTALLED changes nothing durable, needs
    # no root on macOS, and is the commonest of the three Docker failures — so
    # it is the assistant's to offer, like the pip installs. Installing Docker
    # is a different question and stays below.
    if [ "$PLATFORM" = macos ]; then
      MISSING_AGENT+=("open -a Docker && printf 'waiting for the Docker daemon' && until docker info >/dev/null 2>&1; do printf .; sleep 2; done; echo ' up'")
    else
      # systemctl needs root, so this one is still the user's to run.
      MISSING_USER+=("Start the Docker daemon: sudo systemctl start docker")
    fi
  fi
else
  row "Docker" "MISSING" "gates B and C run WordPress in a throwaway container"
  if [ "$PLATFORM" = macos ]; then
    MISSING_USER+=("Docker Desktop — https://www.docker.com/products/docker-desktop/ (or: brew install --cask docker)")
  else
    MISSING_USER+=("Docker Engine + compose plugin — https://docs.docker.com/engine/install/")
  fi
fi

# ---------------------------------------------------------------- small tools
for pair in "php:php:php-cli" "jq:jq:jq" "curl:curl:curl" "tar:tar:tar"; do
  bin="${pair%%:*}"; rest="${pair#*:}"; brewname="${rest%%:*}"; aptname="${rest#*:}"
  if command -v "$bin" >/dev/null 2>&1; then
    row "$bin" "ok" ""
  else
    case "$bin" in
      php) why="make-zip.sh lints every PHP file before packaging" ;;
      jq)  why="the conversion manifest and every gate report" ;;
      *)   why="" ;;
    esac
    row "$bin" "MISSING" "$why"
    c="$(pkg_cmd "$brewname" "$aptname")"
    if [ -n "$c" ]; then MISSING_AGENT+=("$c"); else MISSING_USER+=("$bin — install it with your package manager"); fi
  fi
done

# ---------------------------------------------------------------- output
if [ "$FORMAT" = "commands" ]; then
  for c in "${MISSING_AGENT[@]:-}"; do [ -n "$c" ] && echo "$c"; done
  exit $(( ${#MISSING_AGENT[@]} + ${#MISSING_USER[@]} > 0 ? 1 : 0 ))
fi

echo
echo "html2wp — can this machine run a conversion?"
[ -n "$PM" ] && echo "($PLATFORM, $PM)" || echo "($PLATFORM, no package manager found)"
echo
for r in "${ROWS[@]}"; do echo "  $r"; done
echo

if [ "${#MISSING_AGENT[@]}" -eq 0 ] && [ "${#MISSING_USER[@]}" -eq 0 ]; then
  echo "Everything is here. Start the conversion."
  exit 0
fi

if [ "${#MISSING_AGENT[@]}" -gt 0 ]; then
  echo "These are user-local and need no root — ask, then run them one at a time:"
  for c in "${MISSING_AGENT[@]}"; do echo "    $c"; done
  echo
fi

if [ "${#MISSING_USER[@]}" -gt 0 ]; then
  echo "These change the machine, so they are the user's own call. Do NOT run"
  echo "them for the user — say what is needed and wait:"
  for c in "${MISSING_USER[@]}"; do echo "    - $c"; done
  echo
fi

echo "Re-run this script afterwards. A conversion that starts without these"
echo "fails somewhere between stage 0 and stage 6, having already spent the time."
exit 1
