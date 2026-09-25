#!/usr/bin/env python3
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
"""A change after delivery, applied to the running preview — in seconds.

    apply-change.py {workspace} --what "<the owner's request, one line>"
                    [--page <key or /route/>]... [--skip-install]
    apply-change.py {workspace} --repair <stage> --what "<the lever>" [--page …]

After delivery the owner asks for changes in the chat, and a change is made in
the LIVE theme, never as a new build (SKILL.md, "Changes after delivery"). The
model edits only the installed theme's files — {workspace}/theme/<slug>/:
templates, parts, the site's CSS and assets, clara-content/sources — and then
runs this, which:

1. packages the edited theme with make-zip.sh (its lint and refusals), and
   names the files that changed since the last change reached the preview;
2. installs that package into the running preview through WordPress's own
   upload (install-theme.py): the theme files update, and the importer
   re-imports every page whose stored source is still the bundle's own — an
   owner's edit in the preview is never overwritten;
3. screenshots the touched pages (the --page ones, else every page whose
   source changed, else the front page) at 1440 and 390, for the model to
   look at before it answers;
4. logs the change in {workspace}/changes.json (what, files, pages,
   screenshots, when) and sets changedSinceZip, which the owner's "Make
   release" (package-theme.py) clears. Its own ZIP only carries the theme into
   the preview; it is never a release.

No stage runs: no prerender, no build, no service conversion, no
progress.json change. A preview that is down is brought back with
`test-env.sh up <slug>`. --skip-install records and checks the package
without touching a preview.

--repair <stage> is the same one more allowed application during a Flash
run: a lever of the repair budget (SKILL.md, "Flash repairs") edited the
theme, and this repacks the run's own ZIP ({workspace}/<slug>-<version>.zip,
the file stage 5 installed and the run delivers — replaced only when the
edited theme packages), installs it into the preview and screenshots the
pages, recording it on the repair attempt progress.sh opened for that stage.
It needs that open attempt; there is no change log and nothing is delivered
yet.

Exit 0 = applied; 1 = failed (the package or the install refused; the reason
on stderr and in the log); 2 = not a delivered project (or, with --repair, no
open repair of that stage), or usage; 3 = no theme file changed since the last
change — nothing to apply.
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import theme_state as ts  # noqa: E402

WIDTHS = (1440, 390)


def answers(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="HEAD"), timeout=10) as r:
            return r.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except OSError:
        return False


def preview(ws, slug):
    state_path = ws / f".test-env-{slug}.json"
    if not state_path.is_file():
        raise RuntimeError("no preview WordPress for this project: there is nothing to apply the change to")
    state = json.loads(state_path.read_text())
    if not answers(state["url"] + "/"):
        up = subprocess.run(["bash", str(HERE / "test-env.sh"), "up", slug], cwd=ws, capture_output=True,
                            text=True, timeout=900)
        state = json.loads(state_path.read_text())
        if up.returncode != 0 or not answers(state["url"] + "/"):
            raise RuntimeError("the preview WordPress did not come back up")
    return state_path, state["url"].rstrip("/")


def touched_pages(changed, asked):
    """The pages to look at: those asked for; else the pages whose stored
    source changed; else (templates, parts, CSS) the front page."""
    if asked:
        return asked
    keys = [Path(p).stem for p in changed if p.startswith("clara-content/sources/") and p.endswith(".html")]
    return keys or ["front-page"]


def route_of(page):
    if page.startswith("/"):
        return page
    return "/" if page in ("front-page", "index") else f"/{page}/"


def screenshots(url, pages, folder, stem):
    from playwright.sync_api import sync_playwright
    shots = []
    folder.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for page_name in pages:
            for width in WIDTHS:
                page = browser.new_page(viewport={"width": width, "height": 900})
                page.goto(url + route_of(page_name), wait_until="networkidle", timeout=60000)
                name = f"{stem}-{page_name.strip('/').replace('/', '-') or 'front-page'}-{width}.png"
                page.screenshot(path=str(folder / name), full_page=True)
                shots.append(f"changes/{name}")
                page.close()
        browser.close()
    return shots


def repair(ws, args):
    """A repair lever's edit: into the run's own ZIP and the preview."""
    manifest = ts.read(ws / "conversion-manifest.json")
    progress = ts.read(ws / "progress.json")
    rows = [r for r in (progress or {}).get("repairs") or [] if isinstance(r, dict)]
    row = next((r for r in reversed(rows) if r.get("stage") == args.repair and r.get("outcome") == "open"), None)
    if not isinstance(manifest, dict) or row is None:
        print(f"apply-change: no open repair of stage {args.repair} — a lever is opened with "
              "progress.sh repair <stage> <lever> <signature> first", file=sys.stderr)
        return 2
    theme = ts.theme_dir(ws, manifest)
    slug, version = manifest["site"]["slug"], (manifest.get("site") or {}).get("version") or "1.0.0"
    delivered = ws / f"{slug}-{version}.zip"
    current = ts.tree(theme)
    changed = ts.differ(ts.zip_tree(delivered) if delivered.is_file() else {}, current)
    if not changed:
        print(f"apply-change: no file of {theme} differs from {delivered.name} — the lever changed nothing "
              "(edit the theme's own files)", file=sys.stderr)
        return 3
    applied = {"files": changed, "pages": [], "screenshots": []}
    started = time.time()
    code = 0
    try:
        package = ws / ".change-apply" / delivered.name
        ok, said = ts.make_zip(theme, package, ws / "conversion-manifest.json")
        if not ok:
            raise RuntimeError("the edited theme does not package: " + (said.splitlines()[-1] if said else "make-zip failed"))
        # The run's ZIP is replaced only by a theme that packages.
        tmp = delivered.with_name(delivered.name + ".part")
        tmp.write_bytes(package.read_bytes())
        tmp.replace(delivered)
        if args.skip_install:
            applied["install"] = "skipped"
        else:
            state_path, url = preview(ws, slug)
            run = subprocess.run([sys.executable, str(HERE / "install-theme.py"), "--env", str(state_path),
                                  "--theme", str(delivered), f"--manifest={ws / 'conversion-manifest.json'}",
                                  "--out", str(ws / ".change-apply" / "install"), "--update"],
                                 capture_output=True, text=True, timeout=900)
            if run.returncode != 0:
                tail = (run.stdout + run.stderr).strip().splitlines()[-2:]
                raise RuntimeError("the preview did not take the repair: " + " | ".join(tail))
            applied["install"] = "installed"
            applied["pages"] = touched_pages(changed, args.page)
            stem = f"repair-{args.repair.replace('.', '_')}-{row.get('attempt', 1)}"
            applied["screenshots"] = screenshots(url, applied["pages"], ws / "changes", stem)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        applied["error"] = str(error)[:600]
        print(f"apply-change: {applied['error']}", file=sys.stderr)
        code = 1
    applied["seconds"] = round(time.time() - started, 1)
    # Onto the attempt progress.sh opened; progress.sh closes it.
    progress = ts.read(ws / "progress.json") or {}
    for r in reversed(progress.get("repairs") or []):
        if isinstance(r, dict) and r.get("stage") == args.repair and r.get("outcome") == "open":
            r["applied"] = applied
            break
    ts.write_json(ws / "progress.json", progress)
    print(json.dumps({"repair": args.repair, **{k: applied[k] for k in ("files", "pages", "screenshots", "seconds")}}))
    return code


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("workspace")
    ap.add_argument("--what", default="", help="the owner's request, one line, for the change log")
    ap.add_argument("--page", action="append", default=[], help="a page to look at: its key or its /route/")
    ap.add_argument("--skip-install", action="store_true", help="package and log only; no preview")
    ap.add_argument("--repair", default="", metavar="STAGE",
                    help="during a Flash run: a repair lever's theme edit, into the run's ZIP and the preview")
    args = ap.parse_args(argv)
    ws = Path(args.workspace).resolve()
    if args.repair:
        return repair(ws, args)
    if not args.what.strip():
        ap.error("--what is required: the owner's request, one line, for the change log")
    result = ts.delivered(ws)
    manifest = ts.read(ws / "conversion-manifest.json")
    if result is None or not isinstance(manifest, dict):
        print("apply-change: not a delivered project (no result.json with status delivered) — a change after "
              "delivery needs the delivered theme", file=sys.stderr)
        return 2
    theme = ts.theme_dir(ws, manifest)
    slug, version = manifest["site"]["slug"], (manifest.get("site") or {}).get("version") or "1.0.0"
    output = ts.output_dir(ws)
    current = ts.tree(theme)
    applied = ts.read(ws / ".theme-applied.json")
    zipped = ts.last_zip(ws, result, output)
    base = applied if isinstance(applied, dict) else (ts.zip_tree(zipped) if zipped else {})
    changed = ts.differ(base, current)
    if not changed:
        print(f"apply-change: no file of {theme} changed since the last change reached the preview — "
              "nothing to apply (edit the theme's own files, never the source)", file=sys.stderr)
        return 3

    log = ts.load_changes(ws)
    entry = {"id": len(log["changes"]) + 1, "at": ts.now(), "what": args.what.strip()[:300], "files": changed,
             "pages": [], "screenshots": [], "applied": False}
    started = time.time()
    try:
        package = ws / ".change-apply" / f"{slug}-{version}.zip"
        ok, said = ts.make_zip(theme, package, ws / "conversion-manifest.json")
        if not ok:
            raise RuntimeError("the edited theme does not package: " + said.splitlines()[-1] if said else "make-zip failed")
        if args.skip_install:
            entry["install"] = "skipped"
        else:
            state_path, url = preview(ws, slug)
            run = subprocess.run([sys.executable, str(HERE / "install-theme.py"), "--env", str(state_path),
                                  "--theme", str(package), f"--manifest={ws / 'conversion-manifest.json'}",
                                  "--out", str(ws / ".change-apply" / "install"), "--update"],
                                 capture_output=True, text=True, timeout=900)
            if run.returncode != 0:
                tail = (run.stdout + run.stderr).strip().splitlines()[-2:]
                raise RuntimeError("the preview did not take the change: " + " | ".join(tail))
            entry["install"] = "installed"
            entry["pages"] = touched_pages(changed, args.page)
            entry["screenshots"] = screenshots(url, entry["pages"], ws / "changes", f"{entry['id']:03d}")
        entry["applied"] = True
        ts.write_json(ws / ".theme-applied.json", current)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        entry["error"] = str(error)[:600]
        print(f"apply-change: {entry['error']}", file=sys.stderr)
    entry["seconds"] = round(time.time() - started, 1)
    log["changes"].append(entry)
    last = ts.zip_tree(zipped) if zipped else {}
    log["changedSinceZip"] = bool(ts.differ(last, current))
    # How many applied changes the last ZIP does not have yet.
    since = (log.get("lastZip") or {}).get("at") or ""
    log["sinceZip"] = sum(1 for c in log["changes"] if c.get("applied") and c.get("at", "") > since)
    ts.write_json(ws / "changes.json", log)
    print(json.dumps({k: entry[k] for k in ("id", "applied", "files", "pages", "screenshots", "seconds") if k in entry}))
    return 0 if entry["applied"] else 1


if __name__ == "__main__":
    sys.exit(main())
