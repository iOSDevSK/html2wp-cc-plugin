#!/usr/bin/env python3
# Copyright (c) 2026 BELNEM s.r.o. html2wp Source-Available Licence — see LICENSE.
"""The owner's "Make release": the live theme, packaged — no model turn.

The app runs it from its "Make release" button (outside the app, the owner
runs it). The model never runs it in a change turn.

    package-theme.py {workspace} [--output DIR]

After delivery the theme changes in place (apply-change.py). This packages the
theme as it is now with make-zip.sh — the same packer and refusals as stage 6
— and, when it differs from the last ZIP, delivers it as the next revision:
{slug}-{version}-r{N}.zip in the output directory (and the workspace), and
result.json names it (theme.file, sha256, bytes, revision). result.json's gate
rows stay those of the revision the checks ran on (checkedRevision); a
revision packaged after changes says so, it is not re-verified. The change
log's changedSinceZip is cleared.

An Astro run (result.json target "astro") releases its Astro project the same
way: the project as it is now — sources and the built site — as
{slug}-astro-{version}-r{N}.zip, named in result.json's `astro`.

When nothing changed since the last ZIP, that ZIP is the answer: nothing is
written, and the output says reused.

Prints one JSON object: {"file", "sha256", "bytes", "revision", "reused"}.
Exit 0; 1 = the theme does not package (make-zip's reason on stderr; for an
Astro run, no project); 2 = not a delivered project, or usage.
"""
import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "lib"))
import theme_state as ts  # noqa: E402


def package_astro(ws, args, result, manifest):
    """The Astro run's "Make release": the Astro project as it is now — its
    sources and the built site — as the next revision, or the last ZIP."""
    output = ts.output_dir(ws, args.output)
    slug, version = manifest["site"]["slug"], (manifest.get("site") or {}).get("version") or "1.0.0"
    previous = ts.last_astro_zip(ws, result, output)
    revision = int(result.get("revision") or 1)
    log = ts.load_changes(ws)
    with tempfile.TemporaryDirectory(prefix="h2wp-package-") as tmp:
        candidate = Path(tmp) / f"{slug}-astro-{version}.zip"
        if not ts.zip_astro_project(ws / "astro-project", candidate, f"{slug}-astro"):
            print("package-theme: no Astro project to package (astro-project/package.json)", file=sys.stderr)
            return 1
        if previous and ts.astro_zip_tree(previous) == ts.astro_zip_tree(candidate):
            log.update(changedSinceZip=False, sinceZip=0)
            ts.write_json(ws / "changes.json", log)
            row = result.get("astro") or {}
            print(json.dumps({"file": row.get("file"), "sha256": row.get("sha256"), "bytes": row.get("bytes"),
                              "revision": revision, "reused": True}))
            return 0
        revision += 1
        name = f"{slug}-astro-{version}-r{revision}.zip"
        output.mkdir(parents=True, exist_ok=True)
        for folder in (ws, output):
            shutil.copyfile(candidate, folder / f".{name}.part")
            (folder / f".{name}.part").replace(folder / name)
    row = {"file": name, "sha256": ts.sha256((output / name).read_bytes()), "bytes": (output / name).stat().st_size}
    result.update(astro=row, revision=revision, packagedAt=ts.now(),
                  checkedRevision=int(result.get("checkedRevision") or 1),
                  changesSinceChecks=len([c for c in log["changes"] if c.get("applied")]))
    log.update(changedSinceZip=False, sinceZip=0, lastZip={"file": name, "revision": revision, "at": result["packagedAt"]})
    ts.write_json(ws / "changes.json", log)
    ts.write_json(ws / "result.json", result)
    ts.write_json(output / "result.json", result)
    print(json.dumps({**row, "revision": revision, "reused": False}))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("workspace")
    ap.add_argument("--output", default="")
    args = ap.parse_args(argv)
    ws = Path(args.workspace).resolve()
    result = ts.delivered(ws)
    manifest = ts.read(ws / "conversion-manifest.json")
    if result is None or not isinstance(manifest, dict):
        print("package-theme: not a delivered theme (no result.json with status delivered)", file=sys.stderr)
        return 2
    if result.get("target") == "astro":
        return package_astro(ws, args, result, manifest)
    output = ts.output_dir(ws, args.output)
    theme = ts.theme_dir(ws, manifest)
    slug, version = manifest["site"]["slug"], (manifest.get("site") or {}).get("version") or "1.0.0"
    previous = ts.last_zip(ws, result, output)
    revision = int(result.get("revision") or 1)
    log = ts.load_changes(ws)

    with tempfile.TemporaryDirectory(prefix="h2wp-package-") as tmp:
        candidate = Path(tmp) / f"{slug}-{version}.zip"
        ok, said = ts.make_zip(theme, candidate, ws / "conversion-manifest.json")
        if not ok:
            print("package-theme: the theme does not package — " + (said.splitlines()[-1] if said else "make-zip failed"),
                  file=sys.stderr)
            return 1
        if previous and ts.zip_tree(previous) == ts.zip_tree(candidate):
            log.update(changedSinceZip=False, sinceZip=0)
            ts.write_json(ws / "changes.json", log)
            theme_row = result.get("theme") or {}
            print(json.dumps({"file": theme_row.get("file"), "sha256": theme_row.get("sha256"),
                              "bytes": theme_row.get("bytes"), "revision": revision, "reused": True}))
            return 0
        revision += 1
        name = f"{slug}-{version}-r{revision}.zip"
        output.mkdir(parents=True, exist_ok=True)
        for folder in (ws, output):
            shutil.copyfile(candidate, folder / f".{name}.part")
            (folder / f".{name}.part").replace(folder / name)
    digest = ts.sha256((output / name).read_bytes())
    theme_row = {"file": name, "sha256": digest, "bytes": (output / name).stat().st_size}
    result.update(theme=theme_row, revision=revision, packagedAt=ts.now(),
                  checkedRevision=int(result.get("checkedRevision") or 1),
                  changesSinceChecks=len([c for c in log["changes"] if c.get("applied")]))
    log.update(changedSinceZip=False, sinceZip=0,
               lastZip={"file": name, "revision": revision, "at": result["packagedAt"]})
    ts.write_json(ws / "changes.json", log)
    # The workspace copy first, the output's last: its presence is what a UI reads.
    ts.write_json(ws / "result.json", result)
    ts.write_json(output / "result.json", result)
    print(json.dumps({**theme_row, "revision": revision, "reused": False}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
