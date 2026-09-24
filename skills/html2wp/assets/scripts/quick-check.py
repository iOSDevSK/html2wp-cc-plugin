#!/usr/bin/env python3
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
"""The quick check — seconds, not minutes: is the installed theme alive?

  python3 quick-check.py --env {workspace}/.test-env-<slug>.json \\
      --manifest=conversion-manifest.json [--out {workspace}/quick-check.json] \\
      [--target html|gutenberg]

Run against the test-env WordPress after the theme is installed and its
content imported. It checks, with no screenshot and no visual comparison:

  - the manifest's theme (site.slug) is the ACTIVE theme;
  - every PHP file of workspace/theme/<slug> lints (`php -l`);
  - every entry the theme's importer published (HTML target: meta
    _clara_ve_key; Gutenberg: _h2wp_gb_source), and the front page, answers
    HTTP 200 without a PHP warning, error or fatal — read from the reporter
    below, because the test WordPress does not print PHP errors into a page.

The reporter is quick-check-mu.php, copied into the test-env WordPress as a
must-use plugin (wp-content/mu-plugins/h2wp-quick-check.php). It answers only
a request that sends `X-H2WP-Quick: 1`, appending the request's PHP problems
as a base64 HTML comment; every other request is untouched. A route whose
answer carries no such comment fails too: its PHP errors could not be seen.

Notices and deprecations count only from the theme's own files — a core or
plugin deprecation is not the theme's problem; warnings and errors count from
anywhere. Block-editor validity (the Gutenberg editor's own parse of every
template, part and entry) is NOT part of this script: the full Gutenberg gate
(gutenberg-verify-local.py) owns it.

Writes schema h2wp-quick-check/1 (passed, problems, warnings, routes). Exit 0
passed, 1 failed, 2 usage.

Carried over from the desktop app (runtime/runner.py quick_check, and
runtime/quick-check-mu.php), so the plugin alone gives a Flash run and a
change after delivery the same seconds-long functional proof.
"""
import argparse
import base64
import json
import os
import re
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
from manifest_paths import workspace_of  # noqa: E402

SCHEMA = "h2wp-quick-check/1"
REPORTER = Path(__file__).resolve().with_name("quick-check-mu.php")
MU_PATH = "/var/www/html/wp-content/mu-plugins/h2wp-quick-check.php"

# PHP output WordPress prints into a page when a theme misbehaves.
PHP_PROBLEM = re.compile(r'(<b>)?(Fatal error|Parse error|Warning|Notice|Deprecated|Recoverable fatal error)(</b>)?:\s.{0,200}? in (<b>)?/[^\s<]+\.php|There has been a critical error on this website', re.S)

# E_WARNING, E_USER_WARNING and every error type count from any file; notices
# and deprecations only from the theme's own files (a core or plugin
# deprecation is not the theme's problem).
SERIOUS = 1 | 2 | 4 | 16 | 64 | 256 | 512 | 4096


def reported_problems(body):
    """The reporter's problems for this answer, or None when it did not answer."""
    m = re.search(r"<!--h2wp-quick:([A-Za-z0-9+/=]+)-->\s*$", body)
    if not m:
        return None
    try:
        data = json.loads(base64.b64decode(m.group(1)))
    except ValueError:
        return None
    root = str(data.get("themeRoot") or "")
    found = []
    for row in data.get("problems") or []:
        file = str(row.get("file", ""))
        own = bool(root) and file.startswith(root)
        if int(row.get("type", 0)) & SERIOUS or own:
            where = file[len(root):] if own else Path(file).name
            found.append(f"PHP {row.get('message', '')} in {where}:{row.get('line')}")
    return found


def route_problem(status, body):
    """Why a route fails, or None."""
    if status != 200:
        return f"HTTP {status}"
    found = reported_problems(body)
    if found:
        return "; ".join(found[:3])[:600]
    m = PHP_PROBLEM.search(body)
    if m:
        return re.sub(r"<[^>]+>", "", m.group(0))[:300]
    return None if found is not None else "the quick-check error reporter did not answer (PHP errors cannot be seen)"


def local_url(url, base):
    """A permalink on the test WordPress's own origin: the importer writes
    absolute URLs, and the origin the check reaches is the state file's."""
    return re.sub(r"^https?://[^/]+", base.rstrip("/"), url)


def route_path(url):
    return re.sub(r"^https?://[^/]+", "", url) or "/"


def fetch_route(url):
    request = urllib.request.Request(url, headers={"User-Agent": "html2wp-quick-check", "X-H2WP-Quick": "1"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read(4_000_000).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(200_000).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError) as e:
        return 0, f"unreachable: {e}"


def run(argv, timeout=120):
    try:
        out = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return out.returncode, out.stdout + out.stderr


def install_reporter(container):
    """Copy the reporter into the test WordPress as a must-use plugin. None on
    success, else why not."""
    code, out = run(["docker", "exec", container, "mkdir", "-p", str(Path(MU_PATH).parent)], 60)
    if code == 0:
        code, out = run(["docker", "cp", str(REPORTER), f"{container}:{MU_PATH}"], 60)
    if code == 0:
        # WordPress's own user owns wp-content; a root-owned file there is
        # the kind of stray an install check then trips over.
        code, out = run(["docker", "exec", container, "chown", "-R", "www-data:www-data", str(Path(MU_PATH).parent)], 60)
    return None if code == 0 else out.strip()[-300:]


def target_of(args, mf):
    value = args.target or os.environ.get("H2WP_TARGET", "")
    if value in ("html", "gutenberg"):
        return value
    return "gutenberg" if mf.get("schema") == "html2wp/2" or mf.get("target") == "gutenberg" else "html"


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--env", required=True, help=".test-env-<slug>.json written by test-env.sh up")
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", default="", help="default: {workspace}/quick-check.json")
    ap.add_argument("--target", default="", choices=("", "html", "gutenberg"))
    args = ap.parse_args(argv)
    try:
        env = json.loads(Path(args.env).read_text())
        mf = json.loads(Path(args.manifest).read_text())
    except (OSError, ValueError) as e:
        print(f"quick-check: {e}", file=sys.stderr)
        return 2
    base, cli, container = env.get("url", ""), env.get("wpCli", ""), env.get("wpContainer", "")
    slug = (mf.get("site") or {}).get("slug", "")
    if not (base and cli and container and slug):
        print("quick-check: the state file needs url, wpCli and wpContainer, the manifest site.slug", file=sys.stderr)
        return 2
    kind = target_of(args, mf)
    ws = workspace_of(mf, args.manifest)
    out = Path(args.out) if args.out else ws / "quick-check.json"
    wp = shlex.split(cli)
    report = {"schema": SCHEMA, "target": kind, "theme": slug, "passed": False, "problems": [], "warnings": [],
              "notes": ["block-editor validity is not part of the quick check; the full Gutenberg gate owns it"]
              if kind == "gutenberg" else []}
    problems = report["problems"]

    why = install_reporter(container)
    if why:
        problems.append(f"the PHP error reporter could not be installed: {why}")
    code, active = run(wp + ["option", "get", "stylesheet"], 60)
    if code != 0 or active.strip().splitlines()[-1:] != [slug]:
        problems.append(f"The theme {slug} is not the active theme in the test WordPress.")
    for path in sorted((ws / "theme" / slug).rglob("*.php")):
        code, linted = run(["php", "-l", str(path)], 60)
        if code != 0:
            problems.append("PHP syntax: " + linted.strip()[-300:])

    meta = "_h2wp_gb_source" if kind == "gutenberg" else "_clara_ve_key"
    code, listed = run(wp + ["post", "list", "--post_type=any", "--post_status=publish", "--meta_key=" + meta,
                             "--fields=ID,post_type,url", "--format=json", "--posts_per_page=400"], 120)
    try:
        rows = json.loads(listed[listed.index("["):]) if code == 0 and "[" in listed else []
    except ValueError:
        rows = []
    entities = [r for r in rows if r.get("post_type") not in ("wp_navigation", "attachment", "product_variation")]
    if code != 0:
        problems.append("wp-cli could not list the imported entries: " + listed.strip()[-300:])
    elif not entities:
        problems.append("The test WordPress has no imported entries. Install the theme and apply its content first.")

    report["routes"] = []
    for url in dict.fromkeys([base.rstrip("/") + "/"] + [e["url"] for e in entities]):
        status, body = fetch_route(local_url(url, base))
        problem = route_problem(status, body)
        report["routes"].append({"path": route_path(url), "status": status, "problem": problem})
        if problem:
            problems.append(f"{route_path(url)}: {problem}")

    report["passed"] = not problems
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2) + "\n")
    summary = f"{len(report['routes'])} route(s) answered"
    if problems:
        print("QUICK CHECK FAILED — " + summary)
        for p in problems[:20]:
            print("  - " + p)
        return 1
    print(f"quick check passed — {summary}, theme {slug} active, PHP clean -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
