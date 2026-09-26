#!/usr/bin/env python3
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
"""What kind of project is this, and what prepares it for stage 0?

    detect-project.py <project-dir> [--out detect.json] [--prepare <workspace>]

Read-only unless --prepare is given. Prints one JSON object (and writes it to
--out):

    {"schema": "h2wp-project-kind/1", "kind": "...", "root": "...",
     "generator": ..., "framework": ..., "pages": N, "prepare": [...], "reason": ...}

kind is one of

    static-html     a directory of .html pages: stage 0 takes it as it is
    static-site     a static-site generator project (Astro, not server output):
                    static-site.py builds it and takes its own pages, no browser
    html2wp-astro   an Astro 5 project html2wp exported: its dist/ is the input
                    and the project itself is the workspace's astro-project/
    web-app         a buildable client-rendered app (Vite/React, TanStack Start,
                    Lovable, Bolt, v0): stage -1, prerender-spa.py
    none            nothing convertible (no pages, no build script) — `reason`

`prepare` lists the stages that turn the project into stage 0's input, in
order. The desktop app used to decide this (files.rs detect(), conversion.rs
preparation_steps); a plugin that runs by itself decides it here.

--prepare <workspace> does the one preparation that is a copy, not a build:
an html2wp Astro export's dist/ becomes <workspace>/static-src (the input and
gate A's original), the project becomes <workspace>/astro-project and its
.html2wp/astro-report.json becomes <workspace>/astro-report.json. Every other
kind is prepared by its stage's own script.

Exit 0 = a convertible kind, 1 = none, 2 = usage.
"""
import argparse
import json
import re
import shutil
import sys
from pathlib import Path

SCHEMA = "h2wp-project-kind/1"
ASTRO_REPORT = ".html2wp/astro-report.json"
ASTRO_CONFIGS = ("astro.config.mjs", "astro.config.ts", "astro.config.js", "astro.config.mts", "astro.config.cjs")
MAX_DEPTH = 10


def input_root(root):
    """The project inside an upload: the directory itself when it holds a
    package.json or an index.html, else its one subdirectory (a ZIP that
    unpacked into a folder, or a site in a named folder beside a README),
    a few levels down at most."""
    for _ in range(3):
        if (root / "package.json").exists() or (root / "index.html").exists():
            return root
        dirs = [c for c in root.iterdir() if c.is_dir() and not c.is_symlink()
                and not c.name.startswith((".", "__MACOSX")) and c.name != "node_modules"]
        if len(dirs) != 1 or any(c.suffix.lower() in (".html", ".htm") for c in root.iterdir() if c.is_file()):
            return root
        root = dirs[0]
    return root


def read_package(root):
    try:
        return json.loads((root / "package.json").read_text())
    except (OSError, ValueError):
        return None


def depends(package, name):
    return bool(package) and any(name in (package.get(k) or {}) for k in ("dependencies", "devDependencies"))


def html_pages(root):
    pages = []
    for path in sorted(root.rglob("*.html")):
        rel = path.relative_to(root)
        if len(rel.parts) > MAX_DEPTH or any(p in ("node_modules", ".git", "__MACOSX") for p in rel.parts):
            continue
        pages.append(rel.as_posix())
    return pages


def html2wp_astro(root, package):
    """An Astro 5 project html2wp exported (Exports → Astro project): its page
    fragments, its built site and the converter's report."""
    return (depends(package, "astro")
            and any((root / c).is_file() for c in ASTRO_CONFIGS[:3])
            # All-self-contained exports keep pages in public/. Their empty
            # fragments directory is intentionally absent from the ZIP.
            and ((root / "src/fragments/bodies").is_dir() or (root / "public/index.html").is_file())
            and (root / "dist/index.html").is_file()
            and (root / ASTRO_REPORT).is_file())


def static_site_generator(root, package):
    """The generator whose build writes every page as complete HTML. Astro is
    the one known to pass; server output renders on request and has no pages
    to take."""
    if not package or not isinstance((package.get("scripts") or {}).get("build"), str):
        return None
    if not depends(package, "astro"):
        return None
    for name in ASTRO_CONFIGS:
        try:
            config = re.sub(r"\s", "", (root / name).read_text())
        except OSError:
            continue
        if "output:'server'" in config or 'output:"server"' in config:
            return None
    return "astro"


def framework(root, package):
    if depends(package, "@tanstack/react-start"):
        return "tanstack-start"
    if depends(package, "next"):
        return "next"
    if depends(package, "react-router-dom") or depends(package, "react-router"):
        return "react-router"
    if depends(package, "vue"):
        return "vue"
    if depends(package, "svelte"):
        return "svelte"
    if depends(package, "react"):
        return "react"
    return None


def detect(project):
    project = Path(project).resolve()
    if not project.is_dir():
        return {"schema": SCHEMA, "kind": "none", "root": str(project), "reason": "not a directory"}
    root = input_root(project)
    package = read_package(root)
    out = {"schema": SCHEMA, "root": str(root), "generator": None, "framework": None, "pages": 0, "reason": None}
    if package is not None and html2wp_astro(root, package):
        pages = html_pages(root / "dist")
        if not pages:
            return {**out, "kind": "none", "reason": "the Astro project's dist/ has no built pages; build it first"}
        return {**out, "kind": "html2wp-astro", "generator": "astro", "pages": len(pages),
                "prepare": ["detect-project.py --prepare"]}
    generator = static_site_generator(root, package) if package is not None else None
    if generator:
        return {**out, "kind": "static-site", "generator": generator, "prepare": ["static-site.py"]}
    if package is not None:
        if not isinstance((package.get("scripts") or {}).get("build"), str):
            # A package.json beside finished pages is a static site with tooling.
            pages = html_pages(root)
            if pages:
                return {**out, "kind": "static-html", "pages": len(pages), "prepare": []}
            return {**out, "kind": "none", "reason": "package.json has no build script and there are no HTML pages; "
                    "export the built site or add a build script"}
        return {**out, "kind": "web-app", "framework": framework(root, package), "prepare": ["prerender-spa.py"]}
    pages = html_pages(root)
    if not pages:
        return {**out, "kind": "none", "reason": "no HTML pages and no buildable project"}
    return {**out, "kind": "static-html", "pages": len(pages), "prepare": []}


def prepare_astro_export(root, workspace):
    """The export's built pages are the input; the project is the Astro project."""
    workspace.mkdir(parents=True, exist_ok=True)
    for src, dest in ((root / "dist", workspace / "static-src"), (root, workspace / "astro-project")):
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest, symlinks=False,
                        ignore=shutil.ignore_patterns("node_modules", ".git", "__MACOSX", ".DS_Store", "._*"))
    sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
    from source_assets import recover, safe_target
    from urllib.parse import quote
    assets = recover(workspace / 'static-src', root,
                     report_path=workspace / 'source-assets-report.json')
    for entry in assets['recovered']:
        # Keep the restored file through the imported Astro project's next build.
        for output in ('public', 'dist'):
            target, _ = safe_target(workspace / 'astro-project' / output, '/' + quote(entry['file'], safe='/'))
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                shutil.copyfile(workspace / 'static-src' / entry['file'], target)
    shutil.copyfile(root / ASTRO_REPORT, workspace / "astro-report.json")
    carried = workspace / "astro-project/.html2wp"
    if carried.exists():
        shutil.rmtree(carried)
    (workspace / ".astro-project-imported").write_text("")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("project")
    ap.add_argument("--out", default="")
    ap.add_argument("--prepare", default="", metavar="WORKSPACE",
                    help="copy an html2wp Astro export into this workspace (other kinds: nothing)")
    args = ap.parse_args(argv)
    result = detect(args.project)
    if args.prepare and result["kind"] == "html2wp-astro":
        prepare_astro_export(Path(result["root"]), Path(args.prepare).resolve())
        result["prepared"] = str(Path(args.prepare).resolve())
    text = json.dumps(result, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n")
    print(text)
    return 0 if result["kind"] != "none" else 1


if __name__ == "__main__":
    sys.exit(main())
