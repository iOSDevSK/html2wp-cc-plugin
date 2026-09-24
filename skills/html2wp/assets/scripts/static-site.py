#!/usr/bin/env python3
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
"""Stage -1 for a static-site generator (Astro): build it, take its own pages.

    static-site.py --project <dir> --out {workspace}/static-src
                   [--report {workspace}/static-site-report.json]
                   [--skip-build --dist <built-dir>]

A static-site generator already writes every page as complete HTML, so no
browser capture is needed and the site keeps its own scripts — the same bar
as a static HTML import. The project is built the way prerender-spa.py builds
an app (in the build sandbox: dependencies with the network and no scripts,
the build offline; H2WP_NO_SANDBOX=1 builds on the host), then
lib/static_site.py writes the pages in the flat shape stage 0 takes
(`about/index.html` → `about.html`, page links relative, resources rebased).

When the output is not a plain static site — an app shell, a hydrating
framework root, a <base> element, a stale build — nothing is written and the
answer says so: the browser capture applies instead.

Exit 0 = pages written to --out; 3 = not a plain static site, run
prerender-spa.py instead (`fallback: prerender` in the report); 1 = the build
failed; 2 = usage.

Carried over from the desktop app (runtime/static_site.py and its `ssg` step),
so the plugin alone prepares an Astro project as the app did.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import sandbox  # noqa: E402
import static_site  # noqa: E402

OUTPUT_DIRS = ("dist", "dist/client", ".output/public", "build", "out")


def built_output(root):
    """The build's static output folder: the first of dist, dist/client,
    .output/public, build, out that holds a real index.html."""
    for name in OUTPUT_DIRS:
        candidate = root / name
        if candidate.is_symlink() or not candidate.is_dir():
            continue
        index = candidate / "index.html"
        if index.is_file() and not index.is_symlink() and candidate.resolve().is_relative_to(root.resolve()):
            return candidate
    return None


def build(project, build_cmd):
    """(dist, error). Sandboxed unless H2WP_NO_SANDBOX=1 — never a silent
    host fallback."""
    install_timeout = int(os.environ.get("H2WP_NPM_TIMEOUT", "900"))
    build_timeout = int(os.environ.get("H2WP_BUILD_TIMEOUT", "1200"))
    if sandbox.unsafe_override():
        sandbox.warn_unsandboxed("H2WP_NO_SANDBOX=1")
        if not (project / "node_modules").exists():
            print("- installing dependencies (host)")
            if subprocess.run("npm install --no-audit --no-fund", shell=True, cwd=project,
                              timeout=install_timeout).returncode != 0:
                return None, "npm install failed"
        print(f"- {build_cmd} (host)")
        if subprocess.run(build_cmd, shell=True, cwd=project, timeout=build_timeout).returncode != 0:
            return None, "the build failed"
        return built_output(project), None
    why = sandbox.reason_unavailable()
    if why:
        return None, f"sandbox unavailable ({why}); refusing to execute project code on the host"
    bad = sandbox.validate_dependency_metadata(project)
    if bad:
        return None, f"dependency acquisition refused: {bad}"
    work, deps = sandbox.prepare_workspace(project)
    steps = (
        ("npm install", "npm install --ignore-scripts --no-audit --no-fund --registry=https://registry.npmjs.org/",
         deps, install_timeout, True),
        ("npm rebuild", "npm rebuild --offline", work, install_timeout, False),
        ("build", build_cmd, work, build_timeout, False),
    )
    for label, command, where, timeout, network in steps:
        print(f"- {label}{'' if network else ' (offline)'}")
        try:
            result = sandbox.run_in_sandbox(command, where, timeout, label, network=network)
        except subprocess.TimeoutExpired:
            return None, f"{label} timed out"
        if result.returncode != 0:
            return None, f"{label} failed"
        if label == "npm install":
            sandbox.promote_dependencies(deps, work)
    return built_output(work), None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--project", required=True)
    ap.add_argument("--out", required=True, help="where the flat pages go: stage 0's input")
    ap.add_argument("--report", default="", help="default: static-site-report.json beside --out")
    ap.add_argument("--build-cmd", default="npm run build")
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--dist", default="", help="an existing build output (with --skip-build)")
    args = ap.parse_args(argv)
    project = Path(args.project).resolve()
    out = Path(args.out).resolve()
    report = Path(args.report).resolve() if args.report else out.parent / "static-site-report.json"
    started = time.time()
    if args.skip_build:
        dist = Path(args.dist).resolve() if args.dist else built_output(project)
    else:
        dist, error = build(project, args.build_cmd)
        if error:
            print(f"static-site: {error}", file=sys.stderr)
            report.write_text(json.dumps({"schema": static_site.SCHEMA, "ok": False, "reason": error}, indent=2))
            return 1
    if dist is None:
        print("static-site: the build wrote no index.html in " + ", ".join(OUTPUT_DIRS), file=sys.stderr)
        return 1
    # The build copy keeps the project's file times, so an index.html older
    # than this build is a stale one the build did not write.
    result = static_site.run(dist, out, report, built_after=None if args.skip_build else started - 2)
    print(result["message"])
    if result.get("ok"):
        return 0
    return 3 if result.get("fallback") == "prerender" else 1


if __name__ == "__main__":
    sys.exit(main())
